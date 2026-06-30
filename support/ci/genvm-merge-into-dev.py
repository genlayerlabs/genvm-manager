#!/usr/bin/env python3
"""Gate and perform a Merge of a PR into a v<X>-dev branch.

Invoked by the reusable .github/workflows/branch_merge_into_dev.yaml when a
maintainer ticks the "Merge" box on the PR action panel. It re-checks every
gate against the EXACT PR head commit and then advances the dev branch by a
plain (fast-forward-only) push, so what lands is byte-identical to what CI
and E2E validated.

Gates (all required, all on the head commit):
1. base branch is a v<X>-dev branch
2. PR carries the `rtm` (ready-to-merge) label
3. full GenVM CI (queue.yaml) concluded success
4. the cross-repo E2E check concluded success
5. the PR is 0 commits behind base

Strategy: 1 commit -> fast-forward the original commit (SHA preserved);
more -> squash into one commit on top of base, then fast-forward. The PR
is closed afterwards.

Executor mirror: the manager release line (e.g. v0.6) is independent of the
executor lines it ships (v0.2, v0.3), so a single manager commit carries a
gitlink into EACH active line's submodule. We mirror every active line by
fast-forwarding its own `<line>-dev` branch (`v0.2-dev`, `v0.3-dev`, ...) to
that line's gitlink commit. True cross-repo atomicity is impossible, so we
(1) fast-forward-check all sides up front, then (2) push the executors first —
they are the dependencies the manager commit points at — and (3) push the
manager. If the manager push then loses a race the executors are left one
fast-forward ahead, which is harmless (monotonic) and reconciled when Merge is
re-ticked. Lines whose gitlink is unchanged are skipped.

After a successful merge each line's executor mirror branch
(`pr/<line>/<feature>`, opened by branch_executor_prs.yaml) is deleted — its
tip now lives in the executor's `<line>-dev`, so the branch and its PR are
redundant.

(The dev -> version release-gate merge is a separate, not-yet-automated
flow; on that one the executor's version branch is fast-forwarded and its
dev/version branches are kept at the same point.)

Talks to GitHub through the `gh` CLI and moves refs through `git`; it
never executes PR code. The dev branches are protected, so the workflow
checks out with the GENVM_CI_PRIVATE_KEY deploy key and pushes non-force
(a base that advanced is safely rejected). The active executor submodules
must be checked out with a remote `origin` that can push the executor repo
(the workflow wires its own deploy key); the active lines come from
.genvm-monorepo-root via branch-versions.py.

Env: GITHUB_REPOSITORY, PR_NUMBER, GH_TOKEN, E2E_CHECK_PATTERN.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = os.environ['GITHUB_REPOSITORY']
PR = os.environ['PR_NUMBER']
E2E_PATTERN = os.environ.get('E2E_CHECK_PATTERN', 'e2e')
HERE = Path(__file__).resolve().parent


def run(*args, check=True):
	return subprocess.run(args, check=check, text=True, capture_output=True)


def gh(*args):
	return run('gh', *args).stdout


def git(*args, check=True):
	return run('git', *args, check=check)


def egit(submodule, *args, check=True):
	"""git inside an executor submodule checkout."""
	return run('git', '-C', submodule, *args, check=check)


def active_lines():
	"""Active executor lines as `v<X>` tags (e.g. ['v0.2', 'v0.3']). The manager
	release line is independent of these; a manager PR ships every active line."""
	out = run(sys.executable, str(HERE / 'branch-versions.py'), 'list').stdout
	return [f'v{v}' for v in out.split()]


def block(msg):
	"""Comment the reason on the PR and fail the job."""
	run(
		'gh',
		'pr',
		'comment',
		PR,
		'--repo',
		REPO,
		'--body',
		f'❌ Merge blocked: {msg}',
		check=False,
	)
	sys.exit(1)


def pr_view(*fields):
	out = gh('pr', 'view', PR, '--repo', REPO, '--json', ','.join(fields))
	return json.loads(out)


def check_gates(pr):
	if pr['state'] != 'OPEN':
		block(f'PR is not open (state: {pr["state"]}).')
	if pr['isDraft']:
		block('PR is a draft.')

	base = pr['baseRefName']
	if not re.fullmatch(r'v.*-dev', base):
		block(f'base branch `{base}` is not a `v<X>-dev` branch.')

	# 2. rtm label
	if not any(label['name'] == 'rtm' for label in pr['labels']):
		block('missing the `rtm` (ready-to-merge) label.')

	head_sha = pr['headRefOid']

	# 3. full GenVM CI (queue.yaml) green on the head commit. The same head may
	# carry skipped runs (label events that didn't run full tests) alongside the
	# real one, and the run may have been started by either a push
	# (event=pull_request) or the action panel (event=workflow_dispatch) — so we
	# query all events and require ANY completed run on this exact commit to have
	# succeeded, not just the most recent.
	runs = json.loads(
		gh(
			'api',
			f'repos/{REPO}/actions/workflows/queue.yaml/runs?head_sha={head_sha}',
		)
	)['workflow_runs']
	if not any(r['status'] == 'completed' and r['conclusion'] == 'success' for r in runs):
		got = ', '.join(f'{r["status"]}/{r["conclusion"]}' for r in runs) or 'no run'
		block(f'GenVM CI (queue.yaml) is not green on `{head_sha}` (runs: {got}).')

	# 4. cross-repo E2E check green on the head commit
	checks = json.loads(
		gh(
			'api',
			f'repos/{REPO}/commits/{head_sha}/check-runs?per_page=100',
		)
	)['check_runs']
	e2e = [c for c in checks if re.search(E2E_PATTERN, c['name'], re.I)]
	if not e2e:
		block(f'no E2E check found on `{head_sha}` (run it on this PR first).')
	bad = [c for c in e2e if c['conclusion'] != 'success']
	if bad:
		conclusions = ' '.join(c['conclusion'] or 'pending' for c in e2e)
		block(f'E2E is not green on `{head_sha}` (conclusions: {conclusions}).')

	return base, head_sha


def delete_executor_mirror_branch(submodule, line, head_ref):
	"""Delete the executor mirror branch `pr/<line>/<head_ref>` after a merge.

	The manager PR's work for this line lived on that namespaced branch; its tip is
	now contained in the executor's `<line>-dev` (we just fast-forwarded it there),
	so the branch — and the auto-opened executor PR it backed (branch_executor_prs.yaml)
	— are redundant. Deleting the branch also closes that PR. Best-effort: a failure
	here never unwinds an otherwise-complete merge. The branch is unprotected, so the
	executor deploy key can delete it.
	"""
	branch = f'pr/{line}/{head_ref}'
	r = egit(submodule, 'push', 'origin', '--delete', branch, check=False)
	if r.returncode == 0:
		print(f'deleted executor mirror branch {branch}')
	else:
		print(f'note: could not delete executor mirror branch {branch}: {r.stderr.strip()}')


def executor_gitlink(commit, submodule):
	"""Executor submodule commit (gitlink) recorded at `commit`'s tree."""
	out = git('ls-tree', commit, submodule).stdout
	# "160000 commit <sha>\t<path>"
	m = re.match(r'\S+\s+commit\s+([0-9a-f]+)\t', out)
	if not m:
		block(
			f'could not resolve the executor submodule pointer ({submodule}) at `{commit}`.'
		)
	return m.group(1)


def executor_mirror_action(submodule, exec_base, exec_sha):
	"""Decide how to mirror `exec_sha` onto the executor's `<exec_base>` branch.

	Returns 'push' (fast-forward, including a fresh create) or 'skip' (the
	branch already contains exec_sha because the executor advanced past what
	this PR pins). Blocks if exec_sha is unknown to the executor repo or the
	branch has diverged from it."""
	# Bring every executor head (and thus exec_sha, if pushed) local.
	egit(submodule, 'fetch', '--no-tags', 'origin', '+refs/heads/*:refs/remotes/origin/*')

	if (
		egit(submodule, 'cat-file', '-e', f'{exec_sha}^{{commit}}', check=False).returncode
		!= 0
	):
		block(
			f'executor commit `{exec_sha}` ({submodule}) is not in the executor repo; '
			f'push the executor side of this change first, then re-tick Merge.'
		)

	ref = f'refs/remotes/origin/{exec_base}'
	if (
		egit(submodule, 'rev-parse', '--verify', '--quiet', ref, check=False).returncode
		!= 0
	):
		return 'push'  # branch absent -> push creates it

	tip = egit(submodule, 'rev-parse', ref).stdout.strip()
	if tip == exec_sha:
		return 'skip'  # already there
	if (
		egit(
			submodule, 'merge-base', '--is-ancestor', exec_sha, tip, check=False
		).returncode
		== 0
	):
		# Executor moved ahead of what this PR pins; the pinned commit is
		# already contained, so there is nothing to push.
		return 'skip'
	if (
		egit(
			submodule, 'merge-base', '--is-ancestor', tip, exec_sha, check=False
		).returncode
		== 0
	):
		return 'push'  # clean fast-forward
	block(
		f'executor `{exec_base}` (`{tip[:12]}`) has diverged from the PR executor '
		f'commit `{exec_sha[:12]}`; mirror is not a fast-forward.'
	)


def mirror_executor_lines(push_sha):
	"""Fast-forward each active executor line's `<line>-dev` to the gitlink this
	merge pins, BEFORE the manager push (the manager commit references them). Lines
	whose gitlink is unchanged are skipped. Returns the lines that were pinned so
	their mirror branches can be cleaned up after the manager push lands."""
	pinned = []
	for line in active_lines():
		submodule = f'executors/{line}.x'
		exec_base = f'{line}-dev'
		exec_sha = executor_gitlink(push_sha, submodule)
		if executor_mirror_action(submodule, exec_base, exec_sha) == 'push':
			print(f'Mirroring executor {exec_base} -> {exec_sha}')
			if (
				egit(
					submodule, 'push', 'origin', f'{exec_sha}:refs/heads/{exec_base}', check=False
				).returncode
				!= 0
			):
				block(
					f'fast-forward push of executor `{exec_base}` was rejected (it advanced); '
					f're-tick Merge.'
				)
		else:
			print(f'Executor {exec_base} already contains {exec_sha}; nothing to mirror')
		pinned.append((submodule, line))
	return pinned


def merge(pr, base, head_sha):
	# Fetch the exact head commit (works for fork PRs too) and live base tip.
	git(
		'fetch',
		'--no-tags',
		'origin',
		f'refs/pull/{PR}/head:refs/prhead',
		f'+refs/heads/{base}:refs/remotes/origin/{base}',
	)

	fetched = git('rev-parse', 'refs/prhead').stdout.strip()
	if fetched != head_sha:
		block(
			f'head moved during merge (expected `{head_sha}`, got `{fetched}`); re-tick Merge.'
		)

	# 5. authoritative 0-commits-behind check at merge time.
	if (
		git(
			'merge-base', '--is-ancestor', f'origin/{base}', 'refs/prhead', check=False
		).returncode
		!= 0
	):
		behind = git('rev-list', '--count', f'refs/prhead..origin/{base}').stdout.strip()
		block(
			f'PR is {behind} commit(s) behind `{base}`; update the branch and re-tick Merge.'
		)

	git('config', 'user.name', 'genvm-ci')
	git('config', 'user.email', 'genvm-ci@genlayer.com')

	if len(pr['commits']) == 1:
		print(f'Single commit: fast-forwarding {base} to {head_sha}')
		push_sha = head_sha
	else:
		print(f'Squashing {len(pr["commits"])} commits onto {base}')
		author = git('log', '-1', '--format=%an <%ae>', head_sha).stdout.strip()
		git('checkout', '-B', '_merge', f'origin/{base}')
		git('merge', '--squash', head_sha)
		message = f'{pr["title"]} (#{PR})\n\n{pr["body"] or ""}\n'
		git('commit', '--author', author, '-m', message)
		push_sha = git('rev-parse', 'HEAD').stdout.strip()

	# Mirror every active executor line onto its own `<line>-dev` branch. The
	# manager line (e.g. v0.6) is independent of the executor lines (v0.2, v0.3)
	# it ships, so one merge fast-forwards each changed line. Fast-forward-check
	# all of them BEFORE the manager push (the manager side was checked above),
	# pushing the executors first: the manager commit gitlinks into them, so the
	# dependencies must land before the referrer.
	pinned = mirror_executor_lines(push_sha)

	# Non-force FF push; rejected if base advanced since the checks. The
	# executors are already advanced at this point; a rejection here leaves them
	# one fast-forward ahead (harmless, monotonic) and re-ticking reconciles.
	if (
		git('push', 'origin', f'{push_sha}:refs/heads/{base}', check=False).returncode != 0
	):
		block(
			f'fast-forward push to manager `{base}` was rejected (base advanced) — the '
			f'executor lines were already fast-forwarded; re-tick Merge to land the '
			f'manager side.'
		)

	run(
		'gh',
		'pr',
		'comment',
		PR,
		'--repo',
		REPO,
		'--body',
		f'✅ Merged into `{base}` (`{push_sha}`) via fast-forward.',
		check=False,
	)
	run('gh', 'pr', 'close', PR, '--repo', REPO, check=False)

	# Clean up the now-redundant executor mirror branches (and the PRs they backed).
	for submodule, line in pinned:
		delete_executor_mirror_branch(submodule, line, pr['headRefName'])


def main():
	pr = pr_view(
		'baseRefName',
		'headRefName',
		'headRefOid',
		'commits',
		'labels',
		'state',
		'isDraft',
		'title',
		'body',
	)
	base, head_sha = check_gates(pr)
	merge(pr, base, head_sha)


if __name__ == '__main__':
	main()
