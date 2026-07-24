# Contributing to GenVM development

## Sub pages
- [first-contribution tutorial](./tutorial/first-contribution.md) — end-to-end walkthrough: patch an executor, branch, commit, push, open the PR.
- [how-to guides](./howto/README.md) — setup, build, test, submodules, versioning, extension recipes, …

## PR requirements
All checks must pass: [pre-commit](https://pre-commit.com) hooks and the test
suites. PRs land through a merge queue that runs the full test set and merges
*iff* everything passes — this keeps `HEAD` of `main` always stable.

## Security
GenVM is consensus-critical. Do not open public issues for vulnerabilities —
follow [SECURITY.md](../../SECURITY.md) (private reporting).
