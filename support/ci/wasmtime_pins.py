#!/usr/bin/env python3
"""
One definition of "which upstream sources does an executor line vendor, and are
any of them subject to a published advisory".

The subjects live in the manager's own checkout: the gitlink at
`executors/<line>.x` is the snapshot that actually ships, so this reads them out
of the submodule tree rather than resolving the executor's `<line>-dev` /
`<line>.x` refs through the API — those move independently of what the manager
pins. Two files, because upstream is vendored twice over:

1. `.git-third-party/manifest.json` names each vendored repo and the upstream
	commit it is pinned at;
2. `executor/Cargo.lock` names the crates that come out of those repos. A
	vendored tree is dozens of crates, not one — `cranelift-codegen`,
	`wasmparser` and `wiggle` are where most advisories against this stack are
	actually published, and none of them shares a name with the repo it lives in.

Both are queried against OSV, because neither covers the other: a commit query
misses a crate whose advisory names no commit range, and a version query misses
a vendored tree pinned between releases.

A crate is vendored if the lock gives it no `source` (so it is a path
dependency) and no tracked `Cargo.toml` in the executor repo declares it. The
residue of that rule is a first-party crate with no committed manifest, which
gets one pointless crates.io query — a false positive a reader can dismiss,
where the opposite mistake is a silent gap.

Nothing here knows which features GenVM disables or what its patch series
changes, so a hit is a finding to rule on, not a proven exposure.
"""

import dataclasses
import json
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import ci_lib

MANIFEST = Path('.git-third-party') / 'manifest.json'
CARGO_LOCK = Path('executor') / 'Cargo.lock'
OSV_QUERY_URL = 'https://api.osv.dev/v1/query'

# OSV is a third party on a daily job's critical path, so a blip must not cost a
# day of monitoring. Mirrors gh_common.gh's policy for the same reason.
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF_S = 2.0
# A server that keeps handing back a page token would otherwise spin until the
# job's timeout. No real query is anywhere near this deep.
_MAX_PAGES = 50


@dataclasses.dataclass(frozen=True, order=True)
class Subject:
	"""
	One thing an advisory can be published against, in one executor line.

	Exactly one of `commit` (a vendored repo at its pinned upstream commit) and
	`version` (a crate that comes out of one) is set.
	"""

	line: str
	name: str
	version: str = ''
	commit: str = ''

	def query(self) -> dict:
		if self.commit:
			return {'commit': self.commit}
		return {
			'package': {'name': self.name, 'ecosystem': 'crates.io'},
			'version': self.version,
		}

	def __str__(self) -> str:
		what = f'@{self.commit[:12]}' if self.commit else f' {self.version}'
		return f'`{self.line}` {self.name}{what}'


@dataclasses.dataclass(frozen=True, order=True)
class Advisory:
	id: str
	summary: str

	@property
	def url(self) -> str:
		return f'https://osv.dev/vulnerability/{self.id}'


def canonical_id(entry: dict) -> str:
	"""
	The single id an OSV entry is tracked under here.

	OSV returns the same advisory under several aliases and reports whichever the
	matching source used, so the raw `id` is not stable across queries — and this
	id is what `wasmtime_watch` compares to decide whether a finding is new. GHSA
	first because every advisory reaching a Rust crate has one; RUSTSEC next; the
	OSV id only when neither exists.
	"""
	ids = sorted({entry['id'], *entry.get('aliases', [])})
	for prefix in ('GHSA-', 'RUSTSEC-'):
		for candidate in ids:
			if candidate.startswith(prefix):
				return candidate
	return entry['id']


def workspace_crates(tree: Path) -> set[str]:
	"""
	Crate names declared by a tracked `Cargo.toml` in the executor repo.

	Tracked, not on disk: the vendored trees are git-ignored, so they are absent
	from this list whether or not `git third-party update` has materialized them.
	Globbing the filesystem instead would classify every vendored crate as
	first-party the moment someone runs the tool on a materialized checkout,
	which is exactly the failure that stays silent.
	"""
	paths = subprocess.run(
		['git', '-C', str(tree), 'ls-files', '*Cargo.toml'],
		check=True,
		text=True,
		capture_output=True,
	).stdout.split()

	names = set()
	for path in paths:
		name = tomllib.loads((tree / path).read_text()).get('package', {}).get('name')
		if isinstance(name, str):
			names.add(name)
	return names


def subjects(line: str, *, root: Path | None = None) -> list[Subject]:
	"""
	Everything of one executor line that an advisory could be published against.

	Needs the submodule checked out; `get-all-git.py --third-party=none` is
	enough, since both files it reads are tracked.
	"""
	tree = (root or ci_lib.ROOT_DIR) / 'executors' / f'{line}.x'
	repos = json.loads((tree / MANIFEST).read_bytes()).get('repos')
	if not repos:
		# A monitor that finds nothing to monitor is indistinguishable from a
		# clean bill of health, so refuse to report one.
		raise ValueError(f'{line}: {MANIFEST} lists no vendored repos')

	result = []
	for key, repo in sorted(repos.items()):
		commit = repo.get('commit')
		if not isinstance(commit, str) or len(commit) != 40:
			raise ValueError(f'{line}: {key} has no usable commit pin: {commit!r}')
		result.append(Subject(line=line, name=key.rsplit('/', 1)[-1], commit=commit))

	first_party = workspace_crates(tree)
	lock = tomllib.loads((tree / CARGO_LOCK).read_text())
	for package in sorted(lock.get('package', []), key=lambda p: str(p.get('name'))):
		name, version = package.get('name'), package.get('version')
		if 'source' in package or name in first_party:
			continue
		if not isinstance(name, str) or not isinstance(version, str):
			raise ValueError(f'{line}: {CARGO_LOCK} has a package with no name/version')
		result.append(Subject(line=line, name=name, version=version))
	return result


def _post(query: dict) -> dict:
	request = urllib.request.Request(
		OSV_QUERY_URL,
		data=json.dumps(query).encode(),
		headers={'Content-Type': 'application/json', 'User-Agent': 'genvm-ci'},
		method='POST',
	)
	for attempt in range(_RETRY_ATTEMPTS):
		try:
			with urllib.request.urlopen(request, timeout=30) as response:
				return json.load(response)
		except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
			# A 4xx is the caller's fault and will fail identically on a replay.
			terminal = isinstance(error, urllib.error.HTTPError) and error.code < 500
			if terminal or attempt + 1 == _RETRY_ATTEMPTS:
				raise RuntimeError(f'OSV query {query} failed: {error}') from error
			time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
	raise AssertionError('unreachable')


def _osv(query: dict) -> list[dict]:
	entries: list[dict] = []
	page = dict(query)
	for _ in range(_MAX_PAGES):
		response = _post(page)
		entries.extend(response.get('vulns', []))
		token = response.get('next_page_token')
		if not token:
			return entries
		page = {**query, 'page_token': token}
	raise RuntimeError(f'OSV query {query} did not stop paginating')


def advisories_for(subject: Subject, cache: dict | None = None) -> list[Advisory]:
	"""
	Published advisories matching one subject.

	`cache` is keyed by the query rather than by the subject, so the two lines'
	shared crates cost one request between them.
	"""
	query = subject.query()
	key = json.dumps(query, sort_keys=True)
	if cache is not None and key in cache:
		return cache[key]

	found: dict[str, Advisory] = {}
	for entry in _osv(query):
		canonical = canonical_id(entry)
		found[canonical] = Advisory(
			id=canonical,
			summary=(entry.get('summary') or 'no summary').replace('\n', ' '),
		)
	result = sorted(found.values())
	if cache is not None:
		cache[key] = result
	return result


def scan(
	lines: list[str], *, root: Path | None = None
) -> dict[Advisory, list[Subject]]:
	"""
	Every advisory affecting `lines`, with the subjects it was matched against.
	"""
	cache: dict[str, list[Advisory]] = {}
	result: dict[Advisory, list[Subject]] = {}
	for line in lines:
		checked = subjects(line, root=root)
		print(f'{line}: {len(checked)} subject(s)')
		for subject in checked:
			for advisory in advisories_for(subject, cache):
				print(f'  {subject}: {advisory.id}')
				result.setdefault(advisory, []).append(subject)
	return dict(sorted(result.items()))
