"""Hook-execution engine: nix tool profiles, file filtering, runner, builtins.

Repos are processed concurrently (disjoint working trees) and each repo's tool
profile is built concurrently; hooks *within* a repo run sequentially so the
in-place fixers (trailing-whitespace, clang-format, cargo fmt) never race on the
same file.
"""

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .. import common
from . import mdlinks

# Extension -> identify-style type classes used by hooks' `types_or` filter.
# `.h` is ambiguous, so it matches both c and c++.
EXT_TYPES = {
	'.py': {'python'},
	'.pyi': {'python'},
	'.c': {'c'},
	'.h': {'c', 'c++'},
	'.cc': {'c++'},
	'.cpp': {'c++'},
	'.cxx': {'c++'},
	'.hpp': {'c++'},
	'.hh': {'c++'},
	'.rs': {'rust'},
	'.lua': {'lua'},
	'.ts': {'ts'},
	'.tsx': {'tsx'},
	'.json': {'json'},
	'.yaml': {'yaml'},
	'.yml': {'yaml'},
	'.toml': {'toml'},
	'.md': {'markdown'},
	'.markdown': {'markdown'},
}


# --- subprocess ------------------------------------------------------------


async def _run(ctx: common.Context, argv: list[str], **kw) -> tuple[int, str]:
	"""Spawn `argv`, capture combined output, return (rc, output)."""
	ctx.logger.trace('exec', argv=argv, cwd=kw.get('cwd'))
	proc = await asyncio.create_subprocess_exec(
		*argv,
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.STDOUT,
		**kw,
	)
	out, _ = await proc.communicate()
	return proc.returncode, out.decode('utf-8', 'replace')


# --- nix tool profile ------------------------------------------------------

_PROFILE_KEY_RE = re.compile(r'[0-9a-f]{16}$')


def _profile_key(precommit_dir: Path) -> str:
	h = hashlib.sha256()
	for name in ('flake.nix', 'flake.lock'):
		f = precommit_dir / name
		if f.is_file():
			h.update(f.read_bytes())
	return h.hexdigest()[:16]


async def build_profile(ctx: common.Context, repo: common.Repo) -> Path | None:
	"""Realise the repo's lint-tool buildEnv; return its `bin/` (or None).

	Cached as a gc-root out-link named after the hash of flake.nix+flake.lock;
	a present link with a live store target is reused, stale profile siblings are
	pruned (the `genvm-tool` install link, a non-hash name, is left alone).
	"""
	pcd = repo.precommit_dir
	if not (pcd / 'flake.nix').is_file():
		return None
	key = _profile_key(pcd)
	cache = repo.cache_dir
	cache.mkdir(parents=True, exist_ok=True)
	link = cache / key
	if link.exists():  # present and store target alive
		ctx.logger.debug('tool profile cached', repo=repo.name, key=key)
		return link / 'bin'
	for sib in cache.iterdir():
		if sib.name != key and _PROFILE_KEY_RE.fullmatch(sib.name) and sib.is_symlink():
			sib.unlink()
	ctx.logger.info('building tool profile', repo=repo.name)
	rc, out = await _run(ctx, ['nix', 'build', f'path:{pcd}', '--out-link', str(link)])
	if rc != 0:
		raise common.ToolError(f'nix build failed for {repo.name}:\n{out}')
	return link / 'bin'


# --- hook manifests + file filtering ---------------------------------------


def load_hooks(ctx: common.Context, repo: common.Repo, stage: str) -> list[dict]:
	"""Hooks for `repo`/`stage`, sourced from that repo's `.genvm-tool.py:hooks`."""
	project = common.load_project(repo.path)
	hooks_fn = getattr(project, 'hooks', None) if project else None
	raw = list(hooks_fn(ctx)) if hooks_fn else []
	return [h for h in raw if stage in h.get('stages', [common.DEFAULT_STAGE])]


def _file_types(path: str) -> set[str]:
	return EXT_TYPES.get(Path(path).suffix.lower(), set())


def _is_binary(path: Path) -> bool:
	try:
		with open(path, 'rb') as fh:
			return b'\0' in fh.read(8192)
	except OSError:
		return True


def filter_files(hook: dict, files: list[str], repo: common.Repo) -> list[str]:
	inc = re.compile(hook['files']) if hook.get('files') else None
	exc = re.compile(hook['exclude']) if hook.get('exclude') else None
	types_or = set(hook['types_or']) if hook.get('types_or') else None
	# `text` is a pseudo-type (any non-binary file) — mirrors pre-commit's
	# default `types: [text]`, which keeps the in-place fixers off binaries.
	want_text = bool(types_or) and 'text' in types_or
	ext_types = (types_or - {'text'}) if types_or else None
	out = []
	for f in files:
		if inc and not inc.search(f):
			continue
		if exc and exc.search(f):
			continue
		if types_or is not None:
			ok = bool(ext_types and (_file_types(f) & ext_types))
			if not ok and want_text and not _is_binary(repo.path / f):
				ok = True
			if not ok:
				continue
		out.append(f)
	return out


# --- runner ----------------------------------------------------------------


def _hook_args(hook: dict, fix: bool) -> list[str]:
	"""Args for the hook in the requested mode. In fix mode a hook's
	`fix_args` (the rewrite-in-place invocation) replace its `args`; a hook
	without `fix_args` has no fix mode and runs its plain `args` either way."""
	if fix and 'fix_args' in hook:
		return list(hook['fix_args'])
	return list(hook.get('args', []))


def _argv(repo: common.Repo, hook: dict, hook_args: list[str]) -> list[str]:
	entry = hook['entry']
	# A local hook whose entry is a path is resolved against the repo root;
	# a bare command (local or not) is resolved from PATH (profile first).
	if hook.get('local') and '/' in entry:
		argv = [str(repo.path / entry)]
	else:
		argv = [entry]
	return argv + hook_args


def _profile_env(profile_bin: Path | None) -> dict:
	env = dict(os.environ)
	if profile_bin is not None:
		env['PATH'] = f'{profile_bin}{os.pathsep}' + env.get('PATH', '')
	return env


@dataclass
class HookResult:
	repo: str
	hook_id: str
	status: str  # 'pass' | 'skip' | 'fail'
	output: str = ''


async def _builtin_cargo_fmt(ctx, repo, env, files, fix) -> tuple[int, str]:
	"""Format (or in check mode verify) every cargo crate tracked in `repo`.

	Per-crate `cargo fmt` picks up each crate's edition from its Cargo.toml, so
	this is correct across the repo's independent workspaces. In fix mode it
	rewrites in place; in check mode it adds `--check` and fails on drift.
	"""
	mode = [] if fix else ['--check']
	dirs = sorted(
		{str(Path(m).parent) for m in common.ls_files(repo) if m.endswith('Cargo.toml')}
	)
	rc_all, chunks = 0, []
	for d in dirs:
		rc, out = await _run(ctx, ['cargo', 'fmt', *mode], cwd=str(repo.path / d), env=env)
		if rc != 0:
			rc_all = 1
			chunks.append(f'# {d}\n{out}'.rstrip())
	return rc_all, '\n'.join(chunks)


async def _builtin_md_local_links(ctx, repo, env, files, fix) -> tuple[int, str]:
	"""Verify local file-path links in the given markdown files resolve.

	Check-only; broken links cannot be auto-fixed, so `fix` is ignored."""
	problems = []
	for f in files:
		problems += mdlinks.broken_links(repo.path / f, repo.path)
	return (1 if problems else 0), '\n'.join(problems)


# Hooks may set `builtin = "<name>"` instead of `entry` to run logic that does
# not map to a single file-list invocation (per-crate cargo fmt; in-process
# markdown link checking). Each receives (ctx, repo, env, selected_files, fix).
BUILTINS = {
	'cargo-fmt': _builtin_cargo_fmt,
	'md-local-links': _builtin_md_local_links,
}


async def execute_hook(
	ctx: common.Context,
	repo: common.Repo,
	hook: dict,
	files: list[str],
	profile_bin: Path | None,
	*,
	stage: str,
	msg_file: str | None = None,
	fix: bool = True,
) -> HookResult:
	"""Run one hook, capturing its output."""
	has_selector = bool(hook.get('files') or hook.get('types_or'))
	label = HookResult(repo.name, hook['id'], 'pass')
	env = _profile_env(profile_bin)

	if stage == 'commit-msg':
		selected = []
	else:
		selected = filter_files(hook, files, repo)
		# Nothing to do: a filename-passing hook with no files would otherwise
		# get zero args and scan the whole tree; a selector hook with no match
		# is simply skipped. (Matches pre-commit.)
		if not selected and (hook.get('pass_filenames', True) or has_selector):
			label.status = 'skip'
			return label

	if builtin := hook.get('builtin'):
		rc, out = await BUILTINS[builtin](ctx, repo, env, selected, fix)
	else:
		argv = _argv(repo, hook, _hook_args(hook, fix))
		if stage == 'commit-msg':
			if msg_file is not None:
				argv.append(msg_file)
		elif hook.get('pass_filenames', True):
			argv += selected
		rc, out = await _run(ctx, argv, cwd=str(repo.path), env=env)

	label.status = 'pass' if rc == 0 else 'fail'
	label.output = out
	return label


async def process_repo(
	ctx: common.Context,
	repo: common.Repo,
	stage: str,
	files: list[str],
	*,
	msg_file: str | None = None,
	fix: bool = True,
) -> list[HookResult]:
	"""Build the profile, then run every `stage` hook of `repo` sequentially."""
	hooks = load_hooks(ctx, repo, stage)
	if not hooks:
		return []
	profile_bin = await build_profile(ctx, repo)
	results = []
	for hook in hooks:
		res = await execute_hook(
			ctx, repo, hook, files, profile_bin, stage=stage, msg_file=msg_file, fix=fix
		)
		_report(ctx, res)
		results.append(res)
	return results


def _report(ctx: common.Context, res: HookResult) -> None:
	tag = {'pass': '✓', 'skip': '-', 'fail': '✗'}[res.status]
	name = f'{res.repo}:{res.hook_id}'
	if res.status == 'fail':
		ctx.printer.put(f'{tag} {name} FAILED', output=res.output.rstrip())
	elif res.status == 'skip':
		ctx.logger.debug(f'{tag} {name} (skipped)')
	else:
		ctx.logger.info(f'{tag} {name}')
