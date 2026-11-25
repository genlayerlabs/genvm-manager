#!/usr/bin/env python3

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'common'))

import conf_common  # noqa: E402

script_dir = Path(__file__).resolve().parent
genvm_root_dir = script_dir
while not genvm_root_dir.joinpath('.genvm-monorepo-root').exists():
	genvm_root_dir = genvm_root_dir.parent

docs_src_root_dir = genvm_root_dir.joinpath('docs', 'website', 'src')

# Runners belong to the EXECUTOR, not the manager: a runner's version is an
# executor-version, tagged in the executor repo, and its sources live under that
# repo's runners/. So every git lookup behind this page — tags, tag messages, the
# log between two runner versions — has to run against an executor checkout, not
# the manager's. All active lines are branches of ONE repo, so any of them sees
# every executor tag; take the first.
monorepo = json.loads(genvm_root_dir.joinpath('.genvm-monorepo-root').read_text())


def _current_line() -> str:
	"""
	The `current` executor line, e.g. `v0.3`.

	Must agree with `support/ci/tools/versions.py:current_line`, which decides
	the sub-site this page is generated into; otherwise this page renders one
	line's runner data into another line's site.
	"""
	current = [
		line
		for line, entry in monorepo['executor-lines'].items()
		if entry['status'] == 'current'
	]
	if len(current) != 1:
		sys.exit(f'.genvm-monorepo-root: exactly 1 `current` line, found {current}')
	return current[0]


executor_dir = genvm_root_dir.joinpath('executors', f'{_current_line()}.x')


def executor_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
	return subprocess.run(
		['git', '-C', str(executor_dir), *args],
		check=check,
		capture_output=True,
		text=True,
	)


origin_url = executor_git('remote', 'get-url', 'origin').stdout.strip()

proc = executor_git('ls-remote', '--tags', origin_url)

commit2tag = {}

bad_id = re.compile(r'[^0-9a-z._\-A-Z]')

for line in proc.stdout.splitlines():
	line = line.strip()
	comm_rev = line.split()
	if len(comm_rev) != 2:
		continue
	commit, rev = comm_rev

	rev = rev.removeprefix('refs/tags/')

	commit2tag[commit] = bad_id.sub('_', rev)

# Ensure tags are available locally (CI may use a shallow checkout)
executor_git('fetch', '--tags', check=False)

# Collect tag messages (one-line) for descriptions
proc_tags = executor_git('tag', '-l', '-n1')
tag_messages = {}
for line in proc_tags.stdout.splitlines():
	parts = line.split(None, 1)
	if len(parts) == 2:
		tag_messages[parts[0]] = parts[1].strip()
	elif len(parts) == 1:
		tag_messages[parts[0]] = ''

eval_file = genvm_root_dir.joinpath('runners', 'docs.nix')

proc = subprocess.run(
	[
		'nix',
		'eval',
		'--verbose',
		'--impure',
		'--read-only',
		'--show-trace',
		'--json',
		'--file',
		str(eval_file),
		'--apply',
		'f: f { }',
	],
	check=True,
	capture_output=True,
	text=True,
)

all_runners = json.loads(proc.stdout)


def line_of(executor_version: str) -> str:
	(line,) = (
		line
		for line in monorepo['executor-lines']
		if executor_version.startswith(line + '.')
	)
	return line


for runner in all_runners:
	runner['line'] = line_of(runner['version'])

# Latest is what nothing else in its line supersedes; a line without
# `supersedes` data (the legacy one) keeps every hash of an id, since each is
# equally accepted
superseded = {
	(r['line'], r['id'], r['supersedes']) for r in all_runners if r['supersedes']
}
for runner in all_runners:
	runner['latest'] = (runner['line'], runner['id'], runner['hash32']) not in superseded

# Consumed by runner_pages.py, which runs in each line's own docs shell, and the
# executor sites' conf.py
runners_json = genvm_root_dir / 'build' / 'doc' / 'runners.json'
runners_json.parent.mkdir(parents=True, exist_ok=True)
runners_json.write_text(json.dumps(all_runners, indent=2))

# The changelog below is per line: post-split the lines are independent — v0.2
# is not "before" v0.3 — so other lines' hashes must not leak in as history
_line = _current_line()
res: dict[str, dict[str, list[str]]] = {}
for runner in all_runners:
	if runner['line'] == _line:
		res.setdefault(runner['version'], {}).setdefault(runner['id'], []).append(
			runner['hash32']
		)

# --- Post-process: sort by semver, enrich with descriptions, diffs, commits ---

SEMVER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(.*)$')


def semver_key(version: str):
	"""Return sort key: numeric semver ascending."""
	m = SEMVER_RE.match(version)
	if m:
		return (0, int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4))
	return (1, version)


def semver_key_desc(version: str):
	"""Return sort key: numeric semver descending."""
	m = SEMVER_RE.match(version)
	if m:
		return (0, -int(m.group(1)), -int(m.group(2)), -int(m.group(3)), m.group(4))
	return (1, version)


def is_semver(version: str) -> bool:
	return SEMVER_RE.match(version) is not None


# Build tag2commit reverse mapping
tag2commit = {tag: commit for commit, tag in commit2tag.items()}

# Sort versions ascending by semver for diffing
all_versions = sorted(res.keys(), key=semver_key)

semver_versions = [v for v in all_versions if is_semver(v)]

# Compute runner changes between consecutive versions
runner_changes_map = {}  # version -> {runner: {old, new}}
prev_runners = {}
for ver in semver_versions:
	runners = res[ver]
	changes = {}
	for rid, hashes in runners.items():
		old_hashes = prev_runners.get(rid)
		if old_hashes is None:
			# Runner newly introduced
			changes[rid] = {'old': None, 'new': hashes[0] if hashes else None}
		elif set(hashes) != set(old_hashes):
			changes[rid] = {
				'old': old_hashes[0] if old_hashes else None,
				'new': hashes[0] if hashes else None,
			}
	runner_changes_map[ver] = changes
	prev_runners = runners

# Collect git log between consecutive versions
commits_map = {}  # version -> [commit messages]
for i, ver in enumerate(semver_versions):
	if i == 0:
		commits_map[ver] = []
		continue
	prev_ver = semver_versions[i - 1]
	prev_commit_hash = tag2commit.get(prev_ver)
	curr_commit_hash = tag2commit.get(ver)
	if prev_commit_hash and curr_commit_hash:
		# In the executor repo, and over ITS runners/ — the manager's history has
		# no executor commits (they sit behind a gitlink), so asking it for the
		# log between two runner versions silently yields nothing.
		log_proc = executor_git(
			'log',
			'--oneline',
			f'{prev_commit_hash}..{curr_commit_hash}',
			'--',
			'runners/',
			check=False,
		)
		commits_map[ver] = (
			[
				line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else line
				for line in log_proc.stdout.strip().splitlines()
				if line.strip()
			]
			if log_proc.returncode == 0
			else []
		)
	else:
		commits_map[ver] = []

# Build enriched JSON
enriched_versions = []
for ver in sorted(semver_versions, key=semver_key_desc):
	entry = {
		'version': ver,
		'description': tag_messages.get(ver, ''),
		'runners': res[ver],
		'runner_changes': runner_changes_map.get(ver, {}),
		'commits': commits_map.get(ver, []),
	}
	enriched_versions.append(entry)

# Build per-runner version history
all_runner_ids = set()
for ver in semver_versions:
	all_runner_ids.update(res[ver].keys())

runners_history = {}
for rid in sorted(all_runner_ids):
	seen_hashes = []
	for ver in sorted(semver_versions, key=semver_key_desc):
		hashes = res[ver].get(rid)
		if hashes:
			h = hashes[0]
			if not seen_hashes or seen_hashes[-1]['hash'] != h:
				seen_hashes.append({'hash': h, 'introduced_in': ver})
	runners_history[rid] = {'versions': seen_hashes}

enriched_json = {
	'versions': enriched_versions,
	'runners': runners_history,
}

json_path = Path(sys.argv[1])
json_path.write_text(json.dumps(enriched_json, indent=2))

# --- RST generation helpers ---


def rst_heading(text, char):
	return f'{text}\n{char * len(text)}'


def make_table(rows, headers):
	"""Build an RST grid table from headers + rows (list of lists)."""
	col_widths = [len(h) for h in headers]
	for row in rows:
		for i, cell in enumerate(row):
			col_widths[i] = max(
				col_widths[i], max((len(l) for l in cell.split('\n')), default=0)
			)

	def sep(ch='-'):
		return '+' + '+'.join(ch * (w + 2) for w in col_widths) + '+'

	def fmt_row(row):
		split_cells = [cell.split('\n') for cell in row]
		max_lines = max(len(c) for c in split_cells)
		lines = []
		for line_idx in range(max_lines):
			parts = []
			for i, cell_lines in enumerate(split_cells):
				text = cell_lines[line_idx] if line_idx < len(cell_lines) else ''
				parts.append(f' {text:<{col_widths[i]}} ')
			lines.append('|' + '|'.join(parts) + '|')
		return '\n'.join(lines)

	parts = [sep('-')]
	parts.append(fmt_row(headers))
	parts.append(sep('='))
	for row in rows:
		parts.append(fmt_row(row))
		parts.append(sep('-'))
	return '\n'.join(parts)


def version_to_anchor(ver):
	"""Convert version string to changelog anchor: v0.1.8 -> v0-1-8"""
	res = 'gvm_ver_' + ver.replace('.', '-')
	while res[-1] in ['_', '-']:
		res = res[:-1]
	return res


RUNNER_DESCRIPTIONS = {
	'py-genlayer': 'Standard runner for single-file Python intelligent contracts. Includes CPython, the genlayer standard library, and all core dependencies.',
	'py-genlayer-multi': 'Runner for multi-file Python contract packages. Same dependencies as py-genlayer.',
	'py-lib-genlayer-embeddings': 'Adds semantic search and vector embedding support. Must be specified before py-genlayer in a Seq block.',
	'cpython': 'CPython 3.13 interpreter compiled to WebAssembly.',
	'softfloat': 'Software floating-point implementation for deterministic arithmetic.',
	'py-lib-cloudpickle': 'Object serialization library.',
	'py-lib-genlayer-std': 'GenLayer standard library providing the gl.* API.',
	'py-lib-protobuf': 'Protocol Buffers serialization.',
	'py-lib-word_piece_tokenizer': 'Tokenizer for embedding models.',
	'models-all-MiniLM-L6-v2': 'Pre-trained sentence embedding model (ONNX).',
}

# --- Generate each line's runner table ---


def runner_link(runner: dict, page_depth: int) -> str:
	"""
	RST link from a page `page_depth` directories below an executor site root
	"""
	target = '../' * page_depth + conf_common.relative(
		'executor', conf_common.runner_path(runner['id'], runner['hash32']) + 'index.html'
	)
	return f'`{runner["hash32"]} <{target}>`__'


def line_runners_rst(line: str, page_depth: int) -> str:
	runners = sorted(
		(r for r in all_runners if r['line'] == line), key=lambda r: (r['id'], r['hash32'])
	)
	status = monorepo['executor-lines'][line]['status']
	parts = [
		rst_heading('Available Runners', '='),
		'',
		f'Every runner executor {runners[0]["version"] if runners else line} ships. '
		'A runner is addressed by its content hash, so each links to the one page '
		'for that exact content',
		'',
		rst_heading('Latest', '-'),
		'',
	]
	rows = [
		[r['id'], RUNNER_DESCRIPTIONS.get(r['id'], ''), runner_link(r, page_depth)]
		for r in runners
		if r['latest']
	]
	parts += [make_table(rows, ['Runner', 'Description', 'Hash']), '']
	parts += [rst_heading('All', '-'), '']
	rows = [
		[
			r['id'],
			runner_link(r, page_depth)
			+ (f' :gl-latest-{status}:`latest`' if r['latest'] else ''),
			f'``{r["fork"]}``' if r['fork'] else '',
			f'``{r["supersedes"]}``' if r['supersedes'] else '',
		]
		for r in runners
	]
	parts += [make_table(rows, ['Runner', 'Hash', 'Fork', 'Supersedes']), '']
	return '\n'.join(parts)


SDK_REFERENCE_RUNNERS = ['py-lib-genlayer-std', 'py-lib-genlayer-embeddings']


def line_reference_rst(line: str, page_depth: int) -> str:
	latest = [r for r in all_runners if r['line'] == line and r['latest']]
	parts = [
		rst_heading('API Reference', '='),
		'',
		'The reference is published with each runner version; the ones this executor ships:',
		'',
	]
	for rid in SDK_REFERENCE_RUNNERS:
		parts += [
			f'- ``{rid}`` {runner_link(r, page_depth)}' for r in latest if r['id'] == rid
		]
	return '\n'.join(parts) + '\n'


for line in monorepo['executor-lines']:
	sdk_dir = (
		genvm_root_dir
		/ 'executors'
		/ f'{line}.x'
		/ 'docs'
		/ 'website'
		/ 'src'
		/ 'python-sdk'
	)
	if sdk_dir.is_dir():
		(sdk_dir / 'available-runners.rst').write_text(line_runners_rst(line, 1))
		if not (sdk_dir / 'reference').is_dir():
			(sdk_dir / 'reference.rst').write_text(line_reference_rst(line, 1))

# --- Generate changelog ---

# Written into the current line's SDK docs, from runner data only the manager has
sdk_src_root_dir = executor_dir / 'docs' / 'website' / 'src'

changelog_rst_path = sdk_src_root_dir / 'python-sdk' / 'changelog.rst'

changelog_rst = []

changelog_rst = [
	"""\
Changelog
=========

Release history for the Python SDK, including API changes and runner version updates.
"""
]

for entry in enriched_versions:
	ver = entry['version']
	desc = entry['description']
	runners = entry['runners']
	changes = entry['runner_changes']
	commits = entry['commits']

	# Find previous version for "no changes" message
	ver_idx = semver_versions.index(ver)
	prev_ver = semver_versions[ver_idx - 1] if ver_idx > 0 else None

	changelog_rst.append(f'.. _{version_to_anchor(ver)}:')
	changelog_rst.append('')
	changelog_rst.append(rst_heading(ver, '-'))
	changelog_rst.append('')
	if desc:
		changelog_rst.append(f'*{desc}*')
		changelog_rst.append('')

	if changes:
		n = len(changes)
		s = 's' if n != 1 else ''
		changelog_rst.append('')
		change_rows = []
		for rid in sorted(changes.keys()):
			ch = changes[rid]
			old_h = ch['old']
			new_h = ch['new']
			if old_h is None:
				change_rows.append([rid, '*new*', f'``{new_h}``'])
			else:
				change_rows.append([rid, f'``{old_h}``', f'``{new_h}``'])
		changelog_rst.append(make_table(change_rows, ['Runner', 'From', 'To']))
		changelog_rst.append('')

		if commits:
			changelog_rst.append('Commits touching runners:')
			changelog_rst.append('')
			for msg in commits:
				changelog_rst.append(f'- ``{msg}``')
			changelog_rst.append('')

		# Full runner hashes table
		rows = []
		for rid in sorted(runners.keys()):
			hashes = runners[rid]
			hash_str = '\n'.join(f'``{h}``' for h in hashes)
			rows.append([rid, hash_str])
		if rows:
			changelog_rst.append(make_table(rows, ['Runner', 'Hash']))
			changelog_rst.append('')
	else:
		if prev_ver:
			changelog_rst.append(f'No runner changes from {prev_ver}.')
		else:
			changelog_rst.append('Initial release.')
		changelog_rst.append('')

changelog_rst_path.write_text('\n'.join(changelog_rst) + '\n')

# --- Executor sites this manager ships, included by overview/index.rst ---


# The overview page sits 1 level below manager/
executor_links = []
for line, entry in monorepo['executor-lines'].items():
	ver = conf_common.executor_version(line)
	target = '../' + conf_common.relative('manager', conf_common.executor_path(ver))
	executor_links.append(f'- `{ver} <{target}index.html>`__ ({entry["status"]})')
(docs_src_root_dir / 'overview' / 'executor-lines_generated.rst').write_text(
	'\n'.join(executor_links) + '\n'
)
