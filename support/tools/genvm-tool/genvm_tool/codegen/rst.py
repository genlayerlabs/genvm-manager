"""reStructuredText backend for ``genvm-tool codegen`` (ports ``rst.rb``).

Generates the spec appendix listing every constant/enum/trie path with stable
``.. _gvm-def-*`` cross-reference anchors.
"""

from __future__ import annotations

from . import model
from .model import Const, Consts, Definition, Enum, StrTrie


def _dash(s: str) -> str:
	return s.replace('_', '-')


def render(defs: list[Definition], **_opts) -> str:
	buf: list[str] = []
	buf.append('Constants\n')
	buf.append('=========\n')
	buf.append('\n')
	for d in defs:
		if isinstance(d, Enum):
			buf.append(f'.. _gvm-def-enum-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append(f'Type: {d.repr}\n\n')
			for k, v in d.values.items():
				buf.append(f'.. _gvm-def-enum-value-{_dash(d.name)}-{_dash(k)}:\n\n')
				buf.append(f'{k}\n')
				buf.append('~' * len(k) + '\n\n')
				buf.append(f'Value: ``{v}``\n\n')
		elif isinstance(d, Const):
			buf.append(f'.. _gvm-def-const-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append(f'Type: {d.repr}\n\n')
			buf.append(f'Value: ``{d.value}``\n\n')
		elif isinstance(d, Consts):
			buf.append(f'.. _gvm-def-consts-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append(f'Type: {d.repr}\n\n')
			for k, v in d.values.items():
				buf.append(f'.. _gvm-def-consts-value-{_dash(d.name)}-{_dash(k)}:\n\n')
				buf.append(f'{k}\n')
				buf.append('~' * len(k) + '\n\n')
				buf.append(f'Value: ``{v}``\n\n')
		elif isinstance(d, StrTrie):
			buf.append(f'.. _gvm-def-str-trie-{_dash(d.name)}:\n\n')
			buf.append(f'{d.name}\n')
			buf.append('-' * len(d.name) + '\n\n')
			buf.append('Type: str_trie\n\n')
			for path, param in model.enumerate_paths(d.entries):
				rst_name = path.replace('_', '-').replace(' ', '-')
				buf.append(f'.. _gvm-def-str-trie-value-{_dash(d.name)}-{rst_name}:\n\n')
				buf.append(f'``{path}``\n')
				buf.append('~' * (len(path) + 4) + '\n\n')
				if param is not None:
					buf.append(f'Param: {param}\n\n')
	return ''.join(buf)


__all__ = ['render']
