"""`genvm-tool codegen` — generate language bindings from genvm data JSON.

Renders a data JSON file (`-i`) to a `--lang` backend (rust/python/rst/go) at
`-o`. The build runs this for the rust/python/rst outputs; `--lang go` is a
manual entry point (its output lands in a separate Go module — set the package
name with `--go-package`).
"""

from pathlib import Path

from . import common

NAME = 'codegen'
HELP = 'generate language bindings (rust/python/rst/go) from genvm data JSON'


def configure(parser):
	from . import codegen

	parser.add_argument(
		'--lang',
		required=True,
		choices=codegen.LANGS,
		help='target language backend',
	)
	parser.add_argument(
		'-i',
		'--in',
		dest='input',
		required=True,
		help='path to the codegen data JSON',
	)
	parser.add_argument(
		'-o',
		'--out',
		dest='output',
		required=True,
		help='path to write the generated file',
	)
	parser.add_argument(
		'--go-package',
		default='genvm',
		help="package name for the go backend (default: 'genvm')",
	)
	parser.add_argument(
		'--rst-anchor-ns',
		default='',
		help="namespace inserted into the rst backend's `gvm-def-*` anchors, so "
		'two data files rendering into the same appendix do not collide',
	)


def main(ctx: common.Context, args) -> int:
	from . import codegen

	codegen.generate(
		args.lang,
		Path(args.input),
		Path(args.output),
		go_package=args.go_package,
		rst_anchor_ns=args.rst_anchor_ns,
	)
	ctx.printer.put(
		'codegen', lang=args.lang, input=str(args.input), output=str(args.output)
	)
	return 0
