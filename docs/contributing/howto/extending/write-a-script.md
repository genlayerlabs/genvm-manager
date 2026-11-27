# Writing a Script

1. Prefer Python over Bash, with `argparse` for the CLI
2. Integrate into `genvm-tool` or `support/ci` when it fits, rather than adding
   a loose entry point. A standalone script goes to `support/scripts/`,
   executable and pure-stdlib, so it runs without the dev shell
3. If the script checks something, print every rejection reason to `stdout`, so
   a failure needs no further digging

## Wiring a Check as a Pre-Commit Hook

Hooks are declared in `flake.nix`; the committed `.pre-commit-config.yaml` is
generated from it by `git-hooks.nix`, so never edit it by hand. Add an entry
under the local-guards block:

```nix
check-source-text = {
  enable = true;
  name = "check-source-text";
  entry = "${hooks-python}/bin/python3 support/scripts/check-source-text.py";
  files = "\\.rs$";        # narrow to the file types you check
};
```

pre-commit passes the changed files as arguments, so take them as an `argparse`
positional; exit non-zero to reject the commit, printing each rejection in a
greppable form, ideally `path:lineno: <offending line>`.
`support/scripts/check-source-text.py` is a worked example, including a
per-file `check-source-text: off` opt-out

## Guards Shared With a Submodule

A commit inside an executor submodule runs that repo's own hooks from its own
checkout — it cannot reach into the umbrella. A guard needed in both places
keeps a copy under the submodule's own `support/scripts/`, wired from its
`flake.nix`; keep the copies in sync, and diff them when you touch either
