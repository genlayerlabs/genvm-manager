use super::*;

fn test_ctx(retention: &str, permits: usize) -> Ctx {
    Ctx {
        known_executions: Default::default(),
        host_genvm_ids: std::sync::Mutex::new(HashMap::new()),
        boot_id: 7,
        execution_retention: ManagerDuration::try_from(retention).unwrap(),
        next_genvm_id: std::sync::atomic::AtomicU64::new(1),
        permits: Arc::new(tokio::sync::Semaphore::new(permits)),
        max_permits: tokio::sync::Mutex::new(PermitsData {
            max: permits,
            num_throttled: 0,
            throttled: None,
        }),
        permits_sync: 1,
        permits_nondet: 2,
        executors_path: std::path::PathBuf::new(),
    }
}

fn fake_execution(
    genvm_id: GenVMId,
    finished_at: Option<chrono::DateTime<chrono::Utc>>,
) -> sync::DArc<SingleGenVMContext> {
    fake_execution_with_host_id(genvm_id, finished_at, None)
}

fn fake_execution_with_host_id(
    genvm_id: GenVMId,
    finished_at: Option<chrono::DateTime<chrono::Utc>>,
    host_genvm_id: Option<String>,
) -> sync::DArc<SingleGenVMContext> {
    let events = tokio::sync::watch::Sender::new(Snapshot::Queued {
        genvm_id,
        host_genvm_id: host_genvm_id.clone(),
    });
    let exec = sync::DArc::new(SingleGenVMContext {
        id: genvm_id,
        host_genvm_id,
        version: std::sync::RwLock::new("v0.test".to_owned()),
        version_major: AtomicU16::new(0),
        version_minor: AtomicU16::new(0),
        result: tokio::sync::OnceCell::new(),
        started_event: tokio::sync::OnceCell::new(),
        terminal_event: tokio::sync::OnceCell::new(),
        terminal_at: tokio::sync::OnceCell::new(),
        events,
        started_at: chrono::Utc::now(),
        strict_deadline: chrono::Utc::now() + chrono::Duration::hours(1),
        stdout_stderr_sem: Arc::new(tokio::sync::Semaphore::new(2)),
        stdout: tokio::sync::OnceCell::new(),
        stderr: tokio::sync::OnceCell::new(),
        log_sink: Arc::new(LogSinkInner::new(false)),
        consumed_result: tokio::sync::OnceCell::new(),
        process_handle: tokio::sync::Mutex::new(None),
        cancel_requested: AtomicBool::new(false),
        cancel_notify: tokio::sync::Notify::new(),
        finish_cause: std::sync::Mutex::new(None),
        all_permits: crossbeam::atomic::AtomicCell::new(None),
        nested_runs: AtomicUsize::new(0),
        nested_runs_done: tokio::sync::Notify::new(),
        execution_context: tokio::sync::OnceCell::new(),
    });

    if let Some(finished_at) = finished_at {
        exec.result
            .set(SingleGenVMContextDone {
                finished_at,
                stdout: "out".to_owned(),
                stderr: "err".to_owned(),
                genvm_log: Vec::new(),
                metrics: serde_json::Value::Null,
                consumed_result: Some(vec![1, 2, 3]),
                version_major: 0,
                version_minor: 0,
                cause: FinishCause::Exited,
                exit_code: Some(0),
            })
            .unwrap();
    }

    exec
}

#[test]
fn duration_strings_parse_with_units() {
    assert_eq!(
        ManagerDuration::try_from("30s").unwrap().to_std(),
        std::time::Duration::from_secs(30)
    );
    assert_eq!(
        ManagerDuration::try_from("10.5m").unwrap().to_std(),
        std::time::Duration::from_secs(630)
    );
    assert_eq!(
        ManagerDuration::try_from("250ms").unwrap().to_std(),
        std::time::Duration::from_millis(250)
    );
    assert_eq!(
        ManagerDuration::try_from("1h").unwrap().to_std(),
        std::time::Duration::from_secs(3600)
    );
}

#[test]
fn duration_strings_reject_missing_or_unknown_units() {
    assert!(ManagerDuration::try_from("30").is_err());
    assert!(ManagerDuration::try_from("30q").is_err());
}

#[test]
fn reserve_execution_is_atomic_for_shared_host_id() {
    const TASKS: usize = 32;

    let ctx = test_ctx("5m", 2);
    let host_genvm_id = "host-run-1";
    let barrier = std::sync::Barrier::new(TASKS);
    let builds = AtomicUsize::new(0);
    let reservations = std::thread::scope(|s| {
        let mut handles = Vec::with_capacity(TASKS);
        for _ in 0..TASKS {
            let ctx = &ctx;
            let barrier = &barrier;
            let builds = &builds;
            handles.push(s.spawn(move || {
                barrier.wait();
                ctx.reserve_execution(Some(host_genvm_id), |genvm_id| {
                    builds.fetch_add(1, Ordering::SeqCst);
                    fake_execution_with_host_id(genvm_id, None, Some(host_genvm_id.to_owned()))
                })
            }));
        }
        handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect::<Vec<_>>()
    });

    let mut existing = 0;
    let mut reserved = 0;
    let genvm_ids = reservations
        .into_iter()
        .map(|reservation| match reservation {
            Reservation::Existing(genvm_id) => {
                existing += 1;
                genvm_id
            }
            Reservation::Reserved(exec_ctx) => {
                reserved += 1;
                exec_ctx.id
            }
        })
        .collect::<Vec<_>>();

    assert_eq!(builds.load(Ordering::SeqCst), 1);
    assert_eq!((reserved, existing), (1, TASKS - 1));
    assert!(genvm_ids.iter().all(|genvm_id| *genvm_id == genvm_ids[0]));
}

#[test]
fn ack_removes_retained_result_but_status_is_not_destructive() {
    let ctx = test_ctx("5m", 2);
    let genvm_id = GenVMId(1);
    let host_genvm_id = "host-run-1";
    ctx.known_executions.pin().insert(
        genvm_id,
        fake_execution_with_host_id(
            genvm_id,
            Some(chrono::Utc::now()),
            Some(host_genvm_id.to_owned()),
        ),
    );
    ctx.host_genvm_ids
        .lock()
        .unwrap()
        .insert(host_genvm_id.to_owned(), genvm_id);

    assert!(ctx.status(genvm_id).is_some());
    assert!(ctx.status(genvm_id).is_some());
    assert_eq!(ctx.ack(genvm_id), AckOutcome::Acked);
    assert!(ctx.status(genvm_id).is_none());
    assert!(!ctx
        .host_genvm_ids
        .lock()
        .unwrap()
        .contains_key(host_genvm_id));
}

#[test]
fn ack_before_terminal_is_refused_and_preserves_run() {
    let ctx = test_ctx("5m", 2);
    let genvm_id = GenVMId(1);
    let host_genvm_id = "host-run-1";
    ctx.known_executions.pin().insert(
        genvm_id,
        fake_execution_with_host_id(genvm_id, None, Some(host_genvm_id.to_owned())),
    );
    ctx.host_genvm_ids
        .lock()
        .unwrap()
        .insert(host_genvm_id.to_owned(), genvm_id);

    let acked = ctx.ack(genvm_id);
    let attach_succeeds = ctx.attach(ctx.boot_id(), genvm_id).is_ok();
    let status_contains_run = ctx.status_executions().get(genvm_id.to_string()).is_some();
    let token_mapping = ctx
        .host_genvm_ids
        .lock()
        .unwrap()
        .get(host_genvm_id)
        .copied();

    assert_eq!(
        (acked, attach_succeeds, status_contains_run, token_mapping),
        (AckOutcome::NotFinished, true, true, Some(genvm_id))
    );
}

#[test]
fn ack_does_not_orphan_reused_token() {
    let ctx = test_ctx("5m", 2);
    let host_genvm_id = "host-run-1";
    let old_id = GenVMId(1);
    let new_id = GenVMId(2);
    ctx.known_executions.pin().insert(
        old_id,
        fake_execution_with_host_id(
            old_id,
            Some(chrono::Utc::now()),
            Some(host_genvm_id.to_owned()),
        ),
    );
    ctx.host_genvm_ids
        .lock()
        .unwrap()
        .insert(host_genvm_id.to_owned(), old_id);
    ctx.known_executions.pin().insert(
        new_id,
        fake_execution_with_host_id(new_id, None, Some(host_genvm_id.to_owned())),
    );
    ctx.host_genvm_ids
        .lock()
        .unwrap()
        .insert(host_genvm_id.to_owned(), new_id);

    assert_eq!(ctx.ack(old_id), AckOutcome::Acked);

    let token_mapping = ctx
        .host_genvm_ids
        .lock()
        .unwrap()
        .get(host_genvm_id)
        .copied();
    assert!(ctx.attach(ctx.boot_id(), new_id).is_ok());
    assert_eq!(token_mapping, Some(new_id));
}

#[test]
fn token_is_reusable_once_its_execution_is_gone() {
    let ctx = test_ctx("5m", 2);
    let host_genvm_id = "host-run-1";
    let first_id = match ctx.reserve_execution(Some(host_genvm_id), |genvm_id| {
        fake_execution_with_host_id(genvm_id, None, Some(host_genvm_id.to_owned()))
    }) {
        Reservation::Reserved(exec_ctx) => exec_ctx.id,
        Reservation::Existing(_) => panic!("first reservation already existed"),
    };
    ctx.known_executions.pin().remove(&first_id);

    let second_id = match ctx.reserve_execution(Some(host_genvm_id), |genvm_id| {
        fake_execution_with_host_id(genvm_id, None, Some(host_genvm_id.to_owned()))
    }) {
        Reservation::Reserved(exec_ctx) => exec_ctx.id,
        Reservation::Existing(_) => panic!("stale token returned an existing execution"),
    };

    assert_ne!(second_id, first_id);
}

#[tokio::test]
async fn ttl_expiry_removes_finished_execution() {
    let ctx = sync::DArc::new(test_ctx("1ms", 2));
    let genvm_id = GenVMId(1);
    ctx.known_executions.pin().insert(
        genvm_id,
        fake_execution(
            genvm_id,
            Some(chrono::Utc::now() - chrono::Duration::seconds(2)),
        ),
    );

    gc_step(&ctx).await;
    assert!(ctx.status(genvm_id).is_none());
}

#[tokio::test]
async fn gc_does_not_orphan_reused_token() {
    // A finished run past retention and a fresh run that reused its idempotency
    // token: GC must drop the old execution without touching the token mapping
    // the new run installed.
    let ctx = sync::DArc::new(test_ctx("1ms", 2));
    let host_genvm_id = "host-run-1";
    let old_id = GenVMId(1);
    let new_id = GenVMId(2);
    ctx.known_executions.pin().insert(
        old_id,
        fake_execution_with_host_id(
            old_id,
            Some(chrono::Utc::now() - chrono::Duration::seconds(2)),
            Some(host_genvm_id.to_owned()),
        ),
    );
    ctx.known_executions.pin().insert(
        new_id,
        fake_execution_with_host_id(new_id, None, Some(host_genvm_id.to_owned())),
    );
    ctx.host_genvm_ids
        .lock()
        .unwrap()
        .insert(host_genvm_id.to_owned(), new_id);

    gc_step(&ctx).await;

    let token_mapping = ctx
        .host_genvm_ids
        .lock()
        .unwrap()
        .get(host_genvm_id)
        .copied();
    assert!(ctx.status(old_id).is_none());
    assert!(ctx.attach(ctx.boot_id(), new_id).is_ok());
    assert_eq!(token_mapping, Some(new_id));
}

#[test]
fn attach_observes_concurrent_terminal() {
    // Races a terminal publish against `attach`. Before the watch channel the
    // window was between taking the snapshot and subscribing: a terminal
    // landing there was in neither. The snapshot now comes from the receiver
    // itself, so the sweep passes by construction rather than by luck.
    const ROUNDS: u64 = 10_000;
    let ctx = test_ctx("5m", 2);
    let execs: Vec<_> = (1..=ROUNDS)
        .map(|i| {
            let genvm_id = GenVMId(i);
            let exec = fake_execution(genvm_id, None);
            ctx.known_executions.pin().insert(genvm_id, exec.clone());
            exec
        })
        .collect();

    // A rendezvous channel rather than a barrier, and no panics inside the
    // scope: a failure breaks out of the loop, the sender drops when the
    // closure ends, the publisher unblocks, and the scope joins cleanly.
    // Panicking inside would leave the publisher parked in `recv()` forever.
    let (start_tx, start_rx) = std::sync::mpsc::sync_channel::<()>(0);
    let execs_ref = &execs;
    let mut failure = None;
    std::thread::scope(|s| {
        s.spawn(move || {
            for (round, exec) in execs_ref.iter().enumerate() {
                let event = Event::FailedToStart {
                    genvm_id: exec.id,
                    host_genvm_id: None,
                    error: "raced".to_owned(),
                };
                if start_rx.recv().is_err() {
                    return;
                }
                for _ in 0..(round % 16) {
                    std::hint::spin_loop();
                }
                exec.publish_terminal(event);
            }
        });

        let start_tx = start_tx;
        'rounds: for exec in &execs {
            start_tx.send(()).unwrap();
            let (snapshot, mut rx) = ctx.attach(ctx.boot_id(), exec.id).unwrap();
            if matches!(&snapshot, Snapshot::Event(event) if event.is_terminal()) {
                continue;
            }
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
            loop {
                match rx.has_changed() {
                    Ok(true) => {
                        if matches!(
                            rx.borrow_and_update().clone(),
                            Snapshot::Event(event) if event.is_terminal()
                        ) {
                            break;
                        }
                    }
                    Ok(false) => {
                        if std::time::Instant::now() >= deadline {
                            failure = Some(format!(
                                "genvm {}: terminal event in neither snapshot nor subscription",
                                exec.id.0
                            ));
                            break 'rounds;
                        }
                        std::thread::yield_now();
                    }
                    Err(e) => {
                        failure = Some(format!("genvm {}: subscription broke: {e}", exec.id.0));
                        break 'rounds;
                    }
                }
            }
        }
    });

    if let Some(failure) = failure {
        panic!("{failure}");
    }
}

#[tokio::test]
async fn cancel_while_queued_consumes_no_permit() {
    let permits = Arc::new(tokio::sync::Semaphore::new(0));
    let exec = fake_execution(GenVMId(1), None);
    let waiter = tokio::spawn({
        let permits = permits.clone();
        let exec = exec.clone();
        async move {
            let permit_future = acquire_run_permits(permits, 1);
            tokio::pin!(permit_future);
            tokio::select! {
                _ = exec.wait_cancelled() => false,
                permit = &mut permit_future => {
                    drop(permit);
                    true
                }
            }
        }
    });

    tokio::task::yield_now().await;
    exec.request_finish(FinishCause::Cancelled);
    assert!(!waiter.await.unwrap());
    assert_eq!(permits.available_permits(), 0);
    permits.add_permits(1);
    assert_eq!(permits.available_permits(), 1);
}

#[tokio::test]
async fn permits_cannot_drop_below_the_most_expensive_run() {
    let ctx = test_ctx("5m", 8);
    assert_eq!(ctx.min_permits(), 2);

    assert_eq!(ctx.set_permits(1).await, 8);
    assert_eq!(ctx.get_current_permits(), 8);

    assert_eq!(ctx.set_permits(2).await, 2);
    assert_eq!(ctx.get_current_permits(), 2);
}

#[tokio::test]
async fn nested_run_does_not_consume_parent_permit() {
    let ctx = test_ctx("5m", 1);
    let exec = fake_execution(GenVMId(1), None);
    let permit = ctx.permits.clone().acquire_owned().await.unwrap();
    exec.all_permits.store(Some(Box::new(permit)));

    let registration = NestedRunRegistration::new(exec.clone());
    assert_eq!(ctx.permits.available_permits(), 0);
    drop(registration);
    exec.wait_for_nested_runs().await;
    assert_eq!(ctx.permits.available_permits(), 0);

    exec.all_permits.store(None);
    assert_eq!(ctx.permits.available_permits(), 1);
}

#[tokio::test]
async fn nested_run_observes_cancel_and_deadline() {
    let cancelled = fake_execution(GenVMId(1), None);
    cancelled.request_finish(FinishCause::Cancelled);
    assert_eq!(
        wait_for_process_stop(&cancelled, None, std::time::Duration::from_secs(60)).await,
        ProcessStop::Cancelled
    );

    let deadline = fake_execution(GenVMId(2), None);
    assert_eq!(
        wait_for_process_stop(&deadline, None, std::time::Duration::from_millis(1)).await,
        ProcessStop::Deadline
    );
}

#[tokio::test]
async fn parent_stream_close_stops_and_reaps_nested_run() {
    let exec = fake_execution(GenVMId(1), None);
    let stream = ManagerHostStreamState::default();
    let registration = NestedRunRegistration::new(exec.clone());

    stream.close();
    assert_eq!(
        wait_for_process_stop(&exec, Some(&stream), std::time::Duration::from_secs(60)).await,
        ProcessStop::CallerClosed
    );

    drop(registration);
    tokio::time::timeout(
        std::time::Duration::from_secs(1),
        exec.wait_for_nested_runs(),
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn nested_reply_uses_little_endian_length_prefix() {
    use tokio::io::AsyncReadExt;

    let reply = nested_internal_error();
    let body = calldata::encode_obj(&reply);
    let (mut writer, mut reader) = tokio::io::duplex(1024);
    let expected = body.clone();
    let write = tokio::spawn(async move {
        write_length_prefixed(&mut writer, &body).await.unwrap();
    });

    let mut prefix = [0; 4];
    reader.read_exact(&mut prefix).await.unwrap();
    assert_eq!(prefix, (expected.len() as u32).to_le_bytes());
    let mut actual = vec![0; expected.len()];
    reader.read_exact(&mut actual).await.unwrap();
    assert_eq!(actual, expected);
    write.await.unwrap();

    let decoded: genvm_modules_interfaces::NestedRunReply = calldata::decode_obj(&actual).unwrap();
    assert_eq!(
        decoded.result.kind,
        genvm_modules_interfaces::ResultCode::InternalError
    );
    assert!(!decoded.effect_free);
}

/// The result a well-behaved nested callee reports: an outcome and nothing
/// else. Built from the real `ReportedResult`, so a field added to it breaks
/// this at compile time rather than at runtime.
fn clean_reported() -> genvm_modules_interfaces::ReportedResult {
    genvm_modules_interfaces::ReportedResult {
        execution_hash: bytes::Bytes::from(vec![1; 32]),
        small_hash: bytes::Bytes::from(vec![2; 32]),
        kind: genvm_modules_interfaces::ResultCode::Return,
        data: calldata::Value::Null.into(),
        backtrace: None,
        wasm_store_hashes: Default::default(),
        storage_changes: Vec::new(),
        emissions: Vec::new(),
        nondet_disagreement: None,
        nondet_results: Vec::new(),
        data_fees_remaining: Vec::new(),
        data_fees_consumed: Default::default(),
        llm_consumption: primitive_types::U256::zero(),
    }
}

/// Builds the frame an executor sends.
fn encoded_nested_result(reported: &genvm_modules_interfaces::ReportedResult) -> Vec<u8> {
    let mut encoded = vec![reported.kind as u8];
    encoded.extend(calldata::encode_obj(reported));
    encoded
}

#[test]
fn top_level_guard_accepts_a_valid_report() {
    let encoded = encoded_nested_result(&clean_reported());

    assert_eq!(
        guard_top_level_consumed_result(encoded.clone(), GenVMId(1)).unwrap(),
        encoded
    );
}

#[test]
fn top_level_guard_rejects_disagreeing_result_codes() {
    let mut encoded = encoded_nested_result(&clean_reported());
    encoded[0] = genvm_modules_interfaces::ResultCode::VmError as u8;

    assert!(guard_top_level_consumed_result(encoded, GenVMId(1)).is_err());
}

#[test]
fn top_level_guard_rejects_invalid_framing() {
    for encoded in [Vec::new(), vec![5], vec![0]] {
        assert!(guard_top_level_consumed_result(encoded, GenVMId(1)).is_err());
    }
}

#[test]
fn top_level_guard_rejects_invalid_hash_lengths() {
    let mut reported = clean_reported();
    reported.execution_hash = bytes::Bytes::new();

    assert!(guard_top_level_consumed_result(encoded_nested_result(&reported), GenVMId(1)).is_err());
}

#[cfg(debug_assertions)]
#[test]
#[should_panic(expected = "fatal VM error after its publication boundary")]
fn top_level_guard_asserts_on_fatal_in_debug_builds() {
    let mut reported = clean_reported();
    reported.kind = genvm_modules_interfaces::ResultCode::FatalVmError;

    let _ = guard_top_level_consumed_result(encoded_nested_result(&reported), GenVMId(1));
}

#[cfg(not(debug_assertions))]
#[test]
fn top_level_guard_downgrades_fatal_in_release_builds() {
    let mut reported = clean_reported();
    reported.kind = genvm_modules_interfaces::ResultCode::FatalVmError;

    let encoded =
        guard_top_level_consumed_result(encoded_nested_result(&reported), GenVMId(1)).unwrap();
    let (kind, reported) = decode_reported_result(&encoded, "test").unwrap();

    assert_eq!(kind, genvm_modules_interfaces::ResultCode::VmError);
    assert_eq!(reported.kind, genvm_modules_interfaces::ResultCode::VmError);
}

#[test]
fn fatal_downgrade_updates_both_result_codes() {
    let mut reported = clean_reported();
    reported.kind = genvm_modules_interfaces::ResultCode::FatalVmError;
    let encoded = downgrade_fatal_reported_result(reported);
    let (kind, reported) = decode_reported_result(&encoded, "test").unwrap();

    assert_eq!(kind, genvm_modules_interfaces::ResultCode::VmError);
    assert_eq!(reported.kind, genvm_modules_interfaces::ResultCode::VmError);
}

fn some_storage_delta() -> genvm_modules_interfaces::StorageDelta {
    genvm_modules_interfaces::StorageDelta::new([0; 36], vec![1])
}

fn some_emission() -> genvm_modules_interfaces::ExecutionEmission {
    genvm_modules_interfaces::ExecutionEmission::EmitEvent {
        topics: Vec::new(),
        blob: calldata::Map::new().into(),
        storage_fee: primitive_types::U256::zero(),
    }
}

#[test]
fn nested_reply_accepts_an_effect_free_report() {
    let reply = nested_reply_from_consumed_result(&encoded_nested_result(&clean_reported()))
        .expect("a report with no effects is accepted");
    assert!(reply.effect_free);
}

#[test]
fn nested_reply_refuses_every_reported_effect() {
    // One case per field `nested_effect` classifies as an effect. Not reachable
    // end to end -- the boundary derivation clears the permission behind each of
    // them -- so this is the only place the refusal is exercised.
    let mutate: [(&str, fn(&mut genvm_modules_interfaces::ReportedResult)); 10] = [
        ("storage_changes", |r| {
            r.storage_changes.push(some_storage_delta())
        }),
        ("emissions", |r| r.emissions.push(some_emission())),
        ("nondet_disagreement", |r| r.nondet_disagreement = Some(0)),
        ("nondet_results", |r| {
            r.nondet_results.push(bytes::Bytes::from_static(&[1]))
        }),
        ("llm_consumption", |r| {
            r.llm_consumption = primitive_types::U256::one()
        }),
        ("storage", |r| {
            r.data_fees_consumed.storage = primitive_types::U256::one()
        }),
        ("message_receipt", |r| {
            r.data_fees_consumed.message_receipt = primitive_types::U256::one()
        }),
        ("nondet_output", |r| {
            r.data_fees_consumed.nondet_output = primitive_types::U256::one()
        }),
        ("message_fee", |r| {
            r.data_fees_consumed.message_fee = primitive_types::U256::one()
        }),
        ("event", |r| {
            r.data_fees_consumed.event = primitive_types::U256::one()
        }),
    ];

    for (name, mutate) in mutate {
        let mut reported = clean_reported();
        mutate(&mut reported);
        assert!(
            nested_reply_from_consumed_result(&encoded_nested_result(&reported)).is_err(),
            "a report carrying `{name}` must be refused"
        );
    }
}

#[test]
fn nested_permissions_a_boundary_can_serve_are_accepted() {
    use genvm_modules_interfaces::NestedPermissions as P;

    for permissions in [
        P::default(),
        P::DETERMINISTIC,
        P::READ_STORAGE,
        P::CALL_OTHERS,
        P::REGISTER_RUNNERS,
        P::DETERMINISTIC | P::READ_STORAGE | P::CALL_OTHERS,
    ] {
        assert!(
            check_nested_permissions(permissions).is_ok(),
            "{permissions:?}"
        );
    }
}

#[test]
fn nested_permissions_the_boundary_cannot_serve_are_refused() {
    use genvm_modules_interfaces::NestedPermissions as P;

    for permissions in [
        P::SPAWN_NONDET,
        P::WRITE_STORAGE,
        P::SEND_MESSAGES,
        P::USE_BALANCE_FOR_MESSAGE_FEES,
        P::CALL_OTHERS | P::SPAWN_NONDET,
    ] {
        assert!(
            check_nested_permissions(permissions).is_err(),
            "{permissions:?}"
        );
    }
}

#[tokio::test]
async fn terminal_event_is_published_once() {
    let exec = fake_execution(GenVMId(1), None);
    let mut rx = exec.events.subscribe();

    assert!(finish_execution(&exec, None, FinishCause::Exited).await);
    assert!(!finish_execution(&exec, None, FinishCause::Cancelled).await);

    rx.changed().await.unwrap();
    assert!(matches!(
        rx.borrow_and_update().clone(),
        Snapshot::Event(Event::Finished { .. })
    ));
    assert!(matches!(rx.has_changed(), Ok(false)));
}

#[test]
fn under_or_at_limit_is_unchanged() {
    let mut s = String::from("hello");
    keep_tail(&mut s, 10);
    assert_eq!(s, "hello");
    keep_tail(&mut s, 5);
    assert_eq!(s, "hello");
}

#[test]
fn keeps_the_tail() {
    let mut s = String::from("0123456789");
    keep_tail(&mut s, 4);
    assert_eq!(s, "6789");
}

#[test]
fn never_cuts_mid_char() {
    // "aéb" = a(1) é(2) b(1) = 4 bytes; cutting at byte 2 is mid-'é', so the
    // cut advances to the next boundary, dropping the partial char.
    let mut s = String::from("aéb");
    keep_tail(&mut s, 2);
    assert_eq!(s, "b");
    assert!(std::str::from_utf8(s.as_bytes()).is_ok());
}

/// Field names of [`Request`] as the wire sees them.
///
/// The destructure is load-bearing: adding a field to `Request` stops this
/// compiling until the name is listed, which is what keeps
/// [`request_schema_matches_the_rust_struct`] from silently going stale.
fn request_field_names() -> Vec<&'static str> {
    // A value is needed only to destructure against; nothing reads it.
    #[allow(unused_variables)]
    let name_of = |req: Request| {
        let Request {
            selector,
            message,
            is_sync,
            debug_mode,
            max_execution_minutes,
            bucket_totals,
            host_data,
            timestamp,
            host,
            extra_args,
            calldata,
            code,
            permissions,
            no_modules,
            unsafe_overrides,
            leader_nondet_results,
            gas_data,
            message_fee_allocation,
            initial_time_units_allocation,
            record_actions,
            host_genvm_id,
            deadline,
            host_hello_data,
            hook_cross_contract_calls,
        } = req;
    };

    vec![
        "selector",
        "message",
        "is_sync",
        "debug_mode",
        "max_execution_minutes",
        "bucket_totals",
        "host_data",
        "timestamp",
        "host",
        "extra_args",
        "calldata",
        "code",
        "permissions",
        "no_modules",
        "unsafe_overrides",
        "leader_nondet_results",
        "gas_data",
        "message_fee_allocation",
        "initial_time_units_allocation",
        "record_actions",
        "host_genvm_id",
        "deadline",
        "host_hello_data",
        "hook_cross_contract_calls",
    ]
}

fn documented_properties(schema: &str) -> Vec<String> {
    let doc: serde_yaml::Value = serde_yaml::from_str(
        &std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../docs/website/src/impl-spec/appendix/manager-api.yaml"
        ))
        .expect("reading the documented manager API"),
    )
    .expect("parsing the documented manager API");

    let properties = doc["components"]["schemas"][schema]["properties"]
        .as_mapping()
        .unwrap_or_else(|| panic!("{schema} has no documented properties"));

    let mut names: Vec<String> = properties
        .keys()
        .map(|k| k.as_str().expect("property name is a string").to_owned())
        .collect();
    names.sort();
    names
}

/// A host implementing against the schema alone must see every field the
/// manager accepts, and none it does not. Drift here is not cosmetic: an
/// undocumented `permissions` silently grants the `wscn` default, and a
/// documented-but-absent field makes a host send something that is ignored.
#[test]
fn request_schema_matches_the_rust_struct() {
    let mut accepted = request_field_names()
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    accepted.sort();

    assert_eq!(documented_properties("GenvmRunRequest"), accepted);
}

/// Same contract for the message the host builds by hand.
#[test]
fn message_schema_matches_the_rust_struct() {
    #[allow(unused_variables)]
    let name_of = |message: genvm_modules_interfaces::MessageData| {
        let genvm_modules_interfaces::MessageData {
            contract_address,
            sender_address,
            origin_address,
            signer_address,
            chain_id,
            value,
            is_init,
            datetime,
        } = message;
    };

    let mut accepted = [
        "contract_address",
        "sender_address",
        "origin_address",
        "signer_address",
        "chain_id",
        "value",
        "is_init",
        "datetime",
    ]
    .map(str::to_owned)
    .to_vec();
    accepted.sort();

    assert_eq!(documented_properties("MessageData"), accepted);
}

#[test]
fn a_delegated_chain_is_bounded_more_tightly_than_plain_recursion() {
    // The first hop clamps to the cross-major cap, and every hop after it
    // spends one, so a chain of delegated calls dies well before the VM
    // recursion budget it was minted from would.
    let mut remaining = 1_000;
    let mut hops = 0;
    loop {
        remaining = cross_major_recursion(remaining);
        if remaining == 0 {
            break;
        }
        hops += 1;
        // What an executor spends on the sub-VM it spawns for the next hop.
        remaining -= 1;
    }

    assert_eq!(hops, CROSS_MAJOR_RECURSION as usize);
}

#[test]
fn a_delegated_chain_never_widens_the_budget_it_was_given() {
    for remaining in [
        0,
        1,
        CROSS_MAJOR_RECURSION - 1,
        CROSS_MAJOR_RECURSION,
        1_000,
    ] {
        assert!(cross_major_recursion(remaining) <= remaining, "{remaining}");
    }
}
