# Your first contribution: patching an executor

End-to-end walkthrough for the most common change — fixing something inside an
executor line (`executors/<line>.x`). Each step links to the how-to guide with
the full details; read those when a step surprises you.

## 1. Set up the checkout

Follow [setup.md](../howto/setup.md): materialize submodules and vendored trees
(`python3 support/scripts/get-all-git.py`), then enter the dev shell
(`nix develop '.?submodules=1#full'`, usually auto-loaded via direnv).

## 2. Make and verify the change

Edit inside `executors/<line>.x/`, then build and test:

```bash
genvm-tool configure && ninja -C build all/bin        # building/build.md
genvm-tool test run --filter-name 'executors/<line>.x/'  # testing/README.md
```

## 3. Create the branches

```bash
genvm-tool git create-branches feat/<name>
```

It surveys which repos carry new content and creates the branch in the ones
you tick: the manager gets `feat/<name>` verbatim, each executor gets
`pr/<line>/feat/<name>` (all lines push to one shared executor remote, hence
the namespace). Branch model:
[submodules.md](../howto/committing/submodules.md).

## 4. Commit, then update the gitlink

The manager pins each submodule by a gitlink (a pinned commit), so the order
matters ([submodules.md](../howto/committing/submodules.md)):

1. Commit **inside** the executor: `cd executors/<line>.x && git add … && git commit`.
2. In the manager, stage the moved gitlink: `git add executors/<line>.x`.
3. Commit the manager (gitlink bump + any manager-side changes together).

If you touched runners, do the hash hygiene first:
[committing/runners.md](../howto/committing/runners.md).

## 5. Pass pre-commit

The dev shell installs each repo's pre-commit hooks, so `git commit` already
runs them where you commit — do **not** bypass with `--no-verify`. To run them
ahead of time:

- manager only: `nix fmt` (runs the whole generated pre-commit config over the
  working tree);
- everything at once, as CI does: `./support/ci/run.sh pipeline commit-hooks`.

## 6. Push — executor first, then manager

```bash
genvm-tool git check-for-push   # readiness per repo + suggested push command
git -C executors/<line>.x push origin pr/<line>/feat/<name>
git push origin feat/<name>
```

Submodules must be pushed before the manager so its pinned gitlinks resolve.

## 7. Open one PR — in the manager only

Open the manager PR against its dev base (e.g. `v0.6-dev`). Do **not** open
executor PRs yourself: CI automatically opens the matching executor PR for
every active line and links each on the manager PR as an `executor: <url>`
line.

Merge requirements (tests, merge queue): [../README.md](../README.md).
