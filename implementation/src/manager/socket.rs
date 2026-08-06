use std::collections::HashMap;

use anyhow::{Context, Result};
use axum::extract::ws::{Message, WebSocket};
use futures_util::{Sink, SinkExt, Stream, StreamExt};
use genlayer_calldata as calldata;
use genlayer_calldata::codec::{Decode as DecodeTrait, Encode as EncodeTrait};
use genlayer_calldata::{Decode, Encode};
use genvm_common::*;
use genvm_modules_interfaces::manager_api::{self, Errors, Methods};

use super::{run, AppContext};

const HEADER_LEN: usize = 10;
const ARTIFACT_CHUNK_CAP: u32 = 256 * 1024;
const CONTROL_QUEUE_CAP: usize = 256;
const ARTIFACT_QUEUE_CAP: usize = 8;

#[derive(Debug, Clone)]
struct Frame {
    method: Methods,
    request_id: u64,
    payload: Vec<u8>,
}

impl Frame {
    fn new(
        method: Methods,
        request_id: u64,
        payload: &impl EncodeTrait<Vec<u8>, Error = std::convert::Infallible>,
    ) -> Self {
        Self {
            method,
            request_id,
            payload: calldata::encode_obj(payload),
        }
    }

    fn body_len(&self) -> usize {
        HEADER_LEN + self.payload.len()
    }
}

#[derive(Clone)]
struct Writer {
    control: tokio::sync::mpsc::Sender<Frame>,
    artifact: tokio::sync::mpsc::Sender<Frame>,
}

impl Writer {
    /// Non-blocking, for the paths that run inside the read loop's error
    /// handling and must not park. Everything that can await uses the async
    /// twin instead, so a stalled peer applies backpressure rather than
    /// costing frames.
    fn send_control(&self, frame: Frame) -> Result<()> {
        self.control
            .try_send(frame)
            .context("connection writer queue is full")
    }

    async fn send_control_async(&self, frame: Frame) -> Result<()> {
        self.control
            .send(frame)
            .await
            .context("connection writer queue is closed")
    }

    async fn send_artifact_async(&self, frame: Frame) -> Result<()> {
        self.artifact
            .send(frame)
            .await
            .context("connection writer queue is closed")
    }

    fn error(&self, request_id: u64, code: Errors, message: impl Into<String>) -> Result<()> {
        self.send_control(error_frame(request_id, code, message))
    }
}

async fn writer_loop<S>(
    mut sink: S,
    mut control_rx: tokio::sync::mpsc::Receiver<Frame>,
    mut artifact_rx: tokio::sync::mpsc::Receiver<Frame>,
) -> Result<()>
where
    S: Sink<Message> + Unpin,
    S::Error: std::error::Error + Send + Sync + 'static,
{
    loop {
        while let Ok(frame) = control_rx.try_recv() {
            write_frame(&mut sink, frame).await?;
        }

        tokio::select! {
            biased;
            frame = control_rx.recv() => {
                let Some(frame) = frame else {
                    break;
                };
                write_frame(&mut sink, frame).await?;
            }
            frame = artifact_rx.recv() => {
                let Some(frame) = frame else {
                    if control_rx.is_closed() {
                        break;
                    }
                    continue;
                };
                write_frame(&mut sink, frame).await?;
            }
        }
    }
    Ok(())
}

async fn write_frame<S>(sink: &mut S, frame: Frame) -> Result<()>
where
    S: Sink<Message> + Unpin,
    S::Error: std::error::Error + Send + Sync + 'static,
{
    let mut message = Vec::with_capacity(frame.body_len());
    message.extend_from_slice(&frame.method.value().to_be_bytes());
    message.extend_from_slice(&frame.request_id.to_be_bytes());
    message.extend_from_slice(&frame.payload);
    sink.send(Message::Binary(message.into()))
        .await
        .context("writing websocket message")?;
    Ok(())
}

#[derive(Decode)]
enum RunPayload {
    #[calldata(rename = "run")]
    Run(run::Request),
}

#[derive(Decode)]
enum AttachPayload {
    #[calldata(rename = "attach")]
    Attach(AttachRequest),
}

#[derive(Decode)]
enum CancelPayload {
    #[calldata(rename = "cancel")]
    Cancel(IdRequest),
}

#[derive(Decode)]
enum AckPayload {
    #[calldata(rename = "ack")]
    Ack(IdRequest),
}

#[derive(Decode)]
enum GetArtifactPayload {
    #[calldata(rename = "get_artifact")]
    GetArtifact(GetArtifactRequest),
}

#[derive(Decode)]
struct AttachRequest {
    boot_id: u64,
    genvm_id: run::GenVMId,
}

#[derive(Decode)]
struct IdRequest {
    genvm_id: run::GenVMId,
}

#[derive(Decode)]
struct GetArtifactRequest {
    genvm_id: run::GenVMId,
    field: String,
    offset: u64,
    max_len: u32,
}

#[derive(Encode)]
enum HelloPayload {
    #[calldata(rename = "hello")]
    Hello(Hello),
}

#[derive(Encode)]
struct Hello {
    boot_id: u64,
    protocol_major: u32,
}

#[derive(Encode)]
struct RunResponse {
    genvm_id: run::GenVMId,
}

#[derive(Encode)]
struct EmptyResponse {}

#[derive(Encode)]
struct ErrorPayload {
    code: u8,
    message: String,
}

#[derive(Encode)]
struct ArtifactResponse {
    total_len: u64,
    data: bytes::Bytes,
}

#[derive(Encode)]
struct AttachResponse {
    snapshot: EventPayload,
}

#[derive(Encode)]
enum EventPayload {
    #[calldata(rename = "queued")]
    Queued {
        genvm_id: run::GenVMId,
        host_genvm_id: Option<String>,
    },
    #[calldata(rename = "started")]
    Started {
        genvm_id: run::GenVMId,
        host_genvm_id: Option<String>,
    },
    #[calldata(rename = "failed_to_start")]
    FailedToStart {
        genvm_id: run::GenVMId,
        host_genvm_id: Option<String>,
        error: String,
    },
    #[calldata(rename = "finished")]
    Finished {
        genvm_id: run::GenVMId,
        host_genvm_id: Option<String>,
        cause: String,
        exit_code: Option<i64>,
        consumed_result: Option<bytes::Bytes>,
        #[calldata(serialize_with = encode_json_value)]
        metrics: serde_json::Value,
        finished_at: String,
        version_major: u32,
        version_minor: u32,
        artifact_sizes: ArtifactSizesPayload,
    },
}

#[derive(Encode)]
struct ArtifactSizesPayload {
    stdout: u64,
    stderr: u64,
    genvm_log: u64,
}

fn encode_json_value<W: calldata::Writer>(
    value: &serde_json::Value,
    enc: &mut calldata::Encoder<W>,
) -> std::result::Result<(), W::Error> {
    match value {
        serde_json::Value::Null => enc.push_null(),
        serde_json::Value::Bool(value) => enc.push_bool(*value),
        serde_json::Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                enc.push_i64(value)
            } else if let Some(value) = value.as_u64() {
                enc.push_u64(value)
            } else {
                enc.push_null()
            }
        }
        serde_json::Value::String(value) => enc.push_str(value),
        serde_json::Value::Array(values) => {
            enc.start_array(values.len() as u64)?;
            for value in values {
                encode_json_value(value, enc)?;
            }
            Ok(())
        }
        serde_json::Value::Object(map) => {
            let mut entries = map.iter().collect::<Vec<_>>();
            entries.sort_by_key(|(key, _)| key.as_str());
            enc.start_map(entries.len() as u64)?;
            for (key, value) in entries {
                enc.push_map_k(key)?;
                encode_json_value(value, enc)?;
            }
            Ok(())
        }
    }
}

fn error_frame(request_id: u64, code: Errors, message: impl Into<String>) -> Frame {
    Frame::new(
        Methods::Error,
        request_id,
        &ErrorPayload {
            code: code.value(),
            message: message.into(),
        },
    )
}

fn hello_frame(boot_id: u64) -> Frame {
    Frame::new(
        Methods::Hello,
        0,
        &HelloPayload::Hello(Hello {
            boot_id,
            protocol_major: manager_api::CURRENT_MAJOR,
        }),
    )
}

fn event_frame(event: run::Event) -> Frame {
    Frame::new(Methods::Event, 0, &event_payload(event))
}

fn event_payload(event: run::Event) -> EventPayload {
    match event {
        run::Event::Started {
            genvm_id,
            host_genvm_id,
        } => EventPayload::Started {
            genvm_id,
            host_genvm_id,
        },
        run::Event::FailedToStart {
            genvm_id,
            host_genvm_id,
            error,
        } => EventPayload::FailedToStart {
            genvm_id,
            host_genvm_id,
            error,
        },
        run::Event::Finished {
            genvm_id,
            host_genvm_id,
            cause,
            exit_code,
            consumed_result,
            metrics,
            finished_at,
            version_major,
            version_minor,
            artifact_sizes,
        } => EventPayload::Finished {
            genvm_id,
            host_genvm_id,
            cause: cause.as_str().to_owned(),
            exit_code,
            consumed_result: consumed_result.map(bytes::Bytes::from),
            metrics,
            finished_at: finished_at.to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true),
            version_major: u32::from(version_major),
            version_minor: u32::from(version_minor),
            artifact_sizes: ArtifactSizesPayload {
                stdout: artifact_sizes.stdout,
                stderr: artifact_sizes.stderr,
                genvm_log: artifact_sizes.genvm_log,
            },
        },
    }
}

fn snapshot_payload(snapshot: run::Snapshot) -> EventPayload {
    match snapshot {
        run::Snapshot::Queued {
            genvm_id,
            host_genvm_id,
        } => EventPayload::Queued {
            genvm_id,
            host_genvm_id,
        },
        run::Snapshot::Event(event) => event_payload(event),
    }
}

async fn read_frame<R>(reader: &mut R, writer: &Writer) -> Result<Option<(Methods, u64, Vec<u8>)>>
where
    R: Stream<Item = std::result::Result<Message, axum::Error>> + Unpin,
{
    loop {
        let Some(message) = reader.next().await else {
            return Ok(None);
        };
        let body = match message.context("reading websocket message")? {
            Message::Binary(body) => body,
            Message::Text(_) => {
                writer.error(0, Errors::MalformedFrame, "text messages are not valid")?;
                continue;
            }
            Message::Close(_) => return Ok(None),
            // axum answers pings itself.
            Message::Ping(_) | Message::Pong(_) => continue,
        };

        if body.len() < HEADER_LEN {
            writer.error(0, Errors::MalformedFrame, "message is shorter than header")?;
            continue;
        }

        let method_id = u16::from_be_bytes(body[..2].try_into().expect("fixed method length"));
        let request_id =
            u64::from_be_bytes(body[2..HEADER_LEN].try_into().expect("fixed id length"));
        let method = match Methods::try_from(method_id) {
            Ok(method) => method,
            Err(()) => {
                writer.error(
                    request_id,
                    Errors::UnknownMethod,
                    format!("unknown method id {method_id}"),
                )?;
                continue;
            }
        };

        return Ok(Some((method, request_id, body[HEADER_LEN..].to_vec())));
    }
}

fn decode_payload<T: DecodeTrait>(
    payload: &[u8],
) -> std::result::Result<T, calldata::codec::DecodeError> {
    calldata::decode_obj(payload)
}

struct Connection<R> {
    ctx: sync::DArc<AppContext>,
    reader: R,
    writer: Writer,
    subscriptions: HashMap<run::GenVMId, tokio::task::JoinHandle<()>>,
}

impl<R> Connection<R>
where
    R: Stream<Item = std::result::Result<Message, axum::Error>> + Unpin,
{
    async fn run(mut self) -> Result<()> {
        loop {
            let Some((method, request_id, payload)) =
                read_frame(&mut self.reader, &self.writer).await?
            else {
                break;
            };
            self.dispatch(method, request_id, &payload).await?;
        }
        Ok(())
    }

    async fn dispatch(&mut self, method: Methods, request_id: u64, payload: &[u8]) -> Result<()> {
        if request_id == 0 {
            self.writer.error(
                0,
                Errors::BadRequestId,
                "client requests must use request_id != 0",
            )?;
            return Ok(());
        }

        match method {
            Methods::Run => self.handle_run(request_id, payload).await,
            Methods::Attach => self.handle_attach(request_id, payload).await,
            Methods::Cancel => self.handle_cancel(request_id, payload).await,
            Methods::Ack => self.handle_ack(request_id, payload).await,
            Methods::GetArtifact => self.handle_get_artifact(request_id, payload).await,
            Methods::Error | Methods::Hello | Methods::Event => {
                self.writer.error(
                    request_id,
                    Errors::UnknownMethod,
                    format!("method {} is not a client request", method.value()),
                )?;
                Ok(())
            }
        }
    }

    async fn handle_run(&mut self, request_id: u64, payload: &[u8]) -> Result<()> {
        let RunPayload::Run(mut req) = match decode_payload::<RunPayload>(payload) {
            Ok(req) => req,
            Err(e) => {
                self.writer
                    .error(request_id, Errors::MalformedFrame, e.to_string())?;
                return Ok(());
            }
        };
        req.patch_legacy_method_key();
        if req
            .host_hello_data
            .get(1)
            .is_some_and(|data| !data.is_empty())
        {
            self.writer.error(
                request_id,
                Errors::MalformedFrame,
                "host_hello_data for manager-owned host index 1 is not allowed",
            )?;
            return Ok(());
        }

        let modules_lock = if req.needs_modules() {
            match super::modules::Ctx::get_module_locks(self.ctx.gep(|x| &x.mod_ctx)).await {
                Some(lock) => Some(lock),
                None => {
                    self.writer.error(
                        request_id,
                        Errors::Internal,
                        "modules are required but not running",
                    )?;
                    return Ok(());
                }
            }
        } else {
            None
        };
        let run_ctx = self.ctx.gep(|x| &x.run_ctx);
        let genvm_id = match run_ctx
            .start(self.ctx.clone(), req, Box::new(modules_lock))
            .await
        {
            Ok(id) => id,
            Err(e) => {
                self.writer
                    .error(request_id, Errors::Internal, format!("{:#}", e))?;
                return Ok(());
            }
        };
        let (snapshot, rx) = match run_ctx.attach(run_ctx.boot_id(), genvm_id) {
            Ok(result) => result,
            Err(e) => {
                self.writer
                    .error(request_id, Errors::UnknownId, format!("{:#}", e))?;
                return Ok(());
            }
        };
        self.writer.send_control(Frame::new(
            Methods::Run,
            request_id,
            &RunResponse { genvm_id },
        ))?;
        if let run::Snapshot::Event(event) = snapshot {
            self.writer.send_control(event_frame(event))?;
        }
        // Subscribing last keeps the frames ordered: the receiver was taken
        // above, so nothing is missed, but a terminal landing meanwhile cannot
        // produce an event for a genvm_id the client has not been told yet.
        if let Err(e) = self.subscribe(genvm_id, Ok(rx)) {
            self.writer
                .error(request_id, Errors::UnknownId, format!("{:#}", e))?;
            return Ok(());
        }
        Ok(())
    }

    async fn handle_attach(&mut self, request_id: u64, payload: &[u8]) -> Result<()> {
        let AttachPayload::Attach(req) = match decode_payload::<AttachPayload>(payload) {
            Ok(req) => req,
            Err(e) => {
                self.writer
                    .error(request_id, Errors::MalformedFrame, e.to_string())?;
                return Ok(());
            }
        };

        let run_ctx = self.ctx.gep(|x| &x.run_ctx);
        let (snapshot, rx) = match run_ctx.attach(req.boot_id, req.genvm_id) {
            Ok(result) => result,
            Err(e) if e.to_string() == "boot_id_mismatch" => {
                self.writer
                    .error(request_id, Errors::BootIdMismatch, "boot_id_mismatch")?;
                return Ok(());
            }
            Err(e) => {
                self.writer
                    .error(request_id, Errors::UnknownId, format!("{:#}", e))?;
                return Ok(());
            }
        };
        self.subscribe(req.genvm_id, Ok(rx))?;
        self.writer.send_control(Frame::new(
            Methods::Attach,
            request_id,
            &AttachResponse {
                snapshot: snapshot_payload(snapshot),
            },
        ))?;
        Ok(())
    }

    async fn handle_cancel(&mut self, request_id: u64, payload: &[u8]) -> Result<()> {
        let CancelPayload::Cancel(req) = match decode_payload::<CancelPayload>(payload) {
            Ok(req) => req,
            Err(e) => {
                self.writer
                    .error(request_id, Errors::MalformedFrame, e.to_string())?;
                return Ok(());
            }
        };
        match self.ctx.run_ctx.cancel(req.genvm_id).await {
            Ok(()) => self.writer.send_control(Frame::new(
                Methods::Cancel,
                request_id,
                &EmptyResponse {},
            ))?,
            Err(e) => self
                .writer
                .error(request_id, Errors::UnknownId, format!("{:#}", e))?,
        }
        Ok(())
    }

    async fn handle_ack(&mut self, request_id: u64, payload: &[u8]) -> Result<()> {
        let AckPayload::Ack(req) = match decode_payload::<AckPayload>(payload) {
            Ok(req) => req,
            Err(e) => {
                self.writer
                    .error(request_id, Errors::MalformedFrame, e.to_string())?;
                return Ok(());
            }
        };
        match self.ctx.run_ctx.ack(req.genvm_id) {
            run::AckOutcome::Acked => {
                if let Some(handle) = self.subscriptions.remove(&req.genvm_id) {
                    handle.abort();
                }
                self.writer.send_control(Frame::new(
                    Methods::Ack,
                    request_id,
                    &EmptyResponse {},
                ))?;
            }
            run::AckOutcome::NotFinished => {
                self.writer
                    .error(request_id, Errors::NotFinished, "run has not finished")?
            }
            run::AckOutcome::Unknown => {
                self.writer
                    .error(request_id, Errors::UnknownId, "unknown_id")?
            }
        }
        Ok(())
    }

    async fn handle_get_artifact(&mut self, request_id: u64, payload: &[u8]) -> Result<()> {
        let GetArtifactPayload::GetArtifact(mut req) =
            match decode_payload::<GetArtifactPayload>(payload) {
                Ok(req) => req,
                Err(e) => {
                    self.writer
                        .error(request_id, Errors::MalformedFrame, e.to_string())?;
                    return Ok(());
                }
            };
        req.max_len = req.max_len.min(ARTIFACT_CHUNK_CAP);
        match self
            .ctx
            .run_ctx
            .get_artifact(req.genvm_id, &req.field, req.offset, req.max_len)
        {
            Ok(artifact) => {
                self.writer
                    .send_artifact_async(Frame::new(
                        Methods::GetArtifact,
                        request_id,
                        &ArtifactResponse {
                            total_len: artifact.total_len,
                            data: artifact.data,
                        },
                    ))
                    .await?
            }
            Err(e) if e.to_string() == "unknown_id" => {
                self.writer
                    .error(request_id, Errors::UnknownId, "unknown_id")?
            }
            Err(e) => self
                .writer
                .error(request_id, Errors::MalformedFrame, format!("{:#}", e))?,
        }
        Ok(())
    }

    fn subscribe(
        &mut self,
        genvm_id: run::GenVMId,
        rx: Result<tokio::sync::watch::Receiver<run::Snapshot>>,
    ) -> Result<()> {
        if self.subscriptions.contains_key(&genvm_id) {
            return Ok(());
        }
        let rx = rx?;
        let handle = tokio::spawn(forward_events(self.writer.clone(), rx));
        self.subscriptions.insert(genvm_id, handle);
        Ok(())
    }
}

async fn forward_events(writer: Writer, mut rx: tokio::sync::watch::Receiver<run::Snapshot>) {
    loop {
        if rx.changed().await.is_err() {
            return;
        }
        let run::Snapshot::Event(event) = rx.borrow_and_update().clone() else {
            continue;
        };
        let is_terminal = event.is_terminal();
        if writer.send_control_async(event_frame(event)).await.is_err() || is_terminal {
            return;
        }
    }
}

impl<R> Drop for Connection<R> {
    fn drop(&mut self) {
        // Each subscriber task holds a `Writer` clone, so without this they
        // outlive the connection and keep its writer task alive until every run
        // they watch terminates.
        for handle in self.subscriptions.values() {
            handle.abort();
        }
    }
}

pub async fn handle_connection(socket: WebSocket, ctx: sync::DArc<AppContext>) {
    let (writer_stream, reader) = socket.split();
    let (control_tx, control_rx) = tokio::sync::mpsc::channel(CONTROL_QUEUE_CAP);
    let (artifact_tx, artifact_rx) = tokio::sync::mpsc::channel(ARTIFACT_QUEUE_CAP);
    let writer = Writer {
        control: control_tx,
        artifact: artifact_tx,
    };

    let writer_task = tokio::spawn(writer_loop(writer_stream, control_rx, artifact_rx));
    if writer
        .send_control(hello_frame(ctx.run_ctx.boot_id()))
        .is_err()
    {
        writer_task.abort();
        return;
    }

    let cancel = ctx.cancel.clone();
    let connection = Connection {
        ctx,
        reader,
        writer,
        subscriptions: HashMap::new(),
    };
    // A client is entitled to hold this connection open indefinitely, so the
    // shutdown signal has to reach the read loop directly: axum's graceful
    // shutdown waits for in-flight connections, and would wait on this one
    // forever.
    tokio::select! {
        result = connection.run() => {
            if let Err(e) = result {
                log_debug!(error:ah = e; "manager websocket connection closed");
            }
        }
        _ = cancel.chan.closed() => {}
    }

    match writer_task.await {
        Ok(Ok(())) => {}
        Ok(Err(e)) => log_debug!(error:ah = e; "manager websocket writer stopped"),
        Err(e) if e.is_cancelled() => {}
        Err(e) => log_warn!(error:err = e; "manager websocket writer task failed"),
    }
}

#[cfg(test)]
#[path = "socket_test.rs"]
mod tests;
