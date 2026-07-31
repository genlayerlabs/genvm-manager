# Pull Requests

There is no GitHub merge queue. A PR lands when a maintainer ticks a box on the
PR action panel, which re-checks every gate. Automation:
`.github/workflows/branch_*.yaml` and `queue.yaml`; why:
[merge-model.md](../explanation/merge-model.md)

## Branches

1. `main` is an alias of the latest `v<X>-dev` and is never developed on
2. A feature PR lands on `v<X>-dev`, which reaches `v<X>.x` through a standing
   release-gate PR
3. `genvm-tool git create-branches` makes `feat/<name>` in the manager and
   `pr/<line>/feat/<name>` per executor line
4. Push the submodules first, then open **one PR, in the manager only**, against
   `v<X>-dev`; a PR targeting `main` is retargeted for you
5. Never open an executor PR by hand — automation opens and links one per active
   line that has a pushed `pr/<line>/…` branch, and they land together

## Panel

Boxes: *Force / Rerun full tests*, *Provision executor PRs* (force-push each
mirror branch to the pinned gitlink, after a rebase or an amend), *Merge into
dev*. Nothing works without the **`ci-safe`** label, since these jobs run the
PR's own `support/ci/` scripts with deploy keys in scope. Authors with write
access get it automatically. Everyone else needs a maintainer to add it after
reading the diff

## Gates

`queue.yaml` always runs the pre-commit hooks. On a `pull_request` event it also
requires 0 commits behind base and the executor precondition — every repo
rebased, every pinned executor commit pushed — which is skipped for fork PRs.
The heavy matrix runs only with the `run-full-tests` marker; without it the
check stays red on purpose

Merging re-verifies against the **exact head sha**: maintainer approval (any
push revokes it), green full CI, green cross-repo E2E, 0 behind. Merges are
serialised repository-wide, so a tick may wait for another PR to land first

Each repo gains one commit per PR. Executor lines are always squashed onto
`<line>-dev`, so the manager commit is rewritten to gitlink the new commit. A
manager PR that is a single commit and rewrites no gitlink keeps its sha

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Panel does nothing | missing `ci-safe` |
| `full tests have not run` | tick *Force run full tests* |
| `... commit(s) behind base` | rebase, `push --force-with-lease` |
| `pinned commit ... is not on the executor repo` | push it, or tick *Provision executor PRs* |
| Approval revoked | you pushed after the review, re-request it |
| Merge run cancelled before it started | another merge was already queued, tick *Merge into dev* again |
