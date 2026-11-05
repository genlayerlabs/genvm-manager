use super::*;

const LINE: &str = "genvm/executor/v0.3";
const FIX: &str = "genvm/executor/v0.3/fix";

fn at(s: &str) -> chrono::DateTime<chrono::Utc> {
    s.parse().unwrap()
}

/// A line and a behaviour fork inside it, scheduled on network `net`.
fn schedule(line: &str, fix: &str, fix_requires: &str) -> String {
    format!(
        r#"{{
            "forks": {{
                "{LINE}": {{}},
                "{FIX}": {{ "requires": [{fix_requires}], "description": "free text" }}
            }},
            "networks": {{ "net": {{ "forks": {{ "{LINE}": "{line}", "{FIX}": "{fix}" }} }} }}
        }}"#
    )
}

fn parse_err(json: &str) -> String {
    Schedule::parse(json).unwrap_err().to_string()
}

#[test]
fn the_shipped_schedule_is_valid() {
    let schedule = Schedule::parse(
        &std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../support/genvm-chainforks.json"
        ))
        .unwrap(),
    )
    .unwrap();

    for network in ["dev", "studio", "bradbury-phase2", "asimov-phase5"] {
        schedule.check_network(network).unwrap();
    }
    let dev = schedule.active("dev", at("1970-01-01T00:00:00Z")).unwrap();
    assert_eq!(dev.to_string(), "genvm/executor/v0.2,genvm/executor/v0.3");
}

#[test]
fn a_dated_fork_activates_at_its_date_inclusive() {
    let schedule = Schedule::parse(&schedule(
        "2025-01-01T00:00:00Z",
        "never",
        &format!("\"{LINE}\""),
    ))
    .unwrap();

    assert!(!schedule
        .active("net", at("2024-12-31T23:59:59Z"))
        .unwrap()
        .contains(LINE));
    let active = schedule.active("net", at("2025-01-01T00:00:00Z")).unwrap();
    assert!(active.contains(LINE));
    assert!(!active.contains(FIX));
    assert_eq!(active.to_string(), LINE);
}

#[test]
fn builtins_are_answered_without_being_listed() {
    let schedule = Schedule::parse(&schedule("never", "never", &format!("\"{LINE}\""))).unwrap();
    let active = schedule.active("net", at("2025-01-01T00:00:00Z")).unwrap();

    assert!(active.contains(ALWAYS));
    assert!(!active.contains(NEVER));
    assert_eq!(active.to_string(), "");
}

#[test]
fn an_unknown_network_is_an_error() {
    let schedule = Schedule::parse(&schedule("always", "always", &format!("\"{LINE}\""))).unwrap();

    let err = schedule
        .active("other", at("2025-01-01T00:00:00Z"))
        .unwrap_err();
    assert!(err.to_string().contains("unknown network `other`"), "{err}");
}

#[test]
fn a_fork_cannot_activate_before_its_requirement() {
    let err = parse_err(&schedule("never", "always", &format!("\"{LINE}\"")));
    assert!(err.contains("before its requirement"), "{err}");

    let err = parse_err(&schedule(
        "2025-01-02T00:00:00Z",
        "2025-01-01T00:00:00Z",
        &format!("\"{LINE}\""),
    ));
    assert!(err.contains("before its requirement"), "{err}");
}

#[test]
fn a_fork_extending_another_must_require_it() {
    let err = parse_err(&schedule("always", "always", ""));
    assert!(err.contains("extends `genvm/executor/v0.3`"), "{err}");
}

#[test]
fn a_requirement_must_be_defined_and_acyclic() {
    let err = parse_err(&schedule("always", "always", "\"genvm/other\""));
    assert!(err.contains("undefined fork `genvm/other`"), "{err}");

    let err = parse_err(
        r#"{ "forks": { "a": { "requires": ["b"] }, "b": { "requires": ["a"] } }, "networks": {} }"#,
    );
    assert!(err.contains("requires itself"), "{err}");
}

#[test]
fn every_network_schedules_exactly_the_defined_forks() {
    let err = parse_err(&format!(
        r#"{{ "forks": {{ "{LINE}": {{}} }}, "networks": {{ "net": {{ "forks": {{}} }} }} }}"#
    ));
    assert!(err.contains("does not schedule fork"), "{err}");

    let err = parse_err(&format!(
        r#"{{ "forks": {{}}, "networks": {{ "net": {{ "forks": {{ "{LINE}": "always" }} }} }} }}"#
    ));
    assert!(err.contains("schedules undefined fork"), "{err}");
}

#[test]
fn builtin_and_malformed_names_cannot_be_defined() {
    for name in [ALWAYS, NEVER] {
        let err = parse_err(&format!(
            r#"{{ "forks": {{ "{name}": {{}} }}, "networks": {{}} }}"#
        ));
        assert!(err.contains("built-in"), "{err}");
    }
    for name in ["", "Genvm", "a//b", "a/", "a,b", "a b", "a|b"] {
        let err = parse_err(&format!(
            r#"{{ "forks": {{ "{name}": {{}} }}, "networks": {{}} }}"#
        ));
        assert!(err.contains("malformed fork name"), "{name:?}: {err}");
    }
}

#[test]
fn unknown_and_duplicate_keys_are_rejected() {
    let err = parse_err(r#"{ "forks": {}, "networks": {}, "extra": 1 }"#);
    assert!(err.contains("unknown field"), "{err}");

    let err = parse_err(r#"{ "forks": {}, "networks": { "net": { "forks": {}, "dates": {} } } }"#);
    assert!(err.contains("unknown field"), "{err}");

    let err =
        parse_err(r#"{ "forks": { "a": { "requires": [], "after": "x" } }, "networks": {} }"#);
    assert!(err.contains("unknown field"), "{err}");

    let err = parse_err(r#"{ "forks": { "a": {}, "a": {} }, "networks": {} }"#);
    assert!(err.contains("duplicate key `a`"), "{err}");
}

#[test]
fn a_date_must_be_rfc3339_or_a_builtin() {
    let err = parse_err(&schedule("2025-01-01", "never", &format!("\"{LINE}\"")));
    assert!(err.contains("RFC 3339"), "{err}");
}

#[test]
fn dates_chrono_alone_would_accept_are_rejected() {
    for date in [
        "2025-01-01t00:00:00Z",
        "2025-01-01 00:00:00Z",
        "2025-01-01T00:00:00z",
        "2025-01-01T00:00:60Z",
    ] {
        let err = parse_err(&schedule(date, "never", &format!("\"{LINE}\"")));
        assert!(err.contains("RFC 3339"), "{date}: {err}");
    }
}

#[test]
fn an_offset_is_normalized_and_equal_dates_are_ordered() {
    let schedule = Schedule::parse(&schedule(
        "2025-01-01T02:00:00+02:00",
        "2025-01-01T00:00:00Z",
        &format!("\"{LINE}\""),
    ))
    .unwrap();

    let active = schedule.active("net", at("2025-01-01T00:00:00Z")).unwrap();
    assert!(active.contains(LINE) && active.contains(FIX), "{active}");
    assert!(!schedule
        .active("net", at("2024-12-31T23:59:59Z"))
        .unwrap()
        .contains(LINE));
}

#[test]
fn wrong_types_are_rejected() {
    let err = parse_err(r#"{ "forks": { "a": { "requires": "b" } }, "networks": {} }"#);
    assert!(err.contains("malformed chainforks schedule"), "{err}");

    let err =
        parse_err(r#"{ "forks": { "a": {} }, "networks": { "n": { "forks": { "a": 1 } } } }"#);
    assert!(err.contains("malformed chainforks schedule"), "{err}");
}

#[test]
fn a_null_description_is_accepted() {
    Schedule::parse(r#"{ "forks": { "a": { "description": null } }, "networks": {} }"#).unwrap();
}
