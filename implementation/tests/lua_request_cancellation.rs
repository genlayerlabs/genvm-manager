use std::{collections::BTreeMap, sync::Arc, time::Duration};

use genvm_common::sync::DArc;
use genvm_modules::{common, scripting};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[tokio::test]
async fn cancellation_drops_http_even_when_lua_retains_the_coroutine() {
    let config = DArc::new(common::ModuleBaseConfig {
        bind_address: None,
        vm_count: 1,
        lua_script_path: String::new(),
        lua_path: String::new(),
        signer_headers: Arc::new(BTreeMap::new()),
        signer_url: Arc::from(""),
        data_dir: String::new(),
    });
    let vm = scripting::UserVM::create(
        &config,
        |_| async { Ok(()) },
        Box::new(|_, _, _: &DArc<()>| Ok(())),
    )
    .await
    .unwrap();
    let ctx = vm.vm.create_table().unwrap();
    let scripting_ctx = scripting::create_ctx_part(
        &common::tests::get_hello(),
        &config,
        DArc::new(scripting::Metrics::default()),
        None,
    )
    .unwrap();
    scripting::setup_lua_default_ctx(DArc::new(scripting_ctx), &vm.vm, &ctx).unwrap();
    let request = vm
        .vm
        .load(
            r#"
        return function(ctx, url)
            retained = coroutine.running()
            return __dflt.request(ctx, {method = "GET", url = url, headers = {}})
        end
    "#,
        )
        .eval::<mlua::Function>()
        .unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/", listener.local_addr().unwrap());
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            request.push(stream.read_u8().await.unwrap());
        }
        started_tx.send(()).unwrap();
        let mut byte = [0];
        assert_eq!(stream.read(&mut byte).await.unwrap(), 0);
    });
    let mut call = Box::pin(vm.call_fn::<mlua::Value>(&request, (ctx.clone(), url)));
    tokio::select! {
        result = &mut call => panic!("request finished before cancellation: {result:?}"),
        _ = started_rx => {}
        _ = tokio::time::sleep(Duration::from_secs(2)) => panic!("request never reached server"),
    }
    drop(call);
    tokio::time::timeout(Duration::from_secs(2), server)
        .await
        .unwrap()
        .unwrap();
    assert!(vm.vm.globals().get::<mlua::Thread>("retained").is_ok());

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            request.push(stream.read_u8().await.unwrap());
        }
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            .await
            .unwrap();
    });
    let result: mlua::Table =
        tokio::time::timeout(Duration::from_secs(2), vm.call_fn(&request, (ctx, url)))
            .await
            .unwrap()
            .unwrap();
    assert_eq!(result.get::<String>("body").unwrap(), "ok");
    server.await.unwrap();
}
