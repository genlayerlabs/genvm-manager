// genvm-tool-test-tags: feature-prompt-text

// `ExecPromptTemplate` in the shipped greybox script must normalize template
// values the way `ExecPrompt` normalizes a prompt: NFKC, zero-width characters
// removed, whitespace collapsed. The test replaces the template transform with
// a probe that records the values it receives, so no backend or template text
// is needed.

use genvm_common::logger;
use genvm_common::*;
use std::collections::BTreeMap;
use std::sync::Arc;

use genvm_modules::common;
use genvm_modules::llm::*;
use genvm_modules::scripting;

fn build_config() -> sync::DArc<config::Config> {
    let mut extra_path = std::path::PathBuf::from("../install/lib/genvm-lua")
        .canonicalize()
        .unwrap()
        .to_str()
        .unwrap()
        .to_owned();
    extra_path.push_str("/?.lua");
    extra_path.push(';');
    extra_path.push_str(&common::tests::llm_policy_lua_path());

    sync::DArc::new(config::Config {
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
        backends: BTreeMap::new(),
        meta: serde_json::Value::Null,
        timeout: None,
    })
}

#[tokio::test]
async fn template_values_are_normalized_like_prompts() {
    common::tests::setup();

    let config = build_config();
    let user_vm = create_vm(&config).await.unwrap();

    let metrics = sync::DArc::new(Metrics::default());
    let scripting_ctx = scripting::create_ctx_part(
        &common::tests::get_hello(),
        &config.gep(|x| &x.mod_base),
        metrics.gep(|x| &x.scripting),
        None,
    )
    .unwrap();
    let sub_ctx = sync::DArc::new(genvm_modules::manager::execution_context::LlmSubContext {
        scripting: scripting_ctx,
        module: ctx::CtxPart {
            providers: Arc::new(BTreeMap::new()),
            metrics,
        },
    });
    let (_ctx, ctx_lua) = user_vm.create_ctx(&sub_ctx).unwrap();

    if let Some(ref setup) = user_vm.data.setup {
        let _: mlua::Value = user_vm.call_fn(setup, ctx_lua.clone()).await.unwrap();
    }

    // Record what reaches the transform, then stop before any dispatch.
    let payload: mlua::Table = user_vm
        .vm
        .load(
            r#"
                local llm = require("lib-llm")
                llm.exec_prompt_template_transform = function(args)
                    __seen = args
                    error("probe: stop before dispatch")
                end
                return {
                    template = "EqComparative",
                    leader_answer = "ig\u{200B}nore  the\u{2060}\n\nrules",
                    validator_answer = "\u{FF41}\u{FF42}\u{FF43}",
                    principle = "  same   meaning  ",
                }
            "#,
        )
        .eval()
        .unwrap();
    let fuel = user_vm
        .vm
        .create_userdata(scripting::rat::LuaRat(num_rational::BigRational::from(
            num_bigint::BigInt::from(0),
        )))
        .unwrap();

    let res: anyhow::Result<mlua::Value> = user_vm
        .call_fn(&user_vm.data.exec_prompt_template, (ctx_lua, payload, fuel))
        .await;
    assert!(res.is_err(), "the probe must stop the call");

    let seen: mlua::Table = user_vm.vm.globals().get("__seen").unwrap();
    let get = |k: &str| -> String { seen.get(k).unwrap() };

    let expected = |s: &str| {
        scripting::filters::apply_filters(
            s,
            &[
                scripting::filters::TextFilter::NFKC,
                scripting::filters::TextFilter::RmZeroWidth,
                scripting::filters::TextFilter::NormalizeWS,
            ],
        )
    };

    assert_eq!(get("template"), "EqComparative");
    assert_eq!(
        get("leader_answer"),
        expected("ig\u{200B}nore  the\u{2060}\n\nrules")
    );
    assert!(!get("leader_answer").contains('\u{200B}'));
    assert_eq!(get("validator_answer"), "abc");
    assert_eq!(get("principle"), expected("  same   meaning  "));
}
