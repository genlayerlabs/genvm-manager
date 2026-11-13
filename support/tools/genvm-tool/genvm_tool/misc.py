import hashlib
from pathlib import Path

from . import gvm32


def fuzz_input_name(data: bytes) -> str:
	"""
	The file name a fuzzing corpus entry is stored under.

	Named by content, in the encoding the rest of GenVM names hashes in. The
	digest is truncated to 224 bits: it only has to tell two corpus entries
	apart.

	:param data: the corpus entry
	:return: its name
	"""
	return gvm32.encode(hashlib.sha3_224(data).digest())


def fuzz_input_path(inputs_dir: Path, name: str) -> Path:
	"""
	Where a corpus entry of that name lives.

	Sharded by the first two characters of the name, so a corpus of thousands of
	entries is a tree of small directories rather than one unreadable listing.

	:param inputs_dir: the corpus
	:param name: as `fuzz_input_name` gives it
	:return: the file to write
	"""
	return inputs_dir / name[0] / name[1] / name


def fuzz_corpus_entries(inputs_dir: Path) -> list[Path]:
	"""
	Every entry of a corpus, sharded or not.

	Entries written before sharding sit directly in the corpus dir and are still
	read from there; nothing rewrites them until a corpus update replaces the
	whole dir.

	:param inputs_dir: the corpus
	:return: its entries, in path order
	"""
	if not inputs_dir.is_dir():
		return []
	return sorted(
		entry
		for entry in inputs_dir.rglob('*')
		if entry.is_file() and not entry.name.startswith('.') and entry.name != 'README.txt'
	)


def parse_duration(s: str) -> float:
	"""
	Parse a duration string into seconds.

	:param s: The duration string to parse.
	:return: The duration in seconds.
	"""
	if s.endswith('ms'):
		return float(s[:-2]) / 1000.0
	elif s.endswith('s'):
		return float(s[:-1])
	elif s.endswith('m'):
		return float(s[:-1]) * 60.0
	elif s.endswith('h'):
		return float(s[:-1]) * 3600.0
	else:
		raise ValueError(f'Invalid duration string: {s} , expected 30s | 2m | 1h')
