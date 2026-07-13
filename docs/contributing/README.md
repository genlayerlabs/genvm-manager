# Contributing to GenVM development

## Sub pages
- [how-to guides](./howto/README.md) — setup, build, test, submodules, versioning, extension recipes, …

## PR requirements
Main requirement is that all tests must pass. It includes [pre-commit](https://pre-commit.com) and test suites. PRs are merged via a queue, that executes all tests and merges *iff* they all pass. It is done to ensure that `HEAD` of `main` branch is always stable

## Security
GenVM is consensus-critical. Do not open public issues for vulnerabilities —
follow [SECURITY.md](../../SECURITY.md) (private reporting).
