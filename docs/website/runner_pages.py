#!/usr/bin/env python3
"""
Build a page per runner of 1 executor line, under `<version dir>/runner/<id>/<hash>/`.

Runs in that line's `gen-docs` shell: reference pages import the line's own runner
sources. Reads `build/doc/runners.json`, which `generate.py` writes.
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

from sphinx.cmd.build import build_main

WEBSITE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WEBSITE_DIR / 'common'))

import conf_common  # noqa: E402

UID_RE = re.compile(r'"([A-Za-z0-9_.\-]+):([0-9a-zA-Z]+)"')

# Listing a cpython zip in full helps nobody
MAX_LISTED_FILES = 64

# Under runner/, beside the runner ids; the leading `_` keeps it apart from them
SHARED_STATIC = '_static'
STATIC_REF_RE = re.compile(r"""((?:href|src)=["'])_static/""")


def runner_zip(runner: dict) -> Path | None:
	h = runner['hash32']
	path = (
		conf_common.MANAGER_ROOT
		/ 'build'
		/ 'out'
		/ 'runners'
		/ runner['id']
		/ h[:2]
		/ f'{h[2:]}.zip'
	)
	return path if path.exists() else None


def reference_docs(line: str, runner_id: str) -> Path | None:
	"""
	Hand-written reference for a runner: `runners/<dir>/docs/<id>.rst` of its line
	"""
	found = sorted(
		(conf_common.executor_dir(line) / 'runners').glob(f'*/docs/{runner_id}.rst')
	)
	if len(found) > 1:
		sys.exit(f'{runner_id}: more than 1 reference page: {found}')
	return found[0] if found else None


def rendered_json(manifest: str, known: set[tuple[str, str]]) -> str:
	"""
	`runner.json` as an HTML block whose runner uids link to their pages
	"""
	pretty = html.escape(json.dumps(json.loads(manifest), indent=2), quote=False)

	def link(match: re.Match) -> str:
		runner_id, h = match.group(1), match.group(2)
		if (runner_id, h) not in known:
			return match.group(0)
		return f'"<a href="../../{runner_id}/{h}/index.html">{runner_id}:{h}</a>"'

	return f'<pre class="gl-runner-json">{UID_RE.sub(link, pretty)}</pre>'


def page_rst(
	runner: dict, known: set[tuple[str, str]], reference: Path | None, src: Path
) -> str:
	uid = f'{runner["id"]}:{runner["hash32"]}'
	title = runner['id']
	parts = [
		'*' * len(title),
		title,
		'*' * len(title),
		'',
		'.. code-block:: text',
		'',
		f'   {uid}',
		'',
		'Depend on exactly this content with:',
		'',
		'.. code-block:: json',
		'',
		f'   {{ "Depends": "{uid}" }}',
		'',
	]

	archive = runner_zip(runner)
	if archive is None:
		parts += [
			'.. note::',
			'',
			'   The archive was not available when this page was built, so its',
			'   manifest and contents are not shown',
			'',
		]
	else:
		with zipfile.ZipFile(archive) as zf:
			names = sorted(n for n in zf.namelist() if not n.endswith('/'))
			manifest = zf.read('runner.json').decode() if 'runner.json' in names else None
			sizes = sum(zf.getinfo(n).file_size for n in names)
		if manifest is not None:
			parts += ['Manifest', '========', '', '.. raw:: html', '']
			parts += ['   ' + line for line in rendered_json(manifest, known).splitlines()]
			parts.append('')
		others = [n for n in names if n != 'runner.json']
		if others:
			parts += ['Contents', '========', '', f'{len(others)} files, {sizes} bytes', '']
			parts += [f'- ``{n}``' for n in others[:MAX_LISTED_FILES]]
			if len(others) > MAX_LISTED_FILES:
				parts.append(f'- … {len(others) - MAX_LISTED_FILES} more')
			parts.append('')

	if reference is not None:
		# Sphinx resolves an absolute include path against the source root
		parts += [
			'Reference',
			'=========',
			'',
			f'.. include:: {os.path.relpath(reference, src)}',
			'',
		]
	return '\n'.join(parts)


def share_static(out: Path, shared_root: Path) -> None:
	"""
	Replace a page's `_static` with a shared copy named by its content hash, so
	pages built with the same theme store it once
	"""
	static = out / '_static'
	digest = hashlib.sha256()
	for path in sorted(p for p in static.rglob('*') if p.is_file()):
		digest.update(str(path.relative_to(static)).encode() + b'\0')
		digest.update(path.read_bytes())
	shared = shared_root / digest.hexdigest()[:20]
	if shared.exists():
		shutil.rmtree(static)
	else:
		shared.parent.mkdir(parents=True, exist_ok=True)
		static.rename(shared)
	# The page sits at runner/<id>/<hash>/, 2 levels below runner/
	target = f'../../{shared_root.name}/{shared.name}/'
	for page in out.glob('*.html'):
		text = page.read_text()
		page.write_text(STATIC_REF_RE.sub(lambda m: m.group(1) + target, text))


def build_page(
	runner: dict, known: set[tuple[str, str]], args: argparse.Namespace
) -> bool:
	reference = reference_docs(args.line, runner['id']) if runner['from-source'] else None
	docs_conf = reference.parent / 'conf.py' if reference is not None else None

	src = args.work / runner['id'] / runner['hash32']
	src.mkdir(parents=True, exist_ok=True)
	(src / 'index.rst').write_text(page_rst(runner, known, reference, src))

	os.environ['GENVM_RUNNER_PAGE'] = json.dumps(
		{
			'id': runner['id'],
			'hash32': runner['hash32'],
			'executor_version': conf_common.executor_version(args.line),
			'docs_conf': str(docs_conf)
			if docs_conf is not None and docs_conf.exists()
			else None,
		}
	)
	out = args.site / conf_common.runner_path(runner['id'], runner['hash32'])
	if out.exists():
		shutil.rmtree(out)
	code = build_main(
		[
			'-q',
			'-E',
			'-b',
			'html',
			'-d',
			str(src / '.doctrees'),
			'-c',
			str(WEBSITE_DIR / 'runner'),
			str(src),
			str(out),
		]
	)
	if code != 0:
		return False
	share_static(out, args.site / 'runner' / SHARED_STATIC)
	return True


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
	parser.add_argument('--line', required=True, help='executor line, e.g. `v0.4`')
	parser.add_argument(
		'--site', required=True, type=Path, help='version directory of the site output'
	)
	parser.add_argument(
		'--work', required=True, type=Path, help='scratch directory for page sources'
	)
	args = parser.parse_args()

	all_runners = json.loads(
		(conf_common.MANAGER_ROOT / 'build' / 'doc' / 'runners.json').read_text()
	)
	known = {(r['id'], r['hash32']) for r in all_runners}

	# A uid several lines ship gets 1 page: preferably from a line that builds it
	# from source, so it keeps its reference, then the first line in
	# `.genvm-monorepo-root` order, which lists the current line first
	line_order = list(conf_common.MONOREPO['executor-lines'])
	owner: dict[tuple[str, str], dict] = {}
	for runner in sorted(
		all_runners, key=lambda r: (not r['from-source'], line_order.index(r['line']))
	):
		owner.setdefault((runner['id'], runner['hash32']), runner)

	failed = []
	for runner in owner.values():
		if runner['line'] != args.line:
			continue
		if not build_page(runner, known, args):
			failed.append(f'{runner["id"]}:{runner["hash32"]}')

	for uid in failed:
		print(f'{uid}: sphinx failed')
	return 1 if failed else 0


if __name__ == '__main__':
	sys.exit(main())
