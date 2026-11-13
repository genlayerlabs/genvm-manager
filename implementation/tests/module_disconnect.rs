use std::{
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};

use genvm_common::sync::DArc;
use genvm_modules::common;
use genvm_modules_interfaces::{GenVMHello, GenVMId};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[derive(Default)]
struct State {
    started: tokio::sync::Notify,
    dropped: AtomicUsize,
    cleanup: AtomicUsize,
}

struct DropProbe(Arc<State>);

impl Drop for DropProbe {
    fn drop(&mut self) {
        self.0.dropped.fetch_add(1, Ordering::SeqCst);
    }
}

struct Context;

impl common::WithGenVMId for Context {
    fn genvm_id(&self) -> GenVMId {
        GenVMId(1)
    }
}

struct Provider {
    state: Arc<State>,
    stall_setup: bool,
    stall_request: bool,
}

struct Handler(Arc<State>, bool);

impl common::MessageHandler<u64, u64> for Handler {
    async fn handle(&self, value: u64) -> anyhow::Result<u64> {
        if self.1 {
            let _probe = DropProbe(self.0.clone());
            self.0.started.notify_one();
            std::future::pending::<()>().await;
        }
        Ok(value)
    }

    async fn cleanup(&self) -> anyhow::Result<()> {
        self.0.cleanup.fetch_add(1, Ordering::SeqCst);
        let _probe = DropProbe(self.0.clone());
        std::future::pending::<()>().await;
        Ok(())
    }
}

impl common::MessageHandlerProvider<u64, u64> for Provider {
    type Ctx = Context;
    fn create_execution_context(&self, _: GenVMHello) -> anyhow::Result<DArc<Context>> {
        Ok(DArc::new(Context))
    }
    async fn new_handler(
        &self,
        _: DArc<Context>,
    ) -> anyhow::Result<impl common::MessageHandler<u64, u64>> {
        if self.stall_setup {
            let _probe = DropProbe(self.state.clone());
            self.state.started.notify_one();
            std::future::pending::<()>().await;
        }
        Ok(Handler(self.state.clone(), self.stall_request))
    }
}

async fn send(host: &mut tokio::io::DuplexStream, data: &[u8]) {
    host.write_u32(data.len() as u32).await.unwrap();
    host.write_all(data).await.unwrap();
}

async fn start(
    stall_setup: bool,
    stall_request: bool,
) -> (
    tokio::io::DuplexStream,
    tokio::task::JoinHandle<()>,
    Arc<State>,
) {
    let state = Arc::new(State::default());
    let provider = Arc::new(Provider {
        state: state.clone(),
        stall_setup,
        stall_request,
    });
    let (mut host, module) = tokio::io::duplex(4096);
    let task = tokio::spawn(common::handle_stream(provider, module, "test", None));
    send(
        &mut host,
        &genlayer_calldata::encode_obj(&*common::tests::get_hello()),
    )
    .await;
    (host, task, state)
}

#[tokio::test]
async fn disconnect_drops_pending_setup() {
    let (host, task, state) = start(true, false).await;
    tokio::time::timeout(Duration::from_secs(2), state.started.notified())
        .await
        .unwrap();
    drop(host);
    tokio::time::timeout(Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(state.dropped.load(Ordering::SeqCst), 1);
    assert_eq!(state.cleanup.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn partial_frame_disconnect_drops_request_and_pending_teardown() {
    let (mut host, task, state) = start(false, true).await;
    send(&mut host, &genlayer_calldata::encode_obj(&1u64)).await;
    tokio::time::timeout(Duration::from_secs(2), state.started.notified())
        .await
        .unwrap();
    host.write_all(&[0, 0]).await.unwrap();
    drop(host);
    tokio::time::timeout(Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(state.dropped.load(Ordering::SeqCst), 2);
    assert_eq!(state.cleanup.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn excess_pipelining_cannot_hide_disconnect() {
    let (mut host, task, state) = start(false, true).await;
    let message = genlayer_calldata::encode_obj(&1u64);
    send(&mut host, &message).await;
    tokio::time::timeout(Duration::from_secs(2), state.started.notified())
        .await
        .unwrap();
    send(&mut host, &message).await;
    send(&mut host, &message).await;
    tokio::time::timeout(Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(state.dropped.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn sequential_requests_keep_framing_and_cleanup() {
    let (mut host, task, state) = start(false, false).await;
    for value in [1u64, 2] {
        let data = genlayer_calldata::encode_obj(&value);
        let length = (data.len() as u32).to_be_bytes();
        host.write_all(&length[..2]).await.unwrap();
        tokio::task::yield_now().await;
        host.write_all(&length[2..]).await.unwrap();
        host.write_all(&data).await.unwrap();
        let length = tokio::time::timeout(Duration::from_secs(2), host.read_u32())
            .await
            .unwrap()
            .unwrap();
        let mut response = vec![0; length as usize];
        host.read_exact(&mut response).await.unwrap();
        let response =
            genlayer_calldata::decode_obj::<genvm_modules_interfaces::Result<u64>>(&response)
                .unwrap();
        assert!(
            matches!(response, genvm_modules_interfaces::Result::Ok(result) if result == value)
        );
    }
    drop(host);
    tokio::time::timeout(Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(state.cleanup.load(Ordering::SeqCst), 1);
    assert_eq!(state.dropped.load(Ordering::SeqCst), 1);
}
