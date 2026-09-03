"""Where a corpus entry is written, and what counts as one when reading back."""

from genvm_tool.misc import fuzz_corpus_entries, fuzz_input_name, fuzz_input_path


def test_an_entry_is_sharded_by_the_first_two_characters_of_its_name(tmp_path):
	name = fuzz_input_name(b'payload')

	assert fuzz_input_path(tmp_path, name) == tmp_path / name[0] / name[1] / name


def test_reading_a_corpus_takes_sharded_and_unsharded_entries(tmp_path):
	sharded = fuzz_input_path(tmp_path, fuzz_input_name(b'new'))
	sharded.parent.mkdir(parents=True)
	sharded.write_bytes(b'new')
	tmp_path.joinpath(fuzz_input_name(b'old')).write_bytes(b'old')
	tmp_path.joinpath('.hidden').write_bytes(b'')
	tmp_path.joinpath('README.txt').write_bytes(b'')

	assert {entry.name for entry in fuzz_corpus_entries(tmp_path)} == {
		fuzz_input_name(b'new'),
		fuzz_input_name(b'old'),
	}


def test_a_corpus_that_does_not_exist_yet_is_empty(tmp_path):
	assert fuzz_corpus_entries(tmp_path / 'inputs-nothing') == []
