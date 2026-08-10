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

/// The sidecar's outer `catch` (`webdriver/src/prj/src/index.ts`,
/// `handleRenderRequest`): a bare `500` with no `Resulting-Status`, which is
/// what an exception thrown *outside* the per-page `try` becomes — e.g.
/// `newPage()` exceeding puppeteer's `protocolTimeout` on
/// `Target.createTarget`.
async fn serve_sidecar_internal_error() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n",
        br#"{"error":"Internal server error","message":"Target.createTarget timed out"}"#,
    )
    .await
}

/// The sidecar's parameter validation (`handleRenderRequest`): a `400` when
/// `url` is missing or `mode` is not one of text/html/screenshot. Reachable
/// whenever the module and the sidecar disagree about the query format, e.g.
/// across a version skew
async fn serve_sidecar_bad_request() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n",
        br#"{"error":"Missing url parameter"}"#,
    )
    .await
}

/// The sidecar's healthcheck path (`handleHealthcheck`): a plain-text `503`
/// when its own probe render fails. A proxy or orchestrator in front of the
/// sidecar answers the same way while the sidecar is down
async fn serve_sidecar_unhealthy() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n",
        b"unhealthy",
    )
    .await
}

/// An address nothing listens on: the sidecar process is gone, so the
/// connection is refused before any status exists. Binds and drops, so the
/// port is known to have been free
async fn sidecar_down() -> std::net::SocketAddr {
    let server = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = server.local_addr().unwrap();
    std::mem::drop(server);
    addr
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

/// The error `Render` failed with, still wrapped, the way `Handler::handle`
/// hands it to the message loop. Panics if `Render` succeeded.
async fn render_raw_err(config: &sync::DArc<config::Config>, url: &str) -> anyhow::Error {
    let err = render(config, url)
        .await
        .err()
        .expect("expected Render to fail");

    log_info!(error:ah = &err; "render failed");

    err
}

/// The module error `Render` failed with. Panics if it succeeded, or if the
/// failure was not a [`ModuleError`].
async fn render_err(config: &sync::DArc<config::Config>, url: &str) -> ModuleError {
    let err = render_raw_err(config, url).await;

    scripting::try_unwrap_any_err(err).expect("expected a module error")
}

/// Asserts that talking to the sidecar described by `addr` fails non-fatally,
/// i.e. as something the contract can catch rather than as an internal error
async fn assert_sidecar_failure_is_not_fatal(
    addr: std::net::SocketAddr,
    what: &str,
) -> ModuleError {
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_err(&config, "https://example.com/").await;

    assert!(
        !err.fatal,
        "{what} from our own webdriver sidecar must be non-fatal, got {err:?}"
    );

    err
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
/// `send_request_get_lua_compatible_response_bytes` raises `STATUS_NOT_OK`
/// as fatal, because it sees only a URL and cannot tell our sidecar from a
/// contract-controlled site. `Render` is the caller that does know, so it
/// wraps the sidecar hop in a `pcall` and re-raises non-fatally, the way
/// `Request` already did
#[tokio::test]
async fn render_sidecar_internal_error_is_not_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_internal_error().await;
    let err = assert_sidecar_failure_is_not_fatal(addr, "a 5xx").await;

    // Not vacuous: the failure really is the status error raised on the
    // sidecar hop, not the `WEBPAGE_LOAD_FAILED` branch further down, which
    // this response never reaches
    assert!(
        err.causes.contains(&"STATUS_NOT_OK".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

/// The sidecar's other non-200 exits take the same branch, and are the same
/// kind of fault: a `400` means the module and the sidecar disagree about the
/// query format, which is a deployment problem, not a statement about the page
#[tokio::test]
async fn render_sidecar_bad_request_is_not_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_bad_request().await;
    let err = assert_sidecar_failure_is_not_fatal(addr, "a 400").await;

    assert!(
        err.causes.contains(&"STATUS_NOT_OK".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

/// A `503`, as answered by the sidecar's healthcheck path or by whatever
/// proxies it while it is down
#[tokio::test]
async fn render_sidecar_unhealthy_is_not_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_unhealthy().await;
    let err = assert_sidecar_failure_is_not_fatal(addr, "a 503").await;

    assert!(
        err.causes.contains(&"STATUS_NOT_OK".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

/// The sidecar being *gone* is the same class of fault as it answering badly,
/// and it arrives on a different path: `map_send_error` raises a fatal
/// `SENDING_REQUEST` before any status exists. The `pcall` covers the whole
/// hop, so this is non-fatal too
#[tokio::test]
async fn render_sidecar_unreachable_is_not_fatal() {
    common::tests::setup();

    let addr = sidecar_down().await;
    let err = assert_sidecar_failure_is_not_fatal(addr, "a refused connection").await;

    assert!(
        err.causes.contains(&"SENDING_REQUEST".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

// ── the classification as the executor reads it ──────────────────────

/// The end the property is really about. `ModuleError.fatal` is only a flag
/// until [`crate::common::module_error_to_wire`] spends it: `FatalError` is
/// what the executor turns into a bare `anyhow` and finally
/// `ResultCode::InternalError`, which the contract cannot catch and which a
/// node reports as a Timeout vote. A sidecar 5xx must not take that path
#[tokio::test]
async fn render_sidecar_internal_error_reaches_the_wire_as_user_error() {
    common::tests::setup();

    let addr = serve_sidecar_internal_error().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_raw_err(&config, "https://example.com/").await;
    let wire: genvm_modules_interfaces::Result<String> =
        crate::common::module_error_to_wire(err, common::tests::get_hello().genvm_id);

    match wire {
        genvm_modules_interfaces::Result::UserError(_) => {}
        genvm_modules_interfaces::Result::FatalError(msg) => {
            panic!("a sidecar 5xx must not abort the run as an internal error: {msg}")
        }
        genvm_modules_interfaces::Result::Ok(_) => panic!("expected Render to fail"),
    }
}

/// Companion to the above, so it is not vacuous: the fatal path is still
/// reachable and still ends in `FatalError`. A malformed URL never reaches the
/// sidecar at all
#[tokio::test]
async fn a_genuinely_fatal_failure_still_reaches_the_wire_as_fatal_error() {
    common::tests::setup();

    let addr = serve_sidecar_page_not_found().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = anyhow::anyhow!("not a module error at all");
    let wire: genvm_modules_interfaces::Result<String> =
        crate::common::module_error_to_wire(err, common::tests::get_hello().genvm_id);

    match wire {
        genvm_modules_interfaces::Result::FatalError(msg) => assert!(
            msg.contains("not a module error at all"),
            "unexpected message: {msg}"
        ),
        genvm_modules_interfaces::Result::Ok(_)
        | genvm_modules_interfaces::Result::UserError(_) => {
            panic!("an unclassified error must stay fatal")
        }
    }

    // and the config above is a working one, so the fatality is not an
    // artifact of a broken fixture
    assert!(
        render(&config, "https://example.com/").await.is_err(),
        "a 404 page is still an error, just a catchable one"
    );
}
