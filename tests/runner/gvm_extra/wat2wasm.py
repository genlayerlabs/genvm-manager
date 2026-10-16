import functools
import subprocess

# wabt 1.0.42 enabled these by default and dropped their `--enable-*` flags
_FEATURES = ('tail-call', 'annotations')


@functools.cache
def _flags() -> tuple[str, ...]:
	help_text = subprocess.run(
		['wat2wasm', '--help'], check=True, capture_output=True, text=True
	).stdout
	return tuple(
		f'--enable-{feature}' for feature in _FEATURES if f'--enable-{feature}' in help_text
	)


def command(out_path: str, wat_path: str) -> list[str]:
	return ['wat2wasm', *_flags(), '-o', out_path, wat_path]
