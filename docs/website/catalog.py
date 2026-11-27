#!/usr/bin/env python3
"""
Write the `index.html` of a manager version directory: its manager, executor and runner pages.
"""

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'common'))

import conf_common  # noqa: E402


def executor_list(root: Path) -> str:
	items = []
	for line, entry in conf_common.MONOREPO['executor-lines'].items():
		version = conf_common.executor_version(line)
		path = conf_common.executor_path(version)
		if not (root / path / 'index.html').exists():
			continue
		status = html.escape(entry['status'])
		items.append(
			f'<tr><td><a href="{html.escape(path)}index.html">{html.escape(version)}</a></td>'
			f'<td><span class="latest {status}">{status}</span></td></tr>'
		)
	if not items:
		return '<p class="empty">Nothing built</p>'
	return f'<table class="lines"><tbody>{"".join(items)}</tbody></table>'


def latest_in() -> dict[tuple[str, str], list[tuple[str, str]]]:
	"""
	`(executor version, line status)` of each line a runner uid is the latest of its id in
	"""
	path = conf_common.MANAGER_ROOT / 'build' / 'doc' / 'runners.json'
	lines = conf_common.MONOREPO['executor-lines']
	res: dict[tuple[str, str], list[tuple[str, str]]] = {}
	for r in json.loads(path.read_text()) if path.exists() else []:
		if r['latest']:
			res.setdefault((r['id'], r['hash32']), []).append(
				(conf_common.executor_version(r['line']), lines[r['line']]['status'])
			)
	return res


def runner_list(root: Path) -> str:
	# `_static` and the like are shared assets, not runners
	ids = (
		sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith('_'))
		if root.is_dir()
		else []
	)
	if not ids:
		return '<p class="empty">Nothing built</p>'
	latest = latest_in()
	rows = []
	for runner_id in ids:
		hashes = sorted(
			d.name for d in (root / runner_id).iterdir() if (d / 'index.html').exists()
		)
		links = ''.join(
			f'<div><a href="runner/{html.escape(runner_id)}/{html.escape(h)}/index.html"><code>{html.escape(h)}</code></a>'
			+ ''.join(
				f'<span class="latest {html.escape(status)}" title="latest in executor {html.escape(v)} ({html.escape(status)})">{html.escape(v)}</span>'
				for v, status in latest.get((runner_id, h), [])
			)
			+ '</div>'
			for h in hashes
		)
		rows.append(
			f'<tr><th scope="row">{html.escape(runner_id)}</th><td>{links}</td></tr>'
		)
	return f'<table class="runners"><tbody>{"".join(rows)}</tbody></table>'


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GenVM {version}</title>
<style>
:root {{
	--fg: #2b2b2b; --muted: #6b6b6b; --bg: #ffffff; --link: #1210ff; --rule: #e4e4e4;
	font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
}}
@media (prefers-color-scheme: dark) {{
	:root {{ --fg: #e0e0e0; --muted: #9a9a9a; --bg: #14181e; --link: #7b9aff; --rule: #2c323b; }}
}}
body {{ margin: 0 auto; max-width: 64rem; padding: 2rem 16px; color: var(--fg); background: var(--bg); }}
a {{ color: var(--link); }}
h1 {{ font-size: 1.8rem; margin-bottom: 0.25rem; }}
h2 {{ font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid var(--rule); padding-bottom: 0.25rem; }}
p.lead, p.empty, p.hint {{ color: var(--muted); }}
ul.versions {{ padding: 0; list-style: none; }}
table.lines {{ border-collapse: collapse; }}
table.lines td {{ padding: 0.25rem 1rem 0.25rem 0; }}
table.lines span.latest {{ margin-left: 0; }}
table.runners {{ border-collapse: collapse; width: 100%; }}
table.runners th {{ text-align: left; white-space: nowrap; padding: 0.35rem 1rem 0.35rem 0; vertical-align: top; }}
table.runners td {{ padding: 0.35rem 0; overflow-wrap: anywhere; }}
table.runners td div + div {{ margin-top: 0.25rem; }}
table.runners tr {{ border-bottom: 1px solid var(--rule); }}
table.runners code {{ font-size: 0.8rem; }}
span.latest {{ margin-left: 0.5rem; padding: 0.05rem 0.45rem; border-radius: 0.6rem; font-size: 0.75rem; font-weight: 600; white-space: nowrap; color: #fff; background: #6b6b6b; }}
span.latest.current {{ background: #1a7f37; }}
span.latest.maintained {{ color: #2b2b2b; background: #e3b341; }}
span.latest.replay-only {{ background: #cf222e; }}
</style>
</head>
<body>
<h1>GenVM {version}</h1>
<p class="lead">Everything this manager version ships</p>
<h2>Manager</h2>
<p class="hint">The host-facing service: how it runs executors, its API and internals</p>
<ul class="versions"><li><a href="{manager}index.html">{version}</a></li></ul>
<h2>Executors</h2>
<p class="hint">The virtual machine: its specification and the Python SDK guides</p>
{executors}
<h2>Runners</h2>
<p class="hint">Content-addressed packages contracts depend on, 1 page per hash. A badge names each executor the hash is the latest in:
<span class="latest current">current</span><span class="latest maintained">maintained</span><span class="latest replay-only">replay-only</span></p>
{runners}
</body>
</html>
"""


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
	parser.add_argument('root', type=Path, help='the version directory of a built site')
	args = parser.parse_args()

	page = PAGE.format(
		version=html.escape(conf_common.MONOREPO['version']),
		manager=conf_common.MANAGER_PATH,
		executors=executor_list(args.root),
		runners=runner_list(args.root / 'runner'),
	)
	(args.root / 'index.html').write_text(page)
	return 0


if __name__ == '__main__':
	sys.exit(main())
