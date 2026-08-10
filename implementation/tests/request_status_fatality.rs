//! How the shared request helpers classify a non-200, for both response
//! shapes.
//!
//! `send_request_get_lua_compatible_response_bytes` and its JSON twin are
//! generic: they are given a URL and a flag, and cannot tell this node's own
//! webdriver sidecar from a site the contract named. A non-200 from the latter
//! is a legitimate observation, a non-200 from the former never is -- so the
//! helpers do not decide fatality on their own, they keep raising `STATUS_NOT_OK`
//! as fatal and leave the exception to the caller that knows which endpoint it
//! is talking to.
//!
//! Today the only caller that lowers it is `Render` in
//! `install/config/genvm-web-default.lua`, which wraps the sidecar hop in a
//! `pcall`; see `web::tests` in the library. The LLM providers
//! (`src/llm/providers.rs`) all pass `error_on_status = true` and rely on the
//! fatal classification pinned here, so a change to the default would move them
//! too.

use genvm_common::*;

use genvm_modules::common;
use genvm_modules::scripting::{self, Metrics};
use tokio::io::AsyncWriteExt as _;

/// A one-shot server answering `head` (status line plus headers,
/// `Content-Length` is appended) followed by `body`
async fn serve(head: &'static str, body: &'static [u8]) -> std::net::SocketAddr {
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

async fn serve_server_error() -> std::net::SocketAddr {
    serve(
        "HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n",
        br#"{"error":"Internal server error"}"#,
    )
    .await
}

fn metrics() -> sync::DArc<Metrics> {
    sync::DArc::new(Metrics::default())
}

fn get(url: &str) -> reqwest::RequestBuilder {
    common::tests::create_client().unwrap().get(url)
}

/// The module error a call failed with. Panics if it succeeded, or if the
/// failure was not a `ModuleError`
fn module_err(err: anyhow::Error) -> common::ModuleError {
    scripting::try_unwrap_any_err(err).expect("expected a module error")
}

fn assert_status_not_ok(err: common::ModuleError) {
    assert!(
        err.causes.contains(&"STATUS_NOT_OK".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
    assert!(
        err.fatal,
        "the generic helper keeps a bad status fatal; lowering it is the \
         caller's decision, see the module docs: {err:?}"
    );
    assert!(
        matches!(
            err.ctx.get("status"),
            Some(genvm_modules_interfaces::GenericValue::Number(s)) if *s == 500.0
        ),
        "the status must survive into ctx for the caller to act on: {:?}",
        err.ctx
    );
}

// -- error_on_status = true: raise, fatally ---------------------------

#[tokio::test]
async fn bytes_bad_status_raises_a_fatal_status_not_ok() {
    common::tests::setup();

    let addr = serve_server_error().await;
    let url = format!("http://{addr}/");

    let err = scripting::send_request_get_lua_compatible_response_bytes(
        &metrics(),
        &url,
        get(&url),
        true,
        usize::MAX,
    )
    .await
    .err()
    .expect("a 500 with error_on_status must fail");

    assert_status_not_ok(module_err(err));
}

/// The twin of the above. It is reached whenever the request sets `json`, which
/// every LLM provider does
#[tokio::test]
async fn json_bad_status_raises_a_fatal_status_not_ok() {
    common::tests::setup();

    let addr = serve_server_error().await;
    let url = format!("http://{addr}/");

    let err = scripting::send_request_get_lua_compatible_response_json(
        &metrics(),
        &url,
        get(&url),
        true,
        usize::MAX,
    )
    .await
    .err()
    .expect("a 500 with error_on_status must fail");

    assert_status_not_ok(module_err(err));
}

// -- error_on_status = false: the status is just data -----------------

#[tokio::test]
async fn bytes_bad_status_is_returned_when_not_erroring_on_status() {
    common::tests::setup();

    let addr = serve_server_error().await;
    let url = format!("http://{addr}/");

    let res = scripting::send_request_get_lua_compatible_response_bytes(
        &metrics(),
        &url,
        get(&url),
        false,
        usize::MAX,
    )
    .await
    .expect("without error_on_status a 500 is an ordinary response");

    assert_eq!(res.status, 500);
    assert_eq!(res.body, br#"{"error":"Internal server error"}"#.to_vec());
}

#[tokio::test]
async fn json_bad_status_is_returned_when_not_erroring_on_status() {
    common::tests::setup();

    let addr = serve_server_error().await;
    let url = format!("http://{addr}/");

    let res = scripting::send_request_get_lua_compatible_response_json(
        &metrics(),
        &url,
        get(&url),
        false,
        usize::MAX,
    )
    .await
    .expect("without error_on_status a 500 is an ordinary response");

    assert_eq!(res.status, 500);
    assert_eq!(
        res.body,
        serde_json::json!({ "error": "Internal server error" })
    );
}

// -- the JSON twin's own failure mode ---------------------------------

/// Only the JSON path can fail this way: a `200` whose body is not JSON. It is
/// fatal for the same reason, and pinned here because the twin had no coverage
/// at all
#[tokio::test]
async fn json_undecodable_body_is_a_fatal_deserializing_error() {
    common::tests::setup();

    let addr = serve(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n",
        b"not json at all",
    )
    .await;
    let url = format!("http://{addr}/");

    let err = scripting::send_request_get_lua_compatible_response_json(
        &metrics(),
        &url,
        get(&url),
        true,
        usize::MAX,
    )
    .await
    .err()
    .expect("an undecodable body must fail");

    let err = module_err(err);
    assert!(
        err.causes.contains(&"DESERIALIZING".to_owned()),
        "unexpected causes: {:?}",
        err.causes
    );
    assert!(err.fatal, "expected a fatal error: {err:?}");
    // the undecodable bytes are kept, so the failure is diagnosable
    assert!(
        matches!(
            err.ctx.get("body"),
            Some(genvm_modules_interfaces::GenericValue::Bytes(b)) if b == b"not json at all"
        ),
        "expected the raw body in ctx: {:?}",
        err.ctx
    );
}
