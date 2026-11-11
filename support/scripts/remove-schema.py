#!/usr/bin/env python3

import argparse
import re
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--tag', required=True, help='current tag')
parser.add_argument('file', help='file to process')

args = parser.parse_args()

SCHEMAS_URL = f'https://raw.githubusercontent.com/genlayerlabs/genvm-manager/refs/tags/{args.tag}/docs/schemas/'

_JSON_SCHEMA = re.compile(r'("\$schema"\s*:\s*")([^"]*)(")')
_REPO_SCHEMA = re.compile(r'(\.\./)+docs/schemas/([A-Za-z0-9._-]+)')

errors: list[str] = []


def process_yaml(p: Path):
	text = p.read_text().splitlines()

	schema_prefix = '# yaml-language-server: $schema='
	bad_prefix = schema_prefix + '../'

	if len(text) > 0 and text[0].startswith(bad_prefix):
		text.pop(0)

	if len(text) > 0 and text[0].startswith(schema_prefix):
		text[0] = text[0].replace('refs/heads/main', f'refs/tags/{args.tag}')
		text[0] = text[0].replace('refs/heads/@GENVM_TAG@', f'refs/tags/{args.tag}')

	Path(p).write_text('\n'.join(text) + '\n')


def process_json(p: Path):
	text = p.read_text()
	m = _JSON_SCHEMA.search(text)
	if m is None or '://' in m.group(2):
		return
	repo = _REPO_SCHEMA.fullmatch(m.group(2))
	if repo is None:
		errors.append(f'{p}: relative $schema outside docs/schemas: {m.group(2)}')
		return
	p.write_text(text[: m.start(2)] + SCHEMAS_URL + repo.group(2) + text[m.end(2) :])


def process_one(p: Path):
	if p.is_symlink():
		return

	if p.is_dir():
		for child in p.iterdir():
			process_one(child)
		return

	if not p.is_file():
		return

	if p.name.endswith(('.yml', '.yaml')):
		process_yaml(p)
	elif p.name.endswith('.json'):
		process_json(p)


process_one(Path(args.file))
for e in errors:
	print(e)
sys.exit(1 if errors else 0)
