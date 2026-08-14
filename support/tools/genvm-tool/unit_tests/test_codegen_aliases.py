import pytest

from genvm_tool import codegen


ENUM = [
	{
		'type': 'enum',
		'name': 'storage_type',
		'repr': 'u8',
		'values': {'default': 0, 'latest_finalized': 1, 'latest_decided': 2},
		'aliases': {
			'latest_final': 'latest_finalized',
			'latest_non_final': 'latest_decided',
		},
	}
]


def test_enum_aliases_render_for_every_language():
	defs = codegen.model.parse(ENUM)

	rust = codegen.render('rust', defs)
	assert 'pub const LatestNonFinal: Self = Self::LatestDecided;' in rust
	assert '#[serde(alias = "LatestNonFinal")]' in rust

	python = codegen.render('python', defs)
	assert 'LATEST_NON_FINAL = LATEST_DECIDED  # Deprecated alias' in python

	go = codegen.render('go', defs)
	assert 'StorageTypeLatestNonFinal StorageType = StorageTypeLatestDecided' in go

	rst = codegen.render('rst', defs)
	assert 'Deprecated alias of :ref:`gvm-def-enum-value-storage-type-latest-decided`.' in rst


@pytest.mark.parametrize(
	'aliases, message',
	[
		({'default': 'latest_decided'}, 'conflicts with a value'),
		({'latest_non_final': 'missing'}, 'targets unknown value'),
	],
)
def test_enum_aliases_fail_closed(aliases, message):
	data = [{**ENUM[0], 'aliases': aliases}]
	with pytest.raises(ValueError, match=message):
		codegen.model.parse(data)
