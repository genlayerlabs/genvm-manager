# Docs

## Website (`docs/website`)

Sphinx site with a poetry env in that dir:

```bash
cd docs/website
poetry install --no-root
poetry run sphinx-build -q -b dirhtml src <out>
```

CI builds `-b html` and `-b text` — see `support/ci/pipelines/src/docs.sh`
(also merges the text output into `_static/ai/*.txt`).

Source layout under `docs/website/src/`:

- `spec/` — the protocol specification (observable behavior).
  `spec/appendix/constants.rst` is **generated** from codegen data — do not
  edit by hand, see [genvm-tool.md](../genvm-tool.md).
- `impl-spec/` — the implementation specification (how this codebase does it).
- `python-sdk/`, `overview/` — SDK API docs and intro material.

`docs/website/generate.py` regenerates derived pages (runner/version tables)
from the repo state; wired into the build via `docs/website/yabuild.rb`.

## ADRs

Architecture decision records live in [`docs/adr/`](../../../adr/) as numbered markdown files
(`000. WASI.md`, …); start new ones from [`docs/adr/_template.md`](../../../adr/_template.md).

## Contributor docs

[`docs/contributing/`](../..) — PR requirements and workflows; how-to guides are in
this directory.
