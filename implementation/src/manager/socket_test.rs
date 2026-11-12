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
        .control
        .try_send(test_frame(Methods::Event, 99, "occupier"))
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

const FORKS_SCHEDULE: &str = r#"{
    "forks": { "line": {}, "line/fix": { "requires": ["line"] } },
    "networks": { "net": { "forks": { "line": "always", "line/fix": "2025-01-01T00:00:00Z" } } }
}"#;

fn get_forks_payload(network: &str, timestamp: &str, network_hash: &[u8]) -> Vec<u8> {
    calldata::encode_obj(&calldata::Value::Map(
        [(
            "get_forks".to_owned(),
            calldata::Value::Map(
                [
                    (
                        "network".to_owned(),
                        calldata::Value::Str(network.to_owned()),
                    ),
                    (
                        "timestamp".to_owned(),
                        calldata::Value::Str(timestamp.to_owned()),
                    ),
                    (
                        "network_hash".to_owned(),
                        calldata::Value::Bytes(network_hash.to_vec()),
                    ),
                ]
                .into(),
            ),
        )]
        .into(),
    ))
}

fn get_forks(schedule: &chainforks::Schedule, payload: &[u8]) -> (Methods, calldata::Value) {
    let frame = get_forks_frame(schedule, 7, payload);
    assert_eq!(frame.request_id, 7);
    (frame.method, calldata::decode_obj(&frame.payload).unwrap())
}

fn error_code(reply: &calldata::Value) -> (u8, String) {
    let calldata::Value::Map(reply) = reply else {
        panic!("expected error map, got {reply:?}");
    };
    let (Some(calldata::Value::Number(code)), Some(calldata::Value::Str(message))) =
        (reply.get("code"), reply.get("message"))
    else {
        panic!("malformed error {reply:?}");
    };
    (u8::try_from(code).unwrap(), message.clone())
}

#[test]
fn get_forks_answers_every_scheduled_fork_at_the_timestamp() {
    let schedule = chainforks::Schedule::parse(FORKS_SCHEDULE).unwrap();
    let hash = schedule.networks["net"].hash;

    for (timestamp, fix) in [
        ("2024-12-31T23:59:59Z", false),
        ("2025-01-01T00:00:00Z", true),
        ("2025-01-01T09:00:00+09:00", true),
    ] {
        let (method, reply) = get_forks(&schedule, &get_forks_payload("net", timestamp, &hash));
        assert_eq!(method, Methods::GetForks, "{reply:?}");
        let expected = calldata::Value::Map(
            [(
                "forks".to_owned(),
                calldata::Value::Map(
                    [
                        ("line".to_owned(), calldata::Value::Bool(true)),
                        ("line/fix".to_owned(), calldata::Value::Bool(fix)),
                    ]
                    .into(),
                ),
            )]
            .into(),
        );
        assert_eq!(reply, expected, "{timestamp}");
    }
}

#[test]
fn get_forks_rejects_an_unknown_or_empty_network() {
    let schedule = chainforks::Schedule::parse(FORKS_SCHEDULE).unwrap();
    let hash = schedule.networks["net"].hash;

    for network in ["other", ""] {
        let (method, reply) = get_forks(
            &schedule,
            &get_forks_payload(network, "2025-01-01T00:00:00Z", &hash),
        );
        assert_eq!(method, Methods::Error);
        let (code, message) = error_code(&reply);
        assert_eq!(code, Errors::UnknownNetwork.value());
        assert!(message.contains(&format!("`{network}`")), "{message}");
    }
}

#[test]
fn get_forks_rejects_a_foreign_schedule_hash() {
    let schedule = chainforks::Schedule::parse(FORKS_SCHEDULE).unwrap();
    let hash = schedule.networks["net"].hash;
    let foreign = [0xab; 32];

    let (method, reply) = get_forks(
        &schedule,
        &get_forks_payload("net", "2025-01-01T00:00:00Z", &foreign),
    );
    assert_eq!(method, Methods::Error);
    let (code, message) = error_code(&reply);
    assert_eq!(code, Errors::ChainforksMismatch.value());
    assert!(message.contains("`net`"), "{message}");
    assert!(message.contains(&hex::encode(hash)), "{message}");
    assert!(message.contains(&hex::encode(foreign)), "{message}");
}

#[test]
fn get_forks_rejects_a_malformed_payload() {
    let schedule = chainforks::Schedule::parse(FORKS_SCHEDULE).unwrap();
    let hash = schedule.networks["net"].hash;

    for payload in [
        b"not calldata".to_vec(),
        get_forks_payload("net", "2025-01-01T00:00:00Z", &hash[..31]),
        get_forks_payload("net", "2025-01-01", &hash),
        calldata::encode_obj(&calldata::Value::Map(
            [(
                "get_forks".to_owned(),
                calldata::Value::Map(Default::default()),
            )]
            .into(),
        )),
    ] {
        let (method, reply) = get_forks(&schedule, &payload);
        assert_eq!(method, Methods::Error);
        assert_eq!(
            error_code(&reply).0,
            Errors::MalformedFrame.value(),
            "{reply:?}"
        );
    }
}
