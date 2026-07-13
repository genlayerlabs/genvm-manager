"""Rust backend for ``genvm-tool codegen`` (ports the old ``rs.rb`` template).

Output is byte-for-byte identical to the previous ruby generator: the build
regenerates these files in-tree and a non-empty diff would surface as drift.
"""

from __future__ import annotations

import json

from .model import Const, Consts, Definition, Enum, StrTrie, TrieNode, to_camel


def _dump(v) -> str:
	if isinstance(v, str):
		return json.dumps(v)
	return str(v)


def _rust_repr(s: str) -> str:
	return "&'static str" if s == 'str' else s


def _rust_repr_from(s: str) -> str:
	return '&str' if s == 'str' else s


def _rust_param_type(s: str) -> str:
	return '&str' if s == 'str' else s


def _enum(e: Enum) -> str:
	buf: list[str] = []
	name = to_camel(e.name)
	buf.append('#[derive(\n')
	for d in (
		'Debug',
		'PartialEq',
		'Clone',
		'Copy',
		'Serialize',
		'Deserialize',
		'::genlayer_calldata::Encode',
		'::genlayer_calldata::Decode',
	):
		buf.append(f'    {d},\n')
	buf.append(')]\n')
	if e.repr != 'str':
		buf.append(f'#[repr({e.repr})]\n')
		buf.append(f'pub enum {name} {{\n')
		for k, v in e.values.items():
			buf.append(f'    {to_camel(k)} = {_dump(v)},\n')
		buf.append('}\n')
	else:
		buf.append(f'pub enum {name} {{\n')
		for k in e.values:
			buf.append(f'    {to_camel(k)},\n')
		buf.append('}\n')
	buf.append('\n')
	buf.append(f'impl {name} {{\n')
	vals = list(e.values.values())
	if (
		e.repr != 'str'
		and all(isinstance(v, int) for v in vals)
		and sorted(vals) == list(range(len(vals)))
	):
		buf.append(f'    pub const SIZE: usize = {len(vals)};\n')
	buf.append(f'    pub fn value(self) -> {_rust_repr(e.repr)} {{\n')
	buf.append('        match self {\n')
	for k, v in e.values.items():
		buf.append(f'            {name}::{to_camel(k)} => {_dump(v)},\n')
	buf.append('        }\n')
	buf.append('    }\n')
	buf.append("    pub fn str_snake_case(self) -> &'static str {\n")
	buf.append('        match self {\n')
	for k in e.values:
		buf.append(f'            {name}::{to_camel(k)} => "{k}",\n')
	buf.append('        }\n')
	buf.append('    }\n')
	buf.append('}\n')
	buf.append('\n')
	buf.append(f'impl TryFrom<{_rust_repr_from(e.repr)}> for {name} {{\n')
	buf.append('    type Error = ();\n')
	buf.append('\n')
	buf.append(
		f'    fn try_from(value: {_rust_repr_from(e.repr)}) -> Result<Self, ()> {{\n'
	)
	buf.append('        match value {\n')
	for k, v in e.values.items():
		buf.append(f'            {_dump(v)} => Ok({name}::{to_camel(k)}),\n')
	buf.append('            _ => Err(()),\n')
	buf.append('        }\n')
	buf.append('    }\n')
	buf.append('}\n')
	return ''.join(buf)


def _emit_struct(node: TrieNode, root_name: str, buf: list[str]) -> None:
	"""Emit one non-root trie struct: children first, then this struct, then its
	param children (matching the original ruby buffer order)."""
	for _head, child in node.methods:
		if child.param is None:
			_emit_struct(child, root_name, buf)
	buf.append(f'pub struct {node.suffix};\n\n')
	buf.append(f'impl {node.suffix} {{\n')
	if node.terminal:
		val = ' '.join(node.parts)
		buf.append(
			f'    pub const fn val(&self) -> {root_name} {{ '
			f'{root_name}(Cow::Borrowed("{val}")) }}\n'
		)
	for name, parts in node.leaves:
		val = ' '.join(parts)
		buf.append(
			f'    pub const fn {name.lower()}(&self) -> {root_name} {{ '
			f'{root_name}(Cow::Borrowed("{val}")) }}\n'
		)
	for head, child in node.methods:
		buf.append(
			f'    pub const fn {head.lower()}(&self) -> {child.suffix} {{ {child.suffix} }}\n'
		)
	buf.append('}\n\n')
	for _head, child in node.methods:
		if child.param is not None:
			_emit_param(child, root_name, buf)


def _emit_param(node: TrieNode, root_name: str, buf: list[str]) -> None:
	param_type, parts = node.param
	fmt_str = ' '.join(parts) + ' {v}'
	buf.append(f'pub struct {node.suffix};\n\n')
	buf.append(f'impl {node.suffix} {{\n')
	buf.append(
		f'    pub fn val_{param_type}(&self, v: {_rust_param_type(param_type)}) '
		f'-> {root_name} {{\n'
	)
	buf.append(f'        {root_name}(Cow::Owned(format!("{fmt_str}")))\n')
	buf.append('    }\n')
	buf.append('}\n\n')


def _trie(t: StrTrie, buf: list[str]) -> None:
	root_name = to_camel(t.name)
	root = t.root
	mod: list[str] = []
	for _head, child in root.methods:
		if child.param is None:
			_emit_struct(child, root_name, mod)
	for _head, child in root.methods:
		if child.param is not None:
			_emit_param(child, root_name, mod)
	mod_buf = ''.join(mod)

	mod_name = f'__{root_name}'
	if mod_buf != '':
		buf.append('#[allow(non_snake_case)]\n')
		buf.append('#[rustfmt::skip]\n')
		buf.append(f'pub mod {mod_name} {{\n')
		buf.append('    use std::borrow::Cow;\n')
		buf.append(f'    use super::{root_name};\n\n')
		for line in mod_buf.splitlines(keepends=True):
			buf.append('\n' if line.strip() == '' else f'    {line}')
		buf.append('}\n\n')

	buf.append('#[derive(Debug, Clone, PartialEq, Eq, Serialize)]\n')
	buf.append(f"pub struct {root_name}(pub Cow<'static, str>);\n\n")
	buf.append(f'impl From<{root_name}> for String {{\n')
	buf.append(f'    fn from(val: {root_name}) -> String {{\n')
	buf.append('        val.0.into()\n')
	buf.append('    }\n')
	buf.append('}\n')
	buf.append('#[rustfmt::skip]\n')
	buf.append(f'impl {root_name} {{\n')
	for name, parts in root.leaves:
		val = ' '.join(parts)
		buf.append(
			f'    pub const fn {name.lower()}() -> Self {{ Self(Cow::Borrowed("{val}")) }}\n'
		)
	for head, child in root.methods:
		buf.append(
			f'    pub const fn {head.lower()}() -> {mod_name}::{child.suffix} {{ '
			f'{mod_name}::{child.suffix} }}\n'
		)
	buf.append('}\n\n')


def render(defs: list[Definition], **_opts) -> str:
	has_str_trie = any(isinstance(d, StrTrie) for d in defs)
	# Only enums and str-tries derive serde; a const-only data file (e.g.
	# public-abi-pending.json) must not emit an unused `use serde` import.
	has_serde = any(isinstance(d, (Enum, StrTrie)) for d in defs)
	buf: list[str] = []
	buf.append('// This file is auto-generated. Do not edit!\n\n')
	buf.append('#![allow(dead_code, clippy::redundant_static_lifetimes)]\n')
	buf.append('\n')
	if has_serde:
		buf.append('use serde::{Deserialize, Serialize};\n\n')
	if has_str_trie:
		buf.append('use std::borrow::Cow;\n\n')
	for d in defs:
		if isinstance(d, Enum):
			buf.append(_enum(d))
		elif isinstance(d, Const):
			buf.append(
				f'pub const {d.name.upper()}: {_rust_repr(d.repr)} = {_dump(d.value)};\n'
			)
		elif isinstance(d, Consts):
			buf.append(f'pub mod {d.name} {{\n')
			for k, v in d.values.items():
				buf.append(f'    pub const {k.upper()}: {_rust_repr(d.repr)} = {_dump(v)};\n')
			buf.append('}\n\n')
		elif isinstance(d, StrTrie):
			_trie(d, buf)
	return ''.join(buf)


__all__ = ['render']
