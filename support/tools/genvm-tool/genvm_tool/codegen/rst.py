"""
reStructuredText backend for ``genvm-tool codegen`` (ports ``rst.rb``).

Generates the spec appendix listing every constant/enum/trie path with stable
``.. _gvm-def-*`` cross-reference anchors.
"""

from __future__ import annotations

from . import model
from .model import Const, Consts, Definition, Enum, StrTrie


def _dash(s: str) -> str:
	return s.replace('_', '-')


def render(defs: list[Definition], rst_anchor_ns: str = '', **_opts) -> str:
	# Two data files render into the same appendix (`public-abi.json` and the
	# pending one), and a `vm_error` trie in both would define the same
	# `gvm-def-str-trie-vm-error` label twice. `rst_anchor_ns` namespaces one of
	# them; it also keeps the pending anchors distinct from the ones the codes
	# will get once they fold into the real trie.
	ns = f'{_dash(rst_anchor_ns)}-' if rst_anchor_ns else ''
	buf: list[str] = []
	buf.append('Constants\n')
	buf.append('=========\n')
	buf.append('\n')
	for d in defs:
		if isinstance(d, Enum):
			buf.append(f'.. _gvm-def-{ns}enum-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append(f'Type: {d.repr}\n\n')
			for k, v in d.values.items():
				buf.append(f'.. _gvm-def-{ns}enum-value-{_dash(d.name)}-{_dash(k)}:\n\n')
				buf.append(f'{k}\n')
				buf.append('~' * len(k) + '\n\n')
				buf.append(f'Value: ``{v}``\n\n')
		elif isinstance(d, Const):
			buf.append(f'.. _gvm-def-{ns}const-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append(f'Type: {d.repr}\n\n')
			buf.append(f'Value: ``{d.value}``\n\n')
		elif isinstance(d, Consts):
			buf.append(f'.. _gvm-def-{ns}consts-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append(f'Type: {d.repr}\n\n')
			for k, v in d.values.items():
				buf.append(f'.. _gvm-def-{ns}consts-value-{_dash(d.name)}-{_dash(k)}:\n\n')
				buf.append(f'{k}\n')
				buf.append('~' * len(k) + '\n\n')
				buf.append(f'Value: ``{v}``\n\n')
		elif isinstance(d, StrTrie):
			buf.append(f'.. _gvm-def-{ns}str-trie-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append('Type: str_trie\n\n')
			for path, param in model.enumerate_paths(d.entries):
				rst_name = path.replace('_', '-').replace(' ', '-')
				buf.append(f'.. _gvm-def-{ns}str-trie-value-{_dash(d.name)}-{rst_name}:\n\n')
				buf.append(f'``{path}``\n')
				buf.append('~' * (len(path) + 4) + '\n\n')
				if param is not None:
					buf.append(f'Param: {param}\n\n')
				if path in d.docs:
					buf.append(d.docs[path].rstrip() + '\n\n')
			if d.suffix is not None:
				for name, _parts in d.suffix.leaves:
					path = f'{model.DETAIL_HEAD} {name}'
					buf.append(
						f'.. _gvm-def-{ns}str-trie-detail-{_dash(d.name)}-{_dash(name)}:\n\n'
					)
					buf.append(f'``{path}``\n')
					buf.append('~' * (len(path) + 4) + '\n\n')
					if path in d.docs:
						buf.append(d.docs[path].rstrip() + '\n\n')
	# Sections are emitted blank-line-separated, so the last one leaves a trailing
	# blank line that `end-of-file-fixer` would strip — permanent codegen drift.
	return ''.join(buf).rstrip('\n') + '\n'


__all__ = ['render']
