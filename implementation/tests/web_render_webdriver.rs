//! Regression tests for the web module's SSRF-filter exemption on the webdriver
//! connection. `Render` talks to the operator-configured `webdriver_host`, which
//! in Docker resolves to a private (RFC1918 / loopback) address the SSRF filter
//! would otherwise drop. That connection must therefore go through the unfiltered
//! client -- while contract-driven `Request` traffic stays filtered.

use std::collections::BTreeMap;
use std::sync::Arc;

use anyhow::Context as _;
use genvm_common::*;

use genvm_modules::common;
use genvm_modules::scripting::{self, Metrics};
use tokio::io::AsyncWriteExt;

struct TestCtx {
    scripting: scripting::CtxPart,
}

// Minimal stand-in for the webdriver: replies `200` with the `resulting-status`
// header `Render` reads, then closes. Bound to loopback but reached over public
// DNS (`localhost.direct`) so the SSRF filter's resolver sees a loopback answer.
async fn serve_webdriver(body: &'static [u8]) -> std::net::SocketAddr {
    let server = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = server.local_addr().unwrap();

    tokio::spawn(async move {
        let (mut client, _) = server.accept().await.unwrap();
        let response = format!(
            "HTTP/1.1 200 OK\r\nresulting-status: 200\r\nContent-Type: text/plain\r\nContent-Length: {}\r\n\r\n",
            body.len()
        );
        client.write_all(response.as_bytes()).await.unwrap();
        client.write_all(body).await.unwrap();
        client.shutdown().await.unwrap();
    });

    addr
}

// A web-module VM running the real `genvm-web-default.lua`. `__web` must exist
// before the script's `require("lib-web")` reads it, so it is set inside the
// data getter ahead of `load_script`.
async fn create_web_vm(webdriver_host: String) -> scripting::UserVM<(), (), TestCtx> {
    let mut lua_dir = std::env::current_dir().unwrap();
    lua_dir.pop();
    lua_dir.push("install");
    lua_dir.push("lib");
    lua_dir.push("genvm-lua");
    let lua_dir = lua_dir
        .canonicalize()
        .with_context(|| format!("canonicalizing {:?}", lua_dir))
        .unwrap();
    let mut lua_path = lua_dir.to_str().unwrap().to_owned();
    lua_path.push_str("/?.lua");

    let mut script_path = std::env::current_dir().unwrap();
    script_path.pop();
    script_path.push("install");
    script_path.push("config");
    script_path.push("genvm-web-default.lua");
    let script_path = script_path
        .canonicalize()
        .with_context(|| format!("canonicalizing {:?}", script_path))
        .unwrap();

    let conf = sync::DArc::new(common::ModuleBaseConfig {
        bind_address: None,
        vm_count: 1,
        lua_script_path: "".to_owned(),
        lua_path,
        signer_headers: Arc::new(BTreeMap::new()),
        signer_url: Arc::from(""),
        data_dir: String::new(),
    });

    scripting::UserVM::create(
        &conf.clone(),
        move |vm: mlua::Lua| async move {
            // Stand in for `web::ctx::create_global`: only the fields the script
            // touches (`config.webdriver_host`, `config.always_allow_hosts`,
            // `allowed_tld`) are populated.
            let web = vm.create_table()?;
            let config = vm.create_table()?;
            config.set("webdriver_host", webdriver_host)?;
            config.set("always_allow_hosts", vm.create_table()?)?;
            web.set("config", config)?;

            let tld = vm.create_table()?;
            tld.set("com", true)?;
            tld.set("direct", true)?;
            web.set("allowed_tld", tld)?;

            vm.globals().set("__web", web)?;

            scripting::load_script(&vm, script_path.to_str().unwrap().to_owned()).await?;

            Ok(())
        },
        Box::new(move |vm, table, ctx: &sync::DArc<TestCtx>| {
            let scripting = ctx.gep(|x| &x.scripting);
            scripting::setup_lua_default_ctx(scripting, vm, table)?;
            Ok(())
        }),
    )
    .await
    .unwrap()
}

// A context with `filter_dns = true`, i.e. the production Docker configuration:
// `client` is the SSRF-filtering client, `client_unfiltered` the plain one.
fn create_filtered_ctx() -> sync::DArc<TestCtx> {
    let hello = common::tests::get_hello();
    let metrics = sync::DArc::new(scripting::Metrics::default());
    let conf = sync::DArc::new(common::ModuleBaseConfig {
        bind_address: None,
        vm_count: 1,
        lua_script_path: "".to_owned(),
        lua_path: "".to_owned(),
        signer_headers: Arc::new(BTreeMap::new()),
        signer_url: Arc::from(""),
        data_dir: String::new(),
    });
    let scripting = scripting::create_ctx_part(&hello, &conf, metrics, true).unwrap();
    sync::DArc::new(TestCtx { scripting })
}

#[tokio::test]
async fn test_render_webdriver_bypasses_dns_filter() {
    common::tests::setup();

    let body = b"rendered by webdriver";
    let addr = serve_webdriver(body).await;

    // `localhost.direct` resolves publicly to loopback, where our stub is bound.
    // With `filter_dns = true` the SSRF resolver would drop it -- so this only
    // succeeds because `Render` sends the webdriver request `unfiltered`.
    let webdriver_host = format!("http://localhost.direct:{}", addr.port());
    let uvm = create_web_vm(webdriver_host).await;

    let render: mlua::Function = uvm.vm.globals().get("Render").unwrap();

    let test_ctx = create_filtered_ctx();
    let (_, ctx_lua) = uvm.create_ctx(&test_ctx).unwrap();

    let payload = uvm.vm.create_table().unwrap();
    payload.set("url", "http://example.com/").unwrap();
    payload.set("mode", "text").unwrap();
    payload.set("wait_after_loaded", 0).unwrap();

    let res: mlua::Table = uvm
        .call_fn(&render, (ctx_lua, payload))
        .await
        .expect("Render must reach the loopback webdriver through the unfiltered client");

    let text: mlua::String = res.get("text").unwrap();
    assert_eq!(text.as_bytes().as_ref(), body);
}

#[tokio::test]
async fn test_web_request_still_filtered() {
    common::tests::setup();

    // Complementary check: general (contract-driven) `Request` traffic is NOT
    // exempt. The same loopback-resolving host, not allowlisted, must be dropped
    // by the SSRF resolver as a non-fatal `ADDRESS_FORBIDDEN`. This also proves
    // the ctx above genuinely has filtering on, so the Render success is due to
    // the exemption, not a disabled filter.
    let uvm = create_web_vm("http://unused.example.com".to_owned()).await;

    let request: mlua::Function = uvm.vm.globals().get("Request").unwrap();

    let test_ctx = create_filtered_ctx();
    let (_, ctx_lua) = uvm.create_ctx(&test_ctx).unwrap();

    let payload = uvm.vm.create_table().unwrap();
    payload.set("url", "http://localhost.direct/").unwrap();
    payload.set("method", "GET").unwrap();
    payload
        .set("headers", uvm.vm.create_table().unwrap())
        .unwrap();

    let err = uvm
        .call_fn::<mlua::Value>(&request, (ctx_lua, payload))
        .await
        .expect_err("a non-allowlisted loopback host must stay filtered");

    let module_err = scripting::try_unwrap_any_err(err).expect("expected a module error");
    assert!(
        module_err.causes.contains(&"ADDRESS_FORBIDDEN".to_owned()),
        "unexpected causes: {:?}",
        module_err.causes
    );
    assert!(!module_err.fatal, "ADDRESS_FORBIDDEN must be non-fatal");
}
