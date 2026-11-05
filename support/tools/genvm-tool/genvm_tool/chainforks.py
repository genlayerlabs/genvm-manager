"""
Build-time check of the chainforks schedule (`support/genvm-chainforks.json`).

Mirrors the manager's startup validation, so a broken schedule fails the build
instead of the first manager start.
"""

import datetime
import decimal
import json
import re
import typing
from pathlib import Path

REL = Path('support') / 'genvm-chainforks.json'

BUILTINS = ('always', 'never')

_NAME_RE = re.compile(r'[a-z0-9._-]+(/[a-z0-9._-]+)*')
_TIMESTAMP_RE = re.compile(
	r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])'
	r'T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?P<fraction>\.[0-9]+)?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])'
)


class Error(Exception):
	pass


def _unique_object(pairs: list[tuple[str, typing.Any]]) -> dict[str, typing.Any]:
	out: dict[str, typing.Any] = {}
	for key, value in pairs:
		if key in out:
			raise Error(f'duplicate key `{key}`')
		out[key] = value
	return out


def _check_keys(
	where: str,
	obj: typing.Any,
	required: typing.AbstractSet[str],
	optional: typing.AbstractSet[str] = frozenset(),
) -> None:
	if not isinstance(obj, dict):
		raise Error(f'{where}: expected an object')
	if missing := required - obj.keys():
		raise Error(f'{where}: missing {sorted(missing)}')
	if unknown := obj.keys() - required - optional:
		raise Error(f'{where}: unknown keys {sorted(unknown)}')


def _activation(where: str, value: typing.Any) -> tuple[int, int, decimal.Decimal]:
	"""
	Sort key: `always` < any date < `never`; a date is whole seconds plus the
	exact fraction, which `datetime` would cut to microseconds."""
	if value == 'always':
		return (0, 0, decimal.Decimal(0))
	if value == 'never':
		return (2, 0, decimal.Decimal(0))
	if isinstance(value, str) and (m := _TIMESTAMP_RE.fullmatch(value)):
		fraction = m.group('fraction') or ''
		try:
			at = datetime.datetime.fromisoformat(value.replace(fraction, '', 1))
		except ValueError:
			pass
		else:
			return (1, int(at.timestamp()), decimal.Decimal('0' + fraction))
	raise Error(
		f'{where}: `{value}` is neither `always`, `never` nor an RFC 3339 timestamp'
	)


def _closure(forks: dict[str, typing.Any], name: str) -> set[str]:
	seen: set[str] = set()
	stack = list(forks[name].get('requires', []))
	while stack:
		required = stack.pop()
		if required == name:
			raise Error(f'fork `{name}` requires itself, directly or transitively')
		if required not in seen:
			seen.add(required)
			stack.extend(forks[required].get('requires', []))
	return seen


def validate(doc: typing.Any) -> None:
	_check_keys('schedule', doc, {'forks', 'networks'})
	forks = doc['forks']
	if not isinstance(forks, dict):
		raise Error('forks: expected an object')

	for name, fork in forks.items():
		if name in BUILTINS:
			raise Error(f'`{name}` is a built-in fork and cannot be defined')
		if not _NAME_RE.fullmatch(name):
			raise Error(f'malformed fork name `{name}`')
		_check_keys(f'fork `{name}`', fork, set(), {'requires', 'description'})
		requires = fork.get('requires', [])
		if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
			raise Error(f'fork `{name}`: `requires` must be a list of names')
		if not isinstance(fork.get('description'), str | None):
			raise Error(f'fork `{name}`: `description` must be a string')
		for required in requires:
			if required not in forks:
				raise Error(f'fork `{name}` requires undefined fork `{required}`')

	for name in forks:
		closure = _closure(forks, name)
		parent = name
		while '/' in parent:
			parent = parent.rsplit('/', 1)[0]
			if parent in forks and parent not in closure:
				raise Error(f'fork `{name}` extends `{parent}` but does not require it')

	networks = doc['networks']
	if not isinstance(networks, dict):
		raise Error('networks: expected an object')
	for network_name, network in networks.items():
		_check_keys(f'network `{network_name}`', network, {'forks'})
		scheduled = network['forks']
		if not isinstance(scheduled, dict):
			raise Error(f'network `{network_name}`: `forks` must be an object')
		for fork in scheduled:
			if fork not in forks:
				raise Error(f'network `{network_name}` schedules undefined fork `{fork}`')
		dates = {}
		for fork in forks:
			if fork not in scheduled:
				raise Error(f'network `{network_name}` does not schedule fork `{fork}`')
			dates[fork] = _activation(
				f'network `{network_name}` fork `{fork}`', scheduled[fork]
			)
		for fork, definition in forks.items():
			for required in definition.get('requires', []):
				if dates[required] > dates[fork]:
					raise Error(
						f'network `{network_name}` activates `{fork}` before its requirement `{required}`'
					)


def load(root: Path) -> dict[str, typing.Any]:
	path = root / REL
	try:
		doc = json.loads(path.read_text(), object_pairs_hook=_unique_object)
		validate(doc)
	except (Error, json.JSONDecodeError) as e:
		raise Error(f'{path}: {e}') from e
	return doc
