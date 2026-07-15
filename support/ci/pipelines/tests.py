import argparse
import os
import sys
from pathlib import Path

import ci_lib


class TestPython(ci_lib.Pipeline):
	"""Run Python tests."""

	def name(self) -> str:
		return 'test-python'

	def handler(self, args: argparse.Namespace) -> int:
		ci_lib.nix_develop(
			'.?submodules=1#py-test',
			[
				'./support/ci/run.sh',
				'pipeline',
				'test-python-src',
			],
		)
		return 0


class TestPythonSrc(ci_lib.Pipeline):
	"""Run Python tests inside the Python test Nix environment."""

	def name(self) -> str:
		return 'test-python-src'

	def handler(self, args: argparse.Namespace) -> int:
		cwd = ci_lib.ROOT_DIR / 'executors' / 'v0.3.x' / 'runners' / 'genlayer-py-std'
		with ci_lib.github_group('build python test env'):
			env_dir = ci_lib.output(
				[
					'nix',
					'build',
					'--no-link',
					'--print-out-paths',
					'path:../../support/nix/py-test',
				],
				cwd=cwd,
			).strip()

		with ci_lib.github_group('pytest (genlayer-py-std)'):
			pythonpath = f'{cwd / "src"}:{cwd / "src-emb"}'
			if os.environ.get('PYTHONPATH'):
				pythonpath = f'{pythonpath}:{os.environ["PYTHONPATH"]}'
			ci_lib.run(
				[Path(env_dir) / 'bin' / 'pytest'], cwd=cwd, env={'PYTHONPATH': pythonpath}
			)
		return 0


class TestRust(ci_lib.Pipeline):
	"""Run Rust tests."""

	def name(self) -> str:
		return 'test-rust'

	def handler(self, args: argparse.Namespace) -> int:
		os.environ['ORIGINAL_PATH'] = os.environ.get('PATH', '')
		os.environ['ORIGINAL_LD_LIBRARY_PATH'] = os.environ.get('LD_LIBRARY_PATH', '')

		with ci_lib.github_group('runner registry manifests'):
			manifest_dir = ci_lib.ROOT_DIR / 'build' / 'out' / 'executor' / 'vTEST' / 'data'
			manifest_dir.mkdir(parents=True, exist_ok=True)
			latest_expr = (
				'let drv = import ./executors/v0.3.x/runners { host-system = builtins.currentSystem; } ; '
				'in builtins.listToAttrs (builtins.map '
				'(x: { name = x.id; value = builtins.head (builtins.match "[^:]+:(.*)" x.uid); }) drv)'
			)
			all_expr = (
				'let drv = import ./executors/v0.3.x/runners { host-system = builtins.currentSystem; } ; '
				'in builtins.listToAttrs (builtins.map '
				'(x: { name = x.id; value = [ (builtins.head (builtins.match "[^:]+:(.*)" x.uid)) ]; }) drv)'
			)
			for name, expr in [('latest.json', latest_expr), ('all.json', all_expr)]:
				with (manifest_dir / name).open('w') as out:
					ci_lib.run(
						[
							'nix',
							'eval',
							'--verbose',
							'--impure',
							'--read-only',
							'--show-trace',
							'--json',
							'--expr',
							expr,
						],
						stdout=out,
					)

		with ci_lib.github_group('download runners'):
			ci_lib.run(
				[
					sys.executable,
					'support/runner-script.py',
					'download',
					'--nix-preload',
					'--allow-partial',
					'--dest',
					'build/out/runners',
					'--registry',
					'build/out/executor/vTEST/data/all.json',
				]
			)

		with ci_lib.github_group('build runners-all'):
			ci_lib.run(
				[
					'nix',
					'build',
					'-v',
					'-L',
					'-o',
					'build/out-runners',
					'.?submodules=1#runners-all',
				]
			)
			(ci_lib.ROOT_DIR / 'build' / 'out' / 'runners').mkdir(parents=True, exist_ok=True)
			ci_lib.run(
				[
					'cp',
					'-rsf',
					f'{(ci_lib.ROOT_DIR / "build" / "out-runners").resolve()}/.',
					'./build/out/runners/.',
				]
			)

		ci_lib.nix_develop(
			'.?submodules=1#rust-test',
			[
				sys.executable,
				'support/runner-script.py',
				'upload',
				'--root',
				'build/out/runners',
				'--registry',
				'build/out/executor/vTEST/data/all.json',
			],
			check=False,
			subcommand_group='try upload runners',
		)

		ci_lib.nix_develop(
			'.?submodules=1#rust-test',
			[
				'./support/ci/run.sh',
				'pipeline',
				'test-rust-src',
			],
		)
		return 0


class TestRustSrc(ci_lib.Pipeline):
	"""Run Rust tests inside the Rust test Nix environment."""

	def name(self) -> str:
		return 'test-rust-src'

	def handler(self, args: argparse.Namespace) -> int:
		nix_bin = str(Path(ci_lib.output(['which', 'nix']).strip()).parent)
		env = {'PATH': f'{nix_bin}:{os.environ.get("PATH", "")}'}
		with ci_lib.github_group('genvm-tool configure'):
			ci_lib.run(['genvm-tool', 'configure'], env=env)
		with ci_lib.github_group('ninja build (all/bin)'):
			ci_lib.run(['ninja', '-v', '-C', 'build', 'all/bin'], env=env)
		with ci_lib.github_group('post-install'):
			ci_lib.run(
				[
					sys.executable,
					'build/out/bin/genvm-post-install',
					'--error-on-missing-executor=false',
					'--default-download=false',
				],
				env=env,
			)
		with ci_lib.github_group('rust tests'):
			filter_tag = (
				(ci_lib.ROOT_DIR / 'tests' / 'presets' / 'rust.txt').read_text().strip()
			)
			ci_lib.run(
				[
					'nix',
					'develop',
					'.?submodules=1#mock-tests',
					'--command',
					'genvm-tool',
					'test',
					'run',
					'--ci',
					'--filter-tag',
					filter_tag,
				],
				env=env,
			)
		return 0


class TestRustFuzz(ci_lib.Pipeline):
	"""Run Rust fuzz tests."""

	def name(self) -> str:
		return 'test-rust-fuzz'

	def handler(self, args: argparse.Namespace) -> int:
		os.environ['ORIGINAL_PATH'] = os.environ.get('PATH', '')
		os.environ['ORIGINAL_LD_LIBRARY_PATH'] = os.environ.get('LD_LIBRARY_PATH', '')
		ci_lib.nix_develop(
			'.?submodules=1#rust-test',
			cmd=[
				'./support/ci/run.sh',
				'pipeline',
				'test-rust-fuzz-src',
			],
		)
		return 0


class TestRustFuzzSrc(ci_lib.Pipeline):
	"""Run Rust fuzz tests inside the Rust test Nix environment."""

	def name(self) -> str:
		return 'test-rust-fuzz-src'

	def handler(self, args: argparse.Namespace) -> int:
		nix_bin = str(Path(ci_lib.output(['which', 'nix']).strip()).parent)
		env = {'PATH': f'{nix_bin}:{os.environ.get("PATH", "")}'}
		with ci_lib.github_group('genvm-tool configure'):
			ci_lib.run(['genvm-tool', 'configure'], env=env)
		with ci_lib.github_group('rust fuzz tests'):
			filter_tag = (
				(ci_lib.ROOT_DIR / 'tests' / 'presets' / 'rust-fuzz.txt').read_text().strip()
			)
			ci_lib.run(
				[
					'nix',
					'develop',
					'.?submodules=1#mock-tests',
					'--command',
					'genvm-tool',
					'test',
					'run',
					'--ci',
					'--filter-tag',
					filter_tag,
				],
				env=env,
			)
		return 0


COMMANDS = [
	TestPython(),
	TestPythonSrc(),
	TestRust(),
	TestRustSrc(),
	TestRustFuzz(),
	TestRustFuzzSrc(),
]
