"""
Assemble the manager's runtime manifest (`data/manifest.yaml`).

The manager reads this YAML at startup: `executor_versions` maps each active
executor version to the chainfork gating it, `runner_forks` lists every fork a
runner names, alongside the runner download-URL templates. It is generated at
build time rather than checked in, so the version keys and forks always track
the executor submodules.

Inputs, all under the monorepo root unless noted:

	* `.genvm-monorepo-root`              -> `active-versions` (the executor trains)
	* `executors/v<ver>.x/manifest.json`  -> `executor-version`, `fork`
	* `runners/forks.nix`, evaluated by the caller -> `runner_forks`
	* `support/manifest-base.yaml`        -> static fields (runner download URLs)
	* `install/data/genvm-chainforks.json` -> validated, and must define every fork

Exposed as the `genvm-tool build-manifest` subcommand, which both the debug
build's ninja graph and release packaging run.
"""

import json
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from . import chainforks, common

# Manager-root-relative; holds the build-independent fields (runner URLs).
BASE_REL = Path('support') / 'manifest-base.yaml'


def _executor_manifest(root: Path, bare_version: str) -> dict:
	path = root / f'executors/v{bare_version}.x' / 'manifest.json'
	return json.loads(path.read_text())


def build(root: Path, runner_forks: list[str]):
	"""
	Return the manifest document: `executor_versions` and `runner_forks`
	followed by the static base fields. Keeps ruamel's round-trip type so
	comments/quoting in the base file survive the dump."""
	schedule = chainforks.load(root)
	runner_forks = sorted(set(runner_forks))
	for fork in runner_forks:
		if fork not in schedule['forks']:
			raise chainforks.Error(f'runner fork `{fork}` is not defined in {chainforks.REL}')
	executor_versions: dict = {}
	for version in common.active_versions(root):
		em = _executor_manifest(root, version)
		fork = em['fork']
		if fork not in schedule['forks']:
			raise chainforks.Error(
				f'executor {em["executor-version"]}: fork `{fork}` is not defined in {chainforks.REL}'
			)
		executor_versions[em['executor-version']] = {
			'fork': DoubleQuotedScalarString(fork),
		}

	doc = YAML(typ='rt').load((root / BASE_REL).read_text())
	doc.insert(0, 'executor_versions', executor_versions)
	doc.insert(
		1, 'runner_forks', [DoubleQuotedScalarString(fork) for fork in runner_forks]
	)
	return doc


def write(root: Path, output: Path, runner_forks: list[str]) -> None:
	output.parent.mkdir(parents=True, exist_ok=True)
	yaml = YAML(typ='rt')
	# Indent block sequences under their key — the manager's post-install reads
	# this with a minimal YAML parser (micro_yaml) that needs nested items
	# indented, not flush with the mapping key.
	yaml.indent(mapping=2, sequence=4, offset=2)
	with output.open('w') as f:
		yaml.dump(build(root, runner_forks), f)
