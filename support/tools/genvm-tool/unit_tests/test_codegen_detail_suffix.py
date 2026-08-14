"""A ``#`` trie entry becomes a detail suffix on the built value, not a path."""

import pytest
from genvm_tool.codegen import go, model, python, rst, rust

DATA = [
	{
		'type': 'str_trie',
		'name': 'vm_error',
		'values': [
			'timeout',
			{'head': 'out_of', 'tail': ['memory']},
			{'head': '#', 'tail': ['internal', 'external']},
		],
	}
]


@pytest.fixture
def defs():
	return model.parse(DATA)


def test_suffix_is_kept_out_of_the_path_trie(defs):
	(trie,) = defs
	assert [head for head, _ in trie.root.methods] == ['out_of']
	assert [path for path, _ in model.enumerate_paths(trie.entries)] == [
		'timeout',
		'out_of memory',
	]
	assert [name for name, _ in trie.suffix.leaves] == ['internal', 'external']


def test_rust_emits_detail_methods_on_the_value(defs):
	out = rust.render(defs)
	for name in ('internal', 'external'):
		assert (
			f'pub fn {name}(self) -> Self '
			'{ assert!(!self.0.contains(" # "), "a value carries at most one detail"); '
			f'Self(Cow::Owned(format!("{{}} # {name}", self.0))) }}' in out
		)


def test_every_backend_refuses_to_stack_two_details(defs):
	assert out_has_guard(rust.render(defs))
	assert out_has_guard(python.render(defs))
	assert out_has_guard(go.render(defs))
	assert 'strings' in go.render(defs)


def out_has_guard(out: str) -> bool:
	return out.count(' # ') >= 2 and 'at most one detail' in out or "' # ' not in" in out


def test_a_detail_with_a_non_list_tail_is_rejected():
	data = [
		{
			'type': 'str_trie',
			'name': 'vm_error',
			'values': [{'head': '#', 'tail': [{'head': 'internal', 'tail': '$str'}]}],
		}
	]
	with pytest.raises(ValueError, match='nested'):
		model.parse(data)


def test_rust_is_valid_ignores_details(defs):
	out = rust.render(defs)
	assert '"out_of memory"' in out
	assert '# internal' not in out.split('pub fn is_valid_')[1]


def test_python_emits_detail_methods_on_the_value(defs):
	out = python.render(defs)
	assert "\tdef internal(self) -> 'VmError':\n" in out
	assert "\t\treturn VmError(f'{self.value} # internal')\n" in out


def test_rst_documents_details_under_the_trie(defs):
	out = rst.render(defs)
	assert '``# internal``' in out
	assert '``# external``' in out


def test_a_nested_detail_is_rejected():
	data = [
		{
			'type': 'str_trie',
			'name': 'vm_error',
			'values': [{'head': '#', 'tail': [{'head': 'a', 'tail': ['b']}]}],
		}
	]
	with pytest.raises(ValueError, match='nested'):
		model.parse(data)
