"""`genvm-tool hook install` — build the tool and install git-hook stubs.

Stubs exec the nix-built `genvm-tool` (so commits work without the dev shell),
falling back to the in-tree wrapper, and `exit 0` in a standalone checkout where
the manager root cannot be located.
"""

import stat
import subprocess
from pathlib import Path

from .. import common

NAME = 'install'
HELP = 'build genvm-tool and install the git-hook stubs into every repo'

STAGES = ('pre-commit', 'commit-msg')
TOOL_LINK = 'genvm-tool'  # out-link name under the manager cache dir

_STUB = """\
#!/usr/bin/env bash
# Installed by `genvm-tool hook install`. Do not edit.
set -euo pipefail
dir="$(git rev-parse --show-toplevel)"
while [ "$dir" != / ] && [ ! -f "$dir/.genvm-monorepo-root" ]; do
	dir="$(dirname "$dir")"
done
[ -f "$dir/.genvm-monorepo-root" ] || exit 0  # standalone checkout: skip
tool="$dir/{cache}/{link}/bin/genvm-tool"
[ -x "$tool" ] || {{ echo "genvm-tool not built (run 'genvm-tool hook install'); skipping {stage}" >&2; exit 0; }}
exec "$tool" hook run --repo {repo} --hook-stage {stage} -- "$@"
"""


def configure(parser):
	parser.add_argument(
		'--repo',
		default='all',
		help='repo selector for hook install (default: all)',
	)


def _hooks_dir(repo: common.Repo) -> Path:
	out = subprocess.run(
		['git', '-C', str(repo.path), 'rev-parse', '--git-path', 'hooks'],
		capture_output=True,
		text=True,
		check=True,
	).stdout.strip()
	p = Path(out)
	return p if p.is_absolute() else repo.path / p


def _repo_selector(repo: common.Repo, stage: str) -> str:
	"""Repo selector baked into the git-hook stub for `repo`/`stage`.

	The manager's `pre-commit` fans out to `all` so a manager commit also lints
	any staged executor changes. `commit-msg` stays manager-local: the message
	belongs to the manager commit, not the children.
	"""
	if repo.rel == '' and stage == 'pre-commit':
		return 'all'
	return repo.name


def _write_stub(path: Path, repo_selector: str, stage: str):
	path.write_text(
		_STUB.format(
			cache=common.CACHE_SUBDIR, link=TOOL_LINK, repo=repo_selector, stage=stage
		)
	)
	path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main(ctx: common.Context, args) -> int:
	root = ctx.root

	# Build genvm-tool from its standalone default.nix (not the umbrella flake)
	# into a gc-root out-link the stubs point at.
	(root / common.CACHE_SUBDIR).mkdir(parents=True, exist_ok=True)
	ctx.logger.info('building genvm-tool')
	subprocess.run(
		[
			'nix-build',
			'support/tools/genvm-tool/default.nix',
			'-o',
			str(root / common.CACHE_SUBDIR / TOOL_LINK),
		],
		cwd=str(root),
		check=True,
	)

	for repo in common.discover_repos(root, args.repo):
		if not repo.is_checked_out():
			ctx.logger.warning('skipping repo (not checked out)', repo=repo.name)
			continue
		hooks_dir = _hooks_dir(repo)
		hooks_dir.mkdir(parents=True, exist_ok=True)
		for stage in STAGES:
			_write_stub(hooks_dir / stage, _repo_selector(repo, stage), stage)
		ctx.printer.put(
			'installed hooks', repo=repo.name, stages=list(STAGES), dir=str(hooks_dir)
		)
	return 0
