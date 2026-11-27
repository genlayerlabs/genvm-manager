# Adding an LLM Provider

An OpenAI-API-compatible provider needs no work at all — configure it as
`openai-compatible`

Otherwise:

1. `implementation/src/llm/providers.rs` — declare the provider and
   `impl Provider` for it; a separate json mode is recommended
2. `implementation/src/llm/config.rs` — add a `Provider` enum value and the
   matching `BackendConfig::to_provider` arm
3. `docs/schemas/default-config.json` — add the id to the provider enum

Testing is optional: add a case to `implementation/src/llm/handler.rs`, pass the
API-key secret in `.github/workflows/queue_test_cell.yaml`, and hand the key to
the repository owners
