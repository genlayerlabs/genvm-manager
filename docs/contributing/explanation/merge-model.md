# Why Merging Is a Panel, Not a Queue

A change spans repositories: the executor lines carry the edit, the manager
carries the gitlinks pinning them. GitHub's merge queue orders pull requests in
one repository and has no opinion about a second, so the landing step is ours —
a maintainer ticks *Merge into dev* and the automation re-checks every gate
before writing anything ([pr.md](../howto/pr.md))

## Why the Gates Re-Run at Merge Time

A green check describes the commit that was tested, not the branch it sits on,
and between review and merge the head, the base and the pinned executor branch
can all move

The heavy matrix is opt-in for a related reason: a check that runs on every push
to every draft gets ignored

## Why Only One Merge Runs at a Time

Checking the gates and advancing the branches is not atomic, and the executor
lines advance before the manager, so a lost race leaves a half-landed change.
Hence one merge repository-wide, and never a cancelled one

That costs a maintainer something: GitHub keeps 1 pending run per concurrency
group, so a third tick cancels the waiting one

## Why `ci-safe` Is a Human Decision

These jobs run the pull request's own `support/ci/` scripts with deploy keys in
scope. Approving a run approves code execution with credentials, which is not
inferable from the diff
