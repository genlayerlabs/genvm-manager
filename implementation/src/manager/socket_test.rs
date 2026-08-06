use super::*;
use std::sync::Arc;

fn test_frame(method: Methods, request_id: u64, text: &str) -> Frame {
    Frame {
        method,
        request_id,
        payload: text.as_bytes().to_vec(),
    }
}

fn decode_written_message(message: &Message) -> (u16, u64, Vec<u8>) {
    let Message::Binary(body) = message else {
        panic!("expected binary websocket message");
    };
    let method = u16::from_be_bytes(body[..2].try_into().unwrap());
    let request_id = u64::from_be_bytes(body[2..HEADER_LEN].try_into().unwrap());
    (method, request_id, body[HEADER_LEN..].to_vec())
}

fn test_writer(
    cap: usize,
) -> (
    Writer,
    tokio::sync::mpsc::Receiver<Frame>,
    tokio::sync::mpsc::Receiver<Frame>,
) {
    let (control, control_rx) = tokio::sync::mpsc::channel(cap);
    let (artifact, artifact_rx) = tokio::sync::mpsc::channel(cap);
    (Writer { control, artifact }, control_rx, artifact_rx)
}

fn started_event(id: u64) -> run::Event {
    run::Event::Started {
        genvm_id: run::GenVMId(id),
        host_genvm_id: None,
    }
}

fn terminal_event(id: u64) -> run::Event {
    run::Event::FailedToStart {
        genvm_id: run::GenVMId(id),
        host_genvm_id: None,
        error: "failed".to_owned(),
    }
}

#[test]
fn finished_event_encodes_consumed_result_as_bytes() {
    let expected = vec![0, 1, 2, 255];
    let payload = EventPayload::Finished {
        genvm_id: run::GenVMId(1),
        host_genvm_id: None,
        cause: "exited".to_owned(),
        exit_code: Some(0),
        consumed_result: Some(expected.clone().into()),
        metrics: serde_json::Value::Null,
        finished_at: "2026-08-06T00:00:00Z".to_owned(),
        version_major: 0,
        version_minor: 3,
        artifact_sizes: ArtifactSizesPayload {
            stdout: 0,
            stderr: 0,
            genvm_log: 0,
        },
    };

    let encoded = calldata::encode_obj(&payload);
    let calldata::Value::Map(event) = calldata::decode_obj(&encoded).unwrap() else {
        panic!("expected event map");
    };
    let Some(calldata::Value::Map(finished)) = event.get("finished") else {
        panic!("expected finished event");
    };
    assert_eq!(
        finished.get("consumed_result"),
        Some(&calldata::Value::Bytes(expected))
    );
}

#[tokio::test]
async fn writer_drains_control_before_artifacts() {
    let written = Arc::new(tokio::sync::Mutex::new(Vec::new()));
    let sink = Box::pin(futures_util::sink::unfold(
        written.clone(),
        |written, message| async move {
            written.lock().await.push(message);
            Ok::<_, std::convert::Infallible>(written)
        },
    ));
    let (control_tx, control_rx) = tokio::sync::mpsc::channel(8);
    let (artifact_tx, artifact_rx) = tokio::sync::mpsc::channel(8);

    artifact_tx
        .send(test_frame(Methods::GetArtifact, 1, "artifact-1"))
        .await
        .unwrap();
    artifact_tx
        .send(test_frame(Methods::GetArtifact, 2, "artifact-2"))
        .await
        .unwrap();
    control_tx
        .send(test_frame(Methods::Event, 0, "event"))
        .await
        .unwrap();
    let writer = tokio::spawn(writer_loop(sink, control_rx, artifact_rx));

    let first = tokio::time::timeout(std::time::Duration::from_secs(1), async {
        loop {
            if let Some(message) = written.lock().await.first().cloned() {
                return message;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();

    let (_, request_id, payload) = decode_written_message(&first);
    assert_eq!(request_id, 0);
    assert_eq!(payload, b"event");

    drop(control_tx);
    drop(artifact_tx);
    writer.await.unwrap().unwrap();
}

#[tokio::test]
async fn full_control_queue_does_not_kill_the_subscription() {
    let (writer, mut control_rx, _artifact_rx) = test_writer(1);
    let (events, events_rx) = tokio::sync::watch::channel(run::Snapshot::Queued {
        genvm_id: run::GenVMId(1),
        host_genvm_id: None,
    });

    // Occupy the capacity-1 control queue, so the forwarder's send cannot land
    // until the peer reads. A stalled client's connection looks exactly like
    // this from the manager's side.
    writer
        .send_control(test_frame(Methods::Event, 99, "occupier"))
        .unwrap();
    let forwarder = tokio::spawn(forward_events(writer.clone(), events_rx));

    events.send_replace(run::Snapshot::Event(started_event(1)));
    // Let the forwarder reach its send and find the queue full.
    for _ in 0..4 {
        tokio::task::yield_now().await;
    }

    let occupier = control_rx.recv().await.expect("occupier frame");
    assert_eq!(occupier.request_id, 99);

    // The queue has room again, so the event must still reach the writer. A
    // subscription that silently died while the connection lives on is the bug.
    let event = tokio::time::timeout(std::time::Duration::from_secs(1), control_rx.recv())
        .await
        .expect("event never delivered: subscription died silently")
        .expect("control channel closed instead of delivering the event");
    assert_eq!(event.method, Methods::Event);

    // And the subscription must still be live afterwards, not merely have
    // survived long enough to flush one frame.
    events.send_replace(run::Snapshot::Event(started_event(2)));
    let next = tokio::time::timeout(std::time::Duration::from_secs(1), control_rx.recv())
        .await
        .expect("subscription stopped forwarding after the queue drained")
        .expect("control channel closed instead of delivering the event");
    assert_eq!(next.method, Methods::Event);
    forwarder.abort();
}

#[tokio::test]
async fn closed_connection_ends_the_forwarder() {
    let (writer, control_rx, _artifact_rx) = test_writer(1);
    let (events, events_rx) = tokio::sync::watch::channel(run::Snapshot::Queued {
        genvm_id: run::GenVMId(1),
        host_genvm_id: None,
    });
    let forwarder = tokio::spawn(forward_events(writer, events_rx));

    drop(control_rx);
    events.send_replace(run::Snapshot::Event(started_event(1)));

    tokio::time::timeout(std::time::Duration::from_secs(1), forwarder)
        .await
        .expect("forwarder must exit once the connection is gone")
        .unwrap();
}

#[tokio::test]
async fn terminal_event_ends_the_forwarder() {
    let (writer, mut control_rx, _artifact_rx) = test_writer(1);
    let (events, events_rx) = tokio::sync::watch::channel(run::Snapshot::Queued {
        genvm_id: run::GenVMId(1),
        host_genvm_id: None,
    });
    let forwarder = tokio::spawn(forward_events(writer, events_rx));
    let terminal = terminal_event(1);
    let expected = event_frame(terminal.clone());

    events.send_replace(run::Snapshot::Event(terminal));

    let event = tokio::time::timeout(std::time::Duration::from_secs(1), control_rx.recv())
        .await
        .expect("terminal event was not delivered")
        .expect("control channel closed instead of delivering the terminal event");
    assert_eq!(event.method, expected.method);
    assert_eq!(event.payload, expected.payload);
    tokio::time::timeout(std::time::Duration::from_secs(1), forwarder)
        .await
        .expect("forwarder must exit after a terminal event")
        .unwrap();
}

#[tokio::test]
async fn coalescing_keeps_the_terminal_event() {
    let (writer, mut control_rx, _artifact_rx) = test_writer(1);
    let (events, events_rx) = tokio::sync::watch::channel(run::Snapshot::Queued {
        genvm_id: run::GenVMId(1),
        host_genvm_id: None,
    });
    let terminal = terminal_event(1);
    let expected = event_frame(terminal.clone());

    events.send_replace(run::Snapshot::Event(started_event(1)));
    events.send_replace(run::Snapshot::Event(terminal));
    let forwarder = tokio::spawn(forward_events(writer, events_rx));

    let event = tokio::time::timeout(std::time::Duration::from_secs(1), control_rx.recv())
        .await
        .expect("coalesced terminal event was not delivered")
        .expect("control channel closed instead of delivering the terminal event");
    assert_eq!(event.method, expected.method);
    assert_eq!(event.payload, expected.payload);
    tokio::time::timeout(std::time::Duration::from_secs(1), forwarder)
        .await
        .expect("forwarder must exit after the coalesced terminal event")
        .unwrap();
}
