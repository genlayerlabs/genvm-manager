// End-to-end tests for the LLM dispatch script after backend selection and the
// retry state machine were delegated to the `llm_policy` engine. Everything here
// runs offline against fake local TCP backends; no API keys, no network.
//
// What is asserted: candidates are tried in the configured priority order, any
// provider error (overloaded or not) falls through to the next candidate, and
// capability requirements filter the catalog before the engine ever routes.

use genvm_common::logger;
use genvm_common::*;
use genvm_modules_interfaces::llm::{self as llm_iface};
use mlua::LuaSerdeExt;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use genvm_modules::common;
use genvm_modules::llm::*;
use genvm_modules::scripting;

const OK_BODY: &str = concat!(
    "{\"choices\":[{\"message\":{\"content\":\"ok\"}}],",
    "\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1,\"total_tokens\":2}}"
);

/// One scripted fake backend. Each incoming connection pops the next status from
/// `responses` (the last one repeats) and appends this backend's name to the
/// shared `hits` log, so a test can assert the exact order providers were tried.
struct FakeBackend {
    name: &'static str,
    addr: String,
}

fn spawn_fake(
    name: &'static str,
    responses: Vec<u16>,
    hits: Arc<Mutex<Vec<&'static str>>>,
) -> FakeBackend {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let addr = format!("http://{}", listener.local_addr().unwrap());
    let listener = tokio::net::TcpListener::from_std(listener).unwrap();

    tokio::spawn(async move {
        let mut idx = 0usize;
        loop {
            let (mut sock, _) = match listener.accept().await {
                Ok(v) => v,
                Err(_) => break,
            };
            hits.lock().unwrap().push(name);

            // Drain the request headers so the client's write side completes.
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf).await;

            let status = responses[idx.min(responses.len() - 1)];
            idx += 1;

            let response = if status == 200 {
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                    OK_BODY.len(),
                    OK_BODY
                )
            } else {
                format!(
                    "HTTP/1.1 {} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                    status
                )
            };
            let _ = sock.write_all(response.as_bytes()).await;
            let _ = sock.shutdown().await;
        }
    });

    FakeBackend { name, addr }
}

fn model_cfg(supports_json: bool) -> config::ModelConfig {
    config::ModelConfig {
        enabled: true,
        supports_json,
        supports_image: false,
        use_max_completion_tokens: false,
        meta: serde_json::Value::Null,
        timeout: None,
    }
}

/// Build a config + provider map from fake backends. `priority` sets
/// `meta.priority` so the dispatch chain order is deterministic and known.
fn build_config(
    backends: &[(&FakeBackend, i64, bool)],
) -> (
    sync::DArc<config::Config>,
    Arc<BTreeMap<String, Box<dyn providers::Provider + Send + Sync>>>,
) {
    let mut cfg_backends = BTreeMap::new();
    let mut provider_map = BTreeMap::new();

    for (fake, priority, supports_json) in backends {
        let backend = config::BackendConfig {
            enabled: true,
            provider: config::Provider::OpenaiCompatible,
            key: "<empty>".to_owned(),
            script_config: config::ScriptBackendConfig {
                models: BTreeMap::from([("model".to_owned(), model_cfg(*supports_json))]),
                meta: serde_json::json!({ "priority": priority }),
                timeout: None,
            },
            host: fake.addr.clone(),
        };
        provider_map.insert(fake.name.to_owned(), backend.to_provider());
        cfg_backends.insert(fake.name.to_owned(), backend);
    }

    let mut extra_path = std::path::PathBuf::from("../install/lib/genvm-lua")
        .canonicalize()
        .unwrap()
        .to_str()
        .unwrap()
        .to_owned();
    extra_path.push_str("/?.lua");
    extra_path.push(';');
    extra_path.push_str(&common::tests::llm_policy_lua_path());

    let config = sync::DArc::new(config::Config {
        base: genvm_common::BaseConfig {
            log_level: logger::Level::Debug,
            threads: 1,
            blocking_threads: 3,
            log_disable: "".to_owned(),
        },
        mod_base: common::ModuleBaseConfig {
            vm_count: 1,
            lua_script_path: "../install/config/genvm-llm-default.lua".to_string(),
            bind_address: None,
            lua_path: extra_path,
            signer_url: Arc::from(""),
            signer_headers: Arc::new(BTreeMap::new()),
            data_dir: String::new(),
        },
        prompt_templates: config::PromptTemplates {
            eq_comparative: serde_json::Value::Null,
            eq_non_comparative_leader: serde_json::Value::Null,
            eq_non_comparative_validator: serde_json::Value::Null,
        },
        backends: cfg_backends,
        meta: serde_json::Value::Null,
        timeout: None,
    });

    (config, Arc::new(provider_map))
}

/// Run one `ExecPrompt` through the shipped dispatch script and return the text
/// answer (or the error). Fresh `Setup` per call, as a real session does.
async fn run_prompt(
    config: &sync::DArc<config::Config>,
    providers: Arc<BTreeMap<String, Box<dyn providers::Provider + Send + Sync>>>,
    format: llm_iface::OutputFormat,
) -> anyhow::Result<String> {
    let user_vm = create_vm(config).await.unwrap();

    let hello = common::tests::get_hello();
    let metrics = sync::DArc::new(Metrics::default());
    let scripting_ctx = scripting::create_ctx_part(
        &hello,
        &config.gep(|x| &x.mod_base),
        metrics.gep(|x| &x.scripting),
        false,
    )
    .unwrap();
    let llm_ctx = ctx::CtxPart {
        providers: providers.clone(),
        metrics,
    };
    let sub_ctx = sync::DArc::new(genvm_modules::manager::execution_context::LlmSubContext {
        scripting: scripting_ctx,
        module: llm_ctx,
    });

    let (_ctx, ctx_lua) = user_vm.create_ctx(&sub_ctx).unwrap();

    if let Some(ref setup) = user_vm.data.setup {
        let _: mlua::Value = user_vm.call_fn(setup, ctx_lua.clone()).await.unwrap();
    }

    let payload = llm_iface::PromptPayload {
        images: Vec::new(),
        response_format: format,
        prompt: "reply with ok".to_owned(),
    };
    let payload = user_vm
        .vm
        .to_value_with(&payload, scripting::DEFAULT_LUA_SER_OPTIONS)
        .unwrap();
    let fuel = user_vm
        .vm
        .create_userdata(scripting::rat::LuaRat(num_rational::BigRational::from(
            num_bigint::BigInt::from(0),
        )))
        .unwrap();

    let res: mlua::Value = user_vm
        .call_fn(&user_vm.data.exec_prompt, (ctx_lua, payload, fuel))
        .await?;
    let table = res.as_table().unwrap();
    let data: llm_iface::PromptAnswerData =
        user_vm.vm.from_value(table.get("data").unwrap()).unwrap();
    match data {
        llm_iface::PromptAnswerData::Text(text) => Ok(text.trim().to_lowercase()),
        _ => anyhow::bail!("unexpected non-text answer variant"),
    }
}

// Higher priority is tried first; an overloaded provider falls through to the
// next in priority order; the survivor's answer is returned.
#[tokio::test]
async fn chain_order_and_overload_fallthrough() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));

    let high = spawn_fake("high", vec![503], hits.clone());
    let mid = spawn_fake("mid", vec![503], hits.clone());
    let low = spawn_fake("low", vec![200], hits.clone());

    let (config, providers) =
        build_config(&[(&high, 100, true), (&mid, 0, true), (&low, -5, true)]);

    let answer = run_prompt(&config, providers, llm_iface::OutputFormat::Text)
        .await
        .unwrap();
    assert_eq!(answer, "ok");
    assert_eq!(*hits.lock().unwrap(), vec!["high", "mid", "low"]);
}

// A non-overload user error (400) must also fall through, matching the behaviour
// of the pre-engine dispatch loop which retried on any user error.
#[tokio::test]
async fn fallthrough_on_bad_request() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));

    let high = spawn_fake("high", vec![400], hits.clone());
    let low = spawn_fake("low", vec![200], hits.clone());

    let (config, providers) = build_config(&[(&high, 10, true), (&low, 0, true)]);

    let answer = run_prompt(&config, providers, llm_iface::OutputFormat::Text)
        .await
        .unwrap();
    assert_eq!(answer, "ok");
    assert_eq!(*hits.lock().unwrap(), vec!["high", "low"]);
}

// A JSON prompt must never reach a backend that cannot produce JSON, even if it
// has the higher priority: capability filtering happens before routing.
#[tokio::test]
async fn json_capability_filtering() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));

    let no_json = spawn_fake("no_json", vec![200], hits.clone());
    let json = spawn_fake("json", vec![200], hits.clone());

    let (config, providers) = build_config(&[(&no_json, 100, false), (&json, 0, true)]);

    let answer = run_prompt(&config, providers, llm_iface::OutputFormat::JSON)
        .await
        .unwrap();
    assert_eq!(answer, "ok");
    assert_eq!(*hits.lock().unwrap(), vec!["json"]);
}
