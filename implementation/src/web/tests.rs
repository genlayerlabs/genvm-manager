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
/// what an exception thrown *outside* the per-page `try` becomes -- e.g.
/// `newPage()` exceeding puppeteer's `protocolTimeout` on
/// `Target.createTarget`.
async fn serve_sidecar_internal_error() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n",
        br#"{"error":"Internal server error","message":"Target.createTarget timed out"}"#,
    )
    .await
}

/// The one navigation failure the sidecar refuses to report:
/// `net::ERR_INTERNET_DISCONNECTED` means the request never left the host, so
/// there is no observation to report and `render.ts` throws instead of
/// returning a status. The outer catch turns that into the same bare `500`,
/// byte-accurate down to the message, which is the only thing that crosses.
///
/// Its wording is the point. The browser and the sidecar answered normally, so
/// an operator told "webdriver unavailable" would go and stare at a working
/// browser; the text has to say the local network is what broke.
async fn serve_sidecar_local_network_unavailable() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n",
        br#"{"error":"Internal server error","message":"Local network fault: this host has no network route (net::ERR_INTERNET_DISCONNECTED). The browser answered, so it is the local network that failed rather than the webdriver, and nothing about the page was observed."}"#,
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

/// The same number on the other channel: the sidecar itself answered fine
/// (`200`) and is telling us the *page's* host refused the connection, which
/// `getNavigationErrorStatus` reports as `503`. Paired with
/// [`serve_sidecar_unhealthy`] on purpose -- the two differ only in which
/// channel carries the 503, and they must be classified oppositely
async fn serve_sidecar_page_unreachable() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nResulting-Status: 503\r\n",
        b"Connection refused",
    )
    .await
}

/// A name that did not resolve, which `getNavigationErrorStatus` reports as a
/// `502` on the observation channel. Paired with
/// [`serve_sidecar_local_network_unavailable`]: both are network failures, and
/// they are classified oppositely on purpose. See
/// [`render_remote_page_name_not_resolved_is_not_fatal`]
async fn serve_sidecar_page_name_not_resolved() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nResulting-Status: 502\r\n",
        b"DNS resolution failed",
    )
    .await
}

/// A navigation that ran out of time, reported as a `408` on the observation
/// channel. See [`render_remote_page_navigation_timeout_is_not_fatal`]
async fn serve_sidecar_page_navigation_timeout() -> std::net::SocketAddr {
    serve_sidecar(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nResulting-Status: 408\r\n",
        b"Navigation timeout",
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
/// `filter_dns = true` -- the webdriver host is an IP literal, which reqwest
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

/// Asserts that talking to the sidecar described by `addr` fails fatally and is
/// labelled as a webdriver fault.
///
/// Fatal is the point, not an accident. A broken sidecar means this validator
/// never observed the page, so it must not hand the contract a result: the run
/// is aborted as an internal error and the node votes Timeout, which is the
/// truthful "I could not do the work". The label is what lets an operator tell
/// this apart from a genuine internal error, and it survives into the
/// `FatalError` string because [`ModuleError`]'s `Display` is its JSON.
async fn assert_sidecar_failure_is_fatal(addr: std::net::SocketAddr, what: &str) -> ModuleError {
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_err(&config, "https://example.com/").await;

    assert!(
        err.fatal,
        "{what} from our own webdriver sidecar must stay fatal so the validator \
         abstains rather than voting on a page it never observed, got {err:?}"
    );
    assert!(
        err.causes.contains(&"WEBDRIVER_UNAVAILABLE".to_owned()),
        "{what} must be labelled as a webdriver fault, got causes {:?}",
        err.causes
    );

    err
}

// -- a failing sidecar is an environment fault, so we abstain ----------

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

/// A 5xx from our *own* webdriver sidecar says nothing about the page -- it is
/// this node's environment failing, so there is no observation to report and
/// nothing legitimate to vote on. It must stay fatal, which the executor turns
/// into `ResultCode::InternalError` and the node into a Timeout vote.
///
/// `Render` is the caller that knows which endpoint it is talking to, so it
/// wraps the sidecar hop in a `pcall` -- not to soften the failure, but to
/// label it and to pin fatality here rather than inherit whatever the generic
/// transport decided.
#[tokio::test]
async fn render_sidecar_internal_error_is_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_internal_error().await;
    let err = assert_sidecar_failure_is_fatal(addr, "a 5xx").await;

    // Not vacuous: the failure really is the status error raised on the
    // sidecar hop, not the `WEBPAGE_LOAD_FAILED` branch further down, which
    // this response never reaches. The transport's own cause is kept as the
    // underlying detail rather than replaced
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
async fn render_sidecar_bad_request_is_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_bad_request().await;
    let err = assert_sidecar_failure_is_fatal(addr, "a 400").await;

    assert!(
        err.causes.contains(&"STATUS_NOT_OK".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

/// A `503`, as answered by the sidecar's healthcheck path or by whatever
/// proxies it while it is down. Half of a pair with
/// [`render_remote_page_unreachable_is_not_fatal`]: same number, opposite
/// verdict, because here it is the sidecar's own HTTP status
#[tokio::test]
async fn render_sidecar_unhealthy_is_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_unhealthy().await;
    let err = assert_sidecar_failure_is_fatal(addr, "a 503").await;

    assert!(
        err.causes.contains(&"STATUS_NOT_OK".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

/// The host this validator runs on has no network at all. The sidecar throws
/// rather than reporting a status, because a request that never left the
/// machine is not an observation about the page, and the outer catch turns
/// that into the same bare `500`.
///
/// It travels the webdriver hop, so it collects the same `WEBDRIVER_UNAVAILABLE`
/// label; that label names the hop, not the diagnosis. Which box actually broke
/// is carried by the sidecar's own message, pinned by
/// [`render_sidecar_local_network_fault_names_the_local_network_on_the_wire`]
#[tokio::test]
async fn render_sidecar_local_network_fault_is_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_local_network_unavailable().await;
    let err = assert_sidecar_failure_is_fatal(addr, "a local network fault").await;

    assert!(
        err.causes.contains(&"STATUS_NOT_OK".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

/// The other half of that pair. A `503` in `Resulting-Status` on an otherwise
/// good `200` is the sidecar telling us the *page's* host refused the
/// connection -- something we did observe -- so it stays catchable. The two
/// tests differ only in which channel carries the 503, which is exactly the
/// distinction a fix is most likely to erase
#[tokio::test]
async fn render_remote_page_unreachable_is_not_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_page_unreachable().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_err(&config, "https://example.com/").await;

    assert!(
        err.causes.contains(&"WEBPAGE_LOAD_FAILED".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
    assert!(
        !err.fatal,
        "a 503 the sidecar *reports* is an observation about the page, and must \
         stay catchable: {err:?}"
    );
    assert!(
        !err.causes.contains(&"WEBDRIVER_UNAVAILABLE".to_owned()),
        "a working sidecar must not be blamed: {:?}",
        err.causes
    );
}

/// A name that does not resolve stays on the observation channel, and this
/// test exists to keep it there.
///
/// It is genuinely ambiguous -- the domain may not exist, or *our* resolver may
/// be broken -- and one validator cannot tell those apart. That is precisely
/// what several validators and an equivalence principle are for: deciding it
/// here, the way `net::ERR_INTERNET_DISCONNECTED` is decided, would suppress
/// the disagreement consensus exists to reconcile. So it stays catchable, the
/// contract sees it, and the vote records what this node saw
#[tokio::test]
async fn render_remote_page_name_not_resolved_is_not_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_page_name_not_resolved().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_err(&config, "https://example.com/").await;

    assert!(
        err.causes.contains(&"WEBPAGE_LOAD_FAILED".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
    assert!(
        !err.fatal,
        "a name that does not resolve is ambiguous between the site and us, and \
         that ambiguity is for the validators to settle, not for this node to \
         pre-judge by abstaining: {err:?}"
    );
    assert!(
        !err.causes.contains(&"WEBDRIVER_UNAVAILABLE".to_owned()),
        "a working sidecar must not be blamed: {:?}",
        err.causes
    );
    // not vacuous: this really is the 502 branch, not some other failure
    assert!(
        matches!(
            err.ctx.get("status"),
            Some(genvm_modules_interfaces::GenericValue::Number(s)) if *s == 502.0
        ),
        "expected the reported page status in ctx: {:?}",
        err.ctx
    );
}

/// The same guard for the other ambiguous case. A navigation timeout is either
/// a slow site or a browser of ours that wedged, and a validator cannot tell
/// which; it stays a contract-visible observation for the same reason
#[tokio::test]
async fn render_remote_page_navigation_timeout_is_not_fatal() {
    common::tests::setup();

    let addr = serve_sidecar_page_navigation_timeout().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_err(&config, "https://example.com/").await;

    assert!(
        err.causes.contains(&"WEBPAGE_LOAD_FAILED".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
    assert!(
        !err.fatal,
        "a navigation timeout is ambiguous between the site and us, and stays \
         catchable so the validators can disagree about it: {err:?}"
    );
    assert!(
        !err.causes.contains(&"WEBDRIVER_UNAVAILABLE".to_owned()),
        "a working sidecar must not be blamed: {:?}",
        err.causes
    );
    assert!(
        matches!(
            err.ctx.get("status"),
            Some(genvm_modules_interfaces::GenericValue::Number(s)) if *s == 408.0
        ),
        "expected the reported page status in ctx: {:?}",
        err.ctx
    );
}

/// The sidecar being *gone* is the same class of fault as it answering badly,
/// and it arrives on a different path: `map_send_error` raises a fatal
/// `SENDING_REQUEST` before any status exists. The `pcall` covers the whole
/// hop, so this is labelled too
#[tokio::test]
async fn render_sidecar_unreachable_is_fatal() {
    common::tests::setup();

    let addr = sidecar_down().await;
    let err = assert_sidecar_failure_is_fatal(addr, "a refused connection").await;

    assert!(
        err.causes.contains(&"SENDING_REQUEST".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
}

// -- the classification as the executor reads it ----------------------

/// The end the property is really about. `ModuleError.fatal` is only a flag
/// until [`crate::common::module_error_to_wire`] spends it: `FatalError` is
/// what the executor turns into a bare `anyhow` and finally
/// `ResultCode::InternalError`, which the contract cannot catch and which a
/// node reports as a Timeout vote. That is where a sidecar 5xx has to land --
/// abstaining is the honest answer when we never ran the render.
///
/// The label has to survive the trip, because `FatalError` carries only a
/// string: without it an operator cannot tell an outage of our own webdriver
/// from a genuine internal error in the contract's execution
#[tokio::test]
async fn render_sidecar_internal_error_reaches_the_wire_as_fatal_error() {
    common::tests::setup();

    let addr = serve_sidecar_internal_error().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_raw_err(&config, "https://example.com/").await;
    let wire: genvm_modules_interfaces::Result<String> =
        crate::common::module_error_to_wire(err, common::tests::get_hello().genvm_id);

    match wire {
        genvm_modules_interfaces::Result::FatalError(msg) => assert!(
            msg.contains("WEBDRIVER_UNAVAILABLE"),
            "the abort must name the webdriver so it is not mistaken for a \
             contract-level internal error: {msg}"
        ),
        genvm_modules_interfaces::Result::UserError(v) => {
            panic!("a sidecar 5xx must not become a result the contract can act on: {v:?}")
        }
        genvm_modules_interfaces::Result::Ok(_) => panic!("expected Render to fail"),
    }
}

/// Companion to the above, so it is not vacuous: the non-fatal path is still
/// reachable and still ends in `UserError`. The sidecar here is working, and
/// what failed is the page -- a real observation the contract may catch and act
/// on
#[tokio::test]
async fn render_remote_page_load_failure_reaches_the_wire_as_user_error() {
    common::tests::setup();

    let addr = serve_sidecar_page_not_found().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_raw_err(&config, "https://example.com/").await;
    let wire: genvm_modules_interfaces::Result<String> =
        crate::common::module_error_to_wire(err, common::tests::get_hello().genvm_id);

    match wire {
        genvm_modules_interfaces::Result::UserError(_) => {}
        genvm_modules_interfaces::Result::FatalError(msg) => {
            panic!("a page that 404s must stay catchable, not abort the run: {msg}")
        }
        genvm_modules_interfaces::Result::Ok(_) => panic!("expected Render to fail"),
    }
}

/// The other thing the abort string has to carry. `WEBDRIVER_UNAVAILABLE` is
/// the name of the hop that failed, and for a machine with no network it is the
/// wrong diagnosis: the browser and the sidecar answered normally, so an
/// operator following that word alone goes and stares at a working browser.
///
/// The sidecar's own message is what distinguishes them, and this pins that it
/// survives all the way to the string an operator reads. The body arrives from
/// the transport as bytes, but the `pcall` round trip through Lua turns it into
/// a string, so it lands in [`ModuleError`]'s JSON as readable text rather than
/// a byte array
#[tokio::test]
async fn render_sidecar_local_network_fault_names_the_local_network_on_the_wire() {
    common::tests::setup();

    let addr = serve_sidecar_local_network_unavailable().await;
    let config = sync::DArc::new(test_config(format!("http://{addr}")));

    let err = render_raw_err(&config, "https://example.com/").await;
    let wire: genvm_modules_interfaces::Result<String> =
        crate::common::module_error_to_wire(err, common::tests::get_hello().genvm_id);

    match wire {
        genvm_modules_interfaces::Result::FatalError(msg) => {
            assert!(
                msg.contains("local network"),
                "the abort must say which part of this node broke: {msg}"
            );
            assert!(
                msg.contains("WEBDRIVER_UNAVAILABLE"),
                "the hop is still named, so the two live side by side: {msg}"
            );
        }
        genvm_modules_interfaces::Result::UserError(v) => {
            panic!("a host with no network observed nothing, so the contract must not act on it: {v:?}")
        }
        genvm_modules_interfaces::Result::Ok(_) => panic!("expected Render to fail"),
    }
}
