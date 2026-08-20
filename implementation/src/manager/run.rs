use std::{
    collections::HashMap,
    ops::DerefMut,
    os::fd::{AsRawFd, FromRawFd},
    str::FromStr,
    sync::{
        atomic::{AtomicBool, AtomicU16, AtomicUsize, Ordering},
        Arc,
    },
};

use anyhow::Context as _;
use genlayer_calldata as calldata;
use genlayer_calldata::codec::{Decode, Encode};
use genvm_common::io::{set_fd_nonblocking, AsyncCustomFD, FdWrapper};
use genvm_common::*;
use serde::{Deserialize, Serialize};
use tokio::io::AsyncBufReadExt;

pub use genvm_modules_interfaces::GenVMId;

use crate::common::{LogSink, LogSinkElement, LogSinkInner, LoggerWithId, GENVM_BY_ID_LOGGER};

const ARTIFACT_CHUNK_CAP: usize = 256 * 1024;

/// How many delegated (cross-major) hops one chain of contract calls may make.
///
/// Deliberately far below any line's `VM_RECURSION`: a hop costs a whole
/// executor process, and the manager -- not any single line -- is the only
/// party that sees the chain as a whole.
const CROSS_MAJOR_RECURSION: u32 = 6;

/// The recursion budget a delegated callee is given: whatever is left of the
/// caller's, but never more than [`CROSS_MAJOR_RECURSION`].
///
/// Clamping rather than counting keeps this stateless -- the manager reads one
/// number off the envelope and writes one back -- and it can only ever tighten
/// the bound the caller already had. The subtree below a delegated call is
/// bounded by the same number, which is the point: a chain that has already
/// crossed a boundary does not get to recurse deeply in-process either.
fn cross_major_recursion(remaining: u32) -> u32 {
    remaining.min(CROSS_MAJOR_RECURSION)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ManagerDuration(std::time::Duration);

impl ManagerDuration {
    pub fn to_std(self) -> std::time::Duration {
        self.0
    }

    fn parse_decimal_millis(number: &str, unit_millis: u128) -> anyhow::Result<u128> {
        if number.is_empty() {
            anyhow::bail!("duration number is empty");
        }

        let mut parts = number.split('.');
        let whole = parts.next().unwrap();
        let frac = parts.next();
        if parts.next().is_some() {
            anyhow::bail!("duration has more than one decimal point");
        }
        if whole.is_empty() && frac.unwrap_or("").is_empty() {
            anyhow::bail!("duration number is empty");
        }
        if !whole.bytes().all(|b| b.is_ascii_digit()) {
            anyhow::bail!("duration whole part is not a number");
        }
        let whole_value = if whole.is_empty() {
            0
        } else {
            whole.parse::<u128>()?
        };

        let frac_millis = if let Some(frac) = frac {
            if frac.is_empty() || !frac.bytes().all(|b| b.is_ascii_digit()) {
                anyhow::bail!("duration fractional part is not a number");
            }
            let scale = 10_u128
                .checked_pow(frac.len() as u32)
                .ok_or_else(|| anyhow::anyhow!("duration fractional part is too long"))?;
            frac.parse::<u128>()?
                .checked_mul(unit_millis)
                .ok_or_else(|| anyhow::anyhow!("duration is too large"))?
                / scale
        } else {
            0
        };

        whole_value
            .checked_mul(unit_millis)
            .and_then(|v| v.checked_add(frac_millis))
            .ok_or_else(|| anyhow::anyhow!("duration is too large"))
    }
}

impl TryFrom<&str> for ManagerDuration {
    type Error = anyhow::Error;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        let (number, unit_millis) = if let Some(number) = value.strip_suffix("ms") {
            (number, 1)
        } else if let Some(number) = value.strip_suffix('s') {
            (number, 1_000)
        } else if let Some(number) = value.strip_suffix('m') {
            (number, 60_000)
        } else if let Some(number) = value.strip_suffix('h') {
            (number, 3_600_000)
        } else {
            anyhow::bail!("duration must end with ms, s, m, or h");
        };

        let millis = Self::parse_decimal_millis(number, unit_millis)?;
        let millis = u64::try_from(millis)?;
        Ok(Self(std::time::Duration::from_millis(millis)))
    }
}

impl Serialize for ManagerDuration {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&format!("{}ms", self.0.as_millis()))
    }
}

impl<'de> Deserialize<'de> for ManagerDuration {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        Self::try_from(s.as_str()).map_err(serde::de::Error::custom)
    }
}

impl<W: calldata::Writer> Encode<W> for ManagerDuration {
    type Error = W::Error;

    fn encode(&self, enc: &mut calldata::Encoder<W>) -> Result<(), Self::Error> {
        enc.push_str(&format!("{}ms", self.0.as_millis()))
    }
}

impl Decode for ManagerDuration {
    fn decode<D: calldata::codec::Deserializer>(
        deserializer: D,
    ) -> Result<Self, calldata::codec::DecodeError> {
        <String as Decode>::decode(deserializer).and_then(|s| {
            Self::try_from(s.as_str())
                .map_err(|e| calldata::codec::DecodeError::UserError(e.into()))
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FinishCause {
    Exited,
    Cancelled,
    Deadline,
    Shutdown,
}

impl FinishCause {
    pub fn as_str(self) -> &'static str {
        match self {
            FinishCause::Exited => "exited",
            FinishCause::Cancelled => "cancelled",
            FinishCause::Deadline => "deadline",
            FinishCause::Shutdown => "shutdown",
        }
    }
}

#[derive(Debug, Clone)]
pub struct ArtifactSizes {
    pub stdout: u64,
    pub stderr: u64,
    pub genvm_log: u64,
}

#[derive(Debug, Clone)]
pub enum Event {
    Started {
        genvm_id: GenVMId,
        host_genvm_id: Option<String>,
    },
    FailedToStart {
        genvm_id: GenVMId,
        host_genvm_id: Option<String>,
        error: String,
    },
    Finished {
        genvm_id: GenVMId,
        host_genvm_id: Option<String>,
        cause: FinishCause,
        exit_code: Option<i64>,
        consumed_result: Option<Vec<u8>>,
        metrics: serde_json::Value,
        finished_at: chrono::DateTime<chrono::Utc>,
        version_major: u16,
        version_minor: u16,
        artifact_sizes: ArtifactSizes,
    },
}

impl Event {
    pub fn is_terminal(&self) -> bool {
        matches!(self, Event::FailedToStart { .. } | Event::Finished { .. })
    }
}

#[derive(Debug, Clone)]
pub enum Snapshot {
    Queued {
        genvm_id: GenVMId,
        host_genvm_id: Option<String>,
    },
    Event(Event),
}

#[derive(Debug, Clone)]
pub struct Artifact {
    pub total_len: u64,
    pub data: bytes::Bytes,
}

/// Spawn relay that passes the stream to the module handler
async fn spawn_module_relay(
    parent_fd: FdWrapper,
    handler: super::modules::StreamHandler,
    genvm_id: GenVMId,
    module_name: &'static str,
    exec_ctx: sync::DArc<super::execution_context::ExecutionContext>,
) {
    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0, module = module_name; "relay starting");

    match parent_fd.into_async_fd() {
        Ok(stream) => {
            handler(Box::new(stream), Some(exec_ctx)).await;
        }
        Err(e) => {
            log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, module = module_name, error:err = e; "failed to create async stream");
        }
    }

    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0, module = module_name; "relay finished");
}

#[derive(Debug)]
pub struct SingleGenVMContextDone {
    pub finished_at: chrono::DateTime<chrono::Utc>,
    pub stdout: String,
    pub stderr: String,
    pub genvm_log: Vec<serde_json::Map<String, serde_json::Value>>,
    pub metrics: serde_json::Value,
    pub consumed_result: Option<Vec<u8>>,
    pub version_major: u16,
    pub version_minor: u16,
    pub cause: FinishCause,
    pub exit_code: Option<i64>,
}

impl serde::Serialize for SingleGenVMContextDone {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        use base64::Engine;
        use serde::ser::SerializeStruct;

        let mut state = serializer.serialize_struct("SingleGenVMContextDone", 8)?;
        state.serialize_field("finished_at", &self.finished_at.timestamp_millis())?;
        state.serialize_field("stdout", &self.stdout)?;
        state.serialize_field("stderr", &self.stderr)?;
        state.serialize_field("genvm_log", &self.genvm_log)?;
        state.serialize_field("metrics", &self.metrics)?;
        state.serialize_field(
            "consumed_result",
            &self
                .consumed_result
                .as_ref()
                .map(|v| base64::engine::general_purpose::STANDARD.encode(v)),
        )?;
        state.serialize_field("version_major", &self.version_major)?;
        state.serialize_field("version_minor", &self.version_minor)?;
        state.end()
    }
}

struct SingleGenVMContext {
    id: GenVMId,
    host_genvm_id: Option<String>,
    version: std::sync::RwLock<String>,
    version_major: AtomicU16,
    version_minor: AtomicU16,
    result: tokio::sync::OnceCell<SingleGenVMContextDone>,
    started_event: tokio::sync::OnceCell<Event>,
    terminal_event: tokio::sync::OnceCell<Event>,
    terminal_at: tokio::sync::OnceCell<chrono::DateTime<chrono::Utc>>,
    events: tokio::sync::watch::Sender<Snapshot>,
    started_at: chrono::DateTime<chrono::Utc>,
    strict_deadline: chrono::DateTime<chrono::Utc>,

    stdout_stderr_sem: Arc<tokio::sync::Semaphore>,
    stdout: tokio::sync::OnceCell<String>,
    stderr: tokio::sync::OnceCell<String>,
    log_sink: LogSink,
    consumed_result: tokio::sync::OnceCell<Vec<u8>>,

    process_handle: tokio::sync::Mutex<Option<tokio::process::Child>>,
    cancel_requested: AtomicBool,
    cancel_notify: tokio::sync::Notify,
    finish_cause: std::sync::Mutex<Option<FinishCause>>,
    all_permits: crossbeam::atomic::AtomicCell<Option<Box<dyn std::any::Any + Send + Sync>>>,
    nested_runs: AtomicUsize,
    nested_runs_done: tokio::sync::Notify,
    execution_context:
        tokio::sync::OnceCell<sync::DArc<super::execution_context::ExecutionContext>>,
}

impl SingleGenVMContext {
    fn set_version(&self, version: String, major: u16, minor: u16) {
        if let Ok(mut current) = self.version.write() {
            *current = version;
        }
        self.version_major.store(major, Ordering::SeqCst);
        self.version_minor.store(minor, Ordering::SeqCst);
    }

    fn version(&self) -> String {
        self.version
            .read()
            .map(|v| v.clone())
            .unwrap_or_else(|_| String::new())
    }

    fn request_finish(&self, cause: FinishCause) {
        let should_notify = !self.cancel_requested.swap(true, Ordering::SeqCst);
        if let Ok(mut stored_cause) = self.finish_cause.lock() {
            if stored_cause.is_none() {
                *stored_cause = Some(cause);
            }
        }
        if should_notify {
            self.cancel_notify.notify_waiters();
        }
    }

    fn finish_cause(&self) -> Option<FinishCause> {
        self.finish_cause.lock().ok().and_then(|cause| *cause)
    }

    async fn wait_cancelled(&self) {
        loop {
            let notified = self.cancel_notify.notified();
            if self.cancel_requested.load(Ordering::SeqCst) {
                return;
            }
            notified.await;
        }
    }

    fn nested_run_started(&self) {
        self.nested_runs.fetch_add(1, Ordering::SeqCst);
    }

    fn nested_run_finished(&self) {
        if self.nested_runs.fetch_sub(1, Ordering::SeqCst) == 1 {
            self.nested_runs_done.notify_waiters();
        }
    }

    async fn wait_for_nested_runs(&self) {
        loop {
            let notified = self.nested_runs_done.notified();
            if self.nested_runs.load(Ordering::SeqCst) == 0 {
                return;
            }
            notified.await;
        }
    }

    fn genvm_log_json_lines(genvm_log: &[serde_json::Map<String, serde_json::Value>]) -> Vec<u8> {
        let mut out = Vec::new();
        for item in genvm_log {
            match serde_json::to_vec(item) {
                Ok(mut line) => {
                    out.append(&mut line);
                    out.push(b'\n');
                }
                Err(e) => {
                    log_error!(error:err = e; "failed to serialize genvm log artifact line");
                }
            }
        }
        out
    }

    fn artifact_sizes_for(result: &SingleGenVMContextDone) -> ArtifactSizes {
        ArtifactSizes {
            stdout: result.stdout.len() as u64,
            stderr: result.stderr.len() as u64,
            genvm_log: Self::genvm_log_json_lines(&result.genvm_log).len() as u64,
        }
    }

    fn finished_event(&self, result: &SingleGenVMContextDone) -> Event {
        Event::Finished {
            genvm_id: self.id,
            host_genvm_id: self.host_genvm_id.clone(),
            cause: result.cause,
            exit_code: result.exit_code,
            consumed_result: result.consumed_result.clone(),
            metrics: result.metrics.clone(),
            finished_at: result.finished_at,
            version_major: result.version_major,
            version_minor: result.version_minor,
            artifact_sizes: Self::artifact_sizes_for(result),
        }
    }

    fn publish_terminal(&self, event: Event) -> bool {
        let _ = self.terminal_at.set(chrono::Utc::now());
        if self.terminal_event.set(event.clone()).is_err() {
            return false;
        }
        self.events.send_replace(Snapshot::Event(event));
        true
    }

    fn publish_started(&self) {
        let event = Event::Started {
            genvm_id: self.id,
            host_genvm_id: self.host_genvm_id.clone(),
        };
        let _ = self.started_event.set(event.clone());
        self.events.send_replace(Snapshot::Event(event));
    }
}

struct PermitsData {
    max: usize,
    num_throttled: usize,
    throttled: Option<tokio::sync::OwnedSemaphorePermit>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AckOutcome {
    Acked,
    NotFinished,
    Unknown,
}

pub struct Ctx {
    known_executions: papaya::HashMap<GenVMId, sync::DArc<SingleGenVMContext>>,
    host_genvm_ids: std::sync::Mutex<HashMap<String, GenVMId>>,
    boot_id: u64,
    execution_retention: ManagerDuration,
    next_genvm_id: std::sync::atomic::AtomicU64,
    pub permits: Arc<tokio::sync::Semaphore>,
    max_permits: tokio::sync::Mutex<PermitsData>,
    /// Permits a single run costs, by kind. One permit is one gigabyte of RAM.
    permits_sync: usize,
    permits_nondet: usize,

    executors_path: std::path::PathBuf,
}

impl Ctx {
    pub fn boot_id(&self) -> u64 {
        self.boot_id
    }

    pub fn executors_path(&self) -> &std::path::Path {
        &self.executors_path
    }

    pub async fn get_max_permits(&self) -> usize {
        let max_permits = self.max_permits.lock().await;
        max_permits.max
    }

    pub fn status_executions(&self) -> serde_json::Value {
        let mut ret = serde_json::Map::new();

        for (genvm_id, exec_ctx) in self.known_executions.pin().iter() {
            let result = if let Some(result) = exec_ctx.result.get() {
                serde_json::json!({
                    "finished_at": result.finished_at.to_rfc3339(),
                })
            } else {
                serde_json::Value::Null
            };

            ret.insert(
                genvm_id.to_string(),
                serde_json::json!({
                    "result": result,
                    "version": exec_ctx.version(),
                    "started_at": exec_ctx.started_at.to_rfc3339(),
                    "strict_deadline": exec_ctx.strict_deadline.to_rfc3339()
                }),
            );
        }

        ret.into()
    }

    pub fn get_current_permits(&self) -> usize {
        self.permits.available_permits()
    }

    pub fn permits_sync(&self) -> usize {
        self.permits_sync
    }

    pub fn permits_nondet(&self) -> usize {
        self.permits_nondet
    }

    /// Below this the most expensive run could never be admitted.
    pub fn min_permits(&self) -> usize {
        self.permits_sync.max(self.permits_nondet)
    }

    fn permits_for(&self, req: &Request) -> u32 {
        let count = if req.needs_modules() {
            self.permits_nondet
        } else {
            self.permits_sync
        };
        count as u32
    }

    pub async fn set_permits(&self, permits: usize) -> usize {
        let mut permits_lock = self.max_permits.lock().await;

        permits_lock.max += permits_lock.num_throttled;
        permits_lock.num_throttled = 0;
        permits_lock.throttled = None;
        // actually this causes drop of previous one, so we can enter more genvms than we have permits, but it's ok for now
        // especially since this method is expected to be called before starting any genvms at all

        let min = self.min_permits();
        if permits < min {
            log_warn!(permits = permits, min = min; "cannot set permits below the most expensive run");
            return permits_lock.max;
        }

        if permits_lock.max > permits {
            let delta = permits_lock.max - permits;
            let p = self
                .permits
                .clone()
                .acquire_many_owned(delta as u32)
                .await
                .unwrap();
            permits_lock.max = permits;
            permits_lock.num_throttled = delta;
            permits_lock.throttled = Some(p);
        } else {
            let delta = permits - permits_lock.max;
            self.permits.add_permits(delta);
            permits_lock.max = permits;
        }

        permits_lock.max
    }

    async fn request_finish(&self, genvm_id: GenVMId, cause: FinishCause) -> anyhow::Result<()> {
        let Some(exec_ctx) = self.known_executions.pin().get(&genvm_id).cloned() else {
            anyhow::bail!("GenVM with id {} not found", genvm_id);
        };

        if exec_ctx.result.get().is_some() {
            return Ok(());
        }

        exec_ctx.request_finish(cause);

        let mut child = exec_ctx.process_handle.lock().await;
        if let Some(child) = child.as_mut() {
            let _ = child.start_kill();
        }

        Ok(())
    }

    pub async fn graceful_shutdown(&self, genvm_id: GenVMId) -> anyhow::Result<()> {
        self.cancel(genvm_id).await?;
        let _ = self.await_terminal(genvm_id).await;
        Ok(())
    }

    pub fn attach(
        &self,
        boot_id: u64,
        genvm_id: GenVMId,
    ) -> anyhow::Result<(Snapshot, tokio::sync::watch::Receiver<Snapshot>)> {
        if boot_id != self.boot_id {
            anyhow::bail!("boot_id_mismatch");
        }
        let Some(exec_ctx) = self.known_executions.pin().get(&genvm_id).cloned() else {
            anyhow::bail!("unknown_id");
        };
        let mut rx = exec_ctx.events.subscribe();
        let snapshot = rx.borrow_and_update().clone();
        Ok((snapshot, rx))
    }

    pub async fn cancel(&self, genvm_id: GenVMId) -> anyhow::Result<()> {
        self.request_finish(genvm_id, FinishCause::Cancelled).await
    }

    pub fn ack(&self, genvm_id: GenVMId) -> AckOutcome {
        let pinned = self.known_executions.pin();
        let Some(exec_ctx) = pinned.get(&genvm_id) else {
            return AckOutcome::Unknown;
        };
        if exec_ctx.result.get().is_none() && exec_ctx.terminal_event.get().is_none() {
            return AckOutcome::NotFinished;
        }
        let Some(exec_ctx) = pinned.remove(&genvm_id) else {
            return AckOutcome::Unknown;
        };
        if let Some(host_genvm_id) = &exec_ctx.host_genvm_id {
            self.release_token(host_genvm_id, genvm_id);
        }
        AckOutcome::Acked
    }

    fn lock_host_ids(&self) -> std::sync::MutexGuard<'_, HashMap<String, GenVMId>> {
        match self.host_genvm_ids.lock() {
            Ok(host_ids) => host_ids,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    /// Drops a token only while it still maps to `genvm_id`. Between removing an
    /// execution and taking the lock, another start can reuse the token for a
    /// fresh run, and that mapping must survive.
    fn release_token(&self, host_genvm_id: &str, genvm_id: GenVMId) {
        let mut host_ids = self.lock_host_ids();
        if host_ids.get(host_genvm_id) == Some(&genvm_id) {
            host_ids.remove(host_genvm_id);
        }
    }

    pub fn status(&self, genvm_id: GenVMId) -> Option<sync::DArc<SingleGenVMContextDone>> {
        let Some(exec_ctx) = self.known_executions.pin().get(&genvm_id).cloned() else {
            log_trace_into!(&LoggerWithId, genvm_id:id = genvm_id.0; "genvm status requested for unknown id");
            return None;
        };

        exec_ctx
            .into_get_sub(|data| data.result.get())
            .lift_option()
            .map(sync::DArcStruct::into_arc)
    }

    pub fn get_artifact(
        &self,
        genvm_id: GenVMId,
        field: &str,
        offset: u64,
        max_len: u32,
    ) -> anyhow::Result<Artifact> {
        let Some(result) = self.status(genvm_id) else {
            anyhow::bail!("unknown_id");
        };

        let data = match field {
            "stdout" => result.stdout.as_bytes().to_vec(),
            "stderr" => result.stderr.as_bytes().to_vec(),
            "genvm_log" => SingleGenVMContext::genvm_log_json_lines(&result.genvm_log),
            _ => anyhow::bail!("unknown artifact field {}", field),
        };

        let total_len = data.len() as u64;
        let offset = usize::try_from(offset)
            .unwrap_or(usize::MAX)
            .min(data.len());
        let len = usize::try_from(max_len)
            .unwrap_or(usize::MAX)
            .min(ARTIFACT_CHUNK_CAP)
            .min(data.len().saturating_sub(offset));

        Ok(Artifact {
            total_len,
            data: bytes::Bytes::copy_from_slice(&data[offset..offset + len]),
        })
    }

    pub async fn await_terminal(&self, genvm_id: GenVMId) -> anyhow::Result<Event> {
        let (snapshot, mut rx) = self.attach(self.boot_id, genvm_id)?;
        if let Snapshot::Event(event) = snapshot {
            if event.is_terminal() {
                return Ok(event);
            }
        }

        loop {
            rx.changed().await?;
            if let Snapshot::Event(event) = rx.borrow_and_update().clone() {
                if event.is_terminal() {
                    return Ok(event);
                }
            }
        }
    }

    #[cfg(target_os = "macos")]
    fn detect_free_gigabytes() -> usize {
        log_warn!("automatic permits detection is not supported on macOS, using default value");

        32
    }

    #[cfg(not(target_os = "macos"))]
    fn detect_free_gigabytes() -> usize {
        let mut sys = sysinfo::System::new_all();
        sys.refresh_memory();
        sys.refresh_all();

        let free_memory = sys.free_memory();
        let free_swap = sys.free_swap();

        log_info!(
            free_memory = free_memory,
            free_swap = free_swap,
            free_memory_gb = free_memory as f64 / (1024.0 * 1024.0 * 1024.0),
            free_swap_gb = free_swap as f64 / (1024.0 * 1024.0 * 1024.0);
            "memory status"
        );

        ((free_memory + free_swap) / (1024 * 1024 * 1024)) as usize
    }

    pub fn new(config: &crate::manager::Config) -> anyhow::Result<Self> {
        let config_permits = config.permits.clone().unwrap_or_default();
        let permits_sync = config_permits.sync;
        let permits_nondet = config_permits.nondet;
        // the upper bound keeps the cast in `permits_for` honest
        anyhow::ensure!(
            (1..=u32::MAX as usize).contains(&permits_sync)
                && (1..=u32::MAX as usize).contains(&permits_nondet),
            "per-run permits must be positive and fit in u32"
        );
        let min_permits = permits_sync.max(permits_nondet);

        let permits = match config_permits.total {
            Some(p) => p,
            None => config_permits.per_gib.apply(Self::detect_free_gigabytes()),
        };
        let permits = if permits < min_permits {
            log_warn!(
                permits = permits, min = min_permits;
                "permits are below the most expensive run, raising"
            );
            min_permits
        } else {
            permits
        };
        log_info!(
            permits = permits,
            permits_sync = permits_sync,
            permits_nondet = permits_nondet,
            per_gib = config_permits.per_gib;
            "estimated concurrent GenVM permits"
        );

        let mut exe_path = std::env::current_exe()?;
        exe_path.pop();
        exe_path.pop();
        exe_path.push("executor");

        Ok(Self {
            known_executions: Default::default(),
            host_genvm_ids: std::sync::Mutex::new(HashMap::new()),
            boot_id: rand::random::<u64>(),
            execution_retention: config.execution_retention,
            next_genvm_id: std::sync::atomic::AtomicU64::new(1),
            permits: Arc::new(tokio::sync::Semaphore::new(permits)),
            max_permits: tokio::sync::Mutex::new(PermitsData {
                max: permits,
                num_throttled: 0,
                throttled: None,
            }),
            permits_sync,
            permits_nondet,

            executors_path: exe_path,
        })
    }
}

/// How often to sweep for expired deadlines and retained results.
///
/// Derived from the retention, because a fixed sweep decides how far past its
/// TTL a result can linger: a one minute sweep against a retention configured
/// in seconds keeps results for a minute regardless of the setting. The floor
/// keeps a tiny retention from spinning the loop.
fn gc_interval(ctx: &Ctx) -> std::time::Duration {
    const FLOOR: std::time::Duration = std::time::Duration::from_millis(250);
    const CEILING: std::time::Duration = std::time::Duration::from_secs(60);

    (ctx.execution_retention.to_std() / 4).clamp(FLOOR, CEILING)
}

async fn gc_step(ctx: &sync::DArc<Ctx>) {
    let now = chrono::Utc::now();

    // copy-out so that we don't hold the lock while doing async operations
    let keys = ctx
        .known_executions
        .pin()
        .iter()
        .map(|kv| *kv.0)
        .collect::<Vec<_>>();

    for key in keys {
        let Some(val) = ctx.known_executions.pin().get(&key).cloned() else {
            continue;
        };

        if val.strict_deadline < now && val.terminal_event.get().is_none() {
            log_warn_into!(&LoggerWithId, genvm_id:id = key.0; "genvm execution exceeded strict deadline, terminating");
            let _ = ctx.request_finish(key, FinishCause::Deadline).await;
        }
    }

    // Remove old finished executions
    let retention = chrono::Duration::from_std(ctx.execution_retention.to_std())
        .unwrap_or_else(|_| chrono::Duration::hours(24));
    let mut expired_host_ids = Vec::new();
    ctx.known_executions.pin().retain(|k, v| {
        let terminal_at = if let Some(result) = v.result.get() {
            result.finished_at
        } else if let Some(terminal_at) = v.terminal_at.get() {
            *terminal_at
        } else {
            return true;
        };
        let passed = now.signed_duration_since(terminal_at);
        if passed > retention {
            log_warn_into!(&LoggerWithId, genvm_id:id = k.0; "removing zombie genvm execution context");
            if let Some(host_genvm_id) = &v.host_genvm_id {
                expired_host_ids.push((host_genvm_id.clone(), *k));
            }
            return false;
        }
        true
    });
    for (host_genvm_id, genvm_id) in expired_host_ids {
        ctx.release_token(&host_genvm_id, genvm_id);
    }
}

pub async fn start_service(
    ctx: sync::DArc<Ctx>,
    cancel: Arc<cancellation::Token>,
) -> anyhow::Result<()> {
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = cancel.chan.closed() => {
                    log_info!("shutting down run service waiter");
                    let keys = ctx
                        .known_executions
                        .pin()
                        .iter()
                        .map(|kv| *kv.0)
                        .collect::<Vec<_>>();
                    for key in keys {
                        let _ = ctx.request_finish(key, FinishCause::Shutdown).await;
                    }
                    break;
                }
                _ = tokio::time::sleep(gc_interval(&ctx)) => {
                    gc_step(&ctx).await;
                }
            }
        }
    });

    Ok(())
}

use serde_with::serde_as;

fn decode_datetime_rfc3339(
    val: calldata::Value,
) -> std::result::Result<chrono::DateTime<chrono::Utc>, calldata::codec::DecodeError> {
    let s = match val {
        calldata::Value::Str(s) => s,
        _ => return Err(calldata::codec::DecodeError::UnexpectedKind(val.kind())),
    };
    match chrono::DateTime::parse_from_rfc3339(&s) {
        Ok(dt) => Ok(dt.to_utc()),
        Err(e) => Err(calldata::codec::DecodeError::UserError(Box::new(e))),
    }
}

fn encode_datetime_rfc3339<W: calldata::Writer>(
    dt: &chrono::DateTime<chrono::Utc>,
    enc: &mut calldata::Encoder<W>,
) -> std::result::Result<(), W::Error> {
    use chrono::SecondsFormat;
    let s = dt.to_rfc3339_opts(SecondsFormat::AutoSi, true);
    enc.push_str(&s)
}

fn default_extra_args() -> Vec<String> {
    Vec::new()
}

fn default_debug_mode() -> genvm_common::DebugMode {
    genvm_common::DebugMode::Disabled
}

fn default_no_modules() -> bool {
    false
}

fn default_reroute_to() -> String {
    String::new()
}

fn default_unsafe_overrides() -> UnsafeOverrides {
    UnsafeOverrides::default()
}

fn default_initial_recursion() -> Option<u32> {
    None
}

/// Overrides that exist to reach boundaries production traffic cannot. Each
/// field states the debug level it needs; none of them are honored with
/// debugging disabled, so consensus traffic is unaffected.
#[derive(
    Clone,
    Default,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Decode,
    genlayer_calldata::Encode,
)]
pub struct UnsafeOverrides {
    /// Run this version instead of the one the request's major resolves to: an
    /// executor directory as it stands, or a `re:`-prefixed pattern over
    /// manifest keys. Empty is no override. Honored from `debug_mode >= Safe`.
    #[serde(default = "default_reroute_to")]
    #[calldata(default = default_reroute_to)]
    pub reroute_to: String,
    /// Initial recursion budget for the chain, replacing the executor's own
    /// `VM_RECURSION`. Exhausting the real limit costs one executor process per
    /// unit of budget, so a boundary test seeds a small one instead. Honored
    /// from `debug_mode >= Unsafe`.
    #[serde(default)]
    #[calldata(default = default_initial_recursion)]
    pub initial_recursion: Option<u32>,
}

#[serde_as]
#[derive(
    Clone,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Decode,
    genlayer_calldata::Encode,
)]
pub struct Request {
    /// Which executor line runs this: for a top-level run the public-ABI major
    /// the host read off the contract's root slot, for a nested one the routing
    /// its caller was given.
    pub selector: genvm_modules_interfaces::ExecutorSelector,
    pub message: genvm_modules_interfaces::MessageData,
    pub is_sync: bool,
    /// Executor debug level. Controls captured-output bounding, tracing, runner
    /// `:latest`/`:test` resolution (under `unsafe`), and (under `unsafe-tracing`)
    /// exposing non-deterministic data. See `genvm_common::DebugMode`.
    #[serde(default)]
    #[calldata(default = default_debug_mode)]
    pub debug_mode: genvm_common::DebugMode,
    #[serde(default = "default_max_execution_minutes")]
    #[calldata(default = default_max_execution_minutes)]
    pub max_execution_minutes: u64,
    pub bucket_totals: Vec<num_bigint::BigInt>,
    pub host_data: String,
    #[calldata(serialize_with = encode_datetime_rfc3339, deserialize_with = decode_datetime_rfc3339)]
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub host: String,
    #[serde(default)]
    #[calldata(default = default_extra_args)]
    pub extra_args: Vec<String>,
    pub calldata: bytes::Bytes,
    pub code: Option<bytes::Bytes>,
    #[serde(default = "default_permissions")]
    #[calldata(default = default_permissions)]
    pub permissions: String,
    /// If true, don't require modules even if permissions suggest they're needed
    #[serde(default)]
    #[calldata(default = default_no_modules)]
    pub no_modules: bool,
    /// Debug-only overrides; see [`UnsafeOverrides`].
    #[serde(default)]
    #[calldata(default = default_unsafe_overrides)]
    pub unsafe_overrides: UnsafeOverrides,
    pub leader_nondet_results: Option<Vec<bytes::Bytes>>,
    /// Host-provided `node` fee constants (moved off `host_data`).
    #[serde(default)]
    #[calldata(default = default_gas_data)]
    pub gas_data: std::collections::BTreeMap<String, String>,
    /// Message-fee allocation tree passed alongside the execution.
    #[serde(default)]
    #[calldata(default = default_message_fee_allocation)]
    pub message_fee_allocation: Vec<genvm_modules_interfaces::fees::MessageAllocationNode>,
    /// Initial time-unit budget for this execution.
    pub initial_time_units_allocation: u32,
    /// Auditable supervisor action kinds to return in the execution result.
    #[serde(default)]
    #[calldata(default = default_record_actions)]
    pub record_actions: Vec<String>,
    #[serde(default)]
    #[calldata(default = default_host_genvm_id)]
    pub host_genvm_id: Option<String>,
    #[serde(default)]
    #[calldata(default = default_deadline)]
    pub deadline: Option<ManagerDuration>,
    #[serde(default)]
    #[calldata(default = default_host_hello_data)]
    pub host_hello_data: Vec<bytes::Bytes>,
    /// Whether the host wants to be asked where a `CallContract` should run.
    /// When false the manager answers `resolve_callcontract_executor` itself
    /// with a null reply, so every call stays in-process and the host never
    /// has to implement that method.
    #[serde(default)]
    #[calldata(default = default_hook_cross_contract_calls)]
    pub hook_cross_contract_calls: bool,
}

fn default_hook_cross_contract_calls() -> bool {
    false
}

fn default_gas_data() -> std::collections::BTreeMap<String, String> {
    std::collections::BTreeMap::new()
}

fn default_message_fee_allocation() -> Vec<genvm_modules_interfaces::fees::MessageAllocationNode> {
    Vec::new()
}

fn default_record_actions() -> Vec<String> {
    Vec::new()
}

fn default_host_genvm_id() -> Option<String> {
    None
}

fn default_deadline() -> Option<ManagerDuration> {
    None
}

fn default_host_hello_data() -> Vec<bytes::Bytes> {
    Vec::new()
}

fn default_permissions() -> String {
    "wscn".to_owned()
}

impl Request {
    /// Returns true if this request needs modules to be running.
    /// Modules are needed when running async (is_sync=false) with nondet permission ('n'),
    /// unless no_modules flag is set.
    pub fn needs_modules(&self) -> bool {
        !self.no_modules && !self.is_sync && self.permissions.contains('n')
    }

    /// The legacy v0.2 ABI stored the contract method name under the calldata
    /// key `"method"`; the current ABI uses the empty key `""`. A client sending
    /// old-format calldata would otherwise reach the executor with a `"method"`
    /// key its runner no longer understands. Detect that here, log an error so
    /// the stale caller is visible, and rewrite the key in place. No-op for
    /// well-formed (new-format) calldata or non-map calldata.
    pub fn patch_legacy_method_key(&mut self) {
        const LEGACY_METHOD_KEY: &str = "method";
        const NEW_METHOD_KEY: &str = "";

        let mut value = match calldata::decode(&self.calldata) {
            Ok(v @ calldata::Value::Map(_)) => v,
            _ => return,
        };
        let calldata::Value::Map(map) = &mut value else {
            return;
        };

        if let Some(method) = map.remove(LEGACY_METHOD_KEY) {
            log_error!(
                "received legacy 'method' key in run calldata; patching it to the new empty key"
            );
            map.insert(NEW_METHOD_KEY.to_owned(), method);
            self.calldata = bytes::Bytes::from(calldata::encode(&value));
        }
    }
}

fn default_max_execution_minutes() -> u64 {
    20
}

trait LogAppender {
    async fn append_structured(
        &mut self,
        level: logger::Level,
        data: serde_json::Map<String, serde_json::Value>,
    );
    async fn append_text(&mut self, serde_err: serde_json::Error, text: &str);
}

/// Used when capture is `disabled`: forwards the executor's logs to the
/// manager's own log instead of buffering them into the result.
struct LogAppenderToLog(GenVMId);

// `read_log_pipe` takes `impl DerefMut<Target = LA>`. The value-capturing
// appender is wrapped in `Arc<Mutex<..>>` (which derefs to it), but this
// forwarding appender holds no shared state, so it derefs to itself.
impl std::ops::Deref for LogAppenderToLog {
    type Target = Self;

    fn deref(&self) -> &Self::Target {
        self
    }
}

impl DerefMut for LogAppenderToLog {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self
    }
}

impl LogAppender for LogAppenderToLog {
    #[inline(always)]
    async fn append_structured(
        &mut self,
        level: logger::Level,
        data: serde_json::Map<String, serde_json::Value>,
    ) {
        log_with_level_into!(level, &LoggerWithId, log:serde = data, genvm_id:id = self.0.0; "genvm log");
    }
    #[inline(always)]
    async fn append_text(&mut self, serde_err: serde_json::Error, text: &str) {
        log_info_into!(&LoggerWithId, error:err = serde_err, genvm_id:id = self.0.0, line = text; "genvm log raw");
    }
}

struct LogAppenderToValue(LogSink);

impl LogAppender for LogAppenderToValue {
    #[inline(always)]
    async fn append_structured(
        &mut self,
        level: logger::Level,
        mut data: serde_json::Map<String, serde_json::Value>,
    ) {
        log_trace!(log:serde = data; "genvm log");

        data.insert("level".to_owned(), level.to_string().into());

        self.0.push(LogSinkElement::Map(data));
    }

    #[inline(always)]
    async fn append_text(&mut self, _serde_err: serde_json::Error, text: &str) {
        self.0.push(LogSinkElement::Line(text.to_owned()));
    }
}

async fn read_log_pipe<LA: LogAppender>(
    reader_fd: std::os::unix::io::OwnedFd,
    mut sink: impl DerefMut<Target = LA>,
) -> std::io::Result<()> {
    let file = AsyncCustomFD::new(reader_fd)?;

    let reader = tokio::io::BufReader::new(file);

    let mut lines = reader.lines();

    while let Some(line) = lines.next_line().await? {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str(line) {
            Ok::<serde_json::Map<String, serde_json::Value>, _>(mut log_record) => {
                let level = log_record
                    .remove("level")
                    .map(|x| {
                        if let serde_json::Value::String(s) = x {
                            Some(s)
                        } else {
                            None
                        }
                    })
                    .unwrap_or(None)
                    .and_then(|x| logger::Level::from_str(&x).ok())
                    .unwrap_or(logger::Level::Info);

                sink.append_structured(level, log_record).await;
            }
            Err(e) => {
                sink.append_text(e, line).await;
            }
        }
    }

    Ok(())
}

/// Maximum bytes of stdout/stderr kept (as a tail) per execution when not in
/// debug mode, to bound manager memory.
const OUTPUT_TAIL_LIMIT: usize = 4 * 1024 * 1024;

/// Keeps only the last `limit` bytes of `s`, trimming whole UTF-8 chars from the
/// front (so the most recent output survives).
fn keep_tail(s: &mut String, limit: usize) {
    if s.len() <= limit {
        return;
    }
    let mut cut = s.len() - limit;
    while cut < s.len() && !s.is_char_boundary(cut) {
        cut += 1;
    }
    s.drain(..cut);
}

async fn pipe_read<P: tokio::io::AsyncReadExt + Unpin>(
    mut reader: P,
    to: sync::DArc<tokio::sync::OnceCell<std::string::String>>,
    permit: tokio::sync::OwnedSemaphorePermit,
    max_bytes: Option<usize>,
) {
    let mut result = String::new();
    let mut buf = vec![0u8; 8192];

    loop {
        let n = reader.read(&mut buf).await;

        log_trace!(n:? = n; "read from genvm stdout/stderr");

        let n = match n {
            Ok(0) => break,
            Ok(n) => n,
            Err(_) => break,
        };

        match std::str::from_utf8(&buf[..n]) {
            Ok(s) => {
                log_trace!(data = s; "read from genvm stdout/stderr");
                result.push_str(s)
            }
            Err(_) => {
                let s = String::from_utf8_lossy(&buf[..n]);
                log_trace!(data = s; "read from genvm stdout/stderr");
                result.push_str(&s);
            }
        }

        // Bound memory while reading: keep only the tail when not in debug mode.
        if let Some(limit) = max_bytes {
            if result.len() > limit {
                keep_tail(&mut result, limit);
            }
        }
    }

    if let Err(e) = to.set(result) {
        log_error!(error:err = e; "failed to set stdout/stderr content");
    }

    std::mem::drop(permit);
}

#[derive(Default)]
struct ManagerHostStreamState {
    closed: AtomicBool,
    closed_notify: tokio::sync::Notify,
}

impl ManagerHostStreamState {
    fn close(&self) {
        if !self.closed.swap(true, Ordering::SeqCst) {
            self.closed_notify.notify_waiters();
        }
    }

    async fn wait_closed(&self) {
        loop {
            let notified = self.closed_notify.notified();
            if self.closed.load(Ordering::SeqCst) {
                return;
            }
            notified.await;
        }
    }
}

struct NestedRunRegistration(sync::DArc<SingleGenVMContext>);

impl NestedRunRegistration {
    fn new(exec_ctx: sync::DArc<SingleGenVMContext>) -> Self {
        exec_ctx.nested_run_started();
        Self(exec_ctx)
    }
}

impl Drop for NestedRunRegistration {
    fn drop(&mut self) {
        self.0.nested_run_finished();
    }
}

async fn read_length_prefixed<R: tokio::io::AsyncRead + Unpin>(
    reader: &mut R,
    max_len: usize,
) -> anyhow::Result<Vec<u8>> {
    use tokio::io::AsyncReadExt;

    let len = reader.read_u32_le().await? as usize;
    anyhow::ensure!(
        len <= max_len,
        "manager host frame is too large: {len} > {max_len}"
    );
    let mut data = vec![0; len];
    reader.read_exact(&mut data).await?;
    Ok(data)
}

async fn write_length_prefixed<W: tokio::io::AsyncWrite + Unpin>(
    writer: &mut W,
    data: &[u8],
) -> anyhow::Result<()> {
    use tokio::io::AsyncWriteExt;

    let len = u32::try_from(data.len()).map_err(|_| anyhow::anyhow!("reply is too large"))?;
    writer.write_u32_le(len).await?;
    writer.write_all(data).await?;
    writer.flush().await?;
    Ok(())
}

/// A nested run gets `no_modules: true`, no message-fee allocation and a state
/// the manager did not derive, so these permissions are unserviceable across the
/// boundary by construction. Refusing an envelope that asserts one is the only
/// enforcement available against a line the manager did not derive: granting it
/// would hand out authority nothing downstream can honour.
fn check_nested_permissions(
    permissions: genvm_modules_interfaces::NestedPermissions,
) -> anyhow::Result<()> {
    use genvm_modules_interfaces::NestedPermissions as P;

    for (bit, name) in [
        (P::SPAWN_NONDET, "spawn_nondet"),
        (P::WRITE_STORAGE, "write_storage"),
        (P::SEND_MESSAGES, "send_messages"),
        (
            P::USE_BALANCE_FOR_MESSAGE_FEES,
            "use_balance_for_message_fees",
        ),
    ] {
        anyhow::ensure!(
            !permissions.contains(bit),
            "nested run envelope asserts `{name}`, which a call crossing a major boundary never carries"
        );
    }

    Ok(())
}

fn nested_internal_error() -> genvm_modules_interfaces::NestedRunReply {
    genvm_modules_interfaces::NestedRunReply {
        result: genvm_modules_interfaces::NestedRunResult {
            kind: genvm_modules_interfaces::ResultCode::InternalError,
            data: calldata::Value::Str("cross-major nested run failed".to_owned()).into(),
        },
        small_hash: bytes::Bytes::new(),
        effect_free: false,
    }
}

fn result_code_from_byte(
    value: u8,
    source: &str,
) -> anyhow::Result<genvm_modules_interfaces::ResultCode> {
    match value {
        0 => Ok(genvm_modules_interfaces::ResultCode::Return),
        1 => Ok(genvm_modules_interfaces::ResultCode::UserError),
        2 => Ok(genvm_modules_interfaces::ResultCode::VmError),
        3 => Ok(genvm_modules_interfaces::ResultCode::InternalError),
        4 => Ok(genvm_modules_interfaces::ResultCode::FatalVmError),
        value => anyhow::bail!("{source} returned unknown result code {value}"),
    }
}

fn decode_reported_result(
    data: &[u8],
    source: &str,
) -> anyhow::Result<(
    genvm_modules_interfaces::ResultCode,
    genvm_modules_interfaces::ReportedResult,
)> {
    let (&kind, encoded) = data
        .split_first()
        .ok_or_else(|| anyhow::anyhow!("{source} returned an empty result"))?;
    let kind = result_code_from_byte(kind, source)?;
    let reported: genvm_modules_interfaces::ReportedResult = calldata::decode_obj(encoded)?;
    // The framing byte and reported map must name the same committed result
    anyhow::ensure!(
        kind == reported.kind,
        "{source} result code {kind:?} disagrees with the reported {:?}",
        reported.kind
    );

    Ok((kind, reported))
}

fn downgrade_fatal_reported_result(
    mut reported: genvm_modules_interfaces::ReportedResult,
) -> Vec<u8> {
    debug_assert_eq!(
        reported.kind,
        genvm_modules_interfaces::ResultCode::FatalVmError
    );
    reported.kind = genvm_modules_interfaces::ResultCode::VmError;
    let mut normalized = vec![genvm_modules_interfaces::ResultCode::VmError as u8];
    normalized.extend(calldata::encode_obj(&reported));
    normalized
}

fn guard_top_level_consumed_result(data: Vec<u8>, genvm_id: GenVMId) -> anyhow::Result<Vec<u8>> {
    let (kind, reported) = decode_reported_result(&data, "top-level executor")?;
    if kind != genvm_modules_interfaces::ResultCode::InternalError {
        anyhow::ensure!(
            reported.execution_hash.len() == 32,
            "top-level executor returned an invalid execution hash length"
        );
        anyhow::ensure!(
            reported.small_hash.len() == 32,
            "top-level executor returned an invalid small hash length"
        );
    }
    if kind != genvm_modules_interfaces::ResultCode::FatalVmError {
        return Ok(data);
    }

    debug_assert_ne!(
        kind,
        genvm_modules_interfaces::ResultCode::FatalVmError,
        "top-level executor returned a fatal VM error after its publication boundary"
    );
    log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0; "top-level executor returned a fatal VM error; downgrading it to vm_error");
    Ok(downgrade_fatal_reported_result(reported))
}

fn nested_reply_from_consumed_result(
    data: &[u8],
) -> anyhow::Result<genvm_modules_interfaces::NestedRunReply> {
    let (kind, reported) = decode_reported_result(data, "nested executor")?;
    if kind != genvm_modules_interfaces::ResultCode::InternalError {
        anyhow::ensure!(
            reported.small_hash.len() == 32,
            "nested executor returned an invalid small hash length"
        );
    }
    // Every effect a nested run could report is gated behind a permission the
    // boundary derivation clears, so a non-empty one means the callee did
    // something its permissions forbade. Refuse the reply instead of passing it
    // up flagged: the manager is the only party that can enforce this against a
    // line it did not derive.
    if let Some(effect) = nested_effect(&reported) {
        anyhow::bail!("nested executor reported `{effect}`, which its permissions forbid");
    }

    Ok(genvm_modules_interfaces::NestedRunReply {
        result: genvm_modules_interfaces::NestedRunResult {
            kind,
            data: reported.data,
        },
        small_hash: reported.small_hash,
        effect_free: true,
    })
}

/// Names the first field of `reported` that proves the callee produced an effect
/// or consumed a shared budget.
///
/// The destructuring is the point: adding a field to `ReportedResult` stops
/// compiling until it is classified here, so a new effect cannot be dropped
/// silently the way the ignored ones below once were.
fn nested_effect(reported: &genvm_modules_interfaces::ReportedResult) -> Option<&'static str> {
    let genvm_modules_interfaces::ReportedResult {
        // The outcome and the hash accounting, not effects. The caller folds
        // `small_hash` into its own `det_subvm_hashes`; the execution hash stays
        // here for the node's own result comparison.
        execution_hash: _,
        small_hash: _,
        kind: _,
        data: _,
        backtrace: _,
        wasm_store_hashes: _,
        // A remaining budget is a report, not a consumption.
        data_fees_remaining: _,
        storage_changes,
        emissions,
        nondet_disagreement,
        nondet_results,
        data_fees_consumed:
            genvm_modules_interfaces::BucketsConsumed {
                storage,
                message_receipt,
                nondet_output,
                message_fee,
                event,
            },
        llm_consumption,
    } = reported;

    if !storage_changes.is_empty() {
        return Some("storage_changes");
    }
    if !emissions.is_empty() {
        return Some("emissions");
    }
    if nondet_disagreement.is_some() {
        return Some("nondet_disagreement");
    }
    if !nondet_results.is_empty() {
        return Some("nondet_results");
    }
    if !llm_consumption.is_zero() {
        return Some("llm_consumption");
    }

    for (bucket, name) in [
        (storage, "data_fees_consumed.storage"),
        (message_receipt, "data_fees_consumed.message_receipt"),
        (nondet_output, "data_fees_consumed.nondet_output"),
        (message_fee, "data_fees_consumed.message_fee"),
        (event, "data_fees_consumed.event"),
    ] {
        if !bucket.is_zero() {
            return Some(name);
        }
    }

    None
}

fn read_manager_host_stream(
    parent_fd: FdWrapper,
    full_ctx: sync::DArc<crate::manager::AppContext>,
    exec_ctx: sync::DArc<SingleGenVMContext>,
    parent_req: Arc<Request>,
    consumed_result: sync::DArc<tokio::sync::OnceCell<Vec<u8>>>,
    genvm_id: GenVMId,
    is_top_level: bool,
    stream_state: Arc<ManagerHostStreamState>,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>> {
    Box::pin(async move {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let file = match parent_fd.into_async_fd() {
            Ok(f) => f,
            Err(e) => {
                log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:err = e; "failed to create async fd for manager host stream");
                stream_state.close();
                return;
            }
        };
        let (mut reader, writer) = tokio::io::split(file);
        let writer = Arc::new(tokio::sync::Mutex::new(writer));
        let stream_state_on_drop = stream_state.clone();
        let _close_guard = sync::DropGuard::new(move || stream_state_on_drop.close());

        loop {
            let mut method_buf = [0u8; 1];
            match reader.read_exact(&mut method_buf).await {
                Ok(_) => {}
                Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0; "manager host stream closed");
                    return;
                }
                Err(e) => {
                    log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:err = e; "failed to read method from manager host stream");
                    return;
                }
            }

            match host_fns::Methods::try_from(method_buf[0]) {
                Ok(host_fns::Methods::ConsumeResult) => {
                    let data = match read_length_prefixed(&mut reader, u32::MAX as usize).await {
                        Ok(data) => data,
                        Err(e) => {
                            log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:ah = &e; "failed to read consumed result");
                            return;
                        }
                    };
                    let data = if is_top_level {
                        match guard_top_level_consumed_result(data, genvm_id) {
                            Ok(data) => data,
                            Err(e) => {
                                log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:ah = &e; "refusing invalid top-level consume_result");
                                return;
                            }
                        }
                    } else {
                        data
                    };
                    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0, len = data.len(); "manager received consume_result");
                    let _ = consumed_result.set(data);

                    let mut writer = writer.lock().await;
                    if let Err(e) = writer.write_all(&[0]).await {
                        log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:err = e; "failed to send ACK for consume_result");
                        return;
                    }
                    if let Err(e) = writer.flush().await {
                        log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:err = e; "failed to flush ACK for consume_result");
                        return;
                    }
                }
                Ok(host_fns::Methods::RunNested) => {
                    // Unbounded, like `consume_result` above: the peer is a child
                    // process this manager spawned, and the envelope's real bound
                    // is the memory limit that child runs under.
                    let data = match read_length_prefixed(&mut reader, u32::MAX as usize).await {
                        Ok(data) => data,
                        Err(e) => {
                            log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:ah = &e; "failed to read run_nested request");
                            return;
                        }
                    };
                    // Served inline, not spawned: replies carry no request id, so
                    // the only thing that pairs one with its request is arrival
                    // order on this stream. The caller is blocked inside its own
                    // call while this runs, so there is nothing to overlap with.
                    let _registration = NestedRunRegistration::new(exec_ctx.clone());
                    let reply = match calldata::decode_obj::<
                        genvm_modules_interfaces::NestedRunEnvelope,
                    >(&data)
                    {
                        Ok(envelope) => {
                            let run_ctx = full_ctx.gep(|ctx| &ctx.run_ctx);
                            match run_ctx
                                .start_nested(
                                    full_ctx.clone(),
                                    exec_ctx.clone(),
                                    parent_req.clone(),
                                    envelope,
                                    stream_state.clone(),
                                )
                                .await
                            {
                                Ok(reply) => reply,
                                Err(e) => {
                                    log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:ah = &e; "nested run failed");
                                    nested_internal_error()
                                }
                            }
                        }
                        Err(e) => {
                            log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:err = e; "failed to decode run_nested envelope");
                            nested_internal_error()
                        }
                    };
                    let encoded = calldata::encode_obj(&reply);
                    let mut writer = writer.lock().await;
                    if let Err(e) = write_length_prefixed(&mut *writer, &encoded).await {
                        log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:ah = &e; "failed to send run_nested reply");
                    }
                }
                Ok(host_fns::Methods::ResolveCallcontractExecutor) => {
                    // The do-nothing route: a request that did not opt into
                    // `hook_cross_contract_calls` gets this arm instead of the
                    // node's host, and a null reply means "stay in-process".
                    let mut request = [0u8; calldata::ADDRESS_SIZE + 2];
                    if let Err(e) = reader.read_exact(&mut request).await {
                        log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:err = e; "failed to read resolve_callcontract_executor request");
                        return;
                    }
                    let encoded = calldata::encode_obj(&calldata::Value::Null);
                    let mut writer = writer.lock().await;
                    if let Err(e) = writer.write_all(&[host_fns::Errors::Ok as u8]).await {
                        log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:err = e; "failed to send resolve_callcontract_executor status");
                        return;
                    }
                    if let Err(e) = write_length_prefixed(&mut *writer, &encoded).await {
                        log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, error:ah = &e; "failed to send resolve_callcontract_executor reply");
                        return;
                    }
                }
                Ok(method) => {
                    log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, method = method as u8; "unexpected method on manager host stream");
                    return;
                }
                Err(()) => {
                    log_error_into!(&LoggerWithId, genvm_id:id = genvm_id.0, method = method_buf[0]; "unknown method on manager host stream");
                    return;
                }
            }
        }
    })
}

async fn acquire_run_permits(
    permits: Arc<tokio::sync::Semaphore>,
    count: u32,
) -> anyhow::Result<tokio::sync::OwnedSemaphorePermit> {
    if count == 1 {
        Ok(permits.acquire_owned().await?)
    } else {
        Ok(permits.acquire_many_owned(count).await?)
    }
}

async fn wait_for_output(exec: &SingleGenVMContext) {
    let _ = exec.stdout_stderr_sem.acquire_many(2).await;
    log_debug!(id = exec.id; "stdout/stderr sem acquired");
}

fn drain_log_sink(log_sink: &LogSink) -> Vec<serde_json::Map<String, serde_json::Value>> {
    let mut as_vec = Vec::new();
    while let Some(data) = log_sink.pop() {
        as_vec.push(data.into_json());
    }
    as_vec
}

async fn finish_execution(
    exec: &SingleGenVMContext,
    status: Option<std::process::ExitStatus>,
    default_cause: FinishCause,
) -> bool {
    if exec.result.initialized() || exec.terminal_event.get().is_some() {
        return false;
    }

    // Bound to a name on purpose: `let _ =` would unregister the sink right
    // here, losing every line this function still logs before it drains it.
    let _by_id_logger_guard = sync::DropGuard::new(|| {
        GENVM_BY_ID_LOGGER.pin().remove(&exec.id);
    });

    let metrics = exec
        .execution_context
        .get()
        .map(|ctx| ctx.collect_metrics())
        .unwrap_or(serde_json::Value::Null);

    log_debug!(id = exec.id, metrics:serde = metrics; "metrics collected");

    exec.all_permits.store(None);
    wait_for_output(exec).await;

    let stdout = exec.stdout.get().map(|x| x.as_str()).unwrap_or("");
    let stderr = exec.stderr.get().map(|x| x.as_str()).unwrap_or("");
    let genvm_log = drain_log_sink(&exec.log_sink);
    let cause = exec.finish_cause().unwrap_or(default_cause);
    let exit_code = status.and_then(|status| status.code()).map(i64::from);

    let done = SingleGenVMContextDone {
        finished_at: chrono::Utc::now(),
        stdout: stdout.to_owned(),
        stderr: stderr.to_owned(),
        genvm_log,
        metrics,
        consumed_result: exec.consumed_result.get().cloned(),
        version_major: exec.version_major.load(Ordering::SeqCst),
        version_minor: exec.version_minor.load(Ordering::SeqCst),
        cause,
        exit_code,
    };
    let event = exec.finished_event(&done);
    if let Err(e) = exec.result.set(done) {
        log_warn!(error:err = e; "error setting genvm result; it can happen rarely due to concurrency");
        return false;
    }
    exec.publish_terminal(event)
}

fn fail_to_start(exec: &SingleGenVMContext, error: anyhow::Error) {
    exec.all_permits.store(None);
    let event = Event::FailedToStart {
        genvm_id: exec.id,
        host_genvm_id: exec.host_genvm_id.clone(),
        error: format!("{:#}", error),
    };
    let _ = exec.publish_terminal(event);
}

/// Creates a pipe for GenVM logging. Returns `(read_fd, write_fd)` with
/// the read end set to nonblocking + cloexec.
fn create_log_pipe() -> anyhow::Result<(std::os::unix::io::OwnedFd, std::os::unix::io::OwnedFd)> {
    let mut read_write_fd = [0; 2];
    let result_code = unsafe { libc::pipe(std::ptr::from_mut(&mut read_write_fd).cast()) };
    if result_code != 0 {
        anyhow::bail!("failed to create pipe for genvm logging: {result_code}");
    }

    let read_fd = unsafe { FdWrapper::from_raw_fd(read_write_fd[0]) };
    let write_fd = unsafe { std::os::unix::io::OwnedFd::from_raw_fd(read_write_fd[1]) };

    read_fd.set_nonblocking(true)?;
    read_fd.set_cloexec(true)?;
    Ok((read_fd.into_inner(), write_fd))
}

/// Builds the `tokio::process::Command` for GenVM with all standard arguments
/// and stdio configuration.
fn build_genvm_command(
    command_path: std::path::PathBuf,
    req: &Request,
    genvm_id: GenVMId,
    log_write_fd: &std::os::unix::io::OwnedFd,
) -> tokio::process::Command {
    let mut proc = tokio::process::Command::new(command_path);

    proc.stdin(std::process::Stdio::piped());
    proc.arg(format!("--log-fd={}", log_write_fd.as_raw_fd()));

    proc.arg("run");
    proc.args(&req.extra_args);

    if req.is_sync {
        proc.arg("--sync");
    }

    proc.arg("--host");
    proc.arg(&req.host);

    proc.arg("--permissions");
    proc.arg(&req.permissions);

    proc.arg(format!("--genvm-id={}", genvm_id.0));

    proc.arg("--debug-mode");
    proc.arg(req.debug_mode.as_arg());

    if req.debug_mode.capture() == genvm_common::debug_mode::Capture::Disabled {
        proc.stdout(std::process::Stdio::null());
        proc.stderr(std::process::Stdio::null());
    } else {
        proc.stdout(std::process::Stdio::piped());
        proc.stderr(std::process::Stdio::piped());
    }

    proc
}

/// Creates socketpairs for module communication, adds `--module-llm`/`--module-web`
/// args to the command, and spawns relay tasks. Returns the child-side FDs that must
/// be kept alive until after the child process is spawned.
fn setup_module_relays(
    proc: &mut tokio::process::Command,
    handlers: (super::modules::StreamHandler, super::modules::StreamHandler),
    execution_context: &sync::DArc<super::execution_context::ExecutionContext>,
    genvm_id: GenVMId,
) -> anyhow::Result<(FdWrapper, FdWrapper)> {
    let (llm_handler, web_handler) = handlers;
    let exec_ctx = execution_context.clone();

    let (llm_parent, llm_child) = FdWrapper::socketpair()?;
    let (web_parent, web_child) = FdWrapper::socketpair()?;

    // Parent end: set cloexec (closed in child after exec)
    llm_parent.set_cloexec(true)?;
    web_parent.set_cloexec(true)?;
    // Child end: clear cloexec (survives exec into child process)
    llm_child.set_cloexec(false)?;
    web_child.set_cloexec(false)?;

    proc.arg(format!("--module-llm=fd://{}", llm_child.as_raw_fd()));
    proc.arg(format!("--module-web=fd://{}", web_child.as_raw_fd()));

    tokio::spawn(spawn_module_relay(
        llm_parent,
        llm_handler,
        genvm_id,
        "llm",
        exec_ctx.clone(),
    ));
    tokio::spawn(spawn_module_relay(
        web_parent,
        web_handler,
        genvm_id,
        "web",
        exec_ctx,
    ));

    Ok((llm_child, web_child))
}

/// Spawns an async task that writes execution data to the child's stdin.
fn spawn_stdin_writer(
    stdin: tokio::process::ChildStdin,
    execution_data_bytes: bytes::Bytes,
) -> tokio::task::JoinHandle<std::io::Result<()>> {
    tokio::task::spawn(async move {
        use tokio::io::AsyncWriteExt;

        let owned_fd = stdin.into_owned_fd().map_err(|e| {
            std::io::Error::other(format!("failed to get owned fd from stdin: {e}"))
        })?;
        set_fd_nonblocking(owned_fd.as_raw_fd())?;

        let mut file = AsyncCustomFD::new(owned_fd)?;
        file.write_all(&execution_data_bytes).await?;
        Ok(())
    })
}

fn strict_deadline_from_request(req: &Request) -> chrono::DateTime<chrono::Utc> {
    let max = std::time::Duration::from_secs(24 * 60 * 60);
    let duration = req
        .deadline
        .map(ManagerDuration::to_std)
        .unwrap_or_else(|| {
            std::time::Duration::from_secs(req.max_execution_minutes.min(24 * 60) * 60)
        })
        .min(max);
    chrono::Utc::now()
        + chrono::Duration::from_std(duration).unwrap_or_else(|_| chrono::Duration::hours(24))
}

fn execution_data_from_request(req: &Request) -> genvm_modules_interfaces::ExecutionData {
    let mut method_hosts = vec![0; host_fns::Methods::SIZE];
    method_hosts[host_fns::Methods::ConsumeResult as usize] = 1;
    method_hosts[host_fns::Methods::RunNested as usize] = 1;
    if !req.hook_cross_contract_calls {
        method_hosts[host_fns::Methods::ResolveCallcontractExecutor as usize] = 1;
    }

    genvm_modules_interfaces::ExecutionData {
        calldata: req.calldata.clone(),
        message: req.message.clone(),
        host_data: req.host_data.clone(),
        code: req.code.clone(),
        leader_nondet_results: req.leader_nondet_results.clone(),
        host_hello_data: req.host_hello_data.clone(),
        method_hosts,
        bucket_totals: req.bucket_totals.clone(),
        gas_data: req.gas_data.clone(),
        message_fee_allocation: req.message_fee_allocation.clone(),
        initial_time_units_allocation: req.initial_time_units_allocation,
        record_actions: req.record_actions.clone(),
        remaining_recursion: if req.debug_mode >= genvm_common::DebugMode::Unsafe {
            req.unsafe_overrides.initial_recursion
        } else {
            None
        },
        nested: None,
    }
}

enum RunResources {
    TopLevel {
        permits: tokio::sync::OwnedSemaphorePermit,
        modules_lock: Box<dyn std::any::Any + Send + Sync>,
    },
    Nested {
        caller_stream: Arc<ManagerHostStreamState>,
    },
}

impl RunResources {
    fn is_top_level(&self) -> bool {
        matches!(self, Self::TopLevel { .. })
    }

    fn caller_stream(&self) -> Option<Arc<ManagerHostStreamState>> {
        match self {
            Self::TopLevel { .. } => None,
            Self::Nested { caller_stream, .. } => Some(caller_stream.clone()),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProcessStop {
    Cancelled,
    Deadline,
    CallerClosed,
}

enum Reservation {
    Existing(GenVMId),
    Reserved(sync::DArc<SingleGenVMContext>),
}

async fn wait_for_process_stop(
    exec_ctx: &SingleGenVMContext,
    caller_stream: Option<&ManagerHostStreamState>,
    deadline_duration: std::time::Duration,
) -> ProcessStop {
    tokio::select! {
        _ = exec_ctx.wait_cancelled() => ProcessStop::Cancelled,
        _ = tokio::time::sleep(deadline_duration) => ProcessStop::Deadline,
        _ = async {
            match caller_stream {
                Some(stream) => stream.wait_closed().await,
                None => std::future::pending().await,
            }
        } => ProcessStop::CallerClosed,
    }
}

impl Ctx {
    fn reserve_execution(
        &self,
        host_genvm_id: Option<&str>,
        build: impl FnOnce(GenVMId) -> sync::DArc<SingleGenVMContext>,
    ) -> Reservation {
        // The guard is held across the lookup, the id allocation and both
        // inserts, so two starts sharing a token cannot both reserve. Keeping
        // this section free of await points is what makes that safe: a papaya
        // pin is an epoch guard rather than a lock, so taking one under the
        // mutex cannot block on `ack` or `gc_step` taking them the other way.
        let mut host_ids = self.lock_host_ids();

        if let Some(host_genvm_id) = host_genvm_id {
            if let Some(genvm_id) = host_ids.get(host_genvm_id).copied() {
                if self.known_executions.pin().get(&genvm_id).is_some() {
                    return Reservation::Existing(genvm_id);
                }
                host_ids.remove(host_genvm_id);
            }
        }

        let genvm_id = GenVMId(
            self.next_genvm_id
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst),
        );
        let exec_ctx = build(genvm_id);
        self.known_executions
            .pin()
            .insert(genvm_id, exec_ctx.clone());
        if let Some(host_genvm_id) = host_genvm_id {
            host_ids.insert(host_genvm_id.to_owned(), genvm_id);
        }

        Reservation::Reserved(exec_ctx)
    }

    pub async fn start(
        &self,
        full_ctx: sync::DArc<crate::manager::AppContext>,
        req: Request,
        modules_lock: Box<dyn std::any::Any + Send + Sync>,
    ) -> anyhow::Result<GenVMId> {
        let reservation = self.reserve_execution(req.host_genvm_id.as_deref(), |genvm_id| {
            let events = tokio::sync::watch::Sender::new(Snapshot::Queued {
                genvm_id,
                host_genvm_id: req.host_genvm_id.clone(),
            });
            sync::DArc::new(SingleGenVMContext {
                result: tokio::sync::OnceCell::new(),
                started_event: tokio::sync::OnceCell::new(),
                terminal_event: tokio::sync::OnceCell::new(),
                terminal_at: tokio::sync::OnceCell::new(),
                events,
                version: std::sync::RwLock::new(String::new()),
                version_major: AtomicU16::new(0),
                version_minor: AtomicU16::new(0),
                id: genvm_id,
                host_genvm_id: req.host_genvm_id.clone(),
                process_handle: tokio::sync::Mutex::new(None),
                started_at: chrono::Utc::now(),
                strict_deadline: strict_deadline_from_request(&req),

                stdout_stderr_sem: Arc::new(tokio::sync::Semaphore::new(2)),
                stdout: tokio::sync::OnceCell::new(),
                stderr: tokio::sync::OnceCell::new(),
                log_sink: Arc::new(LogSinkInner::new(
                    req.debug_mode.capture() == genvm_common::debug_mode::Capture::Unbounded,
                )),
                consumed_result: tokio::sync::OnceCell::new(),

                cancel_requested: AtomicBool::new(false),
                cancel_notify: tokio::sync::Notify::new(),
                finish_cause: std::sync::Mutex::new(None),
                all_permits: crossbeam::atomic::AtomicCell::new(None),
                nested_runs: AtomicUsize::new(0),
                nested_runs_done: tokio::sync::Notify::new(),
                execution_context: tokio::sync::OnceCell::new(),
            })
        });

        let exec_ctx = match reservation {
            Reservation::Existing(genvm_id) => return Ok(genvm_id),
            Reservation::Reserved(exec_ctx) => exec_ctx,
        };
        let genvm_id = exec_ctx.id;

        tokio::spawn(supervise_genvm(full_ctx, exec_ctx, req, modules_lock));

        Ok(genvm_id)
    }

    async fn start_nested(
        &self,
        full_ctx: sync::DArc<crate::manager::AppContext>,
        exec_ctx: sync::DArc<SingleGenVMContext>,
        parent_req: Arc<Request>,
        envelope: genvm_modules_interfaces::NestedRunEnvelope,
        caller_stream: Arc<ManagerHostStreamState>,
    ) -> anyhow::Result<genvm_modules_interfaces::NestedRunReply> {
        anyhow::ensure!(
            !envelope.message.is_init,
            "nested CallContract message cannot be an init"
        );
        anyhow::ensure!(
            envelope.message.value == num_bigint::BigInt::from(0),
            "nested CallContract message value must be zero"
        );
        if parent_req.host.trim_start().starts_with("fd://") {
            log_error_into!(&LoggerWithId, genvm_id:id = exec_ctx.id.0, host = parent_req.host; "cannot start nested executor because host 0 is not addressable");
            anyhow::bail!("host 0 uses fd:// and cannot be re-dialed by a nested executor");
        }

        let routing: genvm_modules_interfaces::ExecutorSelector =
            calldata::decode_obj(&envelope.routing_payload)?;

        let genvm_id = GenVMId(
            self.next_genvm_id
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst),
        );
        let mut host_hello_data = parent_req.host_hello_data.clone();
        if host_hello_data.len() < 2 {
            host_hello_data.resize(2, bytes::Bytes::new());
        }
        host_hello_data[1] = bytes::Bytes::new();

        check_nested_permissions(envelope.permissions)?;
        let permissions = if envelope
            .permissions
            .contains(genvm_modules_interfaces::NestedPermissions::CALL_OTHERS)
        {
            "c".to_owned()
        } else {
            String::new()
        };

        let req = Request {
            selector: routing,
            message: envelope.message.clone(),
            is_sync: true,
            debug_mode: genvm_common::DebugMode::Disabled,
            max_execution_minutes: parent_req.max_execution_minutes,
            bucket_totals: Vec::new(),
            host_data: parent_req.host_data.clone(),
            timestamp: envelope.message.datetime,
            host: parent_req.host.clone(),
            extra_args: Vec::new(),
            calldata: envelope.calldata.clone(),
            code: None,
            permissions,
            no_modules: true,
            unsafe_overrides: Default::default(),
            leader_nondet_results: None,
            gas_data: parent_req.gas_data.clone(),
            message_fee_allocation: Vec::new(),
            initial_time_units_allocation: 0,
            record_actions: Vec::new(),
            host_genvm_id: None,
            deadline: None,
            host_hello_data,
            // A child that may itself call across a boundary needs the same
            // routing as its parent.
            hook_cross_contract_calls: parent_req.hook_cross_contract_calls,
        };
        let mut execution_data = execution_data_from_request(&req);
        execution_data.code = None;
        execution_data.leader_nondet_results = None;
        execution_data.record_actions.clear();
        execution_data.message_fee_allocation.clear();
        execution_data.remaining_recursion =
            Some(cross_major_recursion(envelope.remaining_recursion));
        execution_data.nested = Some(genvm_modules_interfaces::NestedExecutionData {
            memory_limit: envelope.memory_limit,
            stack: envelope.stack,
            permissions: envelope.permissions,
            state_mode: envelope.state_mode,
            topmost_runner_id: envelope.topmost_runner_id,
            remaining_det_fuel: envelope.remaining_det_fuel,
        });

        let consumed_result = sync::DArc::new(tokio::sync::OnceCell::new());
        let status = run_genvm_process(
            full_ctx,
            exec_ctx,
            req,
            execution_data,
            genvm_id,
            consumed_result.clone(),
            RunResources::Nested { caller_stream },
        )
        .await?;
        anyhow::ensure!(
            status.success(),
            "nested executor exited unsuccessfully: {status}"
        );
        let result = consumed_result
            .get()
            .ok_or_else(|| anyhow::anyhow!("nested executor exited without consume_result"))?;
        nested_reply_from_consumed_result(result)
    }
}

async fn supervise_genvm(
    full_ctx: sync::DArc<crate::manager::AppContext>,
    exec_ctx: sync::DArc<SingleGenVMContext>,
    req: Request,
    modules_lock: Box<dyn std::any::Any + Send + Sync>,
) {
    let ctx = full_ctx.gep(|x| &x.run_ctx);
    let permit_count = ctx.permits_for(&req);

    let permit_future = acquire_run_permits(ctx.permits.clone(), permit_count);
    tokio::pin!(permit_future);
    let permits = tokio::select! {
        _ = exec_ctx.wait_cancelled() => {
            let cause = exec_ctx.finish_cause().unwrap_or(FinishCause::Cancelled);
            let _ = finish_execution(&exec_ctx, None, cause).await;
            return;
        }
        permits = &mut permit_future => match permits {
            Ok(permits) => permits,
            Err(e) => {
                fail_to_start(&exec_ctx, e);
                return;
            }
        },
    };

    if exec_ctx.cancel_requested.load(Ordering::SeqCst) {
        let cause = exec_ctx.finish_cause().unwrap_or(FinishCause::Cancelled);
        drop(permits);
        let _ = finish_execution(&exec_ctx, None, cause).await;
        return;
    }

    if let Err(e) =
        supervise_genvm_inner(full_ctx, exec_ctx.clone(), req, modules_lock, permits).await
    {
        fail_to_start(&exec_ctx, e);
    }
}

async fn supervise_genvm_inner(
    full_ctx: sync::DArc<crate::manager::AppContext>,
    exec_ctx: sync::DArc<SingleGenVMContext>,
    req: Request,
    modules_lock: Box<dyn std::any::Any + Send + Sync>,
    permits: tokio::sync::OwnedSemaphorePermit,
) -> anyhow::Result<()> {
    let execution_data = execution_data_from_request(&req);
    let result = run_genvm_process(
        full_ctx,
        exec_ctx.clone(),
        req,
        execution_data,
        exec_ctx.id,
        exec_ctx.gep(|ctx| &ctx.consumed_result),
        RunResources::TopLevel {
            permits,
            modules_lock,
        },
    )
    .await;
    exec_ctx.wait_for_nested_runs().await;
    let status = result?;

    let default_cause = if exec_ctx.cancel_requested.load(Ordering::SeqCst) {
        exec_ctx.finish_cause().unwrap_or(FinishCause::Cancelled)
    } else {
        FinishCause::Exited
    };
    let _ = finish_execution(&exec_ctx, Some(status), default_cause).await;
    Ok(())
}

/// The executor line a selector names.
///
/// A major no line provides is contract input, not a node failure, so it falls
/// back to the newest line and lets that line's own check answer with
/// `invalid_contract major_mismatch`. A version is a direct statement by a
/// trusted party and gets no such benefit of the doubt: no match fails the run.
async fn resolve_selector(
    ver_ctx: &crate::manager::versioning::Ctx,
    selector: &genvm_modules_interfaces::ExecutorSelector,
    timestamp: chrono::DateTime<chrono::Utc>,
    genvm_id: GenVMId,
) -> anyhow::Result<crate::manager::versioning::ResolvedVersion> {
    use genvm_modules_interfaces::{ExecutorSelector, VersionMatch};

    let major = match selector {
        ExecutorSelector::VersionOverride { version } => {
            return match genvm_modules_interfaces::parse_version_match(version) {
                VersionMatch::Exact(version) => {
                    Ok(crate::manager::versioning::exact_version(version))
                }
                VersionMatch::Regex(pattern) => {
                    let pattern = regex::Regex::new(pattern).with_context(|| {
                        format!("version selector `{pattern}` is not a valid regex")
                    })?;

                    ver_ctx
                        .get_matching_version(&pattern, timestamp)
                        .await
                        .ok_or_else(|| {
                            anyhow::anyhow!("no line matches version selector `{pattern}`")
                        })
                }
            };
        }
        ExecutorSelector::MajorOverride { major } => *major,
    };

    if let Some(version) = ver_ctx.get_version(major, timestamp).await {
        return Ok(version);
    }

    log_warn!(
        major = major,
        genvm_id:id = genvm_id.0;
        "no line provides the requested major, falling back to the newest one"
    );

    ver_ctx.get_newest_version(timestamp).await.ok_or_else(|| {
        anyhow::anyhow!(
            "no compatible version found for major {major} and no line is available at all"
        )
    })
}

async fn run_genvm_process(
    full_ctx: sync::DArc<crate::manager::AppContext>,
    exec_ctx: sync::DArc<SingleGenVMContext>,
    req: Request,
    execution_data: genvm_modules_interfaces::ExecutionData,
    genvm_id: GenVMId,
    consumed_result: sync::DArc<tokio::sync::OnceCell<Vec<u8>>>,
    resources: RunResources,
) -> anyhow::Result<std::process::ExitStatus> {
    let is_top_level = resources.is_top_level();
    let caller_stream = resources.caller_stream();
    let module_handlers = if req.needs_modules() {
        let (llm, web) = full_ctx
            .mod_ctx
            .get_handlers()
            .await
            .ok_or_else(|| anyhow::anyhow!("modules are required but not all are running"))?;
        Some((llm, web))
    } else {
        None
    };

    let execution_context = if req.needs_modules() {
        let host_data: genvm_modules_interfaces::HostData = serde_json::from_str(&req.host_data)?;
        let role = if req.leader_nondet_results.is_none() {
            genvm_modules_interfaces::Role::Leader
        } else {
            genvm_modules_interfaces::Role::Validator
        };
        let hello = Arc::new(genvm_modules_interfaces::GenVMHello {
            genvm_id,
            role,
            host_data,
            gas_data: req.gas_data.clone(),
            initial_time_units_allocation: req.initial_time_units_allocation,
        });
        let ctx = full_ctx.mod_ctx.create_execution_context(hello).await?;
        let _ = exec_ctx.execution_context.set(ctx.clone());
        Some(ctx)
    } else {
        None
    };

    // A debug reroute replaces the request's own selector, but only from `safe`
    // up -- production (`disabled`) always resolves what the request states, so
    // consensus can't be steered to a different binary.
    let selector = match &req.unsafe_overrides.reroute_to {
        reroute if !reroute.is_empty() && req.debug_mode >= genvm_common::DebugMode::Safe => {
            genvm_modules_interfaces::ExecutorSelector::VersionOverride {
                version: reroute.clone(),
            }
        }
        _ => req.selector.clone(),
    };
    let version = resolve_selector(&full_ctx.ver_ctx, &selector, req.timestamp, genvm_id).await?;
    let ctx = full_ctx.clone().into_gep(|x| &x.run_ctx);

    // Capture controls how logs and stdout/stderr are kept: disabled (forwarded
    // to the manager log only), bounded, or unbounded.
    use genvm_common::debug_mode::Capture;
    let capture = req.debug_mode.capture();
    let log_sink = exec_ctx.log_sink.clone();
    // Only route per-id logs into the result sink when we actually capture; under
    // `disabled` the sink stays unregistered so manager-internal logs (and the
    // forwarded executor logs) go to the manager log, leaving `genvm_log` empty.
    if capture != Capture::Disabled {
        GENVM_BY_ID_LOGGER.pin().insert(genvm_id, log_sink.clone());
    }
    let log_sink_guard = sync::DropGuard::new(|| {
        GENVM_BY_ID_LOGGER.pin().remove(&genvm_id);
    });

    let mut command_path = ctx.executors_path.clone();
    let version_str: &str = &version.orig_key;
    if is_top_level {
        exec_ctx.set_version(
            version_str.to_owned(),
            version.version.major as u16,
            version.version.minor as u16,
        );
    }
    command_path.push(version_str);
    command_path.push("bin");
    command_path.push("genvm");

    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0, exe:? = command_path, version:? = version; "genvm path");

    // Create log pipe and build command
    let (read_fd, write_fd) = create_log_pipe()?;
    let mut proc = build_genvm_command(command_path, &req, genvm_id, &write_fd);

    // Setup manager host socketpair (host id=1 for consume_result, run_nested
    // and -- unless the request hooks them -- resolve_callcontract_executor)
    let (manager_parent, manager_child) = FdWrapper::socketpair()?;
    manager_parent.set_cloexec(true)?;
    manager_child.set_cloexec(false)?;
    proc.arg("--host");
    proc.arg(format!("fd://{}", manager_child.as_raw_fd()));

    // Setup module relays if needed
    let module_child_fds = if let Some(handlers) = module_handlers {
        let exec_ctx = execution_context
            .as_ref()
            .expect("execution context required when modules are running");
        Some(setup_module_relays(
            &mut proc, handlers, exec_ctx, genvm_id,
        )?)
    } else {
        None
    };

    if req
        .host_hello_data
        .get(1)
        .is_some_and(|data| !data.is_empty())
    {
        anyhow::bail!("host_hello_data for manager-owned host index 1 is not allowed");
    }

    let execution_data_bytes = bytes::Bytes::from(calldata::encode_obj(&execution_data));

    // Spawn log reader: capture into the sink, or (when capture is disabled)
    // forward to the manager log instead of buffering into the result.
    if capture == Capture::Disabled {
        tokio::spawn(read_log_pipe(read_fd, LogAppenderToLog(genvm_id)));
    } else {
        let logger = Arc::new(tokio::sync::Mutex::new(LogAppenderToValue(
            log_sink.clone(),
        )));
        let l = logger.clone().lock_owned().await;
        tokio::spawn(read_log_pipe(read_fd, l));
    }

    // Spawn child process, then drop child-side FDs
    let mut child = proc.spawn()?;
    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0, pid:? = child.id(); "genvm process started");
    std::mem::drop(module_child_fds);
    std::mem::drop(manager_child);

    let stdin_task = child
        .stdin
        .take()
        .map(|stdin| spawn_stdin_writer(stdin, execution_data_bytes));

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let stdout_perm = if stdout.is_some() {
        Some(exec_ctx.stdout_stderr_sem.clone().acquire_owned().await?)
    } else {
        None
    };
    let stderr_perm = if stderr.is_some() {
        Some(exec_ctx.stdout_stderr_sem.clone().acquire_owned().await?)
    } else {
        None
    };

    let manager_stream_state = Arc::new(ManagerHostStreamState::default());
    let manager_stream_state_on_drop = manager_stream_state.clone();
    let _manager_stream_guard = sync::DropGuard::new(move || manager_stream_state_on_drop.close());
    tokio::spawn(read_manager_host_stream(
        manager_parent,
        full_ctx.clone(),
        exec_ctx.clone(),
        Arc::new(req.clone()),
        consumed_result,
        genvm_id,
        is_top_level,
        manager_stream_state.clone(),
    ));

    if let Some(mut stdin_task) = stdin_task {
        let deadline_duration = exec_ctx
            .strict_deadline
            .signed_duration_since(chrono::Utc::now())
            .to_std()
            .unwrap_or_default();
        let stop = wait_for_process_stop(&exec_ctx, caller_stream.as_deref(), deadline_duration);
        tokio::pin!(stop);
        tokio::select! {
            result = &mut stdin_task => match result {
                Ok(Ok(())) => {
                    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0; "execution data written");
                }
                Ok(Err(e)) => {
                    let _ = child.start_kill();
                    let _ = child.wait().await;
                    anyhow::bail!("failed to write execution data to child stdin: {e}");
                }
                Err(e) => {
                    let _ = child.start_kill();
                    let _ = child.wait().await;
                    anyhow::bail!("stdin write task panicked: {e}");
                }
            },
            status = child.wait() => {
                stdin_task.abort();
                return match status {
                    Ok(status) => Ok(status),
                    Err(e) => {
                        let _ = child.start_kill();
                        let _ = child.wait().await;
                        Err(e.into())
                    }
                };
            }
            reason = &mut stop => {
                if reason == ProcessStop::Deadline {
                    exec_ctx.request_finish(FinishCause::Deadline);
                }
                stdin_task.abort();
                let _ = child.start_kill();
                return child.wait().await.map_err(Into::into);
            }
        }
    }

    let mut child = Some(child);
    if is_top_level {
        let mut stored_child = exec_ctx.process_handle.lock().await;
        *stored_child = child.take();
    }

    if let RunResources::TopLevel {
        permits,
        modules_lock,
    } = resources
    {
        exec_ctx
            .all_permits
            .store(Some(Box::new((permits, modules_lock))));
    }

    let out_limit = (capture == Capture::Bounded).then_some(OUTPUT_TAIL_LIMIT);
    if let Some(stdout) = stdout {
        tokio::spawn(pipe_read(
            stdout,
            exec_ctx.gep(|x| &x.stdout),
            stdout_perm.expect("stdout permit must exist when stdout is piped"),
            out_limit,
        ));
    }
    if let Some(stderr) = stderr {
        tokio::spawn(pipe_read(
            stderr,
            exec_ctx.gep(|x| &x.stderr),
            stderr_perm.expect("stderr permit must exist when stderr is piped"),
            out_limit,
        ));
    }

    log_sink_guard.forget();
    if is_top_level {
        exec_ctx.publish_started();
    }

    let deadline_duration = exec_ctx
        .strict_deadline
        .signed_duration_since(chrono::Utc::now())
        .to_std()
        .unwrap_or_default();
    let mut child = if is_top_level {
        let mut stored_child = exec_ctx.process_handle.lock().await;
        stored_child
            .take()
            .ok_or_else(|| anyhow::anyhow!("process handle missing"))?
    } else {
        child
            .take()
            .ok_or_else(|| anyhow::anyhow!("nested process handle missing"))?
    };

    let mut kill_sent = false;
    let stop = wait_for_process_stop(&exec_ctx, caller_stream.as_deref(), deadline_duration);
    tokio::pin!(stop);
    let status = loop {
        if kill_sent {
            break child.wait().await?;
        }
        tokio::select! {
            status = child.wait() => match status {
                Ok(status) => break status,
                Err(e) => {
                    let _ = child.start_kill();
                    let _ = child.wait().await;
                    return Err(e.into());
                }
            },
            reason = &mut stop => {
                if reason == ProcessStop::Deadline {
                    exec_ctx.request_finish(FinishCause::Deadline);
                }
                let _ = child.start_kill();
                kill_sent = true;
            }
        }
    };
    manager_stream_state.close();
    log_debug_into!(&LoggerWithId, genvm_id:id = genvm_id.0, status = status; "genvm exited");
    Ok(status)
}

#[cfg(test)]
#[path = "run_test.rs"]
mod tests;
