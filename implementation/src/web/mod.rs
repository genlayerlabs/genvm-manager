use std::{collections::HashMap, sync::Arc};

use anyhow::{Context, Result};
use genlayer_calldata as calldata;
use genvm_common::*;

use crate::{common, scripting};

type WebSubContext = crate::manager::execution_context::WebSubContext;

pub(crate) mod config;
mod ctx;
mod domains;
mod handler;

#[derive(serde::Serialize, Debug, Default)]
pub(crate) struct Metrics {
    pub scripting: scripting::Metrics,
}

impl<W: calldata::Writer> calldata::codec::Encode<W> for Metrics {
    type Error = W::Error;

    fn encode(&self, enc: &mut calldata::Encoder<W>) -> std::result::Result<(), Self::Error> {
        enc.start_map(1)?;
        enc.push_map_k("scripting")?;
        calldata::codec::Encode::encode(&self.scripting, enc)?;
        Ok(())
    }
}

#[derive(clap::Args, Debug)]
pub struct CliArgs {
    #[arg(long, default_value_t = String::from("${exeDir}/../config/genvm-module-web.yaml"))]
    config: String,

    #[arg(long, default_value_t = false)]
    die_with_parent: bool,
}

/// Creates the Web module and returns the stream handler.
/// The returned future runs the bind loop if bind_address is Some.
pub async fn create_web_module(
    cancel: Arc<cancellation::Token>,
    config: config::Config,
) -> Result<(
    crate::manager::modules::StreamHandler,
    impl std::future::Future<Output = Result<()>>,
    sync::DArc<config::Config>,
)> {
    let _webdriver_host = config.webdriver_host.clone();

    let config = sync::DArc::new(config);

    let moved_config = config.clone();

    let vm_pool = scripting::pool::new(config.mod_base.vm_count, move || {
        let moved_config = moved_config.clone();
        async move {
            let moved_config_for_data = moved_config.clone();
            let user_vm = crate::scripting::UserVM::create(
                &moved_config.mod_base,
                move |vm: mlua::Lua| async move {
                    // set web-related globals
                    vm.globals()
                        .set("__web", ctx::create_global(&vm, &moved_config_for_data)?)?;

                    // load script
                    scripting::load_script(&vm, &moved_config_for_data.mod_base.lua_script_path)
                        .await?;

                    // get functions populated by script
                    let render: mlua::Function = vm.globals().get("Render")?;
                    let request: mlua::Function = vm.globals().get("Request")?;

                    Ok(ctx::VMData { render, request })
                },
                Box::new(
                    move |vm: &mlua::Lua,
                          table: &mlua::Table,
                          sub_ctx: &sync::DArc<WebSubContext>| {
                        let scripting = sub_ctx.gep(|x| &x.scripting);
                        scripting::setup_lua_default_ctx(scripting, vm, table)?;

                        let ctx = Arc::new(ctx::CtxPart {});

                        table.set("__ctx_web", vm.create_userdata(ctx.clone())?)?;

                        Ok(ctx)
                    },
                ),
            )
            .await?;

            Ok(user_vm)
        }
    })
    .await?;

    let handler_provider = Arc::new(handler::HandlerProvider {
        vm_pool,
        config: config.clone(),
    });

    // Create the type-erased stream handler
    let stream_handler: crate::manager::modules::StreamHandler = {
        let hp = handler_provider.clone();
        Arc::new(move |stream: Box<dyn genvm_common::io::Stream>, exec_ctx| {
            let hp = hp.clone();
            Box::pin(async move {
                let sub_ctx = exec_ctx.map(|ctx| ctx.gep(|x| x.web.as_ref().unwrap()));
                crate::common::handle_stream(hp, stream, "relay", sub_ctx).await;
            }) as std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>
        })
    };

    let bind_future = crate::common::run_loop(
        config.mod_base.bind_address.clone(),
        cancel,
        handler_provider,
    );

    Ok((stream_handler, bind_future, config))
}

pub async fn run_web_module(
    cancel: Arc<cancellation::Token>,
    config: config::Config,
) -> Result<()> {
    let (_handler, bind_future, _config) = create_web_module(cancel, config).await?;
    bind_future.await
}

pub fn entrypoint(args: CliArgs) -> Result<()> {
    let config = genvm_common::load_config(HashMap::new(), &args.config)
        .with_context(|| "loading config")?;
    let config: config::Config = serde_yaml::from_value(config)?;

    config.base.setup_logging(std::io::stdout())?;

    let runtime = config.base.create_rt()?;

    let token = common::setup_cancels(&runtime, args.die_with_parent)?;

    runtime.block_on(run_web_module(token, config))?;

    std::mem::drop(runtime);

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::common::ModuleError;
    use genvm_modules_interfaces::web as web_iface;
    use mlua::LuaSerdeExt as _;
    use tokio::io::AsyncWriteExt as _;

    type TestVM = scripting::UserVM<ctx::VMData, Arc<ctx::CtxPart>, WebSubContext>;

    /// Absolute path of `<repo>/<rel>`. Tests run with the crate
    /// directory (`implementation`) as the working directory.
    fn modules_path(rel: &str) -> String {
        let mut path = std::env::current_dir().unwrap();
        path.pop();
        path.push(rel);
        path.canonicalize()
            .with_context(|| format!("canonicalizing {path:?}"))
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned()
    }

    /// Stands in for the webdriver sidecar: answers every connection with
    /// `head` (status line + headers, `Content-Length` is appended) and `body`.
    /// No Chromium and no egress past loopback.
    async fn serve_sidecar(head: &'static str, body: &'static [u8]) -> std::net::SocketAddr {
        let server = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();

        tokio::spawn(async move {
            while let Ok((mut client, _)) = server.accept().await {
                let response = format!("{head}Content-Length: {}\r\n\r\n", body.len());
                client.write_all(response.as_bytes()).await.unwrap();
                client.write_all(body).await.unwrap();
                client.shutdown().await.unwrap();
            }
        });

        addr
    }

    /// The sidecar's success path: `200` with the *page's* status reported in
    /// `Resulting-Status`. A page that 404s looks like this.
    async fn serve_sidecar_page_not_found() -> std::net::SocketAddr {
        serve_sidecar(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nResulting-Status: 404\r\n",
            b"Not Found",
        )
        .await
    }

    /// The sidecar's outer `catch` (`modules/webdriver/src/prj/src/index.ts`): a
    /// bare `500` with no `Resulting-Status`, which is what an exception thrown
    /// *outside* the per-page `try` becomes — e.g. `newPage()` exceeding
    /// puppeteer's `protocolTimeout` on `Target.createTarget`.
    async fn serve_sidecar_internal_error() -> std::net::SocketAddr {
        serve_sidecar(
            "HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n",
            br#"{"error":"Internal server error","message":"Target.createTarget timed out"}"#,
        )
        .await
    }

    fn test_config(webdriver_host: String) -> config::Config {
        config::Config {
            webdriver_host,
            extra_tld: Vec::new(),
            always_allow_hosts: Vec::new(),
            meta: serde_json::Value::Null,
            max_wait_after_loaded: common::Timeout::from_secs(60),
            base: BaseConfig {
                threads: 0,
                blocking_threads: 0,
                log_level: logger::Level::Trace,
                log_disable: String::new(),
            },
            mod_base: common::ModuleBaseConfig {
                bind_address: None,
                vm_count: 1,
                lua_script_path: modules_path("install/config/genvm-web-default.lua"),
                lua_path: format!("{}/?.lua", modules_path("install/lib/genvm-lua")),
                signer_headers: Arc::new(std::collections::BTreeMap::new()),
                signer_url: Arc::from(""),
                data_dir: String::new(),
            },
        }
    }

    /// Same VM wiring as [`create_web_module`], without the pool.
    async fn create_test_vm(config: &sync::DArc<config::Config>) -> TestVM {
        let config_for_data = config.clone();

        scripting::UserVM::create(
            &config.mod_base,
            move |vm: mlua::Lua| async move {
                vm.globals()
                    .set("__web", ctx::create_global(&vm, &config_for_data)?)?;

                scripting::load_script(&vm, &config_for_data.mod_base.lua_script_path).await?;

                let render: mlua::Function = vm.globals().get("Render")?;
                let request: mlua::Function = vm.globals().get("Request")?;

                Ok(ctx::VMData { render, request })
            },
            Box::new(
                move |vm: &mlua::Lua, table: &mlua::Table, sub_ctx: &sync::DArc<WebSubContext>| {
                    let scripting = sub_ctx.gep(|x| &x.scripting);
                    scripting::setup_lua_default_ctx(scripting, vm, table)?;

                    let ctx = Arc::new(ctx::CtxPart {});
                    table.set("__ctx_web", vm.create_userdata(ctx.clone())?)?;

                    Ok(ctx)
                },
            ),
        )
        .await
        .unwrap()
    }

    /// Same context as `HandlerProvider::create_execution_context`, including
    /// `filter_dns = true` — the webdriver host is an IP literal, which reqwest
    /// never sends through the resolver, so the SSRF guard stays out of the way.
    fn create_test_ctx(config: &sync::DArc<config::Config>) -> sync::DArc<WebSubContext> {
        let hello = common::tests::get_hello();
        let metrics = sync::DArc::new(Metrics::default());

        let scripting = scripting::create_ctx_part(
            &hello,
            &config.gep(|x| &x.mod_base),
            metrics.gep(|x| &x.scripting),
            true,
        )
        .unwrap();

        sync::DArc::new(WebSubContext { scripting })
    }

    /// Drives the production `Render` of
    /// `modules/install/config/genvm-web-default.lua`, the way `Handler::handle`
    /// does for `Message::Render`.
    async fn render(
        config: &sync::DArc<config::Config>,
        url: &str,
    ) -> anyhow::Result<web_iface::RenderAnswer> {
        let user_vm = create_test_vm(config).await;
        let sub_ctx = create_test_ctx(config);
        let (_ctx, ctx_val) = user_vm.create_ctx(&sub_ctx)?;

        let payload = user_vm.vm.create_table()?;
        // what `RenderMode::Text` serializes to
        payload.set("mode", "text")?;
        payload.set("url", url)?;
        payload.set("wait_after_loaded", 0.0)?;
        payload.set("size_limit", 1024 * 1024)?;

        let res: mlua::Value = user_vm
            .call_fn(&user_vm.data.render, (ctx_val, payload))
            .await?;

        Ok(user_vm.vm.from_value(res)?)
    }

    /// The module error `Render` failed with. Panics if it succeeded, or if the
    /// failure was not a [`ModuleError`].
    async fn render_err(config: &sync::DArc<config::Config>, url: &str) -> ModuleError {
        let err = render(config, url)
            .await
            .err()
            .expect("expected Render to fail");

        log_info!(error:ah = &err; "render failed");

        scripting::try_unwrap_any_err(err).expect("expected a module error")
    }

    // ── a failing sidecar is an environment fault, not a verdict ──────────

    /// Control. When the *page* fails, the sidecar still answers `200` and puts
    /// the page status in `Resulting-Status`; `Render` turns that into a
    /// non-fatal `WEBPAGE_LOAD_FAILED`, which the executor hands the contract as
    /// a catchable nondeterministic exception.
    #[tokio::test]
    async fn render_remote_page_load_failure_is_not_fatal() {
        common::tests::setup();

        let addr = serve_sidecar_page_not_found().await;
        let config = sync::DArc::new(test_config(format!("http://{addr}")));

        let err = render_err(&config, "https://example.com/").await;

        assert!(
            err.causes.contains(&"WEBPAGE_LOAD_FAILED".to_owned()),
            "unexpected causes: {:?}",
            err.causes
        );
        assert!(!err.fatal, "page load failure must be non-fatal: {err:?}");
    }

    /// A 5xx from our *own* webdriver sidecar says nothing about the page — it
    /// is this node's environment failing. It must travel the same non-fatal
    /// channel as the control above instead of aborting the whole contract run
    /// with an internal error.
    ///
    /// It does not today: `Render` calls `lib.rs.request` with
    /// `error_on_status = true` and, unlike `Request`, without a `pcall`, so
    /// `send_request_get_lua_compatible_response_bytes` raises a **fatal**
    /// `STATUS_NOT_OK` and the `WEBPAGE_LOAD_FAILED` branch below it is never
    /// reached.
    #[tokio::test]
    async fn render_sidecar_internal_error_is_not_fatal() {
        common::tests::setup();

        let addr = serve_sidecar_internal_error().await;
        let config = sync::DArc::new(test_config(format!("http://{addr}")));

        let err = render_err(&config, "https://example.com/").await;

        assert!(
            !err.fatal,
            "a 5xx from our own webdriver sidecar must be non-fatal, got {err:?}"
        );
    }
}
