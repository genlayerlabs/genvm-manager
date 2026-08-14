"""Shared front-end for the ``genvm-tool codegen`` language backends.

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
	aliases: dict[str, str]


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
	"""One node of an unfolded string trie.

	``suffix`` is the CamelCase concatenation of the path heads from the root to
	this node (``''`` at the root); backends use it to name the generated
	struct/function. ``parts`` is the path from the root to this node (``[]`` at the
	root), regardless of whether the node is itself a valid string: only when
	``terminal`` is set does ``' '.join(parts)`` name a leaf. A node is either a
	*param* node (``param`` set: consumes one typed argument) or a regular node
	carrying ``leaves`` (terminal strings) and ``methods`` (child nodes), mirroring
	the original ruby generators.
	"""

	suffix: str
	parts: list[str]
	terminal: bool
	param: tuple[str, list[str]] | None
	leaves: list[tuple[str, list[str]]]
	methods: list[tuple[str, 'TrieNode']]
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
	"""Normalize a trie entry to ``{'head', 'tail'}`` form (recursively).

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
		order=[],
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
				order=[],
			)
			node.methods.append((head, child))
			node.order.append(('method', head, child))
		elif isinstance(tail, list):
			non_null = [e for e in tail if e is not None]
			# A trie level is terminal if any sibling was a `null` placeholder (or it
			# has no real children): the path itself is a valid leaf string.
			is_terminal = (len(tail) != len(non_null)) or len(non_null) == 0
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
		tail = entry['tail']
		current = head if prefix == '' else f'{prefix} {head}'
		if isinstance(tail, str) and tail.startswith('$'):
			result.append((current, tail[1:]))
		elif isinstance(tail, list):
			non_null = [e for e in tail if e is not None]
			is_terminal = (len(tail) != len(non_null)) or len(non_null) == 0
			if is_terminal:
				result.append((current, None))
			result.extend(enumerate_paths(non_null, current))
	return result


def parse(data) -> list[Definition]:
	defs: list[Definition] = []
	for t in data:
		kind = t['type']
		if kind == 'enum':
			values = t['values']
			aliases = t.get('aliases', {})
			if not isinstance(aliases, dict):
				raise ValueError(f"enum {t['name']!r} aliases must be an object")
			for alias, target in aliases.items():
				if alias in values:
					raise ValueError(
						f"enum {t['name']!r} alias {alias!r} conflicts with a value"
					)
				if target not in values:
					raise ValueError(
						f"enum {t['name']!r} alias {alias!r} targets unknown value {target!r}"
					)
			defs.append(Enum(t['name'], t['repr'], values, aliases))
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
