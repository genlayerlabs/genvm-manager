"""
`genvm-tool audit` — list published advisories against third-party dependencies.

3 passes, each skippable:

1. lockfiles: every lockfile tracked in the manager and its executor submodules,
	through `osv-scanner` (in the dev shell). Crates of vendored upstream trees
	(wasmtime, wasm-tools) are included, first-party path crates are not
2. vendored: each repo in an executor's `.git-third-party/manifest.json`, by its
	pinned commit, against OSV. An advisory the lockfile pass already reported
	is not repeated
3. native: every `cpes` entry in `libs/deps/dependency-urls.json`, against NVD.
	Only native code the manager or an executor links opts in, by listing the CPE
	of each product it ships at the version it ships. Set `NVD_API_KEY` to lift
	NVD's rate limit

None of the sources knows which features GenVM disables or what its patches
change, so a finding is a candidate to rule on, not a proven exposure.

`--markdown` writes a report; `--sarif` writes the findings for GitHub code
scanning, which is where the nightly run publishes them. Exits 0 whether or not
anything is found; a scanner failure, or a pass with nothing to scan, exits
non-zero.
"""

import dataclasses
import hashlib
import json
import operator
import os
import re
import subprocess
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import common

NAME = 'audit'
HELP = 'list published advisories against third-party dependencies'

LOCKFILES = frozenset(
	{
		'Cargo.lock',
		'package-lock.json',
		'poetry.lock',
		'requirements.txt',
		'uv.lock',
	}
)
VENDORED_MANIFEST = '.git-third-party/manifest.json'
DEPS_FILE = Path('libs/deps/dependency-urls.json')
OSV_QUERY_URL = 'https://api.osv.dev/v1/query'
NVD_CVES_URL = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
NVD_API_KEY_ENV = 'NVD_API_KEY'
USER_AGENT = 'genvm-tool-audit'

# osv-scanner exits 1 when it found something; 128 means no package was found,
# which for these lockfiles is a broken scan, not a clean one
_OSV_RESULT_CODES = (0, 1)
_OSV_ATTEMPTS = 4
_OSV_BACKOFF_S = 2.0
_OSV_MAX_PAGES = 50
_GHSA_SEVERITY_SCORES = {'LOW': 3.0, 'MODERATE': 5.5, 'HIGH': 8.0, 'CRITICAL': 9.5}
# NVD allows 5 requests per 30s without a key and 50 with one
_NVD_PAUSE_S = 6.5
_NVD_PAUSE_WITH_KEY_S = 0.7
_NVD_ATTEMPTS = 4
_NVD_BACKOFF_S = 10.0


def configure(parser):
	parser.add_argument(
		'--markdown', metavar='FILE', help='also write the findings as a Markdown report'
	)
	parser.add_argument(
		'--sarif',
		metavar='FILE',
		help='also write the findings as SARIF, for GitHub code scanning',
	)
	parser.add_argument(
		'--no-lockfiles', action='store_true', help='skip the osv-scanner pass'
	)
	parser.add_argument(
		'--no-vendored', action='store_true', help='skip the pinned-commit pass'
	)
	parser.add_argument(
		'--no-native', action='store_true', help=f'skip the NVD pass over {DEPS_FILE}'
	)
	parser.add_argument(
		'--gate',
		choices=['critical', 'high', 'medium', 'low'],
		help='exit 1 on a finding of this severity or worse, except in replay-only lines',
	)
	parser.add_argument(
		'--waivers',
		metavar='FILE',
		help='JSON list of {"id", "path", "message"} of dismissed code scanning alerts, which the gate skips',
	)


@dataclasses.dataclass(frozen=True)
class Finding:
	"""
	1 advisory against 1 pinned package version in 1 file.
	"""

	# Stable across runs whichever alias a source reports, see `canonical_id`
	id: str
	aliases: tuple[str, ...]
	summary: str
	# CVSS base score; None when the advisory carries none (unsound, unmaintained)
	score: float | None
	package: str
	version: str
	fixed: tuple[str, ...]
	path: str
	line: int

	@property
	def url(self) -> str:
		if self.id.startswith('CVE-'):
			return f'https://nvd.nist.gov/vuln/detail/{self.id}'
		return f'https://osv.dev/vulnerability/{self.id}'


def canonical_id(ids: set[str]) -> str:
	"""
	The 1 id an advisory is reported under, whichever alias the source used.

	Code scanning keys alerts, and so their dismissals, on it.
	"""
	for prefix in ('GHSA-', 'RUSTSEC-', 'CVE-'):
		matching = sorted(id for id in ids if id.startswith(prefix))
		if matching:
			return matching[0]
	return min(ids)


def _finding(ids: set[str], **fields) -> Finding:
	id = canonical_id(ids)
	return Finding(id=id, aliases=tuple(sorted(ids - {id})), **fields)


def _tracked(root: Path) -> list[tuple[common.Repo, list[str]]]:
	"""
	Each repo with its tracked files, manager-root-relative; vendored trees are not tracked.
	"""
	repos = common.discover_repos(root)
	missing = [repo.name for repo in repos if not repo.is_checked_out()]
	if missing:
		# Its pins would silently drop out of the scan
		raise common.ToolError(f'not checked out: {", ".join(missing)}')
	gitlinks = common.executor_rels(root)
	return [
		(repo, [repo.prefix(f) for f in common.ls_files(repo, gitlinks)]) for repo in repos
	]


def first_party_crates(root: Path, files: list[str]) -> frozenset[str]:
	"""
	Crates declared by a tracked `Cargo.toml` among `files`.

	osv-scanner looks every locked crate up on crates.io by name, so a path
	dependency is only meaningful when it comes out of a vendored tree.
	"""
	names = set()
	for path in files:
		if Path(path).name == 'Cargo.toml':
			name = tomllib.loads((root / path).read_text()).get('package', {}).get('name')
			if isinstance(name, str):
				names.add(name)
	return frozenset(names)


def _cargo_first_party(lockfile: Path, first_party: frozenset[str]) -> frozenset[str]:
	packages = tomllib.loads(lockfile.read_text()).get('package', [])
	return frozenset(
		p['name'] for p in packages if 'source' not in p and p['name'] in first_party
	)


def _line_of(lines: list[str], package: str, version: str) -> int:
	"""
	1-based line declaring `package` at `version`, or 1 if none can be told apart.
	"""
	declarations = (f'name = "{package}"', f'"node_modules/{package}"', f'{package}==')
	for i, line in enumerate(lines):
		if not line.strip().startswith(declarations):
			continue
		if any(version in near for near in lines[i : i + 3]):
			return i + 1
	return 1


def _fixed_versions(vuln: dict, package: str) -> tuple[str, ...]:
	return tuple(
		sorted(
			{
				event['fixed']
				for affected in vuln.get('affected', [])
				if affected.get('package', {}).get('name') == package
				for range_ in affected.get('ranges', [])
				for event in range_.get('events', [])
				if 'fixed' in event
			}
		)
	)


def _scan_lockfiles(
	ctx: common.Context, tracked: list[tuple[common.Repo, list[str]]]
) -> list[Finding]:
	first_party: dict[str, frozenset[str]] = {}
	for _, files in tracked:
		crates = first_party_crates(ctx.root, files)
		first_party |= {p: crates for p in files if Path(p).name in LOCKFILES}
	if not first_party:
		raise common.ToolError('no tracked lockfile to scan')
	ctx.logger.info('scanning lockfiles', count=len(first_party))
	osv = common.command_to_executable('osv-scanner')
	proc = subprocess.run(
		[str(osv), 'scan', 'source', '--format', 'json']
		+ [f'--lockfile={p}' for p in sorted(first_party)],
		cwd=ctx.root,
		capture_output=True,
		text=True,
	)
	if proc.returncode not in _OSV_RESULT_CODES:
		raise common.ToolError(f'osv-scanner exited {proc.returncode}:\n{proc.stderr}')
	return findings_from_osv(ctx.root, json.loads(proc.stdout), first_party)


def findings_from_osv(
	root: Path, report: dict, first_party: dict[str, frozenset[str]]
) -> list[Finding]:
	"""
	:param first_party: per manager-relative lockfile, the crates of its own repo
	"""
	findings = []
	for result in report.get('results') or []:
		lockfile = Path(result['source']['path'])
		path = lockfile.relative_to(root).as_posix()
		skip = (
			_cargo_first_party(lockfile, first_party.get(path, frozenset()))
			if lockfile.name == 'Cargo.lock'
			else frozenset()
		)
		lines = lockfile.read_text().splitlines()
		for entry in result.get('packages') or []:
			package = entry['package']['name']
			version = entry['package']['version']
			if package in skip:
				continue
			vulns = {v['id']: v for v in entry.get('vulnerabilities') or []}
			# A group is 1 advisory published under several ids
			for group in entry.get('groups') or []:
				ids = set(group['ids']) | set(group.get('aliases') or [])
				primary = next((vulns[id] for id in group['ids'] if id in vulns), {})
				score = group.get('max_severity') or None
				findings.append(
					_finding(
						ids,
						summary=primary.get('summary') or primary.get('details', '').split('\n')[0],
						score=float(score) if score else None,
						package=package,
						version=version,
						fixed=_fixed_versions(primary, package),
						path=path,
						line=_line_of(lines, package, version),
					)
				)
	return findings


def _fetch_json(request: urllib.request.Request, timeout: float) -> dict:
	request.add_header('User-Agent', USER_AGENT)
	with urllib.request.urlopen(request, timeout=timeout) as response:
		return json.load(response)


def _osv_query(query: dict) -> list[dict]:
	vulns: list[dict] = []
	page = dict(query)
	for _ in range(_OSV_MAX_PAGES):
		request = urllib.request.Request(
			OSV_QUERY_URL,
			data=json.dumps(page).encode(),
			headers={'Content-Type': 'application/json'},
			method='POST',
		)
		for attempt in range(_OSV_ATTEMPTS):
			try:
				body = _fetch_json(request, 30)
				break
			except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
				# A 4xx fails identically on a replay
				terminal = isinstance(e, urllib.error.HTTPError) and e.code < 500
				if terminal or attempt == _OSV_ATTEMPTS - 1:
					raise common.ToolError(f'OSV query {query} failed: {e}') from e
				time.sleep(_OSV_BACKOFF_S * (attempt + 1))
		vulns += body.get('vulns', [])
		token = body.get('next_page_token')
		if not token:
			return vulns
		page = {**query, 'page_token': token}
	raise common.ToolError(f'OSV query {query} did not stop paginating')


def _osv_score(vuln: dict) -> float | None:
	"""
	The GitHub severity rating as a CVSS-range score, which is what code scanning ranks by.
	"""
	return _GHSA_SEVERITY_SCORES.get(
		str(vuln.get('database_specific', {}).get('severity', '')).upper()
	)


def findings_from_commit(
	repo: str, commit: str, path: str, line: int, vulns: list[dict]
) -> list[Finding]:
	return [
		_finding(
			{vuln['id'], *(vuln.get('aliases') or [])},
			summary=vuln.get('summary') or vuln.get('details', '').split('\n')[0],
			score=_osv_score(vuln),
			package=repo,
			version=commit[:12],
			fixed=(),
			path=path,
			line=line,
		)
		for vuln in vulns
	]


def _scan_vendored(
	ctx: common.Context, tracked: list[tuple[common.Repo, list[str]]]
) -> list[Finding]:
	"""
	Vendored upstream trees by their pinned commit.

	Their crates are matched by version in the lockfile pass too, but an advisory
	that names only a commit range is found only here.
	"""
	manifests = [
		p for _, files in tracked for p in files if p.endswith(VENDORED_MANIFEST)
	]
	if not manifests:
		raise common.ToolError(f'no tracked {VENDORED_MANIFEST} to scan')
	findings = []
	for path in manifests:
		text = (ctx.root / path).read_text()
		repos = json.loads(text).get('repos')
		if not repos:
			raise common.ToolError(f'{path} lists no vendored repos')
		lines = text.splitlines()
		for key, repo in sorted(repos.items()):
			commit = repo.get('commit')
			if not isinstance(commit, str) or len(commit) != 40:
				raise common.ToolError(f'{path}: {key} has no usable commit pin: {commit!r}')
			ctx.logger.info('querying OSV', repo=key, commit=commit)
			line = next((n + 1 for n, t in enumerate(lines) if commit in t), 1)
			findings += findings_from_commit(
				key.rsplit('/', 1)[-1], commit, path, line, _osv_query({'commit': commit})
			)
	return findings


def _nvd_get(url: str, api_key: str | None) -> dict:
	headers = {'apiKey': api_key} if api_key else {}
	for attempt in range(_NVD_ATTEMPTS):
		try:
			return _fetch_json(urllib.request.Request(url, headers=headers), 60)
		except urllib.error.HTTPError as e:
			# NVD answers its rate limit with 403 or 429, and overload with 503
			if e.code not in (403, 429, 503) or attempt == _NVD_ATTEMPTS - 1:
				raise common.ToolError(f'NVD answered {e.code} for {url}') from e
		except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
			if attempt == _NVD_ATTEMPTS - 1:
				raise common.ToolError(f'NVD query {url} failed: {e}') from e
		time.sleep(_NVD_BACKOFF_S * 2**attempt)
	raise AssertionError('unreachable')


def _nvd_pause(api_key: str | None) -> None:
	time.sleep(_NVD_PAUSE_WITH_KEY_S if api_key else _NVD_PAUSE_S)


def _nvd_cves(cpe: str, api_key: str | None) -> list[dict]:
	"""
	Every non-rejected CVE whose configurations match `cpe`, version ranges included.
	"""
	cves: list[dict] = []
	while True:
		query = urllib.parse.urlencode(
			{'virtualMatchString': cpe, 'startIndex': len(cves), 'noRejected': ''}
		)
		page = _nvd_get(f'{NVD_CVES_URL}?{query}', api_key)
		cves += [v['cve'] for v in page.get('vulnerabilities', [])]
		if not page.get('vulnerabilities') or len(cves) >= page.get('totalResults', 0):
			return cves
		_nvd_pause(api_key)


def _cvss_score(cve: dict) -> float | None:
	metrics = cve.get('metrics', {})
	for key in ('cvssMetricV40', 'cvssMetricV31', 'cvssMetricV30', 'cvssMetricV2'):
		if metrics.get(key):
			return metrics[key][0]['cvssData']['baseScore']
	return None


_NVD_BOUNDS = (
	('versionStartIncluding', operator.ge),
	('versionStartExcluding', operator.gt),
	('versionEndIncluding', operator.le),
	('versionEndExcluding', operator.lt),
)


def _version_key(version: str) -> tuple:
	return tuple(
		(0, int(part)) if part.isdigit() else (1, part)
		for part in re.split(r'[.-]', version)
	)


def _is_vulnerable(cve: dict, vendor: str, product: str, version: str) -> bool:
	"""
	Whether `cve` names this release as vulnerable, not merely as a platform.

	NVD's `virtualMatchString` also returns a CVE whose configuration only needs
	the product to run on, e.g. a PHP bug "running on" SQLite.
	"""
	for config in cve.get('configurations', []):
		for node in config.get('nodes', []):
			for match in node.get('cpeMatch', []):
				_, _, _, m_vendor, m_product, m_version, *_ = match['criteria'].split(':')
				if not match['vulnerable'] or (m_vendor, m_product) != (vendor, product):
					continue
				if m_version != '*':
					if m_version == version:
						return True
					continue
				if all(
					holds(_version_key(version), _version_key(match[bound]))
					for bound, holds in _NVD_BOUNDS
					if bound in match
				):
					return True
	return False


def findings_from_nvd(
	cpe: str, path: str, line: int, cves: list[dict]
) -> list[Finding]:
	_, _, _, vendor, product, version, *_ = cpe.split(':')
	return [
		_finding(
			{cve['id']},
			summary=next(
				(d['value'] for d in cve.get('descriptions', []) if d['lang'] == 'en'), ''
			).split('\n')[0],
			score=_cvss_score(cve),
			package=product,
			version=version,
			fixed=(),
			path=path,
			line=line,
		)
		for cve in cves
		if _is_vulnerable(cve, vendor, product, version)
	]


def _scan_native(ctx: common.Context) -> list[Finding]:
	deps_file = ctx.root / DEPS_FILE
	lines = deps_file.read_text().splitlines()
	cpes = [
		cpe for dep in json.loads(deps_file.read_text()) for cpe in dep.get('cpes', [])
	]
	if not cpes:
		raise common.ToolError(f'no dependency in {DEPS_FILE} lists `cpes`')
	ctx.logger.info('querying NVD', count=len(cpes))
	api_key = os.environ.get(NVD_API_KEY_ENV) or None
	findings = []
	for i, cpe in enumerate(cpes):
		if i:
			_nvd_pause(api_key)
		line = next((n + 1 for n, text in enumerate(lines) if f'"{cpe}"' in text), 1)
		findings += findings_from_nvd(
			cpe, DEPS_FILE.as_posix(), line, _nvd_cves(cpe, api_key)
		)
	return findings


# Worst first: `_severity` takes the first floor a score reaches
SEVERITY_FLOORS = {'critical': 9.0, 'high': 7.0, 'medium': 4.0, 'low': 0.1}
_LINE_RE = re.compile(r'executors/(v\d+\.\d+)\.x/')


def _severity(score: float | None) -> str:
	if score is None:
		return '-'
	for name, floor in SEVERITY_FLOORS.items():
		if score >= floor:
			return name
	return 'none'


def blocking(
	findings: list[Finding],
	statuses: dict[str, str],
	waivers: set[tuple[str, str, str]],
	floor: float,
) -> list[Finding]:
	"""
	Findings at `floor` or above that block a release.

	Only a `replay-only` line is exempt: it replays finalized history, so no
	attacker reaches it. Unscored findings (unmaintained, unsound) never block.
	`waivers` holds `(id, path, package)` of dismissed code scanning alerts.
	"""
	out = []
	for f in findings:
		if f.score is None or f.score < floor or (f.id, f.path, f.package) in waivers:
			continue
		line = _LINE_RE.match(f.path)
		if line and statuses.get(line.group(1)) == 'replay-only':
			continue
		out.append(f)
	return out


def _alert_message(f: Finding) -> str:
	fixed = f' Fixed in {", ".join(f.fixed)}.' if f.fixed else ''
	return f'{f.package}@{f.version} is affected by {f.id}.{fixed}'


# Inverts `_alert_message`; a version holds neither `@` nor spaces
_ALERT_PACKAGE_RE = re.compile(r'(.+)@[^@\s]+ is affected by ')


def load_waivers(path: Path) -> set[tuple[str, str, str]]:
	"""
	`[{"id", "path", "message"}]` of dismissed alerts, keyed like a finding.
	"""
	out = set()
	for w in json.loads(path.read_text()):
		package = _ALERT_PACKAGE_RE.match(w['message'])
		if package is None:
			raise ValueError(f'no package in the message of waiver {w}')
		out.add((w['id'], w['path'], package.group(1)))
	return out


def _by_severity(findings: list[Finding]) -> list[Finding]:
	return sorted(findings, key=lambda f: (-(f.score or 0), f.path, f.package, f.id))


def _markdown(findings: list[Finding]) -> str:
	"""
	1 table per file holding pins, worst first, after a count per severity.
	"""
	ordered = _by_severity(findings)
	counts: dict[str, int] = {}
	for f in ordered:
		counts[_severity(f.score)] = counts.get(_severity(f.score), 0) + 1
	out = [
		'# Dependency Audit',
		'',
		f'{len(findings)} finding(s): '
		+ (', '.join(f'{n} {s}' for s, n in counts.items()) if counts else 'none'),
	]
	by_path: dict[str, list[Finding]] = {}
	for f in ordered:
		by_path.setdefault(f.path, []).append(f)
	for path in sorted(by_path):
		out += [
			'',
			f'## `{path}`',
			'',
			'| Severity | Package | Advisory | Summary | Fixed in |',
			'|---|---|---|---|---|',
		]
		for f in by_path[path]:
			summary = f.summary.replace('|', r'\|')
			out.append(
				f'| {_severity(f.score)} | `{f.package}@{f.version}` | [{f.id}]({f.url}) '
				f'| {summary} | {", ".join(f.fixed) or "-"} |'
			)
	return '\n'.join(out) + '\n'


def _print(findings: list[Finding]) -> None:
	for f in _by_severity(findings):
		fixed = f' (fixed in {", ".join(f.fixed)})' if f.fixed else ''
		print(
			f'{_severity(f.score):8} {f.path}:{f.line}: {f.package}@{f.version} '
			f'{f.id}: {f.summary}{fixed}'
		)
	print(f'{len(findings)} finding(s)')


def _sarif(findings: list[Finding]) -> dict:
	rules = {}
	results = []
	for f in findings:
		rules.setdefault(
			f.id,
			{
				'id': f.id,
				'shortDescription': {'text': f.summary or f.id},
				'helpUri': f.url,
				'help': {
					'text': f'{f.id}: {f.summary}\n{f.url}',
					'markdown': f'[{f.id}]({f.url}): {f.summary}'
					+ (f'\n\nAliases: {", ".join(f.aliases)}' if f.aliases else ''),
				},
				'properties': {
					'tags': ['security', 'dependency'],
					**({'security-severity': f'{f.score:.1f}'} if f.score is not None else {}),
				},
			},
		)
		results.append(
			{
				'ruleId': f.id,
				'level': 'note' if f.score is None else 'warning' if f.score < 7.0 else 'error',
				'message': {'text': _alert_message(f)},
				'locations': [
					{
						'physicalLocation': {
							'artifactLocation': {'uri': f.path},
							'region': {'startLine': f.line},
						}
					}
				],
				# No version: a dismissal survives a bump that is still affected, and
				# a bump that fixes it stops the report, which closes the alert
				'partialFingerprints': {
					'genvmAudit/v1': hashlib.sha256(
						f'{f.path}\0{f.package}\0{f.id}'.encode()
					).hexdigest()
				},
			}
		)
	return {
		'$schema': 'https://json.schemastore.org/sarif-2.1.0.json',
		'version': '2.1.0',
		'runs': [
			{
				'tool': {
					'driver': {
						'name': 'genvm-tool audit',
						'informationUri': 'https://github.com/genlayerlabs/genvm-manager',
						'rules': list(rules.values()),
					}
				},
				'results': results,
			}
		],
	}


def not_yet_reported(earlier: list[Finding], later: list[Finding]) -> list[Finding]:
	"""
	`later` without the advisories `earlier` already reports, under any of their ids.
	"""
	seen = {id for f in earlier for id in (f.id, *f.aliases)}
	return [f for f in later if not seen & {f.id, *f.aliases}]


def main(ctx: common.Context, args) -> int:
	tracked = _tracked(ctx.root)
	findings: list[Finding] = []
	if not args.no_lockfiles:
		findings += _scan_lockfiles(ctx, tracked)
	if not args.no_vendored:
		findings += not_yet_reported(findings, _scan_vendored(ctx, tracked))
	# A native pin is bumped on its own, even when a crate bundles the same code
	if not args.no_native:
		findings += _scan_native(ctx)
	_print(findings)
	if args.markdown:
		_write(Path(args.markdown), _markdown(findings))
	if args.sarif:
		_write(Path(args.sarif), json.dumps(_sarif(findings), indent=1) + '\n')
	if args.gate is None:
		return 0
	config = common.monorepo_config(ctx.root)
	statuses = {line: entry['status'] for line, entry in config['executor-lines'].items()}
	waivers = load_waivers(Path(args.waivers)) if args.waivers else set()
	blocked = blocking(findings, statuses, waivers, SEVERITY_FLOORS[args.gate])
	for f in _by_severity(blocked):
		print(f'blocking: {f.path}:{f.line}: {f.package}@{f.version} {f.id} {f.url}')
	print(f'{len(blocked)} finding(s) block at {args.gate}')
	return 1 if blocked else 0


def _write(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text)
