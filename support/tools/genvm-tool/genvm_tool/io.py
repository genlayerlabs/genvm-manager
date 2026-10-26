"""
Filesystem helpers that do not block the event loop.

Python has no real asynchronous file IO, so every helper here is the
corresponding :mod:`pathlib` call handed to a worker thread.
"""

__all__ = (
	'AsyncFile',
	'read_file_bytes',
	'read_file_text',
	'write_file_bytes',
	'write_file_text',
	'open_file',
	'path_exists',
	'make_dir',
)

import asyncio
import typing
from pathlib import Path


async def read_file_bytes(path: Path) -> bytes:
	return await asyncio.to_thread(path.read_bytes)


async def read_file_text(path: Path) -> str:
	return await asyncio.to_thread(path.read_text)


async def write_file_bytes(path: Path, data: bytes) -> None:
	await asyncio.to_thread(path.write_bytes, data)


async def write_file_text(path: Path, data: str) -> None:
	await asyncio.to_thread(path.write_text, data)


class AsyncFile[T: (str, bytes)]:
	"""
	Open file whose `open` and `close` do not block the event loop.

	`raw` is the underlying file object, for an fd (subprocess redirection) or a
	write path too hot to pay a thread hop per call.
	"""

	def __init__(self, raw: typing.IO[T]):
		self.raw = raw

	async def __aenter__(self) -> typing.Self:
		return self

	async def __aexit__(self, *_args) -> None:
		await self.close()

	async def close(self) -> None:
		await asyncio.to_thread(self.raw.close)


@typing.overload
async def open_file(
	path: Path, mode: typing.Literal['r', 'w', 'a']
) -> AsyncFile[str]: ...


@typing.overload
async def open_file(
	path: Path, mode: typing.Literal['rb', 'wb', 'ab']
) -> AsyncFile[bytes]: ...


async def open_file(path: Path, mode: str) -> AsyncFile:
	return AsyncFile(await asyncio.to_thread(open, path, mode))


async def path_exists(path: Path) -> bool:
	return await asyncio.to_thread(path.exists)


async def make_dir(path: Path) -> None:
	await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
