use super::*;

#[test]
fn status_wait_can_be_absent() {
    let req = StatusRequest { wait: None };

    assert_eq!(parse_status_wait(&req).unwrap(), None);
}

#[test]
fn status_wait_parses_duration() {
    let req = StatusRequest {
        wait: Some("30s".to_owned()),
    };

    assert_eq!(
        parse_status_wait(&req).unwrap(),
        Some(std::time::Duration::from_secs(30))
    );
}

#[test]
fn status_wait_is_clamped() {
    let req = StatusRequest {
        wait: Some("5m".to_owned()),
    };

    assert_eq!(parse_status_wait(&req).unwrap(), Some(STATUS_WAIT_CAP));
}

#[test]
fn status_wait_rejects_malformed_duration() {
    let req = StatusRequest {
        wait: Some("soon".to_owned()),
    };

    assert!(parse_status_wait(&req).is_err());
}

#[test]
fn availability_check_resolves_the_key_from_the_environment() {
    // callers send `${ENV[NAME]}` rather than the secret itself; an unresolved
    // one expands to an empty string instead of failing, so every provider
    // would silently report itself unavailable
    let path = std::env::var("PATH").unwrap();

    let backend = build_check_backend(&LlmProviderConfig {
        host: "http://127.0.0.1:0".to_owned(),
        provider: llm::config::Provider::OpenaiCompatible,
        model: "some-model".to_owned(),
        key: "${ENV[PATH]}".to_owned(),
    })
    .unwrap();

    assert_eq!(backend.key, path);
}
