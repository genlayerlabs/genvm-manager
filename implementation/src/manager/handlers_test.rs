use super::*;

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
