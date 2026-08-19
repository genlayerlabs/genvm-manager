"""
make-zip.py system test.

The writer feeds the executor on every other run — all packaged runners are
built by it, so `Archive::from_zip_bytes` accepting its output is already
covered by the integration suite. What nothing covered is the two properties
that suite cannot see: that the bytes are reproducible, and that a *different*
implementation reads the archive the same way. Runner ids are content hashes,
so a drift in either is consensus-visible.
"""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import genvm_tool.tests
from genvm_tool.tests.test import Result

FIXTURE = {
	'runner.json': b'{ "StartWasm": "main.wasm" }',
	'main.wasm': b'\x00asm\x01\x00\x00\x00',
	'lib/mod.py': b'VALUE = 1\n',
}


def _build(script: Path, work: Path, out: Path) -> None:
	"""
	Lay out the tree make-zip.py expects (a `scripts/` dir plus exactly one
	source dir) and run it."""
	pkg = work / 'pkg'
	for name, contents in FIXTURE.items():
		path = pkg / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(contents)
	scripts = work / 'scripts'
	scripts.mkdir(parents=True, exist_ok=True)
	(scripts / 'make-zip.py').write_bytes(script.read_bytes())

	env = dict(os.environ)
	env['out'] = str(out)
	subprocess.run(
		[sys.executable, 'scripts/make-zip.py'],
		cwd=work,
		env=env,
		check=True,
		capture_output=True,
	)


def _add_case(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	script: Path,
	artifacts_dir: Path,
) -> None:
	desc = genvm_tool.tests.test.Description('tests/system/make_zip/writer').with_tags(
		frozenset({'integration', 'feature-runner-zip'})
	)

	async def validate(previous_results):
		first = artifacts_dir / 'first.zip'
		second = artifacts_dir / 'second.zip'
		_build(script, artifacts_dir / 'run-a', first)
		_build(script, artifacts_dir / 'run-b', second)

		if first.read_bytes() != second.read_bytes():
			return Result(
				passed=False,
				context={'reason': 'writer is not byte-deterministic'},
				elapsed_seconds=0,
			)

		raw = first.read_bytes()
		# CPython is the independent reader: it validates CRCs and both headers,
		# so agreement here is what rules out the local/central divergence class.
		with zipfile.ZipFile(io.BytesIO(raw)) as zf:
			if zf.testzip() is not None:
				return Result(
					passed=False,
					context={'reason': 'CPython rejected an entry', 'entry': zf.testzip()},
					elapsed_seconds=0,
				)
			names = sorted(zf.namelist())
			contents = {name: zf.read(name) for name in names}
			infos = {name: zf.getinfo(name) for name in names}

		for name, expected in FIXTURE.items():
			if name == 'runner.json':
				# The writer normalizes runner.json; compare parsed, not raw.
				if json.loads(contents.get(name, b'null')) != json.loads(expected):
					return Result(
						passed=False,
						context={'reason': 'runner.json content differs', 'entry': name},
						elapsed_seconds=0,
					)
				continue
			if contents.get(name) != expected:
				return Result(
					passed=False,
					context={'reason': 'entry content differs', 'entry': name},
					elapsed_seconds=0,
				)

		for name, info in infos.items():
			if info.compress_type != zipfile.ZIP_STORED:
				return Result(
					passed=False,
					context={'reason': 'entry is not stored', 'entry': name},
					elapsed_seconds=0,
				)
			# Pinned so the bytes cannot pick up anything host-derived.
			if (info.create_system, info.external_attr, info.flag_bits) != (0, 0, 0):
				return Result(
					passed=False,
					context={
						'reason': 'host-derived header field leaked',
						'entry': name,
						'create_system': info.create_system,
						'external_attr': info.external_attr,
						'flag_bits': int(info.flag_bits),
					},
					elapsed_seconds=0,
				)
			if info.date_time != (1980, 1, 1, 0, 0, 0):
				return Result(
					passed=False,
					context={
						'reason': 'mtime leaked into the archive',
						'entry': name,
						'date_time': info.date_time,
					},
					elapsed_seconds=0,
				)

		return Result(passed=True, context={'entries': len(names)}, elapsed_seconds=0)

	ctx.add_case(
		genvm_tool.tests.test.StepsCase(
			description=desc,
			steps=[genvm_tool.tests.exec.step.PythonFunction(validate)],
		)
	)


def collect(ctx: genvm_tool.tests.stage.collection.Context) -> None:
	script = (
		ctx.shared.root_dir
		/ 'executors'
		/ 'v0.3.x'
		/ 'runners'
		/ 'support'
		/ 'scripts'
		/ 'make-zip.py'
	)
	if not script.exists():
		raise FileNotFoundError(f'make-zip.py not found at {script}')

	artifacts_dir = ctx.shared.artifacts_dir / 'make_zip'
	artifacts_dir.mkdir(parents=True, exist_ok=True)
	_add_case(ctx, script=script, artifacts_dir=artifacts_dir)
