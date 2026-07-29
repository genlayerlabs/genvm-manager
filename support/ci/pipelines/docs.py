import argparse
import os
import sys

import ci_lib
from tools.versions import active_lines, newest_line


class Docs(ci_lib.Pipeline):
	"""
	Generate and build documentation.
	"""

	def name(self) -> str:
		return 'docs'

	def handler(self, args: argparse.Namespace) -> int:
		# Every sphinx conf.py (manager + executor sub-sites) reads COPYRIGHT_YEAR
		# from the environment; set it once so all the builds below inherit it.
		os.environ['COPYRIGHT_YEAR'] = ci_lib.output(
			['git', 'log', '-1', '--date=format:%Y', '--format=%ad']
		).strip()

		# Runner versions are per-executor-line data (the runner manifest), so the
		# generated manifest + available-runners page land in the newest line's
		# SDK sub-site, not the manager. generate.py writes available-runners.rst
		# next to this json (json_path.with_name).
		#
		# `newest_line()`, not `active-versions[0]`: that field is hand-ordered, and
		# every other consumer goes through the sorted `tools.versions` helpers — so
		# indexing it raw meant two readers of one field disagreeing on which line
		# is "first".
		primary_line = newest_line()
		runners_versions_json = (
			f'executors/{primary_line}.x/docs/website/src/python-sdk/runners-versions.json'
		)
		with ci_lib.github_group('generate runner-versions'):
			ci_lib.run(
				[sys.executable, 'docs/website/generate.py', runners_versions_json],
			)

		# Manager site first: it owns the intersphinx inventory the executor
		# sub-sites cross-link against (SDK → spec/impl-spec). Cross-linking is
		# one-way, so a single manager pass is enough — nothing here references
		# the sub-sites back.
		#
		# `?submodules=1`: the manager flake imports the executor lines, so the
		# executors/<line>.x paths must be in the flake source tree.
		ci_lib.nix_develop(
			'.?submodules=1#gen-docs',
			['./support/ci/run.sh', 'pipeline', 'docs-src'],
		)

		# Each active executor line builds its own standalone docs sub-site in its
		# own `gen-docs` shell (self-contained sphinx toolchain), accumulated
		# under build/doc/html/executors/<line>/ and linked from the manager's
		# generated executors.rst page. Built after the manager site so its
		# objects.inv exists (intersphinx) and its html root is not clobbered.
		doc_root = ci_lib.ROOT_DIR / 'build' / 'doc'
		for line in active_lines():
			exec_prefix = f'executors/{line}.x'
			src = (ci_lib.ROOT_DIR / exec_prefix / 'docs' / 'website' / 'src').resolve()
			installable = f'./{exec_prefix}#gen-docs'
			ci_lib.nix_develop(
				installable,
				['sphinx-build', '-b', 'html', src, doc_root / 'html' / 'executors' / line],
				subcommand_group=f'sphinx build executor sub-site: {line}',
			)

			# Lines that ship the Python SDK also feed the manager's "docs for
			# LLMs" bundle: build the text output and fold python-sdk into
			# _static/ai/api.txt (where the manager's llms.txt points).
			if (src / 'python-sdk').is_dir():
				text_out = doc_root / 'text' / 'executors' / line
				ci_lib.nix_develop(
					installable,
					['sphinx-build', '-b', 'text', src, text_out],
					subcommand_group=f'sphinx text (ai bundle): {line}',
				)
				ci_lib.run(
					[
						sys.executable,
						'docs/website/merge_txts.py',
						text_out / 'python-sdk',
						doc_root / 'html' / '_static' / 'ai' / 'api.txt',
					]
				)
		return 0


class DocsSrc(ci_lib.Pipeline):
	"""
	Build docs inside the docs Nix environment.
	"""

	def name(self) -> str:
		return 'docs-src'

	def handler(self, args: argparse.Namespace) -> int:
		out_base = (ci_lib.ROOT_DIR / 'build' / 'doc').resolve()
		out_base.mkdir(parents=True, exist_ok=True)

		# sphinx and its extensions come from the gen-docs Nix shell (the
		# `sphinx-build` on PATH is wrapped against that env), so there is no
		# venv/poetry step: conf.py lives at docs/website/src/conf.py, which
		# sphinx-build discovers from the source dir.
		src = (ci_lib.ROOT_DIR / 'docs' / 'website' / 'src').resolve()
		with ci_lib.github_group('sphinx build (html + text)'):
			ci_lib.run(['sphinx-build', '-b', 'html', src, out_base / 'html'])
			ci_lib.run(['sphinx-build', '-b', 'text', src, out_base / 'text'])

		# The Python SDK moved to the executor sub-sites, so its api.txt bundle is
		# produced there (see Docs above); the manager owns spec + impl-spec.
		with ci_lib.github_group('merge AI txt bundles'):
			for section in ('spec', 'impl-spec'):
				ci_lib.run(
					[
						sys.executable,
						'docs/website/merge_txts.py',
						out_base / 'text' / section,
						out_base / 'html' / '_static' / 'ai' / f'{section}.txt',
					]
				)
		return 0


COMMANDS = [Docs(), DocsSrc()]
