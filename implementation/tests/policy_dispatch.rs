// genvm-tool-test-tags: feature-prompt-text, feature-prompt-json

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
/// Every request body is kept in `bodies`, in arrival order.
struct FakeBackend {
    name: &'static str,
    addr: String,
    bodies: Arc<Mutex<Vec<serde_json::Value>>>,
}

async fn read_request_body(sock: &mut tokio::net::TcpStream) -> serde_json::Value {
    let mut data = Vec::new();
    let mut buf = [0u8; 4096];
    let header_end = loop {
        let n = sock.read(&mut buf).await.unwrap();
        assert!(n > 0, "connection closed before the request headers ended");
        data.extend_from_slice(&buf[..n]);
        if let Some(pos) = data.windows(4).position(|w| w == b"\r\n\r\n") {
            break pos + 4;
        }
    };
    let headers = String::from_utf8_lossy(&data[..header_end]).to_lowercase();
    let length: usize = headers
        .lines()
        .find_map(|l| l.strip_prefix("content-length:"))
        .map(|v| v.trim().parse().unwrap())
        .unwrap_or(0);
    while data.len() < header_end + length {
        let n = sock.read(&mut buf).await.unwrap();
        assert!(n > 0, "connection closed before the request body ended");
        data.extend_from_slice(&buf[..n]);
    }
    serde_json::from_slice(&data[header_end..header_end + length]).unwrap()
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
    let bodies = Arc::new(Mutex::new(Vec::new()));
    let bodies_log = bodies.clone();

    tokio::spawn(async move {
        let mut idx = 0usize;
        loop {
            let (mut sock, _) = match listener.accept().await {
                Ok(v) => v,
                Err(_) => break,
            };
            hits.lock().unwrap().push(name);
            let body = read_request_body(&mut sock).await;
            bodies_log.lock().unwrap().push(body);

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

    FakeBackend { name, addr, bodies }
}

type Providers = Arc<BTreeMap<String, Box<dyn providers::Provider + Send + Sync>>>;

/// One fake backend serving a single model named `model`.
struct BackendSpec<'a> {
    fake: &'a FakeBackend,
    priority: i64,
    supports_json: bool,
    supports_image: bool,
    model_meta: serde_json::Value,
}

impl<'a> BackendSpec<'a> {
    fn new(fake: &'a FakeBackend, priority: i64) -> Self {
        Self {
            fake,
            priority,
            supports_json: true,
            supports_image: false,
            model_meta: serde_json::Value::Null,
        }
    }

    fn image(mut self) -> Self {
        self.supports_image = true;
        self
    }

    fn model_meta(mut self, meta: serde_json::Value) -> Self {
        self.model_meta = meta;
        self
    }
}

/// Build a config + provider map from fake backends. `priority` sets
/// `meta.priority` so the dispatch chain order is deterministic and known.
fn build_config(backends: &[(&FakeBackend, i64, bool)]) -> (sync::DArc<config::Config>, Providers) {
    build_config_with_model_meta(backends, serde_json::Value::Null)
}

fn build_config_with_model_meta(
    backends: &[(&FakeBackend, i64, bool)],
    model_meta: serde_json::Value,
) -> (sync::DArc<config::Config>, Providers) {
    let specs: Vec<_> = backends
        .iter()
        .map(|(fake, priority, supports_json)| BackendSpec {
            supports_json: *supports_json,
            ..BackendSpec::new(fake, *priority).model_meta(model_meta.clone())
        })
        .collect();
    build_config_from(&specs, serde_json::Value::Null)
}

fn build_config_from(
    backends: &[BackendSpec],
    script_meta: serde_json::Value,
) -> (sync::DArc<config::Config>, Providers) {
    build_config_with_templates(
        backends,
        script_meta,
        config::PromptTemplates {
            eq_comparative: serde_json::Value::Null,
            eq_non_comparative_leader: serde_json::Value::Null,
            eq_non_comparative_validator: serde_json::Value::Null,
        },
    )
}

fn build_config_with_templates(
    backends: &[BackendSpec],
    script_meta: serde_json::Value,
    prompt_templates: config::PromptTemplates,
) -> (sync::DArc<config::Config>, Providers) {
    let mut cfg_backends = BTreeMap::new();
    let mut provider_map = BTreeMap::new();

    for spec in backends {
        let (fake, priority) = (spec.fake, spec.priority);
        let model = config::ModelConfig {
            enabled: true,
            supports_json: spec.supports_json,
            supports_image: spec.supports_image,
            use_max_completion_tokens: false,
            meta: spec.model_meta.clone(),
            timeout: None,
        };
        let backend = config::BackendConfig {
            enabled: true,
            provider: config::Provider::OpenaiCompatible,
            key: "<empty>".to_owned(),
            script_config: config::ScriptBackendConfig {
                models: BTreeMap::from([("model".to_owned(), model)]),
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
        prompt_templates,
        backends: cfg_backends,
        meta: script_meta,
        timeout: None,
    });

    (config, Arc::new(provider_map))
}

/// Run one `ExecPrompt` through the shipped dispatch script and return the text
/// answer (or the error). Fresh `Setup` per call, as a real session does.
async fn run_prompt(
    config: &sync::DArc<config::Config>,
    providers: Providers,
    format: llm_iface::OutputFormat,
) -> anyhow::Result<String> {
    run_priced_prompt(
        config,
        providers,
        format,
        None,
        primitive_types::U256::zero(),
    )
    .await
}

async fn run_priced_prompt(
    config: &sync::DArc<config::Config>,
    providers: Providers,
    format: llm_iface::OutputFormat,
    time_unit_price: Option<&str>,
    expected_charge: primitive_types::U256,
) -> anyhow::Result<String> {
    let payload = llm_iface::PromptPayload {
        images: Vec::new(),
        response_format: format,
        prompt: "reply with ok".to_owned(),
    };
    run_payload(config, providers, payload, time_unit_price, expected_charge).await
}

enum Call {
    Prompt(llm_iface::PromptPayload),
    Template(llm_iface::PromptTemplatePayload),
}

impl From<llm_iface::PromptPayload> for Call {
    fn from(p: llm_iface::PromptPayload) -> Self {
        Call::Prompt(p)
    }
}

impl From<llm_iface::PromptTemplatePayload> for Call {
    fn from(p: llm_iface::PromptTemplatePayload) -> Self {
        Call::Template(p)
    }
}

async fn run_payload(
    config: &sync::DArc<config::Config>,
    providers: Providers,
    call: impl Into<Call>,
    time_unit_price: Option<&str>,
    expected_charge: primitive_types::U256,
) -> anyhow::Result<String> {
    run_payload_as(
        config,
        providers,
        call,
        time_unit_price,
        expected_charge,
        None,
    )
    .await
}

async fn run_payload_as(
    config: &sync::DArc<config::Config>,
    providers: Providers,
    call: impl Into<Call>,
    time_unit_price: Option<&str>,
    expected_charge: primitive_types::U256,
    node_address: Option<&str>,
) -> anyhow::Result<String> {
    let user_vm = create_vm(config).await?;

    let mut hello = common::tests::get_hello();
    {
        let hello = Arc::get_mut(&mut hello).unwrap();
        if let Some(price) = time_unit_price {
            hello
                .gas_data
                .insert("genPerTimeUnit".to_owned(), price.to_owned());
        }
        if let Some(address) = node_address {
            hello.host_data.node_address = address.to_owned();
        }
    }
    let metrics = sync::DArc::new(Metrics::default());
    let scripting_ctx = scripting::create_ctx_part(
        &hello,
        &config.gep(|x| &x.mod_base),
        metrics.gep(|x| &x.scripting),
        None,
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
        let _: mlua::Value = user_vm.call_fn(setup, ctx_lua.clone()).await?;
    }

    let (entry, payload) = match call.into() {
        Call::Prompt(p) => (
            &user_vm.data.exec_prompt,
            user_vm
                .vm
                .to_value_with(&p, scripting::DEFAULT_LUA_SER_OPTIONS),
        ),
        Call::Template(p) => (
            &user_vm.data.exec_prompt_template,
            user_vm
                .vm
                .to_value_with(&p, scripting::DEFAULT_LUA_SER_OPTIONS),
        ),
    };
    let payload = payload.unwrap();
    let fuel = user_vm
        .vm
        .create_userdata(scripting::rat::LuaRat(num_rational::BigRational::from(
            num_bigint::BigInt::from(0),
        )))
        .unwrap();

    let res: mlua::Value = user_vm.call_fn(entry, (ctx_lua, payload, fuel)).await?;
    let table = res.as_table().unwrap();
    assert_eq!(
        scripting::rat::lua_rat_to_u256(table).unwrap(),
        expected_charge
    );
    let data: llm_iface::PromptAnswerData =
        user_vm.vm.from_value(table.get("data").unwrap()).unwrap();
    match data {
        llm_iface::PromptAnswerData::Text(text) => Ok(text.trim().to_lowercase()),
        _ => anyhow::bail!("unexpected non-text answer variant"),
    }
}

#[tokio::test]
async fn token_charge_rounds_up_the_total_using_rational_prices() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("priced", vec![200], hits);
    let (config, providers) = build_config(&[(&backend, 0, true)]);

    for (price, charge) in [
        ("0", 0u64),
        ("1", 1),
        ("4000", 2),
        ("4001", 3),
        ("8001/2", 3),
        ("9007199254742001", 4503599627372),
    ] {
        let answer = run_priced_prompt(
            &config,
            providers.clone(),
            llm_iface::OutputFormat::Text,
            Some(price),
            primitive_types::U256::from(charge),
        )
        .await
        .unwrap();
        assert_eq!(answer, "ok");
    }
}

#[tokio::test]
async fn model_meta_overrides_the_token_rate() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("priced", vec![200], hits);
    for (rate, charge) in [("0", 0u64), ("1/2", 4)] {
        let (config, providers) = build_config_with_model_meta(
            &[(&backend, 0, true)],
            serde_json::json!({ "time_units_per_1k_tokens": rate }),
        );
        let answer = run_priced_prompt(
            &config,
            providers,
            llm_iface::OutputFormat::Text,
            Some("4000"),
            primitive_types::U256::from(charge),
        )
        .await
        .unwrap();
        assert_eq!(answer, "ok");
    }
}

#[tokio::test]
async fn invalid_token_rates_fail_before_calling_a_provider() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("priced", vec![200], hits.clone());
    for rate in [
        serde_json::json!("-1/4"),
        serde_json::json!("invalid"),
        serde_json::json!(0.25),
        serde_json::json!(false),
    ] {
        let (config, providers) = build_config_with_model_meta(
            &[(&backend, 0, true)],
            serde_json::json!({ "time_units_per_1k_tokens": rate }),
        );
        let error = run_prompt(&config, providers, llm_iface::OutputFormat::Text)
            .await
            .unwrap_err();
        let message = format!("{error:#}");
        assert!(
            message.contains("time_units_per_1k_tokens")
                || message.contains("failed to parse rational"),
            "unexpected error: {message}"
        );
    }
    assert!(
        hits.lock().unwrap().is_empty(),
        "invalid pricing must not call a provider"
    );
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

fn tiny_png() -> bytes::Bytes {
    let img = image::RgbImage::from_fn(4, 4, |x, y| {
        image::Rgb([(x * 60) as u8, (y * 60) as u8, 128])
    });
    let mut out = Vec::new();
    image::DynamicImage::ImageRgb8(img)
        .write_to(&mut std::io::Cursor::new(&mut out), image::ImageFormat::Png)
        .unwrap();
    bytes::Bytes::from(out)
}

fn image_payload() -> llm_iface::PromptPayload {
    llm_iface::PromptPayload {
        images: vec![tiny_png()],
        response_format: llm_iface::OutputFormat::Text,
        prompt: "describe".to_owned(),
    }
}

// Once any model declares `meta.greybox`, text prompts walk the `text` chain
// (lower number first) instead of `meta.priority`, and models outside the
// chain are never tried.
#[tokio::test]
async fn greybox_text_chain_replaces_priority_order() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));

    let first = spawn_fake("first", vec![503], hits.clone());
    let second = spawn_fake("second", vec![200], hits.clone());
    let outside = spawn_fake("outside", vec![200], hits.clone());

    let (config, providers) = build_config_from(
        &[
            BackendSpec::new(&second, 100)
                .model_meta(serde_json::json!({ "greybox": { "text": 2 } })),
            BackendSpec::new(&first, 0).model_meta(serde_json::json!({ "greybox": { "text": 1 } })),
            BackendSpec::new(&outside, 1000),
        ],
        serde_json::Value::Null,
    );

    let answer = run_prompt(&config, providers, llm_iface::OutputFormat::Text)
        .await
        .unwrap();
    assert_eq!(answer, "ok");
    assert_eq!(*hits.lock().unwrap(), vec!["first", "second"]);
}

// Image prompts walk the `image` chain; a model only on the text chain is not
// tried even though it supports images.
#[tokio::test]
async fn greybox_image_chain_for_image_prompts() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));

    let text_only = spawn_fake("text_only", vec![200], hits.clone());
    let image_b = spawn_fake("image_b", vec![503], hits.clone());
    let image_a = spawn_fake("image_a", vec![200], hits.clone());

    let (config, providers) = build_config_from(
        &[
            BackendSpec::new(&text_only, 100)
                .image()
                .model_meta(serde_json::json!({ "greybox": { "text": 1 } })),
            BackendSpec::new(&image_a, 0)
                .image()
                .model_meta(serde_json::json!({ "greybox": { "text": 2, "image": 2 } })),
            BackendSpec::new(&image_b, 0)
                .image()
                .model_meta(serde_json::json!({ "greybox": { "image": 1 } })),
        ],
        serde_json::Value::Null,
    );

    let answer = run_payload(
        &config,
        providers,
        image_payload(),
        None,
        primitive_types::U256::zero(),
    )
    .await
    .unwrap();
    assert_eq!(answer, "ok");
    assert_eq!(*hits.lock().unwrap(), vec!["image_b", "image_a"]);
}

// A greybox chain with nothing usable for the prompt fails instead of falling
// back to models outside the chain.
#[tokio::test]
async fn greybox_without_a_usable_chain_entry_fails() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));

    let text_only = spawn_fake("text_only", vec![200], hits.clone());
    let outside = spawn_fake("outside", vec![200], hits.clone());

    let (config, providers) = build_config_from(
        &[
            BackendSpec::new(&text_only, 0)
                .model_meta(serde_json::json!({ "greybox": { "text": 1 } })),
            BackendSpec::new(&outside, 0).image(),
        ],
        serde_json::Value::Null,
    );

    let error = run_payload(
        &config,
        providers,
        image_payload(),
        None,
        primitive_types::U256::zero(),
    )
    .await
    .unwrap_err();
    assert!(
        format!("{error:#}").contains("NO_PROVIDER_FOR_PROMPT"),
        "unexpected error: {error:#}"
    );
    assert!(hits.lock().unwrap().is_empty());
}

#[tokio::test]
async fn malformed_greybox_meta_is_rejected_before_any_call() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("backend", vec![200], hits.clone());

    for meta in [
        serde_json::json!({ "greybox": 1 }),
        serde_json::json!({ "greybox": { "text": "first" } }),
    ] {
        let (config, providers) = build_config_from(
            &[BackendSpec::new(&backend, 0).model_meta(meta)],
            serde_json::Value::Null,
        );
        let error = run_prompt(&config, providers, llm_iface::OutputFormat::Text)
            .await
            .unwrap_err();
        assert!(
            format!("{error:#}").contains("meta.greybox"),
            "unexpected error: {error:#}"
        );
    }
    assert!(hits.lock().unwrap().is_empty());
}

// Every call carries its own random sampling seed, so validators asking the
// same model the same question still sample independently.
#[tokio::test]
async fn each_call_sends_a_fresh_seed() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("backend", vec![200], hits);
    let (config, providers) = build_config(&[(&backend, 0, true)]);

    for _ in 0..2 {
        let answer = run_prompt(&config, providers.clone(), llm_iface::OutputFormat::Text)
            .await
            .unwrap();
        assert_eq!(answer, "ok");
    }

    let bodies = backend.bodies.lock().unwrap();
    let seeds: Vec<u64> = bodies
        .iter()
        .map(|b| {
            b["seed"]
                .as_u64()
                .unwrap_or_else(|| panic!("no seed in {b}"))
        })
        .collect();
    assert_eq!(seeds.len(), 2);
    assert_ne!(seeds[0], seeds[1]);
}

fn body_temperature(body: &serde_json::Value) -> f64 {
    body["temperature"]
        .as_f64()
        .unwrap_or_else(|| panic!("no temperature in {body}"))
}

fn body_images(body: &serde_json::Value) -> Vec<String> {
    body["messages"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|m| m["content"].as_array())
        .flatten()
        .filter_map(|c| c["image_url"]["url"].as_str().map(str::to_owned))
        .collect()
}

async fn prompt_bodies(
    script_meta: serde_json::Value,
    call: impl Fn() -> Call,
    times: usize,
) -> Vec<serde_json::Value> {
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("backend", vec![200], hits);
    let (config, providers) = build_config_with_templates(
        &[BackendSpec::new(&backend, 0).image()],
        script_meta,
        test_templates(),
    );
    for _ in 0..times {
        run_payload(
            &config,
            providers.clone(),
            call(),
            None,
            primitive_types::U256::zero(),
        )
        .await
        .unwrap();
    }
    let bodies = backend.bodies.lock().unwrap().clone();
    bodies
}

fn test_templates() -> config::PromptTemplates {
    config::PromptTemplates {
        eq_comparative: serde_json::Value::Null,
        eq_non_comparative_leader: serde_json::json!({
            "system": "\u{FF53}\u{FF59}\u{FF53}",
            "user": "\u{FF55}\u{FF53}\u{FF45}\u{FF52} #{task}",
        }),
        eq_non_comparative_validator: serde_json::Value::Null,
    }
}

fn text_call() -> Call {
    llm_iface::PromptPayload {
        images: Vec::new(),
        response_format: llm_iface::OutputFormat::Text,
        prompt: "reply with ok".to_owned(),
    }
    .into()
}

fn template_call() -> Call {
    llm_iface::PromptTemplatePayload::EqNonComparativeLeader(
        llm_iface::PromptEqNonComparativeLeaderPayload {
            task: "x".to_owned(),
            criteria: "c".to_owned(),
            input: "i".to_owned(),
        },
    )
    .into()
}

#[tokio::test]
async fn without_mutate_the_request_is_unchanged() {
    common::tests::setup();

    let bodies = prompt_bodies(serde_json::Value::Null, text_call, 1).await;
    assert!((body_temperature(&bodies[0]) - 0.7).abs() < 1e-6);

    let bodies = prompt_bodies(serde_json::Value::Null, || image_payload().into(), 1).await;
    let expected = format!(
        "data:image/png;base64,{}",
        base64::Engine::encode(&base64::prelude::BASE64_STANDARD, tiny_png())
    );
    assert_eq!(body_images(&bodies[0]), vec![expected]);

    let bodies = prompt_bodies(serde_json::Value::Null, template_call, 1).await;
    let text = bodies[0].to_string();
    assert!(text.contains("\u{FF53}\u{FF59}\u{FF53}"), "{text}");
}

// `meta.mutate` jitters temperature around the prompt's own value, seeded by the
// per-call seed, so two calls sample at different temperatures.
#[tokio::test]
async fn mutate_jitters_temperature_per_call() {
    common::tests::setup();

    let meta = serde_json::json!({ "mutate": { "jitter": { "temperature": 0.3 } } });
    let bodies = prompt_bodies(meta, text_call, 2).await;
    let temps: Vec<f64> = bodies.iter().map(body_temperature).collect();
    for t in &temps {
        assert!(
            (0.4 - 1e-6..=1.0 + 1e-6).contains(t),
            "temperature {t} out of 0.7 ± 0.3"
        );
    }
    assert_ne!(temps[0], temps[1]);
}

// A mutated temperature never goes below what providers accept.
#[tokio::test]
async fn mutated_temperature_is_floored_at_zero() {
    common::tests::setup();

    let meta = serde_json::json!({ "mutate": { "set_param": { "temperature": -1 } } });
    let bodies = prompt_bodies(meta, text_call, 1).await;
    assert_eq!(body_temperature(&bodies[0]), 0.0);
}

#[tokio::test]
async fn mutate_filters_images() {
    common::tests::setup();

    let meta = serde_json::json!({ "mutate": { "filter_image": [{ "GuassianNoise": 1.0 }] } });
    let bodies = prompt_bodies(meta, || image_payload().into(), 1).await;
    let images = body_images(&bodies[0]);
    let original = format!(
        "data:image/png;base64,{}",
        base64::Engine::encode(&base64::prelude::BASE64_STANDARD, tiny_png())
    );
    assert_eq!(images.len(), 1);
    assert_ne!(images[0], original);
}

// Text filters reach what the contract does not control too: template text.
#[tokio::test]
async fn mutate_filters_prompt_text() {
    common::tests::setup();

    let meta = serde_json::json!({ "mutate": { "filter_text": ["NFKC"] } });
    let bodies = prompt_bodies(meta, template_call, 1).await;
    let text = bodies[0].to_string();
    assert!(text.contains("\"sys\""), "{text}");
    assert!(text.contains("user x"), "{text}");
    assert!(!text.contains('\u{FF53}'), "{text}");
}

#[tokio::test]
async fn invalid_mutate_is_rejected_at_load() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("backend", vec![200], hits.clone());

    let (config, _providers) = build_config_from(
        &[BackendSpec::new(&backend, 0)],
        serde_json::json!({ "mutate": { "no_such_op": 1 } }),
    );
    let error = match create_vm(&config).await {
        Ok(_) => panic!("an invalid meta.mutate must fail when the script loads"),
        Err(e) => e,
    };
    assert!(
        format!("{error:#}").contains("mutate"),
        "unexpected error: {error:#}"
    );
    assert!(hits.lock().unwrap().is_empty());
}

fn decode_data_url(url: &str) -> image::RgbImage {
    let b64 = url.split_once(";base64,").unwrap().1;
    let raw = base64::Engine::decode(&base64::prelude::BASE64_STANDARD, b64).unwrap();
    image::load_from_memory(&raw).unwrap().to_rgb8()
}

async fn load_error(script_meta: serde_json::Value, model_meta: serde_json::Value) -> String {
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("backend", vec![200], hits);
    let (config, _providers) = build_config_from(
        &[BackendSpec::new(&backend, 0).model_meta(model_meta)],
        script_meta,
    );
    match create_vm(&config).await {
        Ok(_) => panic!("script load must fail"),
        Err(e) => format!("{e:#}"),
    }
}

#[tokio::test]
async fn invalid_mutate_recipes_and_params_are_rejected_at_load() {
    common::tests::setup();
    for mutate in [
        serde_json::json!({ "filter_text": ["NoSuch"] }),
        serde_json::json!({ "filter_text": [{ "GuassianNoise": 1 }] }),
        serde_json::json!({ "filter_image": ["NFKC"] }),
        serde_json::json!({ "filter_image": [{ "Denoise": 1 }] }),
        serde_json::json!({ "pipe": [{ "filter_image": [{ "JPEG": "x" }] }] }),
        serde_json::json!({ "jitter": { "seed": 3 } }),
        serde_json::json!({ "set_param": { "max_tokens": 10 } }),
        serde_json::json!({ "set_param": { "temperature": "hot" } }),
        serde_json::json!({ "set_param": { "temperature": 2 }, "clamp": { "temperature": 1 } }),
        serde_json::json!({ "pipe": [{ "jitter": { "temperature": 1 }, "filter_text": ["NFKC"] }] }),
        serde_json::json!(false),
    ] {
        let error = load_error(
            serde_json::json!({ "mutate": mutate.clone() }),
            serde_json::Value::Null,
        )
        .await;
        assert!(error.contains("meta.mutate"), "{mutate}: {error}");
    }
}

#[tokio::test]
async fn malformed_greybox_tables_are_rejected_at_load() {
    common::tests::setup();
    for greybox in [
        serde_json::json!({}),
        serde_json::json!({ "txt": 1 }),
        serde_json::json!([1]),
        serde_json::json!(false),
    ] {
        let error = load_error(
            serde_json::Value::Null,
            serde_json::json!({ "greybox": greybox.clone() }),
        )
        .await;
        assert!(error.contains("meta.greybox"), "{greybox}: {error}");
    }
}

fn many_models(count: usize) -> Vec<FakeBackend> {
    let hits = Arc::new(Mutex::new(Vec::new()));
    (0..count)
        .map(|i| spawn_fake(format!("b{i:03}").leak(), vec![200], hits.clone()))
        .collect()
}

#[tokio::test]
async fn catalog_at_the_chain_limit_loads() {
    common::tests::setup();
    let fakes = many_models(64);
    let specs: Vec<_> = fakes
        .iter()
        .map(|f| BackendSpec::new(f, 0).model_meta(serde_json::json!({ "greybox": { "text": 1 } })))
        .collect();
    let (config, _providers) = build_config_from(&specs, serde_json::Value::Null);
    if let Err(e) = create_vm(&config).await {
        panic!("64 models must load: {e:#}");
    }
}

#[tokio::test]
async fn catalog_over_the_chain_limit_names_the_limit() {
    common::tests::setup();
    let fakes = many_models(65);
    let specs: Vec<_> = fakes.iter().map(|f| BackendSpec::new(f, 0)).collect();
    let (config, _providers) = build_config_from(&specs, serde_json::Value::Null);
    let error = match create_vm(&config).await {
        Ok(_) => panic!("65 models must fail to load"),
        Err(e) => format!("{e:#}"),
    };
    assert!(
        error.contains("65 models") && error.contains("64"),
        "{error}"
    );
}

// An image the filter cannot decode is the contract's fault: a user error, not
// an internal one, and no provider is called.
#[tokio::test]
async fn undecodable_image_under_filter_is_a_user_error() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("backend", vec![200], hits.clone());
    let (config, providers) = build_config_from(
        &[BackendSpec::new(&backend, 0).image()],
        serde_json::json!({ "mutate": { "filter_image": [{ "JPEG": 0.5 }] } }),
    );
    let mut truncated = tiny_png().to_vec();
    truncated.truncate(30);
    let payload = llm_iface::PromptPayload {
        images: vec![bytes::Bytes::from(truncated)],
        response_format: llm_iface::OutputFormat::Text,
        prompt: "describe".to_owned(),
    };
    let error = run_payload(
        &config,
        providers,
        payload,
        None,
        primitive_types::U256::zero(),
    )
    .await
    .unwrap_err();
    let message = format!("{error:#}");
    assert!(
        message.contains(r#"{"causes":["INVALID_IMAGE"],"fatal":false"#),
        "unexpected error: {message}"
    );
    assert!(hits.lock().unwrap().is_empty());
}

// Each attempt filters the original image once: the retry sends exactly what
// the first attempt sent, and the pixels really changed.
#[tokio::test]
async fn retries_send_the_same_once_filtered_image() {
    common::tests::setup();
    let hits = Arc::new(Mutex::new(Vec::new()));
    let first = spawn_fake("first", vec![503], hits.clone());
    let second = spawn_fake("second", vec![200], hits.clone());
    let (config, providers) = build_config_from(
        &[
            BackendSpec::new(&first, 10).image(),
            BackendSpec::new(&second, 0).image(),
        ],
        serde_json::json!({ "mutate": { "filter_image": [{ "GuassianNoise": 1.0 }] } }),
    );
    run_payload(
        &config,
        providers,
        image_payload(),
        None,
        primitive_types::U256::zero(),
    )
    .await
    .unwrap();

    let a = body_images(&first.bodies.lock().unwrap()[0]);
    let b = body_images(&second.bodies.lock().unwrap()[0]);
    assert_eq!(a, b);
    let original = image::load_from_memory(&tiny_png()).unwrap().to_rgb8();
    assert_ne!(decode_data_url(&a[0]), original);
}

#[tokio::test]
async fn mutate_applies_on_greybox_chains() {
    common::tests::setup();
    let meta = serde_json::json!({ "mutate": { "set_param": { "temperature": 0.1 } } });
    let hits = Arc::new(Mutex::new(Vec::new()));
    let backend = spawn_fake("backend", vec![200], hits);
    let (config, providers) = build_config_from(
        &[BackendSpec::new(&backend, 0)
            .model_meta(serde_json::json!({ "greybox": { "text": 1 } }))],
        meta,
    );
    run_prompt(&config, providers, llm_iface::OutputFormat::Text)
        .await
        .unwrap();
    let t = body_temperature(&backend.bodies.lock().unwrap()[0]);
    assert!((t - 0.1).abs() < 1e-6, "{t}");
}

fn tie_key(node_address: &str, family: &str) -> Vec<u8> {
    let input = format!("{node_address}/{family}");
    ring::digest::digest(&ring::digest::SHA256, input.as_bytes())
        .as_ref()
        .to_vec()
}

// Equal greybox numbers are ordered by sha256("<node address>/<provider>/<model>"):
// fixed for one node, different across nodes. Strict numbers still win.
#[tokio::test]
async fn greybox_ties_order_by_node_address_hash() {
    common::tests::setup();
    let tie = serde_json::json!({ "greybox": { "text": 2 } });

    let mut orders = std::collections::BTreeSet::new();
    for i in 0..16 {
        let node = format!("0x{i:040x}");
        let hits = Arc::new(Mutex::new(Vec::new()));
        let first = spawn_fake("first", vec![503], hits.clone());
        let b = spawn_fake("b", vec![503], hits.clone());
        let a = spawn_fake("a", vec![503], hits.clone());
        let (config, providers) = build_config_from(
            &[
                BackendSpec::new(&first, 0)
                    .model_meta(serde_json::json!({ "greybox": { "text": 1 } })),
                BackendSpec::new(&b, 0).model_meta(tie.clone()),
                BackendSpec::new(&a, 0).model_meta(tie.clone()),
            ],
            serde_json::Value::Null,
        );

        let mut tied = vec!["a", "b"];
        tied.sort_by_key(|p| tie_key(&node, &format!("{p}/model")));
        let mut expected = vec!["first"];
        expected.extend(&tied);

        for _ in 0..2 {
            hits.lock().unwrap().clear();
            let _ = run_payload_as(
                &config,
                providers.clone(),
                text_call(),
                None,
                primitive_types::U256::zero(),
                Some(&node),
            )
            .await;
            assert_eq!(*hits.lock().unwrap(), expected, "node {node}");
        }
        orders.insert(tied);
    }
    assert_eq!(orders.len(), 2, "16 addresses must produce both tie orders");
}
