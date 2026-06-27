__all__ = ('environ', 'discover_executable', 'watchdog')

import shutil
from pathlib import Path

from . import environ, watchdog


def discover_executable(name: str) -> Path:
	exec_path = shutil.which(name)
	if exec_path is None:
		raise FileNotFoundError(f'Cannot find executable: {name}')
	return Path(exec_path)
