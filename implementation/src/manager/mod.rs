use genvm_common::*;
use std::{collections::HashMap, sync::Arc};

use anyhow::{Context, Result};
use axum::{
    body::Bytes,
    extract::rejection::QueryRejection,
    extract::ws::WebSocketUpgrade,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};

use crate::common;

mod check_install;
pub mod execution_context;
mod handlers;
pub mod modules;
mod run;
mod socket;
mod versioning;

#[derive(serde::Serialize, serde::Deserialize)]
pub struct Config {
    #[serde(flatten)]
    pub base: genvm_common::BaseConfig,
    pub manifest_path: String,

    /// Absent or null keeps every default.
    pub permits: Option<PermitsConfig>,
    #[serde(default = "default_execution_retention")]
    pub execution_retention: run::ManagerDuration,
    #[serde(default = "default_max_message_bytes")]
    pub max_message_bytes: usize,
}

/// Concurrency budget, denominated in gigabytes of RAM.
#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
pub struct PermitsConfig {
    /// Total pool; null derives it from free memory.
    #[serde(default)]
    pub total: Option<usize>,
    #[serde(default = "default_permits_per_gib")]
    pub per_gib: PermitsPerGig,
    #[serde(default = "default_permits_sync")]
    pub sync: usize,
    #[serde(default = "default_permits_nondet")]
    pub nondet: usize,
}

impl Default for PermitsConfig {
    fn default() -> Self {
        Self {
            total: None,
            per_gib: default_permits_per_gib(),
            sync: default_permits_sync(),
            nondet: default_permits_nondet(),
        }
    }
}

/// Permits handed out per gibibyte of free memory.
///
/// A ratio rather than a float: `1/4` is exactly one permit per four gibibytes,
/// and a config saying so must not depend on how a decimal rounds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PermitsPerGig(num_rational::Ratio<u64>);

impl PermitsPerGig {
    /// Floors, so a pool is never wider than the memory backing it.
    pub fn apply(self, gigabytes: usize) -> usize {
        let scaled = gigabytes as u128 * *self.0.numer() as u128 / *self.0.denom() as u128;
        scaled.min(usize::MAX as u128) as usize
    }
}

impl std::str::FromStr for PermitsPerGig {
    type Err = anyhow::Error;

    fn from_str(s: &str) -> Result<Self> {
        let (numer, denom) = match s.split_once('/') {
            Some((numer, denom)) => (numer.trim(), denom.trim()),
            None => (s.trim(), "1"),
        };
        let numer: u64 = numer
            .parse()
            .with_context(|| format!("invalid permits per_gib numerator in {s:?}"))?;
        let denom: u64 = denom
            .parse()
            .with_context(|| format!("invalid permits per_gib denominator in {s:?}"))?;
        anyhow::ensure!(numer > 0, "permits per_gib must be positive, got {s:?}");
        anyhow::ensure!(denom > 0, "permits per_gib must not divide by zero");

        Ok(Self(num_rational::Ratio::new(numer, denom)))
    }
}

impl std::fmt::Display for PermitsPerGig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}/{}", self.0.numer(), self.0.denom())
    }
}

impl serde::Serialize for PermitsPerGig {
    fn serialize<S: serde::Serializer>(
        &self,
        serializer: S,
    ) -> std::result::Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> serde::Deserialize<'de> for PermitsPerGig {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Self, D::Error> {
        /// `1` and `"1/4"` are both natural spellings in yaml
        #[derive(serde::Deserialize)]
        #[serde(untagged)]
        enum Repr {
            Integer(u64),
            Str(String),
        }

        use std::str::FromStr as _;

        match Repr::deserialize(deserializer)? {
            Repr::Integer(n) => Self::from_str(&n.to_string()),
            Repr::Str(s) => Self::from_str(&s),
        }
        .map_err(serde::de::Error::custom)
    }
}

fn default_permits_per_gib() -> PermitsPerGig {
    PermitsPerGig(num_rational::Ratio::from_integer(1))
}

fn default_permits_sync() -> usize {
    4
}

fn default_permits_nondet() -> usize {
    8
}

fn default_execution_retention() -> run::ManagerDuration {
    run::ManagerDuration::try_from("5m").expect("default execution_retention is valid")
}

fn default_max_message_bytes() -> usize {
    64 * 1024 * 1024
}

#[derive(serde::Serialize, serde::Deserialize, Clone)]
pub struct ExecutorVersion {
    pub available_after: String,
}

#[derive(Debug, PartialEq, PartialOrd, Clone)]
pub struct VersionParts {
    pub major: Option<u32>,
    pub minor: Option<u32>,
    pub patch: Option<u32>,
}

pub fn parse_version(version: &str) -> Result<VersionParts> {
    let version_re = regex::Regex::new(r"^v(\d+|\*)\.(\d+|\*)\.(\d+|\*)$").unwrap();
    let captures = version_re
        .captures(version)
        .ok_or_else(|| anyhow::anyhow!("Invalid version format: {}", version))?;

    let parse_part = |part: &str| -> Option<u32> {
        if part == "*" {
            None
        } else {
            part.parse().ok()
        }
    };

    Ok(VersionParts {
        major: parse_part(&captures[1]),
        minor: parse_part(&captures[2]),
        patch: parse_part(&captures[3]),
    })
}

#[derive(serde::Serialize, serde::Deserialize, Clone)]
pub struct Manifest {
    pub executor_versions: std::collections::BTreeMap<String, ExecutorVersion>,
}

pub struct AppContext {
    pub cancel: Arc<cancellation::Token>,
    pub config: sync::DArc<Config>,
    pub mod_ctx: modules::Ctx,
    pub run_ctx: run::Ctx,
    pub ver_ctx: versioning::Ctx,
}

#[derive(clap::Subcommand, Debug)]
pub enum SubCommand {
    /// Verify installed executors' runners (and optionally precompile them).
    CheckInstall(check_install::Args),
}

#[derive(clap::Args, Debug)]
pub struct CliArgs {
    #[command(subcommand)]
    pub command: Option<SubCommand>,

    #[arg(long, default_value_t = 3999)]
    pub port: u16,
    #[arg(long, default_value = "127.0.0.1")]
    pub host: String,

    /// Unix socket path (mutually exclusive with --host/--port)
    #[arg(long, conflicts_with_all = ["host", "port"])]
    pub socket: Option<String>,

    #[arg(long, default_value_t = false)]
    die_with_parent: bool,

    #[arg(long, default_value_t = String::from("${exeDir}/../config/genvm-manager.yaml"))]
    pub config: String,

    #[arg(long)]
    pub manifest_path: Option<String>,
}

fn error_response(status: StatusCode, error: impl Into<String>) -> Response {
    (status, Json(serde_json::json!({"error": error.into()}))).into_response()
}

fn internal_error(error: anyhow::Error) -> Response {
    log_error!(err:ah = &error; "internal server error");
    error_response(StatusCode::INTERNAL_SERVER_ERROR, format!("{:#}", error))
}

/// A malformed body, query or header is the caller's fault: it answers 400 and
/// stays out of the error log, which is for this process' own failures.
fn bad_request(error: anyhow::Error) -> Response {
    log_debug!(err:ah = &error; "bad request");
    error_response(StatusCode::BAD_REQUEST, format!("{:#}", error))
}

fn unwrap_all_anyhow<R: IntoResponse>(result: anyhow::Result<R>) -> Response {
    match result {
        Ok(response) => response.into_response(),
        Err(error) => internal_error(error),
    }
}

async fn not_found() -> Response {
    error_response(StatusCode::NOT_FOUND, "Not Found")
}

/// Parses a json body without requiring a `Content-Type`. warp accepted a body
/// with the header absent, and clients rely on that.
fn parse_json_body(body: Bytes) -> anyhow::Result<serde_json::Value> {
    Ok(serde_json::from_slice(&body)?)
}

fn parse_query<T>(query: std::result::Result<Query<T>, QueryRejection>) -> anyhow::Result<T> {
    Ok(query?.0)
}

fn parse_genvm_id(raw: &str) -> Option<run::GenVMId> {
    if raw.is_empty() || !raw.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    raw.parse::<u64>().ok().map(run::GenVMId)
}

fn deployment_timestamp(headers: &HeaderMap) -> anyhow::Result<String> {
    let value = headers
        .get("Deployment-Timestamp")
        .ok_or_else(|| anyhow::anyhow!("missing Deployment-Timestamp header"))?;
    Ok(value.to_str()?.to_owned())
}

fn create_router(app_ctx: sync::DArc<AppContext>) -> Router {
    // NOTE: when changing routes, also update docs/website/src/impl-spec/appendix/manager-api.yaml
    Router::new()
        .route(
            "/ws",
            get(
                |ws: WebSocketUpgrade, State(ctx): State<sync::DArc<AppContext>>| async move {
                    ws.max_message_size(ctx.config.max_message_bytes)
                        .on_upgrade(move |socket| socket::handle_connection(socket, ctx))
                },
            ),
        )
        .route(
            "/status",
            get(|State(ctx): State<sync::DArc<AppContext>>| async move {
                unwrap_all_anyhow(handlers::handle_status(ctx).await)
            }),
        )
        .route(
            "/module/start",
            post(
                |State(ctx): State<sync::DArc<AppContext>>,
                 body: Bytes| async move {
                    let body = match parse_json_body(body) {
                        Ok(body) => body,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_module_start(ctx, body).await)
                },
            ),
        )
        .route(
            "/module/stop",
            post(
                |State(ctx): State<sync::DArc<AppContext>>,
                 body: Bytes| async move {
                    let body = match parse_json_body(body) {
                        Ok(body) => body,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_module_stop(ctx, body).await)
                },
            ),
        )
        .route(
            "/module/restart",
            post(
                |State(ctx): State<sync::DArc<AppContext>>,
                 body: Bytes| async move {
                    let body = match parse_json_body(body) {
                        Ok(body) => body,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_module_restart(ctx, body).await)
                },
            ),
        )
        .route(
            "/genvm/run",
            post(
                |State(ctx): State<sync::DArc<AppContext>>, body: Bytes| async move {
                    unwrap_all_anyhow(handlers::handle_genvm_run(ctx, &body).await)
                },
            ),
        )
        .route(
            "/contract/detect-version",
            post(
                |State(ctx): State<sync::DArc<AppContext>>,
                 headers: HeaderMap,
                 contract_code: Bytes| async move {
                    let deployment_timestamp = match deployment_timestamp(&headers) {
                        Ok(deployment_timestamp) => deployment_timestamp,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(
                        handlers::handle_contract_detect_version(
                            ctx,
                            contract_code,
                            deployment_timestamp,
                        )
                        .await,
                    )
                },
            ),
        )
        .route(
            "/log/level",
            post(
                |State(ctx): State<sync::DArc<AppContext>>,
                 body: Bytes| async move {
                    let body = match parse_json_body(body) {
                        Ok(body) => body,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_set_log_level(ctx, body).await)
                },
            ),
        )
        .route(
            "/manifest/reload",
            post(|State(ctx): State<sync::DArc<AppContext>>| async move {
                unwrap_all_anyhow(handlers::handle_manifest_reload(ctx).await)
            }),
        )
        .route(
            "/permits",
            get(|State(ctx): State<sync::DArc<AppContext>>| async move {
                unwrap_all_anyhow(handlers::handle_get_permits(ctx).await)
            })
            .post(
                |State(ctx): State<sync::DArc<AppContext>>,
                 body: Bytes| async move {
                    let body = match parse_json_body(body) {
                        Ok(body) => body,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_set_permits(ctx, body).await)
                },
            ),
        )
        .route(
            "/genvm/{genvm_id}",
            delete(
                |State(ctx): State<sync::DArc<AppContext>>,
                 Path(raw_id): Path<String>,
                 query: std::result::Result<Query<handlers::ShutdownRequest>, QueryRejection>| async move {
                    let Some(genvm_id) = parse_genvm_id(&raw_id) else {
                        return not_found().await;
                    };
                    let query = match parse_query(query) {
                        Ok(query) => query,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_genvm_shutdown(ctx, genvm_id, query).await)
                },
            )
            .get(
                |State(ctx): State<sync::DArc<AppContext>>,
                 Path(raw_id): Path<String>,
                 query: std::result::Result<Query<handlers::StatusRequest>, QueryRejection>| async move {
                    let Some(genvm_id) = parse_genvm_id(&raw_id) else {
                        return not_found().await;
                    };
                    let query = match parse_query(query) {
                        Ok(query) => query,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(
                        handlers::handle_genvm_status(ctx, genvm_id, query).await,
                    )
                },
            ),
        )
        .route(
            "/genvm/{genvm_id}/artifact",
            get(
                |State(ctx): State<sync::DArc<AppContext>>,
                 Path(raw_id): Path<String>,
                 query: std::result::Result<Query<handlers::ArtifactRequest>, QueryRejection>| async move {
                    let Some(genvm_id) = parse_genvm_id(&raw_id) else {
                        return not_found().await;
                    };
                    let query = match parse_query(query) {
                        Ok(query) => query,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_genvm_artifact(ctx, genvm_id, query).await)
                },
            ),
        )
        .route(
            "/llm/check",
            post(
                |State(ctx): State<sync::DArc<AppContext>>,
                 body: Bytes| async move {
                    let body = match parse_json_body(body) {
                        Ok(body) => body,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_llm_check(ctx, body).await)
                },
            ),
        )
        .route(
            "/vm-error/describe",
            get(
                |State(ctx): State<sync::DArc<AppContext>>,
                 query: std::result::Result<Query<handlers::DescribeVmErrorRequest>, QueryRejection>| async move {
                    let query = match parse_query(query) {
                        Ok(query) => query,
                        Err(error) => return bad_request(error),
                    };
                    unwrap_all_anyhow(handlers::handle_describe_vm_error(ctx, query).await)
                },
            ),
        )
        .fallback(not_found)
        .method_not_allowed_fallback(not_found)
        // Contract code and calldata routinely exceed the default cap, and an
        // exceeded cap answers with a plain-text 413 rather than our error shape.
        .layer(axum::extract::DefaultBodyLimit::disable())
        .with_state(app_ctx)
}

async fn run_http_server(
    cancel: Arc<cancellation::Token>,
    config: sync::DArc<Config>,
    args: &CliArgs,
) -> Result<()> {
    let app_ctx = sync::DArc::new(AppContext {
        cancel: cancel.clone(),
        mod_ctx: modules::Ctx::new(cancel.clone()),
        config: config.clone(),
        run_ctx: run::Ctx::new(&config)?,
        ver_ctx: versioning::Ctx::new(config.clone()).await?,
    });

    run::start_service(app_ctx.gep(|x| &x.run_ctx), cancel.clone()).await?;

    let routes = create_router(app_ctx);

    let cancellation = cancel.clone();

    if let Some(socket_path) = &args.socket {
        let path = std::path::Path::new(socket_path);

        if path.exists() {
            std::fs::remove_file(path)
                .with_context(|| format!("removing stale socket {}", path.display()))?;
        }

        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() && !parent.exists() {
                std::fs::create_dir_all(parent)?;
            }
        }

        let listener = tokio::net::UnixListener::bind(path)
            .with_context(|| format!("binding to unix socket {}", path.display()))?;

        log_info!(socket_path:? = path; "HTTP server started on Unix socket");

        let server = axum::serve(listener, routes)
            .with_graceful_shutdown(async move { cancellation.chan.closed().await });

        let cleanup_path = path.to_owned();
        let result = server.await;
        let _ = std::fs::remove_file(&cleanup_path);

        log_info!(socket_path:? = cleanup_path; "HTTP server stopped");
        result?;
    } else {
        let addr = std::net::SocketAddr::new(args.host.parse()?, args.port);
        let listener = tokio::net::TcpListener::bind(addr)
            .await
            .with_context(|| format!("binding to tcp address {}", addr))?;
        let addr = listener.local_addr()?;

        log_info!(address:? = addr; "HTTP server started");
        axum::serve(listener, routes)
            .with_graceful_shutdown(async move { cancellation.chan.closed().await })
            .await?;
        log_info!(address:? = addr; "HTTP server stopped");
    }

    Ok(())
}

async fn main_loop(
    cancel: Arc<cancellation::Token>,
    args: &CliArgs,
    config: sync::DArc<Config>,
) -> Result<()> {
    run_http_server(cancel, config, args).await
}

pub fn entrypoint(args: CliArgs) -> Result<()> {
    if let Some(SubCommand::CheckInstall(ci)) = &args.command {
        return check_install::run(&args, ci);
    }

    let config = genvm_common::load_config(HashMap::new(), &args.config)
        .with_context(|| "loading config")?;
    let mut config: Config = serde_yaml::from_value(config)?;

    if let Some(manifest_path) = &args.manifest_path {
        config.manifest_path = manifest_path.clone();
    }

    let config: sync::DArc<Config> = sync::DArc::new(config);

    config.base.setup_logging(std::io::stdout())?;

    let runtime = config.base.create_rt()?;

    let token = common::setup_cancels(&runtime, args.die_with_parent)?;

    runtime.block_on(main_loop(token, &args, config))?;

    Ok(())
}
