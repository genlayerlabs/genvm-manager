use anyhow::Result;
use axum::Json;
use genlayer_calldata as calldata;
use genvm_common::*;
use std::collections::{BTreeMap, HashMap};
use std::str::FromStr;
use std::sync::Arc;

use crate::manager::{
    modules::{self, Ctx},
    run, versioning,
};
use crate::{common, llm, scripting};

use super::AppContext;

pub async fn handle_status(ctx: sync::DArc<AppContext>) -> Result<Json<serde_json::Value>> {
    Ok(Json(serde_json::json!({
        "llm_module": ctx.mod_ctx.get_status(modules::Type::Llm).await,
        "web_module": ctx.mod_ctx.get_status(modules::Type::Web).await,
        "permits": {
            "current": ctx.run_ctx.get_current_permits(),
            "max": ctx.run_ctx.get_max_permits().await,
            "per_sync_run": ctx.run_ctx.permits_sync(),
            "per_nondet_run": ctx.run_ctx.permits_nondet(),
        },
        "executions": ctx.run_ctx.status_executions(),
    })))
}

#[derive(Debug, serde::Deserialize)]
struct StopRequest {
    module_type: modules::Type,
}

pub async fn handle_module_stop(
    ctx: sync::DArc<AppContext>,
    calldata: serde_json::Value,
) -> Result<Json<serde_json::Value>, anyhow::Error> {
    let stop_request = serde_json::from_value::<StopRequest>(calldata.clone())?;

    let res = ctx.mod_ctx.stop(stop_request.module_type).await?;

    let res = if res {
        "module_stopped"
    } else {
        "module_not_running"
    };

    Ok(Json(serde_json::json!({"result": res})))
}

pub async fn handle_module_start(
    ctx: sync::DArc<AppContext>,
    calldata: serde_json::Value,
) -> Result<Json<serde_json::Value>> {
    let req = serde_json::from_value::<modules::StartRequest>(calldata)?;

    ctx.mod_ctx.start(req).await?;

    Ok(Json(serde_json::json!({"result": "module_started"})))
}

pub async fn handle_module_restart(
    ctx: sync::DArc<AppContext>,
    calldata: serde_json::Value,
) -> Result<Json<serde_json::Value>> {
    let req = serde_json::from_value::<modules::StartRequest>(calldata)?;

    ctx.mod_ctx.restart(req).await?;

    Ok(Json(serde_json::json!({"result": "module_restarted"})))
}

pub async fn handle_genvm_run(
    ctx: sync::DArc<AppContext>,
    data: &[u8],
) -> Result<Json<serde_json::Value>> {
    let mut res: super::run::Request = calldata::decode_obj(data)?;
    res.patch_legacy_method_key();

    let modules_lock = if res.needs_modules() {
        let lock = Ctx::get_module_locks(ctx.gep(|x| &x.mod_ctx)).await;
        if lock.is_none() {
            anyhow::bail!(
                "modules are required but not running (is_sync=false with 'n' permission)"
            );
        }
        lock
    } else {
        None
    };

    let run_ctx = ctx.gep(|x| &x.run_ctx);
    let id = run_ctx.start(ctx, res, Box::new(modules_lock)).await?;
    let (snapshot, mut events) = run_ctx.attach(run_ctx.boot_id(), id)?;

    let start_event = match snapshot {
        run::Snapshot::Event(event) => event,
        run::Snapshot::Queued { .. } => loop {
            events.changed().await?;
            if let run::Snapshot::Event(event) = events.borrow_and_update().clone() {
                break event;
            }
        },
    };

    if let run::Event::FailedToStart { error, .. } = start_event {
        anyhow::bail!(error);
    }

    Ok(Json(serde_json::json!({"result": "started", "id": id})))
}

pub async fn handle_contract_detect_version(
    ctx: sync::DArc<AppContext>,
    contract_code: bytes::Bytes,
    deployment_timestamp: String,
) -> Result<Json<serde_json::Value>> {
    let deployment_timestamp =
        chrono::DateTime::parse_from_rfc3339(&deployment_timestamp)?.with_timezone(&chrono::Utc);
    let major = versioning::detect_major_spec(&ctx, &contract_code, deployment_timestamp).await?;
    Ok(Json(serde_json::json!({
        "specified_major": major,
    })))
}

pub async fn handle_set_log_level(
    _ctx: sync::DArc<AppContext>,
    data: serde_json::Value,
) -> Result<Json<serde_json::Value>> {
    let level = data
        .get("level")
        .and_then(|v| v.as_str())
        .and_then(|s| genvm_common::logger::Level::from_str(s).ok())
        .ok_or_else(|| anyhow::anyhow!("invalid log level"))?;

    let Some(logger) = genvm_common::logger::__LOGGER.get() else {
        anyhow::bail!("logger_not_initialized");
    };
    logger.set_filter(level);

    Ok(Json(
        serde_json::json!({"result": "log_level_set", "level": level}),
    ))
}

pub async fn handle_manifest_reload(
    ctx: sync::DArc<AppContext>,
) -> Result<Json<serde_json::Value>> {
    ctx.ver_ctx.reload_manifest().await?;

    Ok(Json(serde_json::json!({"result": "manifest_reloaded"})))
}

pub async fn handle_get_permits(ctx: sync::DArc<AppContext>) -> Result<Json<serde_json::Value>> {
    let permits = ctx.run_ctx.get_max_permits().await;
    Ok(Json(serde_json::json!({"permits": permits})))
}

pub async fn handle_set_permits(
    ctx: sync::DArc<AppContext>,
    data: serde_json::Value,
) -> Result<Json<serde_json::Value>> {
    let permits = data
        .get("permits")
        .and_then(|v| v.as_u64())
        .and_then(|v| usize::try_from(v).ok())
        .ok_or_else(|| anyhow::anyhow!("invalid permits"))?;

    let new_permits = ctx.run_ctx.set_permits(permits).await;

    Ok(Json(
        serde_json::json!({"result": "permits_set", "permits": new_permits}),
    ))
}

#[derive(Debug, serde::Deserialize)]
pub struct ShutdownRequest {}

pub async fn handle_genvm_shutdown(
    ctx: sync::DArc<AppContext>,
    genvm_id: run::GenVMId,
    _req: ShutdownRequest,
) -> Result<Json<serde_json::Value>> {
    let result = async {
        ctx.run_ctx.cancel(genvm_id).await?;
        let _ = ctx.run_ctx.await_terminal(genvm_id).await?;
        anyhow::Ok(())
    }
    .await;

    match result {
        Ok(()) => Ok(Json(serde_json::json!({
            "result": "shutdown_completed",
            "genvm_id": genvm_id
        }))),
        Err(e) => Ok(Json(serde_json::json!({
            "error": format!("{}", e),
            "genvm_id": genvm_id
        }))),
    }
}

const STATUS_WAIT_CAP: std::time::Duration = std::time::Duration::from_secs(120);

#[derive(Debug, serde::Deserialize)]
pub struct StatusRequest {
    #[serde(default)]
    pub wait: Option<String>,
}

fn parse_status_wait(req: &StatusRequest) -> Result<Option<std::time::Duration>> {
    req.wait
        .as_deref()
        .map(run::ManagerDuration::try_from)
        .transpose()
        .map(|wait| {
            wait.map(run::ManagerDuration::to_std)
                .map(|wait| wait.min(STATUS_WAIT_CAP))
        })
}

pub async fn handle_genvm_status(
    ctx: sync::DArc<AppContext>,
    genvm_id: run::GenVMId,
    req: StatusRequest,
) -> Result<Json<serde_json::Value>> {
    if let Some(wait) = parse_status_wait(&req)? {
        if ctx.run_ctx.status(genvm_id).is_none() {
            let _ = tokio::time::timeout(wait, ctx.run_ctx.await_terminal(genvm_id)).await;
        }
    }

    let status = ctx.run_ctx.status(genvm_id);
    if status.is_some() {
        ctx.run_ctx.ack(genvm_id);
    }

    Ok(Json(serde_json::json!({
        "genvm_id": genvm_id,
        "status": status
    })))
}

#[derive(Debug, serde::Deserialize)]
pub struct ArtifactRequest {
    pub field: String,
    #[serde(default)]
    pub offset: u64,
    pub max_len: u32,
}

pub async fn handle_genvm_artifact(
    ctx: sync::DArc<AppContext>,
    genvm_id: run::GenVMId,
    req: ArtifactRequest,
) -> Result<Json<serde_json::Value>> {
    use base64::Engine;

    let artifact = ctx
        .run_ctx
        .get_artifact(genvm_id, &req.field, req.offset, req.max_len)?;
    Ok(Json(serde_json::json!({
        "genvm_id": genvm_id,
        "field": req.field,
        "total_len": artifact.total_len,
        "data_base64": base64::engine::general_purpose::STANDARD.encode(&artifact.data),
    })))
}

#[derive(serde::Deserialize)]
pub struct LlmCheckRequest {
    pub configs: Vec<LlmProviderConfig>,
    pub test_prompts: Vec<llm::prompt::Internal>,
}

#[derive(serde::Deserialize)]
pub struct LlmProviderConfig {
    pub host: String,
    pub provider: llm::config::Provider,
    pub model: String,
    pub key: String,
}

#[derive(serde::Serialize)]
pub struct LlmAvailabilityResult {
    pub config_index: usize,
    pub prompt_index: usize,
    pub available: bool,
    pub error: Option<String>,
    pub response: Option<String>,
}

pub async fn handle_llm_check(
    _ctx: sync::DArc<AppContext>,
    data: serde_json::Value,
) -> Result<Json<Vec<LlmAvailabilityResult>>> {
    let request: LlmCheckRequest = serde_json::from_value(data)?;

    let mut results = Vec::new();

    for (config_idx, config_data) in request.configs.iter().enumerate() {
        for (prompt_idx, test_prompt) in request.test_prompts.iter().enumerate() {
            let result = check_llm_availability(config_data, test_prompt).await;

            let availability_result = match result {
                Ok(response) => LlmAvailabilityResult {
                    config_index: config_idx,
                    prompt_index: prompt_idx,
                    available: true,
                    error: None,
                    response: Some(response),
                },
                Err(error) => LlmAvailabilityResult {
                    config_index: config_idx,
                    prompt_index: prompt_idx,
                    available: false,
                    error: Some(error.to_string()),
                    response: None,
                },
            };

            results.push(availability_result);
        }
    }

    Ok(Json(results))
}

#[derive(Debug, serde::Deserialize)]
pub struct DescribeVmErrorRequest {
    pub error: String,
}

fn is_sub_error(err: &str, sub_err_of: &str) -> bool {
    if !err.starts_with(sub_err_of) {
        return false;
    }
    sub_err_of.len() == err.len() || err.as_bytes()[sub_err_of.len()] == b' '
}

fn describe_vm_error(error: &str) -> Option<&'static str> {
    if is_sub_error(error, "wasm_trap") {
        Some("Web Assembly trap reached")
    } else if is_sub_error(error, "exit_code") {
        Some("Non-zero exit code from the contract. Check stderr for contract-provided details")
    } else if is_sub_error(error, "invalid_contract not_utf8_text") {
        Some(
            r##"The contract was detected to be a plain text contract, however it contains non-UTF8 bytes and hence cannot be parsed. Is deployed runner a valid contract?"##,
        )
    } else if is_sub_error(error, "invalid_contract runner absent")
        || is_sub_error(error, "invalid_contract absent_runner_comment")
    {
        Some(
            r##"The contract was detected to be a plain text contract, however it does not start with a runner comment (such as `# { "Depends": "py-genlayer:..." }`), hence it is impossible to run. Have you forgotten to add it or is there other content before it?"##,
        )
    } else if is_sub_error(error, "invalid_contract") {
        Some("Execution failed before running the contract, likely due to invalid or malformed contract runner")
    } else if is_sub_error(error, "OOM storage") {
        Some("Contract ran out of storage pages it could (re)write")
    } else if is_sub_error(error, "OOM") {
        Some("Contract exceeded allowed execution memory (RAM) limit")
    } else {
        None
    }
}

pub async fn handle_describe_vm_error(
    _ctx: sync::DArc<AppContext>,
    request: DescribeVmErrorRequest,
) -> Result<Json<serde_json::Value>> {
    let description = describe_vm_error(&request.error);

    Ok(Json(serde_json::json!({ "description": description })))
}

fn build_check_backend(config_data: &LlmProviderConfig) -> Result<llm::config::BackendConfig> {
    let backend = serde_json::json!({
        "host": config_data.host,
        "provider": config_data.provider,
        "models": {
            &config_data.model: {}
        },
        "key": config_data.key
    });

    // the key arrives as `${ENV[NAME]}` and is resolved against the manager's own
    // environment; without these vars the templater expands it to an empty string
    let mut vars = HashMap::new();
    genvm_common::populate_default_config_vars(&mut vars)?;

    let backend = genvm_common::templater::patch_json(
        &vars,
        backend,
        &genvm_common::templater::DOLLAR_UNFOLDER_RE,
    )?;

    Ok(serde_json::from_value(backend)?)
}

async fn check_llm_availability(
    config_data: &LlmProviderConfig,
    test_prompt: &llm::prompt::Internal,
) -> Result<String> {
    let backend = build_check_backend(config_data)?;
    let provider = backend.to_provider();

    let ctx = scripting::CtxPart {
        client: common::create_client_unfiltered()?,
        client_unfiltered: common::create_client_unfiltered()?,
        metrics: sync::DArc::new(scripting::Metrics::default()),
        node_address: "test_node".to_owned(),
        sign_headers: Arc::new(BTreeMap::new()),
        sign_url: Arc::from("test_url"),
        sign_vars: BTreeMap::new(),
        hello: Arc::new(genvm_modules_interfaces::GenVMHello {
            genvm_id: genvm_modules_interfaces::GenVMId(999),
            role: genvm_modules_interfaces::Role::Leader,
            host_data: genvm_modules_interfaces::HostData {
                node_address: "test_node".to_owned(),
                tx_id: "test_tx".to_owned(),
                rest: serde_json::Map::new(),
            },
            gas_data: std::collections::BTreeMap::new(),
            initial_time_units_allocation: 0,
        }),
    };

    let response = provider
        .exec_prompt_text(&ctx, test_prompt, &config_data.model)
        .await?;

    Ok(response.result)
}

#[cfg(test)]
#[path = "handlers_test.rs"]
mod tests;
