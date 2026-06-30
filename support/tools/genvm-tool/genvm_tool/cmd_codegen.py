"""`genvm-tool codegen` — generate language bindings from genvm data JSON.

Replaces the per-executor ruby templates (``rs.rb`` / ``py.rb`` / ``rst.rb`` and
the standalone go generator). One typed front-end parses the data JSON; the
``--lang`` backend renders it. The build invokes this for the rust/python/rst
outputs; ``--lang go`` is a manual/external entry point (the generated ``.go``
lands in a separate Go module), with the package name set by ``--go-package``.
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


def main(ctx: common.Context, args) -> int:
	from . import codegen

	codegen.generate(
		args.lang,
		Path(args.input),
		Path(args.output),
		go_package=args.go_package,
	)
	ctx.printer.put(
		'codegen', lang=args.lang, input=str(args.input), output=str(args.output)
	)
	return 0
