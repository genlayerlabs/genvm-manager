use std::time::Duration;

use genvm_common::sync::DArc;
use genvm_modules::{common, scripting};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

async fn request(
    location: &str,
    extra_tld: &[Box<str>],
    allowed: bool,
) -> anyhow::Result<scripting::Response> {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy = format!("http://{}", listener.local_addr().unwrap());
    let location = location.to_owned();
    let server = tokio::spawn(async move {
        for hop in 0..2 {
            let accepted =
                tokio::time::timeout(Duration::from_millis(500), listener.accept()).await;
            if hop == 1 && !allowed {
                assert!(accepted.is_err(), "forbidden redirect reached the proxy");
                return;
            }
            let (mut stream, _) = accepted.unwrap().unwrap();
            let mut header = Vec::new();
            while !header.ends_with(b"\r\n\r\n") {
                header.push(stream.read_u8().await.unwrap());
            }
            let response = if hop == 0 {
                format!("HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            } else {
                "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".to_owned()
            };
            stream.write_all(response.as_bytes()).await.unwrap();
        }
    });
    // A local proxy lets allowed port-80 destinations reach the fixture without public DNS.
    let client = reqwest::Client::builder()
        .proxy(reqwest::Proxy::all(proxy).unwrap())
        .redirect(common::redirect_policy_with_tlds(extra_tld))
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let url = "http://source.com/start";
    let result = scripting::send_request_get_lua_compatible_response_bytes(
        &DArc::new(scripting::Metrics::default()),
        url,
        client.get(url),
        false,
        1024,
    )
    .await;
    server.await.unwrap();
    result
}

#[tokio::test]
async fn redirect_rejects_nonstandard_port_before_connecting() {
    let err = request("http://example.com:8080/", &[], false)
        .await
        .unwrap_err();
    let err = scripting::try_unwrap_any_err(err).unwrap();
    assert_eq!(err.causes, ["PORT_FORBIDDEN"]);
    assert!(!err.fatal);
}

#[tokio::test]
async fn redirect_rejects_unknown_tld_before_connecting() {
    let err = request("http://example.invalid/", &[], false)
        .await
        .unwrap_err();
    let err = scripting::try_unwrap_any_err(err).unwrap();
    assert_eq!(err.causes, ["TLD_FORBIDDEN"]);
    assert!(!err.fatal);
}

#[tokio::test]
async fn redirect_accepts_configured_extra_tld() {
    assert_eq!(
        request("http://example.custom/", &["custom".into()], true)
            .await
            .unwrap()
            .body,
        b"ok"
    );
}

#[tokio::test]
async fn redirect_accepts_public_tld_and_relative_location() {
    assert_eq!(
        request("http://example.org/", &[], true)
            .await
            .unwrap()
            .body,
        b"ok"
    );
    assert_eq!(request("/next", &[], true).await.unwrap().body, b"ok");
}

#[tokio::test]
async fn redirect_keeps_private_ip_protection() {
    let err = request("http://127.0.0.1/", &[], false).await.unwrap_err();
    let err = scripting::try_unwrap_any_err(err).unwrap();
    assert_eq!(err.causes, ["ADDRESS_FORBIDDEN"]);
}
