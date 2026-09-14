"""Post-install fetches a missing executor line only against the hash its manifest pins."""

import functools
import hashlib
import http.server
import io
import json
import shutil
import subprocess
import sys
import tarfile
import threading
import typing
from pathlib import Path

import pytest
from genvm_tool import common

POST_INSTALL = (
	common.find_root(Path(__file__).parent) / 'install/lib/python/post-install'
)
VERSION = 'v0.3.0'
PLATFORM = 'amd64-linux'
ASSET = f'{VERSION}/genvm-{PLATFORM}-executor.tar.xz'


def _tarball() -> bytes:
	"""A line's release tarball: `executor/<version>/...`, relative to the install root."""
	payload = b'#!/bin/sh\n'
	buf = io.BytesIO()
	with tarfile.open(fileobj=buf, mode='w:xz') as tar:
		info = tarfile.TarInfo(f'executor/{VERSION}/bin/genvm')
		info.size = len(payload)
		tar.addfile(info, io.BytesIO(payload))
	return buf.getvalue()


class _Handler(http.server.SimpleHTTPRequestHandler):
	requested: typing.ClassVar[list[str]] = []

	def do_GET(self):
		self.requested.append(self.path)
		super().do_GET()

	def log_message(self, *args):
		pass


@pytest.fixture
def release(tmp_path):
	"""The executor release page, serving `tmp_path/www`; `requested` records every GET."""
	www = tmp_path / 'www'
	www.mkdir()
	_Handler.requested = []
	httpd = http.server.ThreadingHTTPServer(
		('127.0.0.1', 0), functools.partial(_Handler, directory=str(www))
	)
	threading.Thread(target=httpd.serve_forever, daemon=True).start()
	yield www, f'http://127.0.0.1:{httpd.server_address[1]}'
	httpd.shutdown()
	httpd.server_close()


def _install_root(tmp_path: Path, url: str, pins: dict[str, object] | None) -> Path:
	"""A manager-only install: no executor on disk, so post-install must fetch it."""
	root = tmp_path / 'root'
	shutil.copytree(POST_INSTALL, root / 'lib/python/post-install')
	root.joinpath('bin').mkdir()
	# not ELF, so the platform check leaves it alone; bin-check is off
	root.joinpath('bin/genvm-modules').write_text('#!/bin/sh\n')
	lines = [
		'executor_versions:',
		f'  {VERSION}:',
		'    available_after: "2024-09-01T00:00:00Z"',
	]
	if pins is not None:
		lines.append('    sha256:')
		lines.extend(
			f'      {platform}: {json.dumps(digest)}' for platform, digest in pins.items()
		)
	lines += [
		'executor_download_urls:',
		f'  - "{url}/{{version}}/genvm-{{platform}}-executor.tar.xz"',
	]
	root.joinpath('data').mkdir()
	root.joinpath('data/manifest.yaml').write_text('\n'.join(lines) + '\n')
	return root


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		[
			sys.executable,
			'-B',
			str(root / 'lib/python/post-install/__main__.py'),
			'--default-steps=false',
			'--executor-download=true',
			'--runners-download=false',
			'--os=linux',
			'--arch=amd64',
			*extra,
		],
		capture_output=True,
		text=True,
		check=False,
	)


def test_a_tarball_matching_its_pin_is_unpacked(tmp_path, release):
	www, url = release
	data = _tarball()
	www.joinpath(ASSET).parent.mkdir()
	www.joinpath(ASSET).write_bytes(data)
	root = _install_root(
		tmp_path, url, {PLATFORM: hashlib.sha256(data).hexdigest().upper()}
	)

	result = _run(root)

	assert result.returncode == 0, result.stderr
	assert root.joinpath('executor', VERSION, 'bin/genvm').read_bytes() == b'#!/bin/sh\n'


def test_a_tarball_that_does_not_match_its_pin_is_refused(tmp_path, release):
	www, url = release
	www.joinpath(ASSET).parent.mkdir()
	www.joinpath(ASSET).write_bytes(_tarball())
	root = _install_root(tmp_path, url, {PLATFORM: '0' * 64})

	result = _run(root)

	assert result.returncode != 0
	assert f'sha256 mismatch for executor {VERSION} on {PLATFORM}' in result.stderr
	assert not root.joinpath('executor').exists()


@pytest.mark.parametrize(
	'pins',
	[None, {'arm64-macos': '0' * 64}, {PLATFORM: 123}],
	ids=['none', 'other-platform', 'not-a-string'],
)
def test_an_unpinned_platform_is_not_even_downloaded(tmp_path, release, pins):
	www, url = release
	www.joinpath(ASSET).parent.mkdir()
	www.joinpath(ASSET).write_bytes(_tarball())
	root = _install_root(tmp_path, url, pins)

	result = _run(root)

	assert result.returncode != 0
	assert (
		f'manifest pins no sha256 for executor {VERSION} on {PLATFORM}' in result.stderr
	)
	assert _Handler.requested == []


def test_a_release_without_the_asset_unpacks_nothing(tmp_path, release):
	_, url = release
	root = _install_root(tmp_path, url, {PLATFORM: '0' * 64})

	result = _run(root)

	assert result.returncode != 0
	assert f'failed to download executor {VERSION}' in result.stderr
	assert not root.joinpath('executor').exists()


@pytest.mark.parametrize(
	'pins', [None, {PLATFORM: '0' * 64}], ids=['unpinned', 'mismatch']
)
def test_a_missing_executor_is_a_warning_when_asked(tmp_path, release, pins):
	www, url = release
	www.joinpath(ASSET).parent.mkdir()
	www.joinpath(ASSET).write_bytes(_tarball())
	root = _install_root(tmp_path, url, pins)

	result = _run(root, '--error-on-missing-executor=false')

	assert result.returncode == 0, result.stderr
	assert f'Could not download executor {VERSION}' in result.stderr
	assert not root.joinpath('executor').exists()


def test_an_installed_line_is_left_alone(tmp_path, release):
	"""The bundled line wins: the download is a fallback, not a refresh."""
	_, url = release
	root = _install_root(tmp_path, url, None)
	installed = root / 'executor' / VERSION / 'bin/genvm'
	installed.parent.mkdir(parents=True)
	installed.write_text('#!/bin/sh\n')

	result = _run(root)

	assert result.returncode == 0, result.stderr
	assert _Handler.requested == []
