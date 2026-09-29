// `exec_prompt_template_transform` in lib-llm.lua fills the equivalence
// principle templates with values that come from other validators (the
// leader's answer, its output). A value must land in the prompt verbatim: it
// may not be rescanned for placeholders, and the result may not depend on the
// iteration order of the variables.

use mlua::prelude::*;

const TEMPLATE: &str = "<primary_output>#{leader_answer}</primary_output>\
<validator>#{validator_answer}</validator><rule>#{principle}</rule>#{unknown}";

fn load_lib_llm() -> Lua {
    let vm = Lua::new();
    let lib_dir = std::path::PathBuf::from("../install/lib/genvm-lua")
        .canonicalize()
        .unwrap();
    let package: LuaTable = vm.globals().get("package").unwrap();
    package
        .set("path", format!("{}/?.lua", lib_dir.display()))
        .unwrap();

    // Stand-ins for the globals the Rust side installs before the scripts load.
    vm.load("__dflt = { log_json = function() end }")
        .exec()
        .unwrap();
    let templates = vm.create_table().unwrap();
    let eq = vm.create_table().unwrap();
    eq.set("system", "system").unwrap();
    eq.set("user", TEMPLATE).unwrap();
    templates.set("eq_comparative", eq).unwrap();
    let llm = vm.create_table().unwrap();
    llm.set("templates", templates).unwrap();
    llm.set("providers", vm.create_table().unwrap()).unwrap();
    vm.globals().set("__llm", llm).unwrap();
    vm
}

fn transform(vm: &Lua, leader_answer: &str) -> String {
    let code = r#"
        local llm = require("lib-llm")
        return function(leader_answer)
            local mapped = llm.exec_prompt_template_transform({
                template = "EqComparative",
                leader_answer = leader_answer,
                validator_answer = "VALIDATOR",
                principle = "identical",
            })
            return mapped.prompt.user_message
        end
    "#;
    let f: LuaFunction = vm.load(code).eval().unwrap();
    f.call(leader_answer).unwrap()
}

#[test]
fn placeholders_are_substituted_once() {
    let vm = load_lib_llm();
    assert_eq!(
        transform(&vm, "LEADER"),
        "<primary_output>LEADER</primary_output><validator>VALIDATOR</validator>\
<rule>identical</rule>#{unknown}"
    );
}

// The leader's answer contains another placeholder. Whatever order `pairs`
// visits the variables in, it must reach the judge as the literal text.
#[test]
fn values_are_not_rescanned_for_placeholders() {
    for _ in 0..16 {
        let vm = load_lib_llm();
        let out = transform(&vm, "#{validator_answer}");
        assert_eq!(
            out,
            "<primary_output>#{validator_answer}</primary_output>\
<validator>VALIDATOR</validator><rule>identical</rule>#{unknown}"
        );
    }
}

#[test]
fn values_with_pattern_characters_are_literal() {
    let vm = load_lib_llm();
    let out = transform(&vm, "100% sure, %1 and %%");
    assert!(
        out.starts_with("<primary_output>100% sure, %1 and %%</primary_output>"),
        "{out}"
    );
}
