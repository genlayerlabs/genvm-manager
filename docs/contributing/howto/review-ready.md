# Review-Ready

What a change must satisfy before review, checked against the diff and shell

## Intent

1. It does what was asked. The PR traces each requirement to evidence; testing
   the wrong thing does not count
2. Prefer one change, reviewable in one sitting. Fix defects inside the diff;
   record those outside it without folding them in or dropping them

## Proof

3. Run everything testable locally before opening a PR: pre-commit hooks, a
   debug [build](building/build.md) for code changes, and the relevant
   [test suites](testing/README.md). Use CI only for what cannot run locally
4. Map the diff to its tests and run all of them. Behaviour that can drift
   nondeterministically also warrants [agentic fuzzing](testing/fuzzing.md)

[Runner artifacts](building/runners.md) build on Linux only; report an unrun tier
as unrun, never passed

## The Artifact

5. Read the whole diff, including generated and vendored changes it caused
6. Leave no known defect, including false prose in comments, help or docs
7. Land documentation in the same PR: man page and `--help` for CLI changes,
   [spec or impl-spec](building/docs.md) for VM or protocol behaviour, and
   `docs/contributing/` for workflows
8. For applicable changes, check security exposure, diagnostics, config-schema
   compatibility and rollback

An open PR with green checks completes the list. Cross-repo E2E and merging do
not — see [pr.md](pr.md) under *Authority*
