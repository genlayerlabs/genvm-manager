# Why Manager Owns Executor Branches

A change spans repositories: the executor lines carry the edit, the manager
carries the gitlinks pinning them. The E2E App therefore treats the manager PR
as the sole merge member. Once it lands, the manager branch tip is the source of
truth for every executor ref ([pr.md](../howto/pr.md))

## Why Executor Updates Follow Manager

The App squash-merges the exact manager tree E2E tested, preserving its gitlink
values. A push to a manager dev branch projects those gitlinks onto executor dev
branches; a manager release push projects them onto the declared executor
release branches

The projection creates missing refs and otherwise uses plain non-force pushes.
It never rewrites an executor commit or moves a branch backward

## Why Failures Aggregate

Cross-repository updates are not atomic. One executor line can diverge or lose
authorization while the others remain valid, so stopping on its first failure
would strand unrelated lines unnecessarily. Every line is attempted, then the
workflow returns one combined failure for repair and idempotent rerun

The executor commits already exist remotely before manager becomes eligible;
therefore a temporary branch-sync failure does not make manager gitlinks
unresolvable

## Why `ci-safe` Is a Human Decision

The PR panel's full-test and provisioning jobs run pull-request scripts with
credentials in scope. Approving those jobs approves code execution with
credentials, which is not inferable from the diff. The post-merge projection
runs only code already landed on a protected manager branch
