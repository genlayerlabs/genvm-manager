"""
Assemble the manager's runtime manifest (`data/manifest.yaml`).

The manager reads this YAML at startup: `executor_versions` maps each active
executor version to its `available_after` timestamp, alongside the runner
download-URL templates. It is generated at build time rather than checked in, so
the version keys and availability always track the executor submodules.

Inputs, all under the monorepo root:

	* `.genvm-monorepo-root`              -> `active-versions` (the executor trains)
	* `executors/v<ver>.x/manifest.json`  -> `executor-version`, `available-after`, `executor-sha256`
	* `support/manifest-base.yaml`        -> static fields (runner download URLs)

Used in-process by `genvm-tool configure` (dev) and exposed as the
`genvm-tool build-manifest` subcommand (release packaging).
"""

import json
import re
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from . import common

# Manager-root-relative; holds the build-independent fields (runner URLs).
BASE_REL = Path('support') / 'manifest-base.yaml'

# The platforms a release ships (support/ci/pipelines/release.py PLATFORMS).
PLATFORMS = frozenset({'amd64-linux', 'arm64-linux', 'arm64-macos'})
_SHA256 = re.compile(r'[0-9a-fA-F]{64}')


def _executor_manifest(root: Path, bare_version: str) -> dict:
	path = root / f'executors/v{bare_version}.x' / 'manifest.json'
	return json.loads(path.read_text())


def _pins(bare_version: str, pins: dict) -> dict[str, str]:
	"""`executor-sha256` checked here, where a typo is a build error, not a refused install."""
	for platform, digest in pins.items():
		if platform not in PLATFORMS:
			raise common.ToolError(
				f'executors/v{bare_version}.x/manifest.json: executor-sha256 names unknown platform {platform!r}'
			)
		if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
			raise common.ToolError(
				f'executors/v{bare_version}.x/manifest.json: executor-sha256[{platform!r}] is not a hex sha256'
			)
	return {platform: digest.lower() for platform, digest in sorted(pins.items())}


def build(root: Path):
	"""
	Return the manifest document: `executor_versions` followed by the static
	base fields. Keeps ruamel's round-trip type so comments/quoting in the base
	file survive the dump."""
	executor_versions: dict = {}
	for version in common.active_versions(root):
		em = _executor_manifest(root, version)
		# Quote the timestamp so YAML keeps it a string rather than parsing it
		# into a native timestamp (the manager expects an RFC 3339 string).
		entry = {
			'available_after': DoubleQuotedScalarString(em['available-after']),
		}
		# {platform: sha256} of the line's own release tarballs; post-install
		# refuses to download a line whose platform has no pinned hash.
		if 'executor-sha256' in em:
			entry['sha256'] = {
				k: DoubleQuotedScalarString(v)
				for k, v in _pins(version, em['executor-sha256']).items()
			}
		executor_versions[em['executor-version']] = entry

	doc = YAML(typ='rt').load((root / BASE_REL).read_text())
	doc.insert(0, 'executor_versions', executor_versions)
	return doc


def write(root: Path, output: Path) -> None:
	output.parent.mkdir(parents=True, exist_ok=True)
	yaml = YAML(typ='rt')
	# Indent block sequences under their key — the manager's post-install reads
	# this with a minimal YAML parser (micro_yaml) that needs nested items
	# indented, not flush with the mapping key.
	yaml.indent(mapping=2, sequence=4, offset=2)
	with output.open('w') as f:
		yaml.dump(build(root), f)
