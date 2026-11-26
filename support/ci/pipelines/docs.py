import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import ci_lib
from tools.versions import active_lines, current_line

sys.path.insert(0, str(ci_lib.ROOT_DIR / 'docs' / 'website' / 'common'))

import conf_common  # noqa: E402

# What `deploy-docs` publishes as 1 version directory of the site
SITE_DIR = ci_lib.ROOT_DIR / 'build' / 'doc' / 'html'
TEXT_DIR = ci_lib.ROOT_DIR / 'build' / 'doc' / 'text'


def manager_out() -> Path:
	return SITE_DIR / conf_common.MANAGER_PATH


def executor_out(line: str) -> Path:
	return SITE_DIR / conf_common.executor_path(conf_common.executor_version(line))


class Docs(ci_lib.Pipeline):
	"""
	Generate and build documentation.
	"""

	def name(self) -> str:
		return 'docs'

	def handler(self, args: argparse.Namespace) -> int:
		os.environ['GENVM_DOCS_SITE_OUT'] = str(SITE_DIR)

		with ci_lib.github_group('generate runner pages data'):
			ci_lib.run(
				[
					sys.executable,
					'docs/website/generate.py',
					f'executors/{current_line()}.x/docs/website/src/python-sdk/runners-versions.json',
				],
			)

		if SITE_DIR.exists():
			shutil.rmtree(SITE_DIR)
		SITE_DIR.mkdir(parents=True)

		# Manager, executor and runner pages reference each other; the first pass
		# only produces the inventories the second one resolves against
		for build_pass in (1, 2):
			for line in active_lines():
				self.build_line(line, build_pass)
			ci_lib.nix_develop(
				'.?submodules=1#gen-docs',
				[
					'sphinx-build',
					'-q',
					'-E',
					'-b',
					'html',
					'docs/website/src',
					manager_out(),
				],
				subcommand_group=f'sphinx manager (pass {build_pass})',
			)

		self.ai_bundles()

		ci_lib.run([sys.executable, 'docs/website/catalog.py', SITE_DIR])
		print(f'site: {SITE_DIR / "index.html"}')
		return 0

	def build_line(self, line: str, build_pass: int) -> None:
		exec_prefix = f'executors/{line}.x'
		installable = f'./{exec_prefix}#gen-docs'
		ci_lib.nix_develop(
			installable,
			[
				'python3',
				'docs/website/runner_pages.py',
				'--line',
				line,
				'--site',
				SITE_DIR,
				'--work',
				ci_lib.ROOT_DIR / 'build' / 'doc' / 'runner-src',
			],
			subcommand_group=f'runner pages: {line} (pass {build_pass})',
		)
		src = ci_lib.ROOT_DIR / exec_prefix / 'docs' / 'website' / 'src'
		conf = []
		# Lines frozen before the shared settings existed build through a wrapper
		if 'conf_common' not in (src / 'conf.py').read_text():
			os.environ['GENVM_LEGACY_EXECUTOR'] = json.dumps({'conf': str(src / 'conf.py')})
			conf = ['-c', ci_lib.ROOT_DIR / 'docs' / 'website' / 'legacy-executor']
		ci_lib.nix_develop(
			installable,
			['sphinx-build', '-q', '-E', '-b', 'html', *conf, src, executor_out(line)],
			subcommand_group=f'sphinx executor {line} (pass {build_pass})',
		)

	def ai_bundles(self) -> None:
		"""
		Plain-text bundles the `Single text file docs for AI` notes link to
		"""
		out = manager_out()
		ci_lib.nix_develop(
			'.?submodules=1#gen-docs',
			['sphinx-build', '-q', '-b', 'text', 'docs/website/src', TEXT_DIR / 'manager'],
			subcommand_group='sphinx manager text',
		)
		ci_lib.run(
			[
				sys.executable,
				'docs/website/merge_txts.py',
				TEXT_DIR / 'manager' / 'impl-spec',
				out / '_static' / 'ai' / 'impl-spec.txt',
			]
		)
		for line in active_lines():
			src = ci_lib.ROOT_DIR / 'executors' / f'{line}.x' / 'docs' / 'website' / 'src'
			if not (src / 'spec').is_dir():
				continue
			ci_lib.nix_develop(
				f'./executors/{line}.x#gen-docs',
				['sphinx-build', '-q', '-b', 'text', src, TEXT_DIR / 'executor' / line],
				subcommand_group=f'sphinx executor text: {line}',
			)
			ci_lib.run(
				[
					sys.executable,
					'docs/website/merge_txts.py',
					TEXT_DIR / 'executor' / line / 'spec',
					executor_out(line) / '_static' / 'ai' / 'spec.txt',
				]
			)


COMMANDS = [Docs()]
