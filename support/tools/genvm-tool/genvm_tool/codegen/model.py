"""
Shared front-end for the ``genvm-tool codegen`` language backends.

Parses the codegen data JSON (a list of typed definitions) into a small typed
model and provides the shared string-trie machinery every backend walks. The
backends (``rust``, ``python``, ``rst``, ``go``) differ only in how they render
this model; all parsing, trie unfolding, and node classification live here, so a
new language is just a renderer.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path


def to_camel(s: str) -> str:
	"""``a_b_c`` -> ``ABC`` (each underscore-separated part title-cased)."""
	return ''.join(
		'' if len(part) == 0 else part[0].upper() + part[1:].lower()
		for part in s.split('_')
	)


@dataclasses.dataclass
class Enum:
	name: str
	repr: str
	values: dict[str, object]


@dataclasses.dataclass
class Const:
	name: str
	repr: str
	value: object


@dataclasses.dataclass
class Consts:
	name: str
	repr: str
	values: dict[str, object]


@dataclasses.dataclass
class TrieNode:
	"""
	One node of an unfolded string trie.

	``suffix`` is the CamelCase concatenation of the path heads from the root to
	this node (``''`` at the root); backends use it to name the generated
	struct/function. ``parts`` is the path from the root to this node (``[]`` at the
	root), regardless of whether the node is itself a valid string: only when
	``terminal`` is set does ``' '.join(parts)`` name a leaf. A node is either a
	*param* node (``param`` set: consumes one typed argument) or a regular node
	carrying ``leaves`` (terminal strings) and ``methods`` (child nodes), mirroring
	the original ruby generators. ``details`` contains the ``#`` child's terminal
	values when that child exists.
	"""

	suffix: str
	parts: list[str]
	terminal: bool
	param: tuple[str, list[str]] | None
	leaves: list[tuple[str, list[str]]]
	methods: list[tuple[str, 'TrieNode']]
	details: list[str]
	# Children in original JSON order, tagged ``('leaf', head, parts)`` or
	# ``('method', head, child)``. ``leaves``/``methods`` are the grouped views the
	# struct-based backends (rust, python) want; the go backend, which emits flat
	# functions in source order, walks this instead.
	order: list[tuple]


@dataclasses.dataclass
class StrTrie:
	name: str
	root: TrieNode
	entries: list  # unfolded entries, kept for flat path enumeration (rst)
	docs: dict[str, str]


Definition = Enum | Const | Consts | StrTrie


def _unfold(entry):
	"""
	Normalize a trie entry to ``{'head', 'tail'}`` form (recursively).

	A bare string ``"x"`` becomes ``{'head': 'x', 'tail': []}``; a dict whose
	``tail`` is a list has each child unfolded; everything else (``$param`` tails)
	is left as-is.
	"""
	if entry is None:
		return None
	if isinstance(entry, str):
		return {'head': entry, 'tail': []}
	if isinstance(entry, dict):
		tail = entry.get('tail')
		if isinstance(tail, list):
			return {'head': entry['head'], 'tail': [_unfold(e) for e in tail]}
		return entry
	# Anything else (notably a bare `[]` placeholder) is a null/terminal marker,
	# exactly like an explicit `null` — matches ruby's `unfold_trie_entry`.
	return None


def _build(entries, prefix_parts: list[str], suffix: str, terminal: bool) -> TrieNode:
	node = TrieNode(
		suffix=suffix,
		parts=prefix_parts,
		terminal=terminal,
		param=None,
		leaves=[],
		methods=[],
		details=[],
		order=[],
	)
	entries, node.details = _split_details(entries)
	if node.details and not terminal:
		raise ValueError(
			f'{" ".join(prefix_parts) or "root"}: details require a public path'
		)
	for entry in entries:
		if entry is None:
			continue
		head = entry['head']
		tail = entry['tail']
		current = prefix_parts + [head]
		child_suffix = suffix + to_camel(head)
		if isinstance(tail, str) and tail.startswith('$'):
			param_type = tail[1:]
			child = TrieNode(
				suffix=child_suffix,
				parts=current,
				terminal=False,
				param=(param_type, current),
				leaves=[],
				methods=[],
				details=[],
				order=[],
			)
			node.methods.append((head, child))
			node.order.append(('method', head, child))
		elif isinstance(tail, list):
			non_null = [e for e in tail if e is not None]
			path_children = [e for e in non_null if e['head'] != DETAIL_HEAD]
			# A trie level is terminal if any sibling was a `null` placeholder (or it
			# has no path children): the path itself is a valid leaf string.
			is_terminal = (len(tail) != len(non_null)) or len(path_children) == 0
			if len(non_null) == 0:
				node.leaves.append((head, current))
				node.order.append(('leaf', head, current))
			else:
				child = _build(non_null, current, child_suffix, is_terminal)
				node.methods.append((head, child))
				node.order.append(('method', head, child))
	return node


def enumerate_paths(entries, prefix: str = ''):
	"""Flatten a trie to ``(path, param_type | None)`` pairs (used by the rst docs)."""
	result = []
	for entry in entries:
		if entry is None:
			continue
		head = entry['head']
		if head == DETAIL_HEAD:
			continue
		tail = entry['tail']
		current = head if prefix == '' else f'{prefix} {head}'
		if isinstance(tail, str) and tail.startswith('$'):
			result.append((current, tail[1:]))
		elif isinstance(tail, list):
			non_null = [e for e in tail if e is not None]
			path_children = [e for e in non_null if e['head'] != DETAIL_HEAD]
			is_terminal = (len(tail) != len(non_null)) or len(path_children) == 0
			if is_terminal:
				result.append((current, None))
			result.extend(enumerate_paths(non_null, current))
	return result


def enumerate_details(entries, prefix: str = ''):
	"""Flatten detail paths to ``<public path> # <detail>`` strings."""
	result = []
	for entry in entries:
		if entry is None or entry['head'] == DETAIL_HEAD:
			continue
		head = entry['head']
		tail = entry['tail']
		current = head if prefix == '' else f'{prefix} {head}'
		if not isinstance(tail, list):
			continue
		paths, details = _split_details(tail)
		result.extend(f'{current} # {detail}' for detail in details)
		result.extend(enumerate_details(paths, current))
	return result


DETAIL_HEAD = '#'
"""Head of an entry declaring details for its parent public path."""


def _split_details(entries: list) -> tuple[list, list[str]]:
	"""Carve a ``#`` child out of one trie node."""
	found = [e for e in entries if e is not None and e['head'] == DETAIL_HEAD]
	if not found:
		return entries, []
	if len(found) > 1:
		raise ValueError(f'more than one {DETAIL_HEAD!r} entry')
	tail = found[0]['tail']
	if not isinstance(tail, list):
		raise ValueError(f'{DETAIL_HEAD!r} must list details')
	for detail in tail:
		if detail is None or detail['tail'] != []:
			raise ValueError('a detail may not be nested or take a parameter')
	paths = [e for e in entries if e is None or e['head'] != DETAIL_HEAD]
	return paths, [detail['head'] for detail in tail]


def parse(data) -> list[Definition]:
	defs: list[Definition] = []
	for t in data:
		kind = t['type']
		if kind == 'enum':
			defs.append(Enum(t['name'], t['repr'], t['values']))
		elif kind == 'const':
			defs.append(Const(t['name'], t['repr'], t['value']))
		elif kind == 'consts':
			defs.append(Consts(t['name'], t['repr'], t['values']))
		elif kind == 'str_trie':
			entries = [_unfold(e) for e in t['values']]
			defs.append(
				StrTrie(
					t['name'],
					_build(entries, [], '', False),
					entries,
					t.get('docs', {}),
				)
			)
		else:
			raise ValueError(f'unknown codegen type {kind!r}')
	return defs


def load(path: Path) -> list[Definition]:
	return parse(json.loads(Path(path).read_text()))
