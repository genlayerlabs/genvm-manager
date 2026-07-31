---
name: agentic-fuzzing
description: Hunts for determinism violations and internal errors in a GenVM executor by writing throwaway probe contracts and running them across leader/validator/sync. Use when asked to fuzz the VM, look for nondeterminism, or turn a PR into a failing test.
---

# Agentic fuzzing

Target the two `SECURITY.md` severities a Python contract can reach:

- **2 — determinism violation.** Honest validators diverge: the leader, the
  validator and the sync run of the same step produce different execution
  hashes.
- **4 — crash / internal error.** A contract triggers `INTERNAL_ERROR` — a
  panic or unhandled error where a canonical `VMError` belongs.

Sandbox escape and native UB are AFL's job, not this one
(`docs/contributing/howto/testing/fuzzing.md`).

## What is and is not a finding

| Outcome | Finding? |
|---|---|
| leader / validator / sync hashes disagree | **yes**, severity 2 |
| `INTERNAL_ERROR` | **yes**, severity 4 |
| WASM trap | no — that is the sandbox working |
| `UserError`, any other `VMError` | no |
| timeout | no, and they are flaky |
| mock-host error | no — that is the harness, not the VM |

## Before you can find anything

Nothing. The v0.3 line returns `save-hashes: False` from
`executors/v0.3.x/.genvm-tool.py`, but that only stops the committed `.hash`
sidecars from being tracked — the leader-vs-validator/sync comparison, which is
what a severity-2 finding consists of, runs regardless. Leave the setting alone.

(It used to be `ignore-hash: True` and it did switch off every comparison,
including that one. That was a footgun and it is gone.)

## Where to work

Probes are throwaway. Write them to
`executors/v0.3.x/tests/integration/claude/_scratch/<slug>/`, which is
gitignored, and delete them at the end of the session. Collection is a glob over
`tests/integration/**/*.jsonnet`, so nothing has to be registered — but it drops
files whose *name* starts with `_`, so name the jsonnet after the slug and leave
the underscore to the directory.

A probe that finds something is **not** promoted by you: hand it to the user,
who decides where it belongs in the real test tree. A probe that finds nothing
dies, leaving a paragraph in
`executors/v0.3.x/tests/integration/claude/intelligence/EXPLORED_PATHS.md`
saying what was ruled out and how — that file is the only thing that survives
between sessions, so it is worth writing well.

## Writing a probe

Copy the shape from `tests/integration/claude/example/`: one or more `.py`
contracts and a `.jsonnet` scenario. Templates live in `tests/templates/` —
`util.jsonnet` (`addPaths`, `chain`) structures multi-step cases,
`simple_deploy.jsonnet` covers deploy-and-call, `message.json` is the base
message.

On every step:

```jsonnet
expected_semantics_components: [],   // stdout is not what you are checking
modes: 'lvs',                        // the three runs whose hashes must agree
stable_hash: false,                  // compare against the leader's runtime hash
```

and on the top-level object `tags: ['stable']`, which tells the harness the case
needs neither LLM keys nor a webdriver. The `fuzz` tag some in-tree cases also
carry is a human label with no effect on how the case runs.

Two things about contract sources:

- The runner header is the **first line**, and the parser concatenates *every*
  leading `#` line into one JSON document (`executor/src/runners/parse.rs`). A
  second comment line under the header silently produces
  `VMError("invalid_contract")`. Put explanations below the imports.
- `# { "Depends": "py-genlayer:test" }` is the normal header. The `:test` alias
  resolves only from debug mode `unsafe` up.

## Debug mode

Cases run at `unsafe` by default. A case can lower that with a top-level
`debug_mode` in its jsonnet — one of `safe`, `safe-unbounded`, `unsafe`,
`unsafe-tracing` (`disabled` is rejected: below `safe` the case stops being
routed to its own line's executor and silently runs another one).

Lower it to `safe` when you need to be sure a divergence is the contract's and
not a debug facility's — `safe` refuses both wall-clock exposure and the `:test`
alias. Once `:test` no longer resolves the contract must name its runner by
hash, read out of `build/out/executor/<version>/data/latest.json`, e.g.
`# { "Depends": "py-genlayer:9b8kjy…" }`. That hash changes whenever the SDK
does, which is exactly why in-tree cases keep the alias and only probes give it
up.

## Running one

```bash
genvm-tool test run --filter-name 'claude/_scratch/<slug>'
```

`--filter-name` is an unanchored regex over the test name and does isolate a
single case. Artifacts land in
`build/test-artifacts/cases/<test name>/<tree_path><mode>/`, with leading
underscores stripped from the path — a probe in `_scratch/` reports under
`claude/scratch/`. `genvm.log.gz` is the executor's own log, `config.json` what
the step was given, `hash` its base64 execution hash, `stderr.txt` the guest's
traceback. There is no `semantics.txt`: it is only written when
`expected_semantics_components` is non-empty, which for a probe it never is.

### A green probe proves nothing on its own

`expected_semantics_components: []` means no output is compared, and the
leader-vs-validator comparison does not catch a load failure either: it is
identical in all three modes, so the hashes agree and the case still passes — a
probe whose contract never loaded reports `✓` in 50ms. Before believing a pass, open
`genvm.log.gz` and check the run reached the contract, and decode the `hash`
artifact — `base64 -d < hash` on a failed load reads `invalid_contract
malformed_runner`.

## Reviewing a PR for a failing test

When the ask is "find me a failing test for this branch" rather than open-ended
fuzzing:

1. Read the diff first — `git diff <base>...HEAD`. Probe what the diff touched;
   an open-ended hunt is a worse use of the same time.
2. Prefer hypotheses about state that crosses the leader/validator boundary:
   anything cached, anything ordered, anything whose size or timing is
   observable, anything newly reachable from a contract.
3. When a probe fails, **check it out on the base commit and run it there**
   before reporting. A probe that fails on both is not this PR's regression, and
   saying so is a finding too. This step is what makes the report worth reading.
4. Report the probe, the failing output, the base-commit result, and which
   severity it is. Do not fix the code.
