"""What a harvested blob becomes: a framed seed, in the corpus of its own line."""

import types

import pytest
from genvm_tool import cmd_fuzz_corpus
from genvm_tool.misc import fuzz_input_name, fuzz_input_path


@pytest.fixture
def root(tmp_path):
	"""A repo with the split targets on v0.3 only, and calldata on both lines."""
	for line, targets in (
		('0.3', ('executor/fuzz/runners-parse-raw', 'executor/fuzz/entry-data-raw')),
		('0.2', ()),
	):
		for target in targets:
			path = tmp_path / 'executors' / f'v{line}.x' / f'{target}.rs'
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text('fn main() {}')
		decode = (
			tmp_path
			/ 'executors'
			/ f'v{line}.x'
			/ 'executor/crates/calldata/fuzz/genvm-common-decode.rs'
		)
		decode.parent.mkdir(parents=True, exist_ok=True)
		decode.write_text('fn main() {}')
	return tmp_path


def _plan(root, *blobs, max_size=1024):
	ctx = types.SimpleNamespace(root=root)
	args = types.SimpleNamespace(max_size=max_size)
	return cmd_fuzz_corpus._plan(ctx, args, iter(blobs))


def _blob(kind, data, line='0.3'):
	return cmd_fuzz_corpus.Blob(line=line, kind=kind, data=data, origin='<test>')


def test_code_reaches_a_raw_target_verbatim(root):
	plan = _plan(root, _blob('code', b'PK\x03\x04zip'))

	(corpus,) = plan.values()
	assert (
		corpus.inputs == root / 'executors/v0.3.x/executor/fuzz/inputs-runners-parse-raw'
	)
	((name, data),) = corpus.seeds.items()
	assert data == b'PK\x03\x04zip'
	assert name == fuzz_input_name(data)


def test_a_line_that_has_not_split_still_gets_the_selector_byte(root):
	"""The un-split target reads its payload through `Arbitrary`, which eats a byte."""
	unsplit = root / 'executors/v0.2.x/executor/fuzz/runners-parse.rs'
	unsplit.parent.mkdir(parents=True, exist_ok=True)
	unsplit.write_text('fn main() {}')

	plan = _plan(root, _blob('code', b'PK\x03\x04zip', line='0.2'))

	(corpus,) = plan.values()
	assert corpus.inputs == root / 'executors/v0.2.x/executor/fuzz/inputs-runners-parse'
	((_, data),) = corpus.seeds.items()
	assert data == b'\x00PK\x03\x04zip'


def test_calldata_seeds_every_target_that_parses_it(root):
	plan = _plan(root, _blob('calldata', b'\x00'))

	assert {corpus.inputs.name for corpus in plan.values()} == {
		'inputs-genvm-common-decode',
		'inputs-entry-data-raw',
	}
	assert all(
		data == b'\x00' for corpus in plan.values() for data in corpus.seeds.values()
	)


def test_a_line_only_gets_the_targets_it_has(root):
	plan = _plan(root, _blob('code', b'wasm', line='0.2'))

	assert plan == {}


def test_an_entry_the_corpus_already_holds_is_not_re_added(root):
	inputs = root / 'executors/v0.3.x/executor/fuzz/inputs-entry-data-raw'
	path = fuzz_input_path(inputs, fuzz_input_name(b'seen'))
	path.parent.mkdir(parents=True)
	path.write_bytes(b'seen')

	plan = _plan(root, _blob('calldata', b'seen'), _blob('calldata', b'seen'))

	assert plan[inputs].seeds == {}
	assert plan[inputs].known == {fuzz_input_name(b'seen')}


def test_an_entry_left_unsharded_by_an_older_run_still_counts(root):
	inputs = root / 'executors/v0.3.x/executor/fuzz/inputs-entry-data-raw'
	inputs.mkdir()
	inputs.joinpath(fuzz_input_name(b'seen')).write_bytes(b'seen')

	plan = _plan(root, _blob('calldata', b'seen'))

	assert plan[inputs].seeds == {}


def test_a_blob_a_curated_seed_already_holds_is_not_added(root):
	"""Curated names are free-form, so the blob is matched by content."""
	curated = root / 'executors/v0.3.x/executor/fuzz/inputs-entry-data-raw-curated'
	curated.mkdir()
	curated.joinpath('empty-calldata').write_bytes(b'seen')

	plan = _plan(root, _blob('calldata', b'seen'))

	inputs = root / 'executors/v0.3.x/executor/fuzz/inputs-entry-data-raw'
	assert plan[inputs].seeds == {}


def test_an_oversized_blob_is_dropped(root, capsys):
	plan = _plan(root, _blob('code', b'x' * 100), max_size=10)

	assert plan == {}
	assert 'over --max-size' in capsys.readouterr().out


def test_a_blob_carrying_a_host_path_is_dropped(root, capsys):
	plan = _plan(root, _blob('code', b'\x00asm' + str(root).encode()))

	assert plan == {}
	assert str(root) in capsys.readouterr().out


def test_case_line_names_the_executor_the_artifact_came_from(tmp_path):
	cases = tmp_path / 'cases'
	assert (
		cmd_fuzz_corpus._case_line(cases / 'executors/v0.3.x/tests/x/config.json', cases)
		== '0.3'
	)
	assert cmd_fuzz_corpus._case_line(cases / 'tests/system/x/config.json', cases) is None


def test_a_wat_case_yields_the_module_the_run_compiled(tmp_path):
	case_dir = tmp_path / 'case'
	case_dir.mkdir()
	case_dir.joinpath('contract.wasm').write_bytes(b'\0asm')

	conf = {'code': str(tmp_path / 'src' / 'contract.wat')}
	assert cmd_fuzz_corpus._code_path(conf, case_dir) == case_dir / 'contract.wasm'
	assert cmd_fuzz_corpus._code_path({}, case_dir) is None
