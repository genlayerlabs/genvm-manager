"""Python backend for ``genvm-tool codegen`` (ports the old ``py.rb`` template).

Output is byte-for-byte identical to the previous ruby generator (tab-indented,
single-quoted), so regeneration produces no diff.
"""

from __future__ import annotations

import re

from .model import Const, Consts, Definition, Enum, StrTrie, TrieNode, to_camel

_INT_REPR = re.compile(r'^(u|i)\d+$')


def _pydump(v) -> str:
	if isinstance(v, str):
		return f"'{v}'"
	return str(v)


def _py_repr(s: str) -> str:
	return 'int' if _INT_REPR.match(s) else s


def _emit_struct(node: TrieNode, root_name: str, buf: list[str]) -> None:
	for _head, child in node.methods:
		if child.param is None:
			_emit_struct(child, root_name, buf)
	buf.append(f'class _{root_name}{node.suffix}:\n')
	if node.terminal:
		val = ' '.join(node.parts)
		buf.append('\t@staticmethod\n')
		buf.append(f"\tdef val() -> '{root_name}':\n")
		buf.append(f"\t\treturn {root_name}('{val}')\n")
	for name, parts in node.leaves:
		val = ' '.join(parts)
		buf.append('\t@staticmethod\n')
		buf.append(f"\tdef {name.lower()}() -> '{root_name}':\n")
		buf.append(f"\t\treturn {root_name}('{val}')\n")
	for head, child in node.methods:
		buf.append('\t@staticmethod\n')
		buf.append(f"\tdef {head.lower()}() -> '_{root_name}{child.suffix}':\n")
		buf.append(f'\t\treturn _{root_name}{child.suffix}()\n')
	buf.append('\n')
	for _head, child in node.methods:
		if child.param is not None:
			_emit_param(child, root_name, buf)


def _emit_param(node: TrieNode, root_name: str, buf: list[str]) -> None:
	param_type, parts = node.param
	fmt = ' '.join(parts)
	buf.append(f'class _{root_name}{node.suffix}:\n')
	buf.append('\t@staticmethod\n')
	buf.append(f"\tdef val_{param_type}(v: {_py_repr(param_type)}) -> '{root_name}':\n")
	buf.append(f"\t\treturn {root_name}(f'{fmt} {{v}}')\n")
	buf.append('\n')


def _trie(t: StrTrie, buf: list[str]) -> None:
	root_name = to_camel(t.name)
	root = t.root
	for _head, child in root.methods:
		if child.param is None:
			_emit_struct(child, root_name, buf)
	for _head, child in root.methods:
		if child.param is not None:
			_emit_param(child, root_name, buf)

	buf.append(f'class {root_name}:\n')
	buf.append("\t__slots__ = ('value',)\n")
	buf.append('\tdef __init__(self, value: str):\n')
	buf.append('\t\tself.value = value\n')
	buf.append('\tdef __str__(self) -> str:\n')
	buf.append('\t\treturn self.value\n')
	for name, parts in root.leaves:
		val = ' '.join(parts)
		buf.append('\t@staticmethod\n')
		buf.append(f"\tdef {name.lower()}() -> '{root_name}':\n")
		buf.append(f"\t\treturn {root_name}('{val}')\n")
	for head, child in root.methods:
		buf.append('\t@staticmethod\n')
		buf.append(f"\tdef {head.lower()}() -> '_{root_name}{child.suffix}':\n")
		buf.append(f'\t\treturn _{root_name}{child.suffix}()\n')
	buf.append('\n')


def render(defs: list[Definition], **_opts) -> str:
	buf: list[str] = []
	buf.append('# This file is auto-generated. Do not edit!\n')
	buf.append('\n')
	buf.append('# fmt: off\n')
	buf.append('# ruff: noqa\n')
	buf.append('\n')
	buf.append('import typing\n')
	buf.append('from enum import IntEnum, StrEnum\n')
	for d in defs:
		if isinstance(d, Enum):
			base = 'StrEnum' if d.repr == 'str' else 'IntEnum'
			buf.append('\n\n')
			buf.append(f'class {to_camel(d.name)}({base}):\n')
			for k, v in d.values.items():
				buf.append(f'\t{k.upper()} = {_pydump(v)}\n')
			for alias, target in d.aliases.items():
				buf.append(f'\t{alias.upper()} = {target.upper()}  # Deprecated alias\n')
		elif isinstance(d, Const):
			buf.append(
				f'\n\n{d.name.upper()}: typing.Final[{_py_repr(d.repr)}] = {_pydump(d.value)}\n'
			)
		elif isinstance(d, Consts):
			buf.append(f'\n\nclass _{to_camel(d.name)}(typing.NamedTuple):\n')
			for k, v in d.values.items():
				buf.append(f'\t{k.upper()}: {_py_repr(d.repr)} = {_pydump(v)}\n')
			buf.append(f'\n{d.name}: typing.Final = _{to_camel(d.name)}()\n')
		elif isinstance(d, StrTrie):
			buf.append('\n')
			_trie(d, buf)
	return ''.join(buf)


__all__ = ['render']
