# Your First Contribution: Patching an Executor

End-to-end walkthrough of the most common change, a fix inside an executor line.
Each step links to the guide with the full details

1. **Set up** ([setup.md](../howto/setup.md)) —
   `python3 support/scripts/get-all-git.py`, then enter the dev shell
2. **Change and verify** ([build.md](../howto/building/build.md),
   [testing/README.md](../howto/testing/README.md)):

   ```bash
   genvm-tool configure && ninja -C build all/bin
   genvm-tool test run --filter-name 'executors/<line>.x/'
   ```
3. **Branch** — `genvm-tool git create-branches feat/<name>` surveys which repos
   carry new content and branches the ones you tick: the manager gets
   `feat/<name>`, each executor gets `pr/<line>/feat/<name>`, since all lines
   push to one shared executor remote
4. **Commit** ([submodules.md](../howto/committing/submodules.md)) — inside the
   executor first, then `git add executors/<line>.x` in the manager and commit
   the gitlink bump together with any manager-side change. If you touched
   runners, do the hash hygiene first
   ([committing/runners.md](../howto/committing/runners.md))
5. **Pass pre-commit** — the hooks run where you commit, so never
   `--no-verify`. Ahead of time: `nix fmt` for the manager, or
   `./support/ci/run.sh pipeline commit-hooks` for everything, as CI does
6. **Push, submodules first**, so the gitlinks resolve:

   ```bash
   genvm-tool git check-for-push   # readiness per repo + suggested command
   git -C executors/<line>.x push origin pr/<line>/feat/<name>
   git push origin feat/<name>
   ```
7. **Open one PR, in the manager only** ([pr.md](../howto/pr.md)), against the
   dev base. CI opens and links the matching executor PR for every line you
   pushed a branch for
