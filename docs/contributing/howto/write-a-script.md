# Writing a script

1. Prefer python over bash.
2. Use `argparse` for the CLI.
3. Integrate into `genvm-tool` or `support/ci` when it fits, rather than adding a loose entry point.
4. If the script checks something, print each rejection reason to `stdout`, so a failure needs no further digging.

## Where it lives

Standalone scripts go in `support/scripts/`. Keep them executable
(`chmod +x`) and pure-stdlib unless there is a strong reason otherwise, so they
run without the dev shell.

## Wiring a check as a pre-commit hook

Hooks are declared in `flake.nix` (the committed `.pre-commit-config.yaml` is
generated from it by `git-hooks.nix` on `nix develop` / `nix flake check` -- do
not edit it by hand). Add an entry under the local-guards block:

```nix
check-source-text = {
  enable = true;
  name = "check-source-text";
  entry = "${hooks-python}/bin/python3 support/scripts/check-source-text.py";
  files = "\\.rs$";        # narrow to the file types you check
};
```

pre-commit passes the changed files as arguments (`argparse` positional
`files`). Exit non-zero to reject the commit; per point 4, print each rejection
to stdout in a greppable form, ideally `path:lineno: <offending line>`. See
`support/scripts/check-source-text.py` for a worked example (it also supports a
per-file `check-source-text: off` opt-out comment).

## Scripts shared with a submodule

Each executor submodule (`executors/vD.D.x`) is a separate git repo with its own
pre-commit config, and a commit made inside it runs those hooks from its own
checkout -- it cannot reach into this umbrella. (The submodule is not built
standalone; the umbrella drives its build. This is only about its independent
git hooks.) So a guard that must run in both places keeps an in-tree copy under
the submodule's own `support/scripts/`, wired from the submodule's `flake.nix`.
Keep the two copies identical.
