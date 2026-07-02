# Adding an LLM provider

If the provider is OpenAI-API-compatible, no work is needed.

Otherwise (all in the manager's `implementation/src/llm/`):

1. `providers.rs` — declare and implement the provider (`impl Provider`);
   a separate json mode is recommended.
2. `config.rs` — add a `Provider` enum value and a `BackendConfig::to_provider`
   case.

Test (optional): add a case to `implementation/src/llm/handler.rs`, patch
`.github/workflows/queue.yaml` to pass the API-key secret, and provide the key
to the repository owners.
