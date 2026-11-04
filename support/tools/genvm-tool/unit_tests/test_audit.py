"""How scanner answers become findings and reports, and when a scan refuses to run."""

import dataclasses
import io
import json
import subprocess
import types
import urllib.error

import pytest
from genvm_tool import cmd_audit, common

CARGO_LOCK = """\
version = 4

[[package]]
name = "genvm-common"
version = "0.1.0"

[[package]]
name = "wasmtime"
version = "48.0.3"

[[package]]
name = "zip"
version = "4.3.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""


def _package(name, version, groups, vulns):
	return {
		'package': {'name': name, 'version': version, 'ecosystem': 'crates.io'},
		'groups': groups,
		'vulnerabilities': vulns,
	}


def _vuln(id, package, fixed):
	return {
		'id': id,
		'summary': f'{id} summary',
		'affected': [
			{
				'package': {'name': package},
				'ranges': [{'events': [{'introduced': '0'}, {'fixed': fixed}]}],
			},
			{
				'package': {'name': 'unrelated'},
				'ranges': [{'events': [{'fixed': '99.0.0'}]}],
			},
		],
	}


def _findings(tmp_path):
	lock = tmp_path / 'executor' / 'Cargo.lock'
	lock.parent.mkdir()
	lock.write_text(CARGO_LOCK)
	report = {
		'results': [
			{
				'source': {'path': str(lock)},
				'packages': [
					_package(
						'genvm-common',
						'0.1.0',
						[{'ids': ['RUSTSEC-0'], 'max_severity': '5.0'}],
						[_vuln('RUSTSEC-0', 'genvm-common', '0.2.0')],
					),
					_package(
						'wasmtime',
						'48.0.3',
						[{'ids': ['RUSTSEC-1'], 'max_severity': '9.9'}],
						[_vuln('RUSTSEC-1', 'wasmtime', '49.0.0')],
					),
					_package(
						'zip',
						'4.3.0',
						[
							{
								'ids': ['RUSTSEC-2', 'GHSA-2'],
								'aliases': ['RUSTSEC-2', 'GHSA-2', 'CVE-2'],
								'max_severity': '9.1',
							},
							{'ids': ['RUSTSEC-3'], 'max_severity': ''},
						],
						[
							_vuln('RUSTSEC-2', 'zip', '4.4.0'),
							_vuln('GHSA-2', 'zip', '4.4.0'),
							_vuln('RUSTSEC-3', 'zip', '5.0.0'),
						],
					),
				],
			}
		]
	}
	first_party = {'executor/Cargo.lock': frozenset({'genvm-common'})}
	return cmd_audit.findings_from_osv(tmp_path, report, first_party)


def _ctx(root):
	return types.SimpleNamespace(
		root=root, logger=types.SimpleNamespace(info=lambda *args, **kwargs: None)
	)


def test_first_party_path_crates_are_skipped_and_vendored_ones_kept(tmp_path):
	assert [f.package for f in _findings(tmp_path)] == ['wasmtime', 'zip', 'zip']


def test_first_party_crates_come_from_tracked_manifests(tmp_path):
	(tmp_path / 'a').mkdir()
	(tmp_path / 'a' / 'Cargo.toml').write_text('[package]\nname = "genvm-common"\n')
	(tmp_path / 'Cargo.toml').write_text('[workspace]\nmembers = ["a"]\n')
	files = ['Cargo.toml', 'a/Cargo.toml']
	assert cmd_audit.first_party_crates(tmp_path, files) == {'genvm-common'}


def test_one_finding_per_advisory_group(tmp_path):
	_, first, second = _findings(tmp_path)
	assert first.id == 'GHSA-2'
	assert first.aliases == ('CVE-2', 'RUSTSEC-2')
	assert first.summary == 'RUSTSEC-2 summary'
	assert first.score == 9.1
	assert first.fixed == ('4.4.0',)
	assert first.path == 'executor/Cargo.lock'
	assert first.line == 12
	assert second.id == 'RUSTSEC-3'
	assert second.score is None


@pytest.mark.parametrize(
	'ids, expected',
	[
		({'RUSTSEC-1', 'GHSA-b', 'GHSA-a', 'CVE-1'}, 'GHSA-a'),
		({'RUSTSEC-1', 'CVE-1'}, 'RUSTSEC-1'),
		({'OSV-1', 'CVE-1'}, 'CVE-1'),
		({'OSV-2', 'OSV-1'}, 'OSV-1'),
	],
)
def test_canonical_id_does_not_depend_on_which_alias_a_source_reports(ids, expected):
	assert cmd_audit.canonical_id(ids) == expected


def test_line_of_matches_the_declaration_not_a_substring():
	lines = ['name = "gzip"', 'version = "1.0"', 'name = "zip"', 'version = "1.0"']
	assert cmd_audit._line_of(lines, 'zip', '1.0') == 3
	assert cmd_audit._line_of(lines, 'zip', '2.0') == 1


def test_commit_findings_take_the_github_severity():
	vulns = [
		{
			'id': 'CVE-9',
			'aliases': ['GHSA-9'],
			'summary': 'bad',
			'database_specific': {'severity': 'HIGH'},
		},
		{'id': 'OSV-1', 'details': 'first line\nrest'},
	]
	first, second = cmd_audit.findings_from_commit(
		'wasmtime', 'a' * 40, 'm.json', 3, vulns
	)
	assert (first.id, first.aliases, first.package, first.version) == (
		'GHSA-9',
		('CVE-9',),
		'wasmtime',
		'a' * 12,
	)
	assert first.score == 8.0
	assert (second.summary, second.score) == ('first line', None)


def test_nvd_findings_name_the_cpe_product_and_prefer_the_newest_cvss():
	cves = [
		{
			'id': 'CVE-1',
			'descriptions': [
				{'lang': 'es', 'value': 'malo'},
				{'lang': 'en', 'value': 'bad\nmore'},
			],
			'metrics': {
				'cvssMetricV2': [{'cvssData': {'baseScore': 5.0}}],
				'cvssMetricV31': [{'cvssData': {'baseScore': 9.8}}],
			},
		},
		{'id': 'CVE-2'},
	]
	first, second = cmd_audit.findings_from_nvd(
		'cpe:2.3:a:sqlite:sqlite:3.24.0:*:*:*:*:*:*:*', 'deps.json', 7, cves
	)
	assert (first.package, first.version, first.summary, first.score) == (
		'sqlite',
		'3.24.0',
		'bad',
		9.8,
	)
	assert second.score is None


def test_a_later_pass_does_not_repeat_an_advisory_under_another_id(tmp_path):
	earlier = _findings(tmp_path)
	commit = cmd_audit.findings_from_commit(
		'wasmtime',
		'a' * 40,
		'm.json',
		1,
		[{'id': 'CVE-2'}, {'id': 'GHSA-new', 'aliases': ['RUSTSEC-1']}, {'id': 'GHSA-8'}],
	)
	assert [f.id for f in cmd_audit.not_yet_reported(earlier, commit)] == ['GHSA-8']


def _fingerprint(finding):
	(result,) = cmd_audit._sarif([finding])['runs'][0]['results']
	return result['partialFingerprints']


def test_sarif_fingerprint_survives_a_still_affected_bump(tmp_path):
	finding = _findings(tmp_path)[1]
	assert _fingerprint(finding) == _fingerprint(
		dataclasses.replace(finding, version='4.3.1', line=40)
	)
	assert _fingerprint(finding) != _fingerprint(dataclasses.replace(finding, path='b'))


def test_sarif_levels_and_severity(tmp_path):
	sarif = cmd_audit._sarif(_findings(tmp_path)[1:])['runs'][0]
	assert [r['level'] for r in sarif['results']] == ['error', 'note']
	rules = {r['id']: r for r in sarif['tool']['driver']['rules']}
	assert rules['GHSA-2']['properties']['security-severity'] == '9.1'
	assert 'security-severity' not in rules['RUSTSEC-3']['properties']


def test_markdown_groups_by_file_and_escapes_pipes(tmp_path):
	wasmtime, zip, _ = _findings(tmp_path)
	findings = [
		dataclasses.replace(zip, path='a.json', summary='x | y', score=None),
		zip,
		wasmtime,
	]
	report = cmd_audit._markdown(findings)
	assert report.startswith('# Dependency Audit\n\n3 finding(s): 2 critical, 1 -\n')
	assert report.index('## `a.json`') < report.index('## `executor/Cargo.lock`')
	assert r'x \| y' in report
	assert (
		'| critical | `zip@4.3.0` | [GHSA-2](https://osv.dev/vulnerability/GHSA-2) '
		in report
	)


def test_an_unchecked_out_submodule_refuses_the_scan(tmp_path, monkeypatch):
	(tmp_path / '.git').mkdir()
	repos = [
		common.Repo('manager', tmp_path, ''),
		common.Repo('executors/v0.3.x', tmp_path / 'executors/v0.3.x', 'executors/v0.3.x'),
	]
	monkeypatch.setattr(common, 'discover_repos', lambda root: repos)
	with pytest.raises(common.ToolError, match='executors/v0.3.x'):
		cmd_audit._tracked(tmp_path)


@pytest.mark.parametrize('returncode', [128, 127])
def test_osv_scanner_exiting_neither_0_nor_1_is_a_failure(
	tmp_path, monkeypatch, returncode
):
	(tmp_path / 'Cargo.lock').write_text(CARGO_LOCK)
	monkeypatch.setattr(common, 'command_to_executable', lambda name: name)
	monkeypatch.setattr(
		subprocess,
		'run',
		lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode, '', 'none'),
	)
	repo = common.Repo('manager', tmp_path, '')
	with pytest.raises(common.ToolError, match=f'exited {returncode}'):
		cmd_audit._scan_lockfiles(_ctx(tmp_path), [(repo, ['Cargo.lock'])])


def test_no_lockfile_is_a_failure(tmp_path):
	repo = common.Repo('manager', tmp_path, '')
	with pytest.raises(common.ToolError, match='no tracked lockfile'):
		cmd_audit._scan_lockfiles(_ctx(tmp_path), [(repo, ['README.md'])])


@pytest.mark.parametrize(
	'manifest, error',
	[
		({'repos': {}}, 'lists no vendored repos'),
		({'repos': {'third-party/wasmtime': {'commit': 'abc'}}}, 'no usable commit pin'),
	],
)
def test_a_vendored_manifest_without_usable_pins_is_a_failure(
	tmp_path, manifest, error
):
	path = tmp_path / cmd_audit.VENDORED_MANIFEST
	path.parent.mkdir()
	path.write_text(json.dumps(manifest))
	repo = common.Repo('manager', tmp_path, '')
	with pytest.raises(common.ToolError, match=error):
		cmd_audit._scan_vendored(_ctx(tmp_path), [(repo, [cmd_audit.VENDORED_MANIFEST])])


def _http_error(code):
	return urllib.error.HTTPError('url', code, 'no', {}, io.BytesIO())


def test_osv_query_retries_5xx_follows_pages_and_fails_fast_on_4xx(monkeypatch):
	monkeypatch.setattr(cmd_audit.time, 'sleep', lambda s: None)
	answers = [
		_http_error(503),
		{'vulns': [{'id': 'A'}], 'next_page_token': 't'},
		{'vulns': [{'id': 'B'}]},
	]
	sent = []

	def fetch(request, timeout):
		sent.append(json.loads(request.data))
		answer = answers.pop(0)
		if isinstance(answer, Exception):
			raise answer
		return answer

	monkeypatch.setattr(cmd_audit, '_fetch_json', fetch)
	assert [v['id'] for v in cmd_audit._osv_query({'commit': 'c'})] == ['A', 'B']
	assert sent[-1] == {'commit': 'c', 'page_token': 't'}

	answers = [_http_error(400)]
	with pytest.raises(common.ToolError, match='400'):
		cmd_audit._osv_query({'commit': 'c'})


def test_osv_query_that_never_stops_paginating_is_a_failure(monkeypatch):
	monkeypatch.setattr(
		cmd_audit, '_fetch_json', lambda request, timeout: {'next_page_token': 't'}
	)
	with pytest.raises(common.ToolError, match='did not stop paginating'):
		cmd_audit._osv_query({'commit': 'c'})


def test_nvd_follows_pages_and_retries_its_rate_limit(monkeypatch):
	monkeypatch.setattr(cmd_audit.time, 'sleep', lambda s: None)
	answers = [
		_http_error(403),
		{
			'totalResults': 3,
			'vulnerabilities': [{'cve': {'id': 'A'}}, {'cve': {'id': 'B'}}],
		},
		{'totalResults': 3, 'vulnerabilities': [{'cve': {'id': 'C'}}]},
	]
	urls = []

	def fetch(request, timeout):
		urls.append(request.full_url)
		answer = answers.pop(0)
		if isinstance(answer, Exception):
			raise answer
		return answer

	monkeypatch.setattr(cmd_audit, '_fetch_json', fetch)
	assert [c['id'] for c in cmd_audit._nvd_cves('cpe:x', None)] == ['A', 'B', 'C']
	assert 'startIndex=2' in urls[-1]

	answers = [_http_error(404)]
	with pytest.raises(common.ToolError, match='404'):
		cmd_audit._nvd_cves('cpe:x', None)


def test_nvd_stops_on_an_empty_page(monkeypatch):
	monkeypatch.setattr(
		cmd_audit,
		'_fetch_json',
		lambda request, timeout: {'totalResults': 5, 'vulnerabilities': []},
	)
	assert cmd_audit._nvd_cves('cpe:x', None) == []
