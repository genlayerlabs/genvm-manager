import json

import pytest
import wasmtime_pins
from tools import wasmtime_watch as watch
from wasmtime_pins import Advisory, Subject

MANIFEST = {
	'repos': {
		'executor/third-party/wasmtime': {'commit': 'a' * 40},
		'executor/third-party/wasm-tools': {'commit': 'b' * 40},
	}
}

LOCK = """
[[package]]
name = "wasmtime"
version = "48.0.1"

[[package]]
name = "cranelift-codegen"
version = "0.123.0"

[[package]]
name = "genvm"
version = "0.1.0"

[[package]]
name = "serde"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
	line = tmp_path / 'executors' / 'v0.3.x'
	(line / '.git-third-party').mkdir(parents=True)
	(line / 'executor').mkdir()
	(line / wasmtime_pins.MANIFEST).write_text(json.dumps(MANIFEST))
	(line / wasmtime_pins.CARGO_LOCK).write_text(LOCK)
	# `git ls-files` needs a real repo; the mapping it feeds is tested separately.
	monkeypatch.setattr(wasmtime_pins, 'workspace_crates', lambda tree: {'genvm'})
	return tmp_path


def test_every_vendored_crate_is_a_subject_not_just_the_repo_basename(tree):
	found = wasmtime_pins.subjects('v0.3', root=tree)

	# The repos, by pinned commit...
	assert [(s.name, s.commit) for s in found if s.commit] == [
		('wasm-tools', 'b' * 40),
		('wasmtime', 'a' * 40),
	]
	# ...and every crate that comes out of one, by locked version. `wasmtime` is
	# both; `cranelift-codegen` shares its name with no repo and is where most
	# advisories against this stack are published.
	assert [(s.name, s.version) for s in found if s.version] == [
		('cranelift-codegen', '0.123.0'),
		('wasmtime', '48.0.1'),
	]


def test_registry_and_first_party_crates_are_not_subjects(tree):
	names = {s.name for s in wasmtime_pins.subjects('v0.3', root=tree) if s.version}

	# `serde` has a source, so Dependabot covers it; `genvm` is ours.
	assert 'serde' not in names
	assert 'genvm' not in names


def test_a_manifest_with_no_vendored_repo_is_an_error_not_a_clean_bill(tree):
	path = tree / 'executors' / 'v0.3.x' / wasmtime_pins.MANIFEST
	path.write_text(json.dumps({'repos': {}}))

	with pytest.raises(ValueError, match='lists no vendored repos'):
		wasmtime_pins.subjects('v0.3', root=tree)


def test_a_pin_without_a_full_commit_is_an_error(tree):
	path = tree / 'executors' / 'v0.3.x' / wasmtime_pins.MANIFEST
	path.write_text(json.dumps({'repos': {'x/wasmtime': {'commit': 'abc'}}}))

	with pytest.raises(ValueError, match='no usable commit pin'):
		wasmtime_pins.subjects('v0.3', root=tree)


def test_workspace_crates_reads_tracked_manifests_only(tmp_path, monkeypatch):
	(tmp_path / 'crate').mkdir()
	(tmp_path / 'crate' / 'Cargo.toml').write_text('[package]\nname = "genvm"\n')
	(tmp_path / 'workspace.toml').write_text('[workspace]\nmembers = []\n')
	monkeypatch.setattr(
		wasmtime_pins.subprocess,
		'run',
		lambda *a, **kw: type('R', (), {'stdout': 'crate/Cargo.toml\nworkspace.toml\n'}),
	)

	# A virtual manifest declares no package and must not contribute a name.
	assert wasmtime_pins.workspace_crates(tmp_path) == {'genvm'}


def test_a_subject_queries_osv_by_commit_or_by_crate_version():
	assert Subject(line='v0.3', name='wasmtime', commit='a' * 40).query() == {
		'commit': 'a' * 40
	}
	assert Subject(line='v0.3', name='wasmtime', version='48.0.1').query() == {
		'package': {'name': 'wasmtime', 'ecosystem': 'crates.io'},
		'version': '48.0.1',
	}


def test_a_query_shared_between_lines_is_only_sent_once(monkeypatch):
	sent = []
	monkeypatch.setattr(wasmtime_pins, '_osv', lambda query: sent.append(query) or [])
	subject = Subject(line='v0.2', name='wasmparser', version='0.1')
	cache: dict = {}

	wasmtime_pins.advisories_for(subject, cache)
	wasmtime_pins.advisories_for(
		Subject(line='v0.3', name='wasmparser', version='0.1'), cache
	)

	assert len(sent) == 1


def test_canonical_id_is_stable_across_the_alias_that_matched():
	entry = {'id': 'RUSTSEC-2024-0001', 'aliases': ['GHSA-zzzz', 'CVE-2024-1']}
	other = {'id': 'GHSA-zzzz', 'aliases': ['RUSTSEC-2024-0001', 'CVE-2024-1']}

	assert wasmtime_pins.canonical_id(entry) == wasmtime_pins.canonical_id(other)
	assert wasmtime_pins.canonical_id({'id': 'OSV-1'}) == 'OSV-1'


def test_osv_pagination_is_bounded(monkeypatch):
	monkeypatch.setattr(
		wasmtime_pins, '_post', lambda page: {'vulns': [], 'next_page_token': 'more'}
	)

	with pytest.raises(RuntimeError, match='did not stop paginating'):
		wasmtime_pins._osv({'commit': 'a' * 40})


def findings(*ids: str) -> dict:
	subject = Subject(line='v0.3', name='wasmtime', version='48.0.1')
	return {Advisory(id=id, summary='boom | bang'): [subject] for id in ids}


def marker(*ids: str, supersedes: int | None = None) -> watch.Marker:
	return watch.Marker(ids=frozenset(ids), supersedes=supersedes)


def test_the_marker_round_trips_the_state_the_issue_carries():
	body = watch.body_for(
		findings('GHSA-a', 'GHSA-b'), 'someone', marker('GHSA-a', 'GHSA-b', supersedes=7)
	)

	assert watch.parse_marker(body) == marker('GHSA-a', 'GHSA-b', supersedes=7)
	assert watch.parse_marker('no marker here') is None
	assert watch.parse_marker(watch.body_for({}, None, marker())) == marker()


def test_the_body_names_the_owner_and_the_superseded_issue():
	assert '@someone' in watch.body_for(findings('GHSA-a'), 'someone', marker('GHSA-a'))
	assert 'WASMTIME_REBASE_OWNER' in watch.body_for(
		findings('GHSA-a'), None, marker('GHSA-a')
	)
	assert 'Supersedes #7.' in watch.body_for(
		findings('GHSA-a'), None, marker('GHSA-a', supersedes=7)
	)


def test_a_pipe_in_a_summary_does_not_break_the_table():
	body = watch.body_for(findings('GHSA-a'), None, marker('GHSA-a'))
	row = next(line for line in body.splitlines() if line.startswith('| [GHSA-a]'))

	assert r'boom \| bang' in row
	assert row.count('|') - row.count(r'\|') == 4


def issue(number: int, ids: list[str], supersedes: int | None = None) -> dict:
	return {
		'number': number,
		'title': watch.title_for(dict.fromkeys(ids, [])),
		'body': marker(*ids, supersedes=supersedes).render(),
	}


def test_marker_issues_drops_prs_and_issues_that_are_not_ours(monkeypatch):
	rows = [
		{'number': 1, 'title': 't', 'body': marker('GHSA-a').render()},
		{'number': 2, 'title': 't', 'body': 'filed by hand, same label'},
	]
	monkeypatch.setattr(
		watch, 'gh', lambda *a: '\n'.join(json.dumps(row) for row in rows) + '\n'
	)

	assert [issue['number'] for issue in watch.marker_issues('open')] == [1]


@pytest.fixture
def sweep(monkeypatch):
	"""
	Drive `sweep` over canned findings and issues, capturing what it would write.
	"""
	calls = []
	monkeypatch.setattr(watch, 'active_lines', lambda: ['v0.3'])
	monkeypatch.setattr(
		watch, 'create_issue', lambda title, body, owner: calls.append(('create', body))
	)
	monkeypatch.setattr(
		watch,
		'update_issue',
		lambda issue, title, body: calls.append(('update', issue['number'], body)),
	)
	monkeypatch.delenv(watch.OWNER_ENV, raising=False)

	def run(found: dict, *, open_issues: list, closed: list, dry_run: bool = False):
		monkeypatch.setattr(watch.wasmtime_pins, 'scan', lambda lines: found)
		monkeypatch.setattr(
			watch,
			'marker_issues',
			lambda state: open_issues if state == 'open' else closed,
		)
		return watch.sweep(dry_run), calls

	return run


def test_an_open_issue_is_edited_rather_than_duplicated(sweep):
	rc, calls = sweep(
		findings('GHSA-a', 'GHSA-b'), open_issues=[issue(1, ['GHSA-a'])], closed=[]
	)

	assert rc == 0
	assert [call[:2] for call in calls] == [('update', 1)]
	assert watch.parse_marker(calls[0][2]).ids == {'GHSA-a', 'GHSA-b'}


def test_an_update_keeps_the_supersede_link_of_the_issue_it_edits(sweep):
	rc, calls = sweep(
		findings('GHSA-a'), open_issues=[issue(1, ['GHSA-a'], supersedes=9)], closed=[]
	)

	assert rc == 0
	assert watch.parse_marker(calls[0][2]).supersedes == 9
	assert 'Supersedes #9.' in calls[0][2]


def test_an_open_issue_is_edited_even_once_the_findings_clear(sweep):
	rc, calls = sweep({}, open_issues=[issue(1, ['GHSA-a'])], closed=[])

	assert rc == 0
	assert watch.parse_marker(calls[0][2]).ids == set()


def test_several_open_issues_are_reported_instead_of_picked_between(sweep):
	rc, calls = sweep(
		findings('GHSA-a'),
		open_issues=[issue(1, ['GHSA-a']), issue(2, ['GHSA-a'])],
		closed=[],
	)

	assert (rc, calls) == (1, [])


def test_findings_already_ruled_on_do_not_reopen_the_conversation(sweep):
	rc, calls = sweep(
		findings('GHSA-a'), open_issues=[], closed=[issue(9, ['GHSA-a', 'GHSA-b'])]
	)

	assert (rc, calls) == (0, [])


def test_a_new_advisory_files_a_fresh_issue_superseding_the_closed_one(sweep):
	rc, calls = sweep(
		findings('GHSA-a', 'GHSA-new'), open_issues=[], closed=[issue(9, ['GHSA-a'])]
	)

	assert rc == 0
	assert [call[0] for call in calls] == ['create']
	assert watch.parse_marker(calls[0][1]) == marker('GHSA-a', 'GHSA-new', supersedes=9)


def test_nothing_is_filed_when_there_is_nothing_to_report(sweep):
	assert sweep({}, open_issues=[], closed=[]) == (0, [])


def test_a_dry_run_writes_nothing(sweep):
	rc, calls = sweep(findings('GHSA-a'), open_issues=[], closed=[], dry_run=True)
	assert (rc, calls) == (0, [])

	rc, calls = sweep(
		findings('GHSA-a'), open_issues=[issue(1, [])], closed=[], dry_run=True
	)
	assert (rc, calls) == (0, [])


def test_an_unchanged_issue_is_not_patched(monkeypatch):
	patched = []
	monkeypatch.setattr(watch.gh_common, 'api_json', lambda *a, **kw: patched.append(a))
	body = watch.body_for(findings('GHSA-a'), None, marker('GHSA-a'))
	title = watch.title_for(findings('GHSA-a'))

	# The same content, and the same content after a CRLF round-trip through the
	# API: a daily no-op PATCH notifies every subscriber.
	watch.update_issue({'number': 1, 'title': title, 'body': body}, title, body)
	watch.update_issue(
		{'number': 1, 'title': title, 'body': body.replace('\n', '\r\n')}, title, body
	)

	assert patched == []
