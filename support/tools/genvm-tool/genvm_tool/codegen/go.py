"""
Go backend for ``genvm-tool codegen``.

Ports the standalone go generator (trie builders emit flat ``func``s rather than
structs). The package name is configurable via ``--go-package`` since the
generated file lands in an external Go module.
"""

from __future__ import annotations

import json

from .model import Const, Consts, Definition, Enum, StrTrie, TrieNode, to_camel

_GO_REPR = {
	'str': 'string',
	'u8': 'uint8',
	'u16': 'uint16',
	'u32': 'uint32',
	'i32': 'int32',
}


def _go_repr(s: str) -> str:
	return _GO_REPR.get(s, s)


def _dump(v) -> str:
	if isinstance(v, str):
		return json.dumps(v)
	return str(v)


def _has_param(node: TrieNode) -> bool:
	for _head, child in node.methods:
		if child.param is not None or _has_param(child):
			return True
	return False


def _trie_inner(node: TrieNode, root_camel: str, buf: list[str]) -> None:
	# Walk children in source order: unlike the struct-based backends, go emits a
	# flat function per terminal/param exactly where it appears in the JSON.
	prefix = root_camel + node.suffix
	for detail in node.details:
		val = f'{" ".join(node.parts)} # {detail}'
		buf.append(
			f'func {prefix}{to_camel(detail)}() {root_camel} {{ return {_dump(val)} }}\n'
		)
	for kind, head, payload in node.order:
		if kind == 'leaf':
			val = ' '.join(payload)
			buf.append(
				f'func {prefix}{to_camel(head)}() {root_camel} {{ return {_dump(val)} }}\n'
			)
			continue
		child = payload
		if child.param is not None:
			param_type, parts = child.param
			fmt_str = ' '.join(parts) + ' %v'
			buf.append(
				f'func {root_camel}{child.suffix}{to_camel(param_type)}'
				f'(v {_go_repr(param_type)}) {root_camel} {{ '
				f'return {root_camel}(fmt.Sprintf({_dump(fmt_str)}, v)) }}\n'
			)
		else:
			if child.terminal:
				val = ' '.join(child.parts)
				buf.append(
					f'func {root_camel}{child.suffix}() {root_camel} {{ return {_dump(val)} }}\n'
				)
			_trie_inner(child, root_camel, buf)


def render(defs: list[Definition], *, go_package: str = 'genvm', **_opts) -> str:
	buf: list[str] = []
	buf.append('// Code generated from genvm data. DO NOT EDIT.\n')
	buf.append('\n')
	buf.append(f'package {go_package}\n')

	needs_fmt = any(isinstance(d, StrTrie) and _has_param(d.root) for d in defs)
	imports = [n for n, needed in (('fmt', needs_fmt),) if needed]
	if len(imports) == 1:
		buf.append(f'\nimport "{imports[0]}"\n')
	elif imports:
		buf.append('\nimport (\n')
		for name in imports:
			buf.append(f'\t"{name}"\n')
		buf.append(')\n')

	for d in defs:
		if isinstance(d, Enum):
			name = to_camel(d.name)
			buf.append('\n')
			buf.append(f'type {name} {_go_repr(d.repr)}\n')
			buf.append('\n')
			buf.append('const (\n')
			for k, v in d.values.items():
				buf.append(f'\t{name}{to_camel(k)} {name} = {_dump(v)}\n')
			buf.append(')\n')
		elif isinstance(d, Const):
			buf.append(f'const {to_camel(d.name)} = {_dump(d.value)}\n')
		elif isinstance(d, Consts):
			buf.append('\nconst (\n')
			for k, v in d.values.items():
				buf.append(
					f'\t{to_camel(d.name)}{to_camel(k)} {_go_repr(d.repr)} = {_dump(v)}\n'
				)
			buf.append(')\n')
		elif isinstance(d, StrTrie):
			root_camel = to_camel(d.name)
			buf.append(f'\ntype {root_camel} string\n\n')
			_trie_inner(d.root, root_camel, buf)
	return ''.join(buf)


__all__ = ['render']
