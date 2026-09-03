# Pull Requests

The E2E GitHub App owns integration landing. After the exact manager snapshot
passes full GenVM CI and cross-repository E2E, a maintainer runs `/merge`. The
App revalidates the snapshot and squash-merges the manager PR; the resulting
manager push projects its gitlinks onto executor branches. Why:
[merge-model.md](../explanation/merge-model.md)

## Branches

1. `main` is an alias of the latest `v<X>-dev` and is never developed on
2. A feature PR lands on `v<X>-dev`, which reaches `v<X>` through a standing
   release-gate PR
3. `genvm-tool git create-branches` makes `feat/<name>` in the manager and
   `pr/<line>/feat/<name>` per executor line
4. Push the submodules first, then open **one PR, in the manager only**, against
   `v<X>-dev`; a PR targeting `main` is retargeted for you
5. Never open an executor PR by hand — automation opens and links one per active
   line that has a pushed `pr/<line>/…` branch; they are review projections

## Panel

Boxes: *Force run full tests*, *Provision executor PRs* (force-push each mirror
branch to the pinned gitlink, after a rebase or an amend). Nothing works without
the **`ci-safe`** label, since these jobs run the
PR's own `support/ci/` scripts with deploy keys in scope. Authors with write
access get it automatically. Everyone else needs a maintainer to add it after
reading the diff

With `ci-safe` and write access, `/genvm-run-tests` links one run and reacts
👀 → 🚀/😕 without changing the sticky `run-full-tests` label

## Gates

`queue.yaml` always runs the pre-commit hooks. On a `pull_request` event it also
requires 0 commits behind base and the executor precondition — every repo
rebased, every pinned executor commit pushed — which is skipped for fork PRs.
The heavy matrix runs with the `run-full-tests` marker or a one-shot
`/genvm-run-tests`; otherwise the check stays red on purpose

The App re-verifies the exact tested head, base, synthetic merge, approvals,
native CI, E2E proof and dependency closure. It squash-merges only the manager
PR. A push to manager `v<X>-dev` then creates or fast-forwards every declared
executor `<line>-dev` to the gitlink in that manager tip; a push to manager
`v<X>` does the same for each executor release branch declared in `.gitmodules`

Executor updates are non-force and independent. Every line is attempted even
if another line fails; the workflow reports all failures together and can be
rerun manually against the live manager branch

## Authority

Needs a maintainer: merging, `--admin`, cross-repo E2E runs, and anything
irreversible outside the PR — releases, deploys, messages to other teams. Never
force-push a shared branch (`v<X>`, `v<X>-dev`); branch protection refuses it
anyway. Force-pushing your own PR branch is routine and needs no one, and so
does fixing a defect you find in your own diff

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Panel does nothing | missing `ci-safe` |
| `full tests have not run` | tick *Force run full tests* or comment `/genvm-run-tests` |
| `... commit(s) behind base` | rebase, `push --force-with-lease` |
| `pinned commit ... is not on the executor repo` | push it, or tick *Provision executor PRs* |
| Approval revoked | re-request approval for the current head |
| Executor branch sync failed | inspect every reported line, resolve non-fast-forward divergence, rerun *branch / sync executor branches* |
