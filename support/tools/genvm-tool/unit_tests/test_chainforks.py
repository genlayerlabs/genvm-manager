"""The build-time chainforks schedule check agrees with the manager's startup rules."""

import json

import pytest
from genvm_tool import chainforks

LINE = 'genvm/executor/v0.3'
FIX = 'genvm/executor/v0.3/fix'


def _schedule(line: str = 'always', fix: str = 'always', requires=(LINE,)) -> dict:
	return {
		'forks': {LINE: {}, FIX: {'requires': list(requires), 'description': 'free text'}},
		'networks': {'net': {'forks': {LINE: line, FIX: fix}}},
	}


def _rejects(doc, match: str) -> None:
	with pytest.raises(chainforks.Error, match=match):
		chainforks.validate(doc)


def test_a_valid_schedule_passes():
	chainforks.validate(_schedule('2025-01-01T00:00:00Z', 'never'))
	chainforks.validate(_schedule('2025-01-01T00:00:00Z', '2025-01-01T00:00:00+00:00'))


def test_a_fork_cannot_activate_before_its_requirement():
	_rejects(_schedule('never', 'always'), 'before its requirement')
	_rejects(
		_schedule('2025-01-02T00:00:00Z', '2025-01-01T00:00:00Z'), 'before its requirement'
	)


def test_a_fork_extending_another_must_require_it():
	_rejects(_schedule(requires=()), 'extends')


def test_a_requirement_must_be_defined_and_acyclic():
	_rejects(_schedule(requires=('genvm/other',)), 'undefined fork')
	_rejects(
		{'forks': {'a': {'requires': ['b']}, 'b': {'requires': ['a']}}, 'networks': {}},
		'requires itself',
	)


def test_every_network_schedules_exactly_the_defined_forks():
	_rejects(
		{'forks': {LINE: {}}, 'networks': {'net': {'forks': {}}}}, 'does not schedule'
	)
	_rejects(
		{'forks': {}, 'networks': {'net': {'forks': {LINE: 'always'}}}}, 'undefined fork'
	)


@pytest.mark.parametrize('name', ['always', 'never'])
def test_builtins_cannot_be_defined(name):
	_rejects({'forks': {name: {}}, 'networks': {}}, 'built-in')


@pytest.mark.parametrize('name', ['', 'Genvm', 'a//b', 'a/', 'a,b', 'a b', 'a|b'])
def test_malformed_names_are_rejected(name):
	_rejects({'forks': {name: {}}, 'networks': {}}, 'malformed fork name')


def test_unknown_keys_are_rejected():
	_rejects({'forks': {}, 'networks': {}, 'extra': 1}, 'unknown keys')
	_rejects(
		{'forks': {}, 'networks': {'net': {'forks': {}, 'dates': {}}}}, 'unknown keys'
	)
	_rejects({'forks': {'a': {'after': 'x'}}, 'networks': {}}, 'unknown keys')


@pytest.mark.parametrize(
	'date', ['2025-01-01', '2025-01-01T00:00Z', '2025-01-01T00:00:00', 'soon']
)
def test_a_date_must_be_rfc3339_or_a_builtin(date):
	_rejects(_schedule(date, 'never'), 'RFC 3339')


def test_duplicate_keys_are_rejected(tmp_path):
	path = tmp_path / chainforks.REL
	path.parent.mkdir(parents=True)
	path.write_text('{"forks": {"a": {}, "a": {}}, "networks": {}}')

	with pytest.raises(chainforks.Error, match='duplicate key `a`'):
		chainforks.load(tmp_path)


def test_load_reports_the_path(tmp_path):
	path = tmp_path / chainforks.REL
	path.parent.mkdir(parents=True)
	path.write_text(json.dumps({'forks': {}}))

	with pytest.raises(chainforks.Error, match='genvm-chainforks.json'):
		chainforks.load(tmp_path)


@pytest.mark.parametrize(
	'date', ['2025-01-01t00:00:00Z', '2025-01-01 00:00:00Z', '2025-01-01T00:00:60Z']
)
def test_dates_chrono_would_accept_are_still_rejected(date):
	_rejects(_schedule(date, 'never'), 'RFC 3339')


def test_requirement_order_sees_below_a_microsecond():
	_rejects(
		_schedule('2025-01-01T00:00:00.0000002Z', '2025-01-01T00:00:00.0000001Z'),
		'before its requirement',
	)


def test_an_offset_is_normalized_before_comparing():
	chainforks.validate(_schedule('2025-01-01T02:00:00+02:00', '2025-01-01T00:00:00Z'))


def test_a_schema_reference_is_accepted_only_at_the_root():
	chainforks.validate({'$schema': 's.json', 'forks': {}, 'networks': {}})
	_rejects({'$schema': 1, 'forks': {}, 'networks': {}}, '`\\$schema` must be a string')
	_rejects({'forks': {'a': {'$schema': 's.json'}}, 'networks': {}}, 'unknown keys')


def test_a_null_description_is_accepted():
	doc = _schedule()
	doc['forks'][FIX]['description'] = None
	chainforks.validate(doc)


def test_a_network_name_cannot_be_empty():
	_rejects({'forks': {}, 'networks': {'': {'forks': {}}}}, 'empty network name')
