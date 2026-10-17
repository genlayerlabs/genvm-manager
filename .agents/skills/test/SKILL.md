---
name: test
description: Runs tests for the GenVM project. Use after making code changes to verify correctness.
---

See docs/contributing/howto/testing/README.md.

Invoke the tool as:

```
nix run '.?submodules=1#genvm-tool' -- test run --filter-tag '!fuzz' ...
```

A bare `genvm-tool` from `PATH` is a nix store copy pinned at dev-shell build
time; it goes stale against the working tree and fails with import errors such
as `ModuleNotFoundError: No module named 'genvm_tool.tests.exec.process'`.
`build/genvm_tool.sh` has the same problem — it bakes the store path in.

After fixing a failure, rerun with `--filter-continue <file>` (path printed in
the failure summary) before any full rerun — see the "Fix–rerun loop" section
there.
