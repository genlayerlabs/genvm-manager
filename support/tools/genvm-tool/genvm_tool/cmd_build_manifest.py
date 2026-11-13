"""
`genvm-tool build-manifest` — generate the manager's `data/manifest.yaml`.

Writes the runtime manifest (executor versions from the active submodules, the
runner forks, and the static base fields) to `--output`. Used by release
packaging and by the debug build's ninja graph, so you rarely run this by hand.
"""

import json
from pathlib import Path

from . import common

NAME = 'build-manifest'
HELP = "generate the manager's data/manifest.yaml"


def configure(parser):
	parser.add_argument(
		'-o',
		'--output',
		required=True,
		help='path to write the generated manifest.yaml',
	)
	parser.add_argument(
		'--runner-forks',
		required=True,
		help='JSON list of the forks runners name, as `runners/forks.nix` evaluates',
	)


def main(ctx: common.Context, args) -> int:
	from . import manifest

	output = Path(args.output)
	runner_forks = json.loads(Path(args.runner_forks).read_text())
	manifest.write(ctx.root, output, runner_forks)
	ctx.printer.put('manifest-built', output=str(output))
	return 0
