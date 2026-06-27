import hashlib
from datetime import datetime

from genvm_tool.tests import SharedContext

from ... import gvm32
from .execution import Env as ExecutionEnv


def _write_continue_file(shared: SharedContext, failed_tests: list[str]) -> str | None:
	"""Write failed test names to a continue file for later re-runs."""
	if not failed_tests:
		return None

	failed_tests.sort()

	continue_dir = shared.artifacts_dir / 'continue'
	continue_dir.mkdir(parents=True, exist_ok=True)

	file_hash = hashlib.sha3_256()
	for f in failed_tests:
		file_hash.update(f.encode('utf-8'))
		file_hash.update(b'\n')

	digest = file_hash.digest()[:4]

	# Generate filename: <date>-<hash>
	date_str = datetime.now().strftime('%Y%m%d-%H')
	filename = f'{date_str}-{gvm32.encode(digest)}'
	filepath = continue_dir / filename

	# 3-line header explaining how to investigate the failures. The continue-file
	# reader ignores lines starting with `#`, so this is safe to feed back in.
	cases_dir = shared.artifacts_dir / 'cases'
	header = (
		f'# {len(failed_tests)} test step(s) FAILED. To see what happened, open the '
		f'per-step logs under: {cases_dir}/<test name>/<step>/\n'
		f'#   log.ytr.log (result+reason), stderr.txt, stdout.txt, genvm.log.gz, semantics.txt\n'
		f'# Re-run only these: genvm-tool test run --filter-continue {filename}\n'
	)

	# Write the header followed by failed test names, one per line.
	filepath.write_text(header + '\n'.join(failed_tests) + '\n')

	return str(filepath)


def run(shared: SharedContext, exec_env: ExecutionEnv) -> bool:
	passed = len(exec_env.failed) == 0

	# Write continue file if there are failures
	continue_file = None
	if not passed:
		continue_file = _write_continue_file(shared, exec_env.failed)

	shared.printer.put(
		'Test execution summary',
		success_count=exec_env.success_count,
		failed_count=len(exec_env.failed),
		failed=exec_env.failed,
		passed=passed,
		continue_file=continue_file,
	)
	return passed
