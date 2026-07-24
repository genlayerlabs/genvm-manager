"""`genvm-tool build-manifest` — generate the manager's `data/manifest.yaml`.

Writes the runtime manifest (executor versions from the active submodules plus
the static base fields) to `--output`. Used by release packaging; dev builds get
the same file from `configure`, so you rarely run this by hand.
"""

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


def main(ctx: common.Context, args) -> int:
	from . import manifest

	output = Path(args.output)
	manifest.write(ctx.root, output)
	ctx.printer.put('manifest-built', output=str(output))
	return 0
