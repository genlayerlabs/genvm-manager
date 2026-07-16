import argparse
import sys
from pathlib import Path

import ci_lib


def build_and_pack(
	*,
	installable: str,
	artifact_name: str,
	globs: list[str] | None = None,
	compression_level: int = 9,
	show_trace: bool = False,
) -> Path:
	"""Realize `installable` and pack its output tree as ARTIFACTS_DIR/`artifact_name`.

	The out-link is keyed by artifact, not by installable: one installable may be
	packed into several artifacts that differ only in `globs`, and each needs its
	own `result-*` link.

	`show_trace` only helps on flake *evaluation* errors — build failures are
	already covered by `-L`.
	"""
	build = artifact_name.removesuffix('.xz').removesuffix('.tar')
	build_trg = ci_lib.ROOT_DIR / 'build' / f'result-{build}'
	# nix does not create the out-link's parent
	build_trg.parent.mkdir(exist_ok=True, parents=True)
	ci_lib.run(
		[
			'nix',
			'build',
			'-v',
			'-L',
			installable,
			'--out-link',
			str(build_trg),
			*(['--show-trace'] if show_trace else []),
		],
	)
	print(f'# built {build_trg} from {installable}')

	ci_lib.ARTIFACTS_DIR.mkdir(exist_ok=True, parents=True)
	tar_path = ci_lib.ARTIFACTS_DIR / artifact_name
	ci_lib.bundle_artifact(
		root_path=build_trg,
		result_path=tar_path,
		globs=globs if globs is not None else ['**/*'],
		compression_level=compression_level,
	)
	print(f'# packed {tar_path} with {compression_level} for {installable}')

	return tar_path


class Build(ci_lib.Pipeline):
	"""Build and package a GenVM target."""

	def name(self) -> str:
		return 'build'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument('--target', required=True)
		parser.add_argument('--compression', default=9, type=int)
		parser.add_argument('--no-refetch', action='store_true')
		parser.add_argument('--show-trace', action='store_true')

	def handler(self, args: argparse.Namespace) -> int:
		head_revision = ci_lib.output(['git', 'rev-parse', 'HEAD']).strip()
		print(f'$TARGET = {args.target}')
		print(f'$HEAD_REVISION = {head_revision}')

		if not args.no_refetch:
			with ci_lib.github_group('prefetch dependencies'):
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

		with ci_lib.github_group(f'nix build ({args.target})'):
			# `?submodules=1`: the executor lines are git submodules; without it the
			# flake source tree has empty executors/<line>.x dirs and evaluation fails.
			build_and_pack(
				installable=f'.?submodules=1#{args.target}',
				artifact_name=f'genvm-{args.target}.tar.xz',
				compression_level=args.compression,
				show_trace=args.show_trace,
			)

		return 0


COMMANDS = [Build()]
