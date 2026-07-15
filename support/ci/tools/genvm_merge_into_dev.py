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

Strategy: every repo ends up with ONE commit per merged PR. The manager's
commits are squashed onto base (a single commit that needs no gitlink rewrite is
fast-forwarded as-is, preserving its SHA); each executor line's commits are
squashed onto its own dev branch. The PR is closed afterwards.

Executor mirror: the manager release line (e.g. v0.6) is independent of the
executor lines it ships (v0.2, v0.3), so a single manager commit carries a
gitlink into EACH active line's submodule. Every active line is squashed onto
its own `<line>-dev` branch (`v0.2-dev`, `v0.3-dev`, ...). Squashing gives the
line a NEW commit, so the sha the PR pinned never lands — the manager commit is
rewritten to gitlink the squashed commit instead (`update-index --cacheinfo`).
Lines whose gitlink is unchanged, or that already sit at a single commit on
their dev tip, are left alone.

True cross-repo atomicity is impossible, so we (1) plan and fast-forward-check
all sides up front, then (2) push the executors first — they are the
dependencies the manager commit points at — and (3) push the manager. If the
manager push then loses a race the executors are left one fast-forward ahead,
which is harmless (monotonic) and reconciled when Merge is re-ticked.

Squashing collapses several authors into one, so `Co-authored-by:` trailers are
added for every other human author in the range (never tool/AI attribution — the
commit-message hook rejects that).

After a successful merge each line's executor mirror branch
(`pr/<line>/<feature>`, opened by branch_executor_prs.yaml) is deleted — its
tip now lives in the executor's `<line>-dev`, so the branch and its PR are
redundant.

(The dev -> version release-gate merge is a separate, not-yet-automated
flow; on that one the executor's version branch is fast-forwarded and its
dev/version branches are kept at the same point.)

Talks to GitHub through the `gh` CLI and moves refs through `git`; it does
not exec arbitrary PR code, though the workflow checks out this script itself
from the PR head (gated on the `ci-safe` label). The dev branches are
protected, so the workflow checks out with the GENVM_CI_PRIVATE_KEY deploy key
and pushes non-force
(a base that advanced is safely rejected). The active executor submodules
must be checked out with a remote `origin` that can push the executor repo
(the workflow wires its own deploy key); the active lines come from
.genvm-monorepo-root via tools.versions.

Env: GITHUB_REPOSITORY, PR_NUMBER, GH_TOKEN, E2E_CHECK_PATTERN.
"""

import json
import os
import re
import subprocess
import sys

from tools.versions import active_versions as configured_active_versions

REPO = os.environ['GITHUB_REPOSITORY']
PR = os.environ['PR_NUMBER']
E2E_PATTERN = os.environ.get('E2E_CHECK_PATTERN', 'e2e')


def run(*args, check=True, env=None):
	full_env = None
	if env is not None:
		full_env = os.environ.copy()
		full_env.update(env)
	return subprocess.run(args, check=check, text=True, capture_output=True, env=full_env)


def gh(*args):
	return run('gh', *args).stdout


def git(*args, check=True):
	return run('git', *args, check=check)


def egit(submodule, *args, check=True, env=None):
	"""git inside an executor submodule checkout."""
	return run('git', '-C', submodule, *args, check=check, env=env)


def is_ancestor(repo, maybe_ancestor, descendant):
	args = ('merge-base', '--is-ancestor', maybe_ancestor, descendant)
	if repo is None:
		return git(*args, check=False).returncode == 0
	return egit(repo, *args, check=False).returncode == 0


# Identities the commit-message hook rejects as AI/tool attribution
# (support/scripts/check-commit-message.py AI_PATTERNS). A squash must not
# resurrect one as a Co-authored-by trailer — the hook would then reject the
# message it produced, and nothing re-validates a generated squash message.
AI_IDENTITY_RE = re.compile(
	r'claude|gpt|copilot|anthropic|openai|\bai\b|noreply@anthropic', re.IGNORECASE
)


def commit_author(repo, sha):
	"""(name, email, author-date) of `sha`. NUL-separated so a name containing
	`<` or spaces round-trips."""
	out = (
		egit(repo, 'log', '-1', '--format=%an%x00%ae%x00%aI', sha)
		if repo is not None
		else git('log', '-1', '--format=%an%x00%ae%x00%aI', sha)
	).stdout.rstrip('\n')
	name, email, date = out.split('\x00')
	return name, email, date


def range_authors(repo, rev_range):
	"""Distinct (name, email) commit authors in `rev_range`, first-seen order."""
	args = ('log', '--format=%an%x00%ae', rev_range)
	out = (git(*args) if repo is None else egit(repo, *args)).stdout

	authors = []
	for line in out.split('\n'):
		if '\x00' not in line:
			continue
		name, _, email = line.partition('\x00')
		if (name, email) not in authors:
			authors.append((name, email))
	return authors


def coauthor_trailers(repo, rev_range, exclude):
	"""`Co-authored-by:` for every human author in the range except `exclude`.

	A squash keeps only one author, so everyone else who wrote a commit in the
	range would otherwise lose attribution. Authors whose identity the
	commit-message hook rejects as AI/tool attribution are dropped — otherwise the
	generated message would fail that hook, and nothing re-checks it.
	"""
	trailers = []
	for name, email in range_authors(repo, rev_range):
		if (name, email) == exclude:
			continue
		ident = f'{name} <{email}>'
		if AI_IDENTITY_RE.search(ident):
			print(f'dropping AI/tool co-author {ident}')
			continue
		trailers.append(f'Co-authored-by: {ident}')
	return trailers


def active_lines():
	"""Active executor lines as `v<X>` tags (e.g. ['v0.2', 'v0.3']). The manager
	release line is independent of these; a manager PR ships every active line."""
	return [f'v{v}' for v in configured_active_versions()]


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
	if m is None:
		block(
			f'could not resolve the executor submodule pointer ({submodule}) at `{commit}`.'
		)
	else:
		return m.group(1)


# Provenance trailer on a squashed executor commit: the PR-side commit whose
# content it carries. A squash keeps only the TREE of that commit, so the sha the
# manager PR pins never becomes an ancestor of the dev branch — ancestry alone can
# no longer answer "did this line already land?". Re-ticking Merge after a failed
# manager push (the executors are pushed first) would otherwise see a tip that is
# neither ancestor nor descendant of the pinned sha and block the PR as diverged,
# forever. This trailer is how the second run recognises its own work.
SQUASHED_FROM = 'GenVM-Squashed-From'


def build_messages(pr):
	"""Return `(manager_message, executor_message)` for this PR.

	The body is the PR body with CRLF normalized (GitHub returns CRLF, and
	`commit-tree` — unlike `git commit` — does no cleanup, so without this the
	executor and manager commits for one PR would differ byte-for-byte) and with
	any pre-existing provenance-trailer line stripped, so a body that quotes
	`GenVM-Squashed-From:` (a revert PR quoting what it reverts) cannot poison the
	re-tick lookup. The executor commit lands in another repo, so its PR reference
	is fully qualified; the manager's bare `#N` resolves in-repo.
	"""
	body = (pr['body'] or '').replace('\r\n', '\n').replace('\r', '\n')
	body = '\n'.join(
		ln for ln in body.split('\n') if not ln.strip().startswith(f'{SQUASHED_FROM}:')
	).strip()
	title = pr['title']
	tail = f'\n\n{body}\n' if body else '\n'
	return f'{title} (#{PR}){tail}', f'{title} ({REPO}#{PR}){tail}'


def already_squashed(submodule, exec_base, exec_sha):
	"""The commit on `<exec_base>` that already carries `exec_sha`'s content, if any."""
	out = egit(
		submodule,
		'log',
		f'refs/remotes/origin/{exec_base}',
		f'--grep=^{SQUASHED_FROM}: {exec_sha}$',
		'--format=%H',
		'-1',
	).stdout.strip()
	return out or None


def plan_executor_line(submodule, exec_base, exec_sha, message):
	"""Work out what this line should land on its `<line>-dev`.

	Returns `(sha, needs_push)`. The PR's commits on this line are squashed into
	one commit on top of the line's dev tip, so the executor history mirrors the
	manager's: one commit per merged PR. `sha` is that squashed commit — or
	`exec_sha` itself when there is nothing to squash (a single commit already
	sitting on the tip, or a branch that has to be created).

	Blocks if exec_sha is unknown to the executor repo or the branch has diverged.
	"""
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
		return exec_sha, True  # branch absent -> push creates it

	tip = egit(submodule, 'rev-parse', ref).stdout.strip()
	if tip == exec_sha:
		return exec_sha, False  # already there

	landed = already_squashed(submodule, exec_base, exec_sha)
	if landed is not None:
		# A previous run squashed this line and pushed it; only the manager push
		# is left. Reuse that commit rather than squashing a second one.
		print(f'Executor {exec_base} already carries {exec_sha[:12]} as {landed[:12]}')
		return landed, False

	if is_ancestor(submodule, exec_sha, tip):
		# Executor moved ahead of what this PR pins; the pinned commit is
		# already contained, so there is nothing to push.
		return exec_sha, False

	tree = egit(submodule, 'rev-parse', f'{exec_sha}^{{tree}}').stdout.strip()
	if not is_ancestor(submodule, tip, exec_sha):
		# A maintainer may have merged the auto-opened executor PR by hand: the
		# tree is already on the tip under a different sha and without our trailer.
		# Same content already landed, so mirror the tip rather than block.
		if egit(submodule, 'rev-parse', f'{tip}^{{tree}}').stdout.strip() == tree:
			print(
				f'Executor {exec_base} already carries {exec_sha[:12]} as its tip {tip[:12]}'
			)
			return tip, False
		block(
			f'executor `{exec_base}` (`{tip[:12]}`) has diverged from the PR executor '
			f'commit `{exec_sha[:12]}`; mirror is not a fast-forward.'
		)

	commits = egit(submodule, 'rev-list', f'{tip}..{exec_sha}').stdout.split()
	if len(commits) == 1:
		# Already a single commit on top of the tip: squashing it would only
		# rewrite it into an identical tree under a new sha.
		return exec_sha, True

	name, email, date = commit_author(submodule, exec_sha)

	trailers = coauthor_trailers(submodule, f'{tip}..{exec_sha}', (name, email))
	# The provenance trailer must be there for a re-tick to recognise this commit.
	trailers.append(f'{SQUASHED_FROM}: {exec_sha}')
	full_message = message + '\n' + '\n'.join(trailers) + '\n'

	# A squash is just the PR's tree on top of the tip; commit-tree builds it
	# without touching the working tree or index.
	squashed = egit(
		submodule,
		'commit-tree',
		tree,
		'-p',
		tip,
		'-m',
		full_message,
		env={
			'GIT_AUTHOR_NAME': name,
			'GIT_AUTHOR_EMAIL': email,
			'GIT_AUTHOR_DATE': date,
			'GIT_COMMITTER_NAME': 'genvm-ci',
			'GIT_COMMITTER_EMAIL': 'genvm-ci@genlayer.com',
		},
	).stdout.strip()
	print(f'Squashed {len(commits)} executor commits ({exec_base}) into {squashed}')
	return squashed, True


def plan_executor_lines(head_sha, message):
	"""Plan every active line off the PR head's gitlinks.

	Returns a list of `(submodule, line, exec_base, pinned_sha, landed_sha,
	needs_push)`. `landed_sha` differs from `pinned_sha` when the line's commits
	were squashed — the manager commit must then be rewritten to gitlink the
	squashed commit, since the one the PR pinned never lands on the dev branch.
	"""
	plans = []
	for line in active_lines():
		submodule = f'executors/{line}.x'
		exec_base = f'{line}-dev'
		pinned = executor_gitlink(head_sha, submodule)
		landed, needs_push = plan_executor_line(submodule, exec_base, pinned, message)
		plans.append((submodule, line, exec_base, pinned, landed, needs_push))
	return plans


def push_executor_lines(plans):
	"""Push each line's landed commit onto its `<line>-dev`, BEFORE the manager
	push (the manager commit gitlinks into them, so the dependencies land first)."""
	for submodule, _line, exec_base, _pinned, landed, needs_push in plans:
		if not needs_push:
			print(f'Executor {exec_base} already contains {landed}; nothing to mirror')
			continue
		print(f'Mirroring executor {exec_base} -> {landed}')
		if (
			egit(
				submodule, 'push', 'origin', f'{landed}:refs/heads/{exec_base}', check=False
			).returncode
			!= 0
		):
			block(
				f'fast-forward push of executor `{exec_base}` was rejected (it advanced); '
				f're-tick Merge.'
			)


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
	if not is_ancestor(None, f'origin/{base}', 'refs/prhead'):
		behind = git('rev-list', '--count', f'refs/prhead..origin/{base}').stdout.strip()
		block(
			f'PR is {behind} commit(s) behind `{base}`; update the branch and re-tick Merge.'
		)

	git('config', 'user.name', 'genvm-ci')
	git('config', 'user.email', 'genvm-ci@genlayer.com')

	manager_message, executor_message = build_messages(pr)

	# Plan every active executor line first: each line's commits are squashed into
	# one commit on its `<line>-dev`, so a line that was squashed lands under a
	# NEW sha and the manager commit has to gitlink that one instead of the sha the
	# PR pinned (which never lands on the dev branch).
	plans = plan_executor_lines(head_sha, executor_message)

	# Refuse to move any line's gitlink backward. The 0-behind gate is on manager
	# commits, not the gitlink, so a PR can be current on commits yet stale on the
	# pointer (another PR advanced the line after this one branched); landing it
	# would silently revert that PR's executor work.
	for submodule, _line, exec_base, _pinned, landed, _push in plans:
		base_link = executor_gitlink(f'origin/{base}', submodule)
		if landed != base_link and is_ancestor(submodule, landed, base_link):
			block(
				f'merging would move the `{submodule}` gitlink backward on `{base}` '
				f'(base pins `{base_link[:12]}`, this PR would set `{landed[:12]}`); '
				f'another change advanced it — update the PR and re-tick Merge.'
			)

	rewritten = {
		submodule: landed
		for submodule, _line, _base, pinned, landed, _push in plans
		if landed != pinned
	}

	if len(pr['commits']) == 1 and not rewritten:
		print(f'Single commit: fast-forwarding {base} to {head_sha}')
		push_sha = head_sha
	else:
		if rewritten:
			print(
				f'Executor lines were squashed; rewriting the manager gitlinks: {rewritten}'
			)
		print(f'Squashing {len(pr["commits"])} commits onto {base}')
		name, email, date = commit_author(None, head_sha)
		git('checkout', '-B', '_merge', f'origin/{base}')
		git('merge', '--squash', head_sha)

		# Repoint each squashed line at the commit that actually lands. The index
		# holds the PR's gitlinks after `merge --squash`; overwrite them in place
		# (no submodule checkout involved).
		for submodule, landed in rewritten.items():
			git('update-index', '--cacheinfo', f'160000,{landed},{submodule}')

		trailers = coauthor_trailers(None, f'origin/{base}..{head_sha}', (name, email))
		full_message = manager_message
		if trailers:
			full_message += '\n' + '\n'.join(trailers) + '\n'

		git(
			'commit',
			'--author',
			f'{name} <{email}>',
			'--date',
			date,
			'-m',
			full_message,
		)
		push_sha = git('rev-parse', 'HEAD').stdout.strip()

	# Push the executors first: the manager commit gitlinks into them, so the
	# dependencies must land before the referrer.
	push_executor_lines(plans)
	pinned = [(submodule, line) for submodule, line, _b, _p, _l, _push in plans]

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
