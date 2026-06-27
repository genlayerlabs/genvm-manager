"""`genvm-tool build-manifest` — generate the manager's `data/manifest.yaml`.

Assembles `executor_versions` (from the active executor submodules) plus the
static base fields into the YAML manifest the manager loads at runtime. The
inputs are documented in `genvm_tool.manifest`; this is the release-packaging
entry point (dev builds call the same logic in-process from `configure`).
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
