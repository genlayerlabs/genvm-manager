"""
Assemble the manager's runtime manifest (`data/manifest.yaml`).

The manager reads this YAML at startup: `executor_versions` maps each active
executor version to its `available_after` timestamp, alongside the runner
download-URL templates. It is generated at build time rather than checked in, so
the version keys and availability always track the executor submodules.

Inputs, all under the monorepo root:

	* `.genvm-monorepo-root`              -> `active-versions` (the executor trains)
	* `executors/v<ver>.x/manifest.json`  -> `executor-version` + `available-after`
	* `support/manifest-base.yaml`        -> static fields (runner download URLs)

Used in-process by `genvm-tool configure` (dev) and exposed as the
`genvm-tool build-manifest` subcommand (release packaging).
"""

import json
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from . import common

# Manager-root-relative; holds the build-independent fields (runner URLs).
BASE_REL = Path('support') / 'manifest-base.yaml'


def _executor_manifest(root: Path, bare_version: str) -> dict:
	path = root / f'executors/v{bare_version}.x' / 'manifest.json'
	return json.loads(path.read_text())


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
		executor_versions[em['executor-version']] = {
			'available_after': DoubleQuotedScalarString(em['available-after']),
		}

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
