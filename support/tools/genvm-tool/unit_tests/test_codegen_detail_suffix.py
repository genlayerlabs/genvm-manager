"""A ``#`` trie child declares details for its parent public path."""

import pytest
from genvm_tool.codegen import go, model, python, rst, rust

DATA = [
	{
		'type': 'str_trie',
		'name': 'vm_error',
		'values': [
			'timeout',
			{
				'head': 'out_of',
				'tail': [
					{
						'head': 'memory',
						'tail': [{'head': '#', 'tail': ['internal', 'external']}],
					}
				],
			},
		],
	}
]


@pytest.fixture
def defs():
	return model.parse(DATA)


def test_detail_is_scoped_to_its_parent_and_kept_out_of_paths(defs):
	(trie,) = defs
	assert [head for head, _ in trie.root.methods] == ['out_of']
	assert [path for path, _ in model.enumerate_paths(trie.entries)] == [
		'timeout',
		'out_of memory',
	]
	memory = trie.root.methods[0][1].methods[0][1]
	assert memory.details == ['internal', 'external']


def test_rust_emits_detail_methods_on_the_parent_builder(defs):
	out = rust.render(defs)
	for name in ('internal', 'external'):
		assert (
			f'pub const fn {name}(&self) -> VmError '
			f'{{ VmError(Cow::Borrowed("out_of memory # {name}")) }}' in out
		)


def test_details_are_not_methods_on_the_built_value(defs):
	rust_out = rust.render(defs).split('pub struct VmError')[1]
	python_out = python.render(defs).split('class VmError:')[1]
	assert 'fn internal' not in rust_out
	assert 'def internal' not in python_out


def test_a_detail_with_a_non_list_tail_is_rejected():
	data = [
		{
			'type': 'str_trie',
			'name': 'vm_error',
			'values': [
				{
					'head': 'timeout',
					'tail': [{'head': '#', 'tail': [{'head': 'internal', 'tail': '$str'}]}],
				}
			],
		}
	]
	with pytest.raises(ValueError, match='nested'):
		model.parse(data)


def test_rust_is_valid_ignores_details(defs):
	out = rust.render(defs)
	assert '"out_of memory"' in out
	assert '# internal' not in out.split('pub fn is_valid_')[1]


def test_python_emits_detail_methods_on_the_parent_builder(defs):
	out = python.render(defs)
	assert "\tdef internal() -> 'VmError':\n" in out
	assert "\t\treturn VmError('out_of memory # internal')\n" in out


def test_go_emits_detail_functions_for_the_parent_path(defs):
	out = go.render(defs)
	assert (
		'func VmErrorOutOfMemoryInternal() VmError '
		'{ return "out_of memory # internal" }' in out
	)


def test_rst_documents_details_under_the_trie(defs):
	out = rst.render(defs)
	assert '``out_of memory # internal``' in out
	assert '``out_of memory # external``' in out


def test_a_detail_may_not_have_children():
	data = [
		{
			'type': 'str_trie',
			'name': 'vm_error',
			'values': [
				{
					'head': 'timeout',
					'tail': [{'head': '#', 'tail': [{'head': 'a', 'tail': ['b']}]}],
				}
			],
		}
	]
	with pytest.raises(ValueError, match='nested'):
		model.parse(data)


def test_a_root_detail_is_rejected():
	data = [
		{
			'type': 'str_trie',
			'name': 'vm_error',
			'values': [{'head': '#', 'tail': ['internal']}],
		}
	]
	with pytest.raises(ValueError, match='public path'):
		model.parse(data)
