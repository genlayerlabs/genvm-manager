import argparse
import sys

import ci_lib


class Build(ci_lib.Pipeline):
	"""Build and package a GenVM target."""

	def name(self) -> str:
		return 'build'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument('--target', required=True)
		parser.add_argument('--executor-version', default='')
		parser.add_argument('--no-refetch', action='store_true')
		parser.add_argument('--compression', default='9', type=int)

	def handler(self, args: argparse.Namespace) -> int:
		head_revision = ci_lib.output(['git', 'rev-parse', 'HEAD']).strip()
		print(f'$TARGET = {args.target}')
		print(f'$HEAD_REVISION = {head_revision}')

		with ci_lib.github_group('prefetch dependencies'):
			if not args.no_refetch:
				ci_lib.run(
					[
						sys.executable,
						'support/runner-script.py',
						'dependencies',
						'prefetch-needed',
						'--all',
						'--target',
						args.target,
					]
				)

		(ci_lib.ROOT_DIR / 'build').mkdir(exist_ok=True)
		with ci_lib.github_group(f'nix build ({args.target})'):
			ci_lib.run(
				[
					'nix',
					'build',
					'-o',
					f'build/out-{args.target}',
					'-v',
					'-L',
					f'.?submodules=1#{args.target}',
					'--show-trace',
				]
			)

		tar_result_path = ci_lib.ROOT_DIR / 'build' / f'genvm-{args.target}.tar.xz'
		ci_lib.bundle_artifact(
			result_path=tar_result_path,
			root_path=ci_lib.ROOT_DIR / 'build' / f'out-{args.target}',
			compression_level=args.compression,
			globs=['**/*'],
		)
		print(f'Written {tar_result_path} with {args.compression}')

		return 0


COMMANDS = [Build()]
