import argparse
import json
import os
import re

import ci_lib
from pipelines.tests import GithubStep, SubStep, step_nix_develop, step_run

# Platforms are named the way nix names them everywhere: cells, flake attrs and
# published assets.
PLATFORMS = ('amd64-linux', 'arm64-linux', 'arm64-macos')

# One cell per release asset. A platform's prepack tree already merges the
# manager with every active executor line, and `universal` holds the
# platform-independent runners — so nothing here has to know about executor
# lines, and a cell is a single build-and-pack.
CELLS = (*PLATFORMS, 'universal')

RELEASE_PRESET = 'tests/presets/release.txt'

# The generated release notes travel as their own artifact so a PR-label run
# produces them too — they are the one release output worth eyeballing before
# the release happens.
NOTES_ARTIFACT = 'release-notes'
NOTES_FILE = 'release-notes.md'


def artifact_of(cell: str) -> str:
	"""
	Artifact name a build cell uploads and a test cell downloads.

	Keyed by cell rather than minted at upload time: every leg of a matrix job
	writes the *same* `outputs` key, so a per-leg name cannot be handed back as
	an output. A name both sides derive from the plan needs no output at all.
	"""
	return f'release-build-{cell}'


def asset_of(cell: str) -> str:
	"""
	The tarball a cell packs — already named as the published release asset.

	Nothing downstream renames it: the publisher attaches what it downloads.
	"""
	return f'genvm-{cell}.tar.xz'


def _build_cell(name: str) -> dict:
	step: SubStep = {
		# `?submodules=1`: the executor lines are git submodules; without it the
		# flake source tree has empty executors/<line>.x dirs.
		'type': 'build-and-pack',
		'installable': f'.?submodules=1#artifact-prepack-genvm-{name}',
		'artifact_name': asset_of(name),
	}
	return {
		'name': name,
		# Every platform cross-compiles from linux (zig does the darwin cross),
		# so even arm64-macos builds here; only its *test* needs a mac.
		'runs_on': 'ubuntu-latest',
		'artifact_name': artifact_of(name),
		'job_json': json.dumps({'build': [step]}),
	}


def build_cells() -> list[dict]:
	return [_build_cell(cell) for cell in CELLS]


# --- test cells ----------------------------------------------------------------
# Each cell exercises the *downloaded* artifacts: the tarballs are extracted into
# build/out, and `genvm-tool configure` writes build/info.json pointing there, so
# the runner never builds.
#
# No step goes through a shell: the replay engine execs argv directly, so
# anything a shell would have done is resolved here instead — the preset is read
# at plan time rather than via `$(cat ...)`, `|| true` is check=False, and
# `|| test $? -eq 124` is expect_exit_codes.

POST_INSTALL = './build/out/bin/genvm-post-install'

# Starting the llm module with no backends proves the binary runs and its config
# parses. It is a daemon, so it never exits on its own: timeout kills it and 124
# is the success signal (0 would mean it died early).
SMOKE_TIMEOUT_SECONDS = 5
SMOKE_EXIT_CODES = [0, 124]
SMOKE_MODULES = [
	'./build/out/bin/genvm-modules',
	'llm',
	'--allow-empty-backends',
	'--config',
	'./build/out/config/genvm-module-llm.yaml',
]


def release_filter_tag() -> str:
	"""
	The release preset's tag expression, read at plan time.

	The old jobs inlined `$(cat ...)` into the shell; resolving it here keeps the
	steps shell-free and puts the expression in the plan, where it is visible.
	"""
	return (ci_lib.ROOT_DIR / RELEASE_PRESET).read_text().strip()


def stable_test_steps(installable: str) -> GithubStep:
	"""
	configure + run the release preset inside `installable`'s shell.

	`configure` must come first: it writes build/info.json, which is what points
	the runner at the extracted build/out tree.
	"""
	commands = [
		(['genvm-tool', 'configure'], 'configure'),
		(
			['genvm-tool', 'test', 'run', '--ci', '--filter-tag', release_filter_tag()],
			'stable tests',
		),
	]
	return [
		step_nix_develop(installable, command, subcommand_group=group)
		for command, group in commands
	]


def _test_cell(
	name: str,
	steps: GithubStep,
	*,
	runs_on: str,
	with_nix: bool = True,
	setup_python: bool = False,
	runners: bool = True,
) -> dict:
	return {
		'name': name,
		'runs_on': runs_on,
		'platform_artifact': artifact_of(name),
		# '' means "this cell does not need the runners artifact".
		'runners_artifact': artifact_of('universal') if runners else '',
		'with_nix': with_nix,
		'setup_python': setup_python,
		'job_json': json.dumps({'test': steps}),
	}


def _smoke_step(timeout_bin: str) -> SubStep:
	return step_run(
		[timeout_bin, str(SMOKE_TIMEOUT_SECONDS), *SMOKE_MODULES],
		expect_exit_codes=SMOKE_EXIT_CODES,
	)


def _cell_amd64_linux() -> dict:
	return _test_cell(
		'amd64-linux',
		[
			step_run([POST_INSTALL, '--default-download=false', '--precompile=true']),
			{'type': 'group-start', 'name': 'smoke test modules startup'},
			_smoke_step('timeout'),
			{'type': 'group-end'},
			*stable_test_steps('.?submodules=1#mock-tests'),
		],
		runs_on='ubuntu-latest',
	)


def _cell_arm64_linux() -> dict:
	qemu_shell = '.?submodules=1#check-qemu'
	qemu = 'qemu-aarch64'
	return _test_cell(
		'arm64-linux',
		[
			# aarch64 binaries on an amd64 host: post-install's bin-check and
			# precompile steps exec them and die with Exec format error, and
			# without the runners artifact the registry is absent too. Neither is
			# a real failure here — the qemu smoke below is what proves the build.
			step_run(
				[POST_INSTALL, '--error-on-missing-executor=false', '--default-download=false'],
				check=False,
			),
			step_nix_develop(
				qemu_shell,
				[qemu, './build/out/bin/genvm-modules', '--version'],
				subcommand_group='version under qemu',
			),
			step_nix_develop(
				qemu_shell,
				['timeout', str(SMOKE_TIMEOUT_SECONDS), qemu, *SMOKE_MODULES],
				subcommand_group='smoke test modules startup',
				expect_exit_codes=SMOKE_EXIT_CODES,
			),
		],
		runs_on='ubuntu-latest',
		# No stable suite: the whole tree is foreign-arch, so there is nothing to
		# run it with beyond the qemu smoke above.
		runners=False,
	)


# No nix on the mac runner, so genvm-tool comes from a venv — and `activate` also
# puts the venv's python3 on PATH, which calling .venv/bin/genvm-tool by path
# would not. That needs a shell, so this leg keeps one.
MACOS_STABLE_TESTS = """\
set -euo pipefail
python3 -m venv .venv
source .venv/bin/activate
pip install ./support/tools/genvm-tool
genvm-tool configure
genvm-tool test run --ci --filter-tag "$1"
"""


def _cell_arm64_macos() -> dict:
	return _test_cell(
		'arm64-macos',
		[
			step_run(['brew', 'install', 'wabt', 'coreutils']),
			step_run(['rustup', 'target', 'add', 'wasm32-wasip1']),
			step_run([POST_INSTALL, '--default-download=false']),
			{'type': 'group-start', 'name': 'smoke test modules startup'},
			# coreutils' gtimeout: macOS ships no timeout(1).
			_smoke_step('gtimeout'),
			{'type': 'group-end'},
			{'type': 'group-start', 'name': 'stable tests'},
			# The filter goes in as $1 rather than interpolated: it is an
			# expression full of & | ! and must not meet the shell.
			step_run(['bash', '-c', MACOS_STABLE_TESTS, 'bash', release_filter_tag()]),
			{'type': 'group-end'},
		],
		runs_on='macos-latest',
		with_nix=False,
		setup_python=True,
	)


def test_cells() -> list[dict]:
	return [_cell_amd64_linux(), _cell_arm64_linux(), _cell_arm64_macos()]


class PlanReleaseMatrix(ci_lib.Pipeline):
	"""
	Emit the release build/test matrices and the tag they are built at.

	One build cell per release artifact (runners + one per platform) and one test
	cell per platform, all keyed by names both sides derive from this plan.
	"""

	def name(self) -> str:
		return 'plan-release-matrix'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument(
			'--version-override',
			default='',
			help='release tag; defaults to `version` from .genvm-monorepo-root',
		)

	def handler(self, args: argparse.Namespace) -> int:
		monorepo = json.loads((ci_lib.ROOT_DIR / '.genvm-monorepo-root').read_text())
		tag = args.version_override or monorepo['version']

		# Shape only. Whether the tag is already taken depends on the remote and
		# is the publisher's business — a PR-label run plans the same tag every
		# time and must not care.
		if not re.match(r'^v[0-9]+\.[0-9]+\.[0-9]+', tag):
			ci_lib.github_error(f'invalid version {tag!r}')
			return 1

		builds = build_cells()
		tests = test_cells()

		outputs = {
			'tag': tag,
			'build_matrix': json.dumps({'include': builds}),
			'test_matrix': json.dumps({'include': tests}),
			'artifact_notes': NOTES_ARTIFACT,
			'notes_file': NOTES_FILE,
		}
		# The artifact names are the caller's whole view of the run: it downloads
		# by them and never learns the matrices produced them. Emitted here (from
		# a plain job) rather than from the build matrix, whose legs would all
		# write the same `outputs` key.
		for cell in builds:
			outputs[f'artifact_{cell["name"].replace("-", "_")}'] = cell['artifact_name']

		out = os.environ.get('GITHUB_OUTPUT')
		if not out:
			ci_lib.github_error('GITHUB_OUTPUT is not set')
			print(json.dumps(outputs, indent=2))
			return 1
		with open(out, 'a') as f:
			for key, value in outputs.items():
				f.write(f'{key}={value}\n')

		print(f'planned release {tag}')
		for cell in builds:
			print(f'  build {cell["name"]} -> {cell["artifact_name"]}')
		for cell in tests:
			print(f'  test  {cell["name"]} on {cell["runs_on"]}')
		return 0


COMMANDS = [PlanReleaseMatrix()]
