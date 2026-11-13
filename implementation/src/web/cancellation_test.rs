use super::*;
use std::time::Duration;
use tokio::io::AsyncReadExt as _;

async fn disconnect_cancels_request(render: bool, body_started: bool) {
    let server = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", server.local_addr().unwrap());
    let mut config = test_config(url.clone());
    config.always_allow_hosts.push("127.0.0.1".into());
    let (cancel, _canceller) = genvm_common::cancellation::make();
    let (handler, _, _) = create_web_module(cancel, config).await.unwrap();
    let (mut host, module) = tokio::io::duplex(4096);
    let task = tokio::spawn(async move { handler(Box::new(module), None).await });
    common::write_message(
        &mut host,
        &calldata::encode_obj(&*common::tests::get_hello()),
    )
    .await
    .unwrap();
    let message = if render {
        web_iface::Message::Render(
            web_iface::RenderPayload {
                url: "https://example.com".to_owned(),
                mode: web_iface::RenderMode::Text,
                wait_after_loaded: web_iface::WaitAfterLoaded::Seconds(60),
            },
            1024,
        )
    } else {
        web_iface::Message::Request(
            web_iface::RequestPayload {
                url: url.clone(),
                method: web_iface::RequestMethod::GET,
                headers: Default::default(),
                body: None,
                sign: false,
            },
            1024,
        )
    };
    common::write_message(&mut host, &calldata::encode_obj(&message))
        .await
        .unwrap();
    let (mut outbound, _) = tokio::time::timeout(Duration::from_secs(2), server.accept())
        .await
        .unwrap()
        .unwrap();
    let mut request = Vec::new();
    while !request.ends_with(b"\r\n\r\n") {
        request.push(outbound.read_u8().await.unwrap());
    }
    if render {
        assert!(String::from_utf8_lossy(&request).starts_with("GET /render?"));
    }
    if body_started {
        outbound
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nResulting-Status: 200\r\n\r\nx")
            .await
            .unwrap();
        tokio::task::yield_now().await;
    }
    drop(host);
    tokio::time::timeout(Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
    let mut byte = [0];
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), outbound.read(&mut byte))
            .await
            .unwrap()
            .unwrap(),
        0
    );
}

#[tokio::test]
async fn host_disconnect_cancels_lua_http_headers() {
    disconnect_cancels_request(false, false).await;
}

#[tokio::test]
async fn host_disconnect_cancels_lua_http_body() {
    disconnect_cancels_request(false, true).await;
}

#[tokio::test]
async fn host_disconnect_cancels_webdriver_render() {
    disconnect_cancels_request(true, false).await;
}

#[tokio::test]
async fn host_disconnect_cancels_webdriver_body() {
    disconnect_cancels_request(true, true).await;
}

#[tokio::test]
async fn allowlisted_request_keeps_unfiltered_redirects() {
    let target = tokio::net::TcpListener::bind("127.0.0.2:0").await.unwrap();
    let location = format!("http://{}/", target.local_addr().unwrap());
    let source = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/", source.local_addr().unwrap());
    let source_task = tokio::spawn(async move {
        let (mut stream, _) = source.accept().await.unwrap();
        let mut header = Vec::new();
        while !header.ends_with(b"\r\n\r\n") {
            header.push(stream.read_u8().await.unwrap());
        }
        stream.write_all(format!("HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
    });
    let target_task = tokio::spawn(async move {
        let (mut stream, _) = target.accept().await.unwrap();
        let mut header = Vec::new();
        while !header.ends_with(b"\r\n\r\n") {
            header.push(stream.read_u8().await.unwrap());
        }
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            .await
            .unwrap();
    });
    let mut config = test_config(String::new());
    config.always_allow_hosts.push("127.0.0.1".into());
    let config = sync::DArc::new(config);
    let vm = create_test_vm(&config).await;
    let (_, ctx) = vm.create_ctx(&create_test_ctx(&config)).unwrap();
    let payload = vm
        .vm
        .to_value(&web_iface::RequestPayload {
            url,
            method: web_iface::RequestMethod::GET,
            headers: Default::default(),
            body: None,
            sign: false,
        })
        .unwrap();
    let response: mlua::Table = tokio::time::timeout(
        Duration::from_secs(2),
        vm.call_fn(&vm.data.request, (ctx, payload)),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(response.get::<String>("body").unwrap(), "ok");
    source_task.await.unwrap();
    target_task.await.unwrap();
}
