# Docs

## Website (`docs/website`)

Sphinx site. The toolchain (sphinx + extensions) comes from the `gen-docs` Nix
shell — no poetry/venv. Build everything with:

```bash
./support/ci/run.sh pipeline docs
```

That regenerates the derived pages, then enters `.#gen-docs` and runs
`sphinx-build` for `-b html` and `-b text` (merging the text output into
`_static/ai/*.txt`). Finally, for every active executor line it enters that
line's own `.#gen-docs` shell (`executors/<line>.x/flake.nix`) and builds the
line's standalone docs sub-site into `build/doc/html/executors/<line>/`; the
manager's generated `executors.rst` page links to them. To iterate on just the
sphinx build:

```bash
nix develop '.?submodules=1#gen-docs' --command \
  sphinx-build -q -b dirhtml docs/website/src <out>
```

See `support/ci/pipelines/docs.py` for the full pipeline.

Source layout under `docs/website/src/`:

- `spec/` — the protocol specification (observable behavior).
  `spec/appendix/constants.rst` is **generated** from codegen data — do not
  edit by hand, see [genvm-tool.md](../genvm-tool.md).
- `impl-spec/` — the implementation specification (how this codebase does it).
- `overview/` — intro material.

The Python SDK reference and the per-line runner-version pages are **not** in the
manager tree — they live in the executor line that ships them, under
`executors/<line>.x/docs/website/src/python-sdk/` (see that line's `conf.py`),
and build as sub-sites under `build/doc/html/executors/<line>/`. Cross-linking is
one-way: the sub-sites reference the manager via intersphinx (the `genvm`
inventory); the manager only links out to them from the generated
`executors.rst` index page.

`docs/website/generate.py` regenerates derived pages — the runner-version tables
(`available-runners.rst` + `runners-versions.json`) and the SDK `changelog.rst`
into the primary line's sub-site, and the `executors.rst` index into the manager
— from the repo state; the `docs` pipeline runs it before the sphinx build.

## ADRs

Architecture decision records live in [`docs/adr/`](../../../adr/) as numbered markdown files
(`000. WASI.md`, …); start new ones from [`docs/adr/_template.md`](../../../adr/_template.md).

## Contributor docs

[`docs/contributing/`](../..) — PR requirements and workflows; how-to guides are in
this directory.
