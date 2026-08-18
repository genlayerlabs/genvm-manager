#!/usr/bin/env python3
"""
Gate and perform a Merge of a PR into a v<X>-dev branch.

Invoked by the reusable .github/workflows/branch_merge_into_dev.yaml when a
maintainer ticks the "Merge" box on the PR action panel. It re-checks every
gate against the EXACT PR head commit and then advances the dev branch by a
plain (fast-forward-only) push, so what lands is byte-identical to what CI
and E2E validated.

Gates (all required, all on the head commit):
1. base branch is a v<X>-dev branch
2. a maintainer (push access) approved the head commit — pushing revokes it — or
	the PR carries the `rtm` (ready-to-merge) label
3. full GenVM CI (queue.yaml) concluded success
4. the cross-repo E2E check (exact name, from the exact app) concluded success
5. the PR is 0 commits behind base

Forced merge: with `$FORCE_ACTOR` set, gates 2-4 are skipped and the skip is
announced on the PR under that login. That the login is a repo admin, and that
the head is still the commit they authorized, is settled in the workflow's own
YAML before this script is fetched — this script runs from the PR head, so it
can neither authorize nor refuse anything on its own. Gates 1, 1b and 5 stay
enforced: they keep the land itself well-formed, which no authority makes safe
to skip. Locally, `--force` does the same and takes the actor from the logged-in
`gh` user; it is refused under GITHUB_ACTIONS, where that user is the bot.

Strategy: every repo ends up with ONE commit per merged PR. The manager's
commits are squashed onto base (a single commit that needs no gitlink rewrite is
fast-forwarded as-is, preserving its SHA); each executor line's commits are
squashed onto its own dev branch. Every linked PR is auto-closed as merged
afterwards (see below).

Executor mirror: the manager release line (e.g. v0.6) is independent of the
executor lines it ships (v0.2, v0.3), so a single manager commit carries a
gitlink into EACH active line's submodule. Every active line is squashed onto
its own `<line>-dev` branch (`v0.2-dev`, `v0.3-dev`, ...). Squashing gives the
line a NEW commit, so the sha the PR pinned never lands — the manager commit is
rewritten to gitlink the squashed commit instead (`update-index --cacheinfo`).
Lines whose gitlink is unchanged, or that already sit at a single commit on
their dev tip, are left alone.

True cross-repo atomicity is impossible, so the landing is staged to keep the
window in which the repos disagree as small as it can be:

1. plan and fast-forward-check every side up front;
2. push all the objects to both remotes under a scratch ref, so that no later
push has anything left to transfer;
3. move the PR head branches (manager, then each executor mirror) onto the
commits about to land, and pause — GitHub needs to see that before the base
moves to record the result as merged rather than closed;
4. move the target branches: the manager first, since it is the only push that
can lose a race, then each `<line>-dev`.

A rejected manager push therefore lands nothing, and the branch moves from
step 3 are rolled back so the PR can be re-ticked. Only step 4's executor
pushes can leave the repos split, and they are reported with the exact ref
update to replay.

A squashed message is the PR title plus one `* <subject>` bullet per collapsed
commit, each followed by that commit's own body; the PR description never lands.
A collapsed commit that was itself a squash contributes its bullets to the same
flat list, so squashing a squash lands what squashing the originals would have.
Squashing also collapses several authors
into one, so `Co-authored-by:` trailers are added for every other human author in
the range (never tool/AI attribution — the commit-message hook rejects that).

Every linked PR is auto-closed as MERGED, without deleting any branch: each
executor line's mirror branch (`pr/<line>/<feature>`, opened by
branch_executor_prs.yaml) and the manager PR's own head branch are force-moved onto
the commit that is about to land on their base (step 3 above), so once the base
moves head == base tip and GitHub marks the PR merged. (Deleting a branch, or
`gh pr close`, closes the PR as merely 'closed' and drops the merged link.)

(The dev -> version release-gate merge is a separate, not-yet-automated
flow; on that one the executor's version branch is fast-forwarded and its
dev/version branches are kept at the same point.)

Talks to GitHub through the `gh` CLI and moves refs through `git`; it does
not exec arbitrary PR code, though the workflow checks out this script itself
from the PR head (gated on the `ci-safe` label). The dev branches are
protected, so the workflow checks out with the GENVM_CI_PRIVATE_KEY deploy key
and pushes them non-force
(a base that advanced is safely rejected); the unprotected head/mirror branches
are instead force-moved onto the landed commit to auto-merge their PRs. The active executor submodules
must be checked out with a remote `origin` that can push the executor repo
(the workflow wires its own deploy key); the active lines come from
.genvm-monorepo-root via tools.versions.

Repo and PR number resolve arg > env > default via gh_common; the token is
optional (ambient `gh` auth when unset). Env also: E2E_CHECK_NAME,
E2E_CHECK_APP_SLUG.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

import behind as behind_lib
import ci_lib
import gh_common
from gh_common import pr_number, repo

from tools.versions import active_lines

RTM_LABEL = 'rtm'

# Scratch ref that carries a merge's objects to both remotes before any branch
# moves; outside refs/heads so nothing watches it and no PR is opened for it.
LANDING_REF_PREFIX = 'refs/genvm-ci/landing'

# Gap between moving the PR head branches and moving the branches they target.
# GitHub decides "merged" vs "closed" from the state it has indexed, and the two
# updates arriving together is enough to make it read a merge as a bare close.
SETTLE_SECONDS = 5


def run(*args, check=True, env=None):
	full_env = None
	if env is not None:
		full_env = os.environ.copy()
		full_env.update(env)
	return subprocess.run(args, check=check, text=True, capture_output=True, env=full_env)


def gh(*args):
	"""
	GitHub API/PR *reads* via the shared retrying wrapper, so a transient
	truncated-JSON / 5xx blip on a gate query doesn't fail the whole merge (the
	exact fragility that wrapper was hardened against).

	Writes stay on plain `run('gh', ...)` so a retry can't double-post a comment.
	Token is optional (ambient `gh` auth when unset).
	"""
	return gh_common.gh_manager(*args)


def git(*args, check=True):
	return run('git', *args, check=check)


def egit(submodule, *args, check=True, env=None):
	"""
	git inside an executor submodule checkout.
	"""
	return run('git', '-C', submodule, *args, check=check, env=env)


def is_ancestor(repo, maybe_ancestor, descendant):
	args = ('merge-base', '--is-ancestor', maybe_ancestor, descendant)
	if repo is None:
		return git(*args, check=False).returncode == 0
	return egit(repo, *args, check=False).returncode == 0


# Identity of the cross-repo E2E check-run on the head commit. A check-run name is
# not a protected namespace: any app installed on the repo — and any workflow in a
# PR — can post one under a name of its choosing. So the gate matches the exact
# name AND the app that posted it; matching a name alone (let alone a substring of
# it) would let an unrelated green check stand in for E2E.
E2E_CHECK_NAME = 'E2E Tests'
E2E_CHECK_APP_SLUG = 'ci-core-e2e-runner'


def e2e_identity():
	return (
		os.environ.get('E2E_CHECK_NAME') or E2E_CHECK_NAME,
		os.environ.get('E2E_CHECK_APP_SLUG') or E2E_CHECK_APP_SLUG,
	)


# Identities the commit-message hook rejects as AI/tool attribution
# (support/scripts/check-commit-message.py AI_PATTERNS). A squash must not
# resurrect one as a Co-authored-by trailer — the hook would then reject the
# message it produced, and nothing re-validates a generated squash message.
AI_IDENTITY_RE = re.compile(
	r'claude|gpt|copilot|anthropic|openai|\bai\b|noreply@anthropic', re.IGNORECASE
)


def commit_author(repo, sha):
	"""
	(name, email, author-date) of `sha`. NUL-separated so a name containing
	`<` or spaces round-trips.
	"""
	out = (
		egit(repo, 'log', '-1', '--format=%an%x00%ae%x00%aI', sha)
		if repo is not None
		else git('log', '-1', '--format=%an%x00%ae%x00%aI', sha)
	).stdout.rstrip('\n')
	name, email, date = out.split('\x00')
	return name, email, date


def range_authors(repo, rev_range):
	"""
	Distinct (name, email) commit authors in `rev_range`, first-seen order.
	"""
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
	"""
	`Co-authored-by:` for every human author in the range except `exclude`.

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


def block(msg):
	"""
	Comment the reason on the PR and fail the job.
	"""
	run(
		'gh',
		'pr',
		'comment',
		pr_number(),
		'--repo',
		repo(),
		'--body',
		f'❌ Merge blocked: {msg}',
		check=False,
	)
	sys.exit(1)


def pr_view(*fields):
	out = gh('pr', 'view', pr_number(), '--repo', repo(), '--json', ','.join(fields))
	return json.loads(out)


def can_maintain(login):
	"""
	Whether `login` has push rights on the repo, i.e. may approve a merge.

	`author_association` on the review would be cheaper but says the wrong thing:
	MEMBER only means "in the org", which is not push access to this repo.
	"""
	try:
		out = gh('api', f'repos/{repo()}/collaborators/{login}/permission')
	except subprocess.CalledProcessError:
		# 403/404 — not a collaborator at all.
		return False
	return json.loads(out).get('permission') in ('admin', 'maintain', 'write')


def check_approval(head_sha, labels):
	"""
	Require a maintainer's approving review of the exact commit being merged, or
	the `rtm` (ready-to-merge) label as the equivalent manual authorization.

	An approval is tied to the commit it was left on, so pushing new commits
	silently revokes it here without needing branch protection to dismiss
	anything. The label is the weaker of the two — it is held by whoever can edit
	labels and survives every subsequent push — so it authorizes only what its
	holder is willing to vouch for. It stands in for a missing approval, not for
	an unwithdrawn objection.

	Only the newest decision per reviewer counts, so a reviewer who approves and then
	requests changes on the same commit does not still authorize it. Reviews are
	returned oldest-first, and COMMENTED/PENDING ones carry no decision. An
	outstanding CHANGES_REQUESTED blocks regardless of which commit it was left on.

	Paginated, because it is the LAST page that decides: on a PR past 100 reviews an
	un-paginated read would keep an early APPROVED and never see the later
	CHANGES_REQUESTED that overrides it. `--jq '.[]'` is what makes that safe — plain
	`--paginate` concatenates one JSON array per page, which is not valid JSON.
	"""
	reviews = [
		json.loads(line)
		for line in gh(
			'api',
			'--paginate',
			f'repos/{repo()}/pulls/{pr_number()}/reviews?per_page=100',
			'--jq',
			'.[]',
		).splitlines()
		if line.strip()
	]

	decision = {}
	latest = {}
	for review in reviews:
		if review['state'] not in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
			continue
		login = (review.get('user') or {}).get('login')
		if login is None:
			continue
		latest[login] = review['state']
		if review.get('commit_id') == head_sha:
			decision[login] = review['state']

	# A change request is not commit-scoped the way an approval is: GitHub keeps
	# it blocking until the reviewer dismisses it or reviews again, however many
	# commits land after it. Judging it on `head_sha` alone would let an approval
	# of a later commit merge over a still-open objection.
	objectors = sorted(
		login for login, state in latest.items() if state == 'CHANGES_REQUESTED'
	)
	if objectors:
		listed = ', '.join(f'`{login}`' for login in objectors)
		block(
			f'changes requested by {listed} and not withdrawn; the reviewer must '
			f'approve, re-review, or dismiss before this can merge.'
		)

	if any(label['name'] == RTM_LABEL for label in labels):
		print(f'authorized by the `{RTM_LABEL}` label')
		return

	approvers = sorted(login for login, state in decision.items() if state == 'APPROVED')
	if not approvers:
		block(
			f'no approving review on `{head_sha[:12]}`, and no `{RTM_LABEL}` label. A '
			f'maintainer must approve this exact commit — approvals of earlier commits '
			f'do not carry over — or set `{RTM_LABEL}`.'
		)

	maintainers = [login for login in approvers if can_maintain(login)]
	if not maintainers:
		listed = ', '.join(f'`{login}`' for login in approvers)
		block(
			f'`{head_sha[:12]}` is approved ({listed}) but by nobody with push access; '
			f'a maintainer must approve it.'
		)

	print(f'approved on {head_sha[:12]} by: {", ".join(maintainers)}')


def check_title(pr):
	"""
	Refuse a PR whose title would not survive the commit-message hook.

	`compose_message` makes the squashed subject `<title> (#N)` verbatim, and
	nothing re-validates a generated message — so an invalid title lands as a
	commit the hook would have rejected. `initial / pr-title` checks this on
	every push, but the title can be edited afterwards without re-triggering CI
	(queue.yaml deliberately does not run on `edited`), so the authoritative
	check is here, against the exact subject about to be written.
	"""
	subject = f'{pr["title"]} {pr_reference(qualified=False)}'
	checker = ci_lib.ROOT_DIR / 'support' / 'scripts' / 'check-commit-message.py'
	result = run(sys.executable, str(checker), '--message-text', subject, check=False)
	if result.returncode != 0:
		detail = (result.stdout or result.stderr).strip()
		block(
			f'the PR title would not pass the commit-message hook, and it becomes the '
			f'squashed subject verbatim:\n\n```\n{subject}\n```\n\n```\n{detail}\n```'
		)


def force_actor() -> str | None:
	"""
	The admin who asked for a forced merge, or None on a normal merge.

	Set only after the workflow has verified, in its own YAML and before this
	file was fetched, that the login is a repo admin and that the head is still
	the commit they authorized. Nothing here re-derives that: this script is
	checked out from the PR head, so a check it ran itself would be worth
	exactly as much as the PR's own code.
	"""
	return (os.environ.get('FORCE_ACTOR') or '').strip() or None


def local_force_actor() -> str:
	"""
	The logged-in `gh` user, for a `--force` merge run by hand.

	`--force` is a LOCAL escape hatch: on a workstation the person at the
	keyboard is the authorization, and asking them to retype a login they are
	already authenticated as only invites a typo in the record. In CI the
	authorization comes from the workflow instead, so `--force` is refused there
	outright — under GITHUB_TOKEN this call answers `github-actions[bot]`, which
	would make the PR's own code the actor of record.
	"""
	if os.environ.get('GITHUB_ACTIONS') == 'true':
		raise SystemExit(
			'--force is for local runs; in CI the actor comes from $FORCE_ACTOR, '
			'set by branch_force_merge.yaml after it verifies the admin role'
		)
	login = gh('api', 'user', '--jq', '.login').strip()
	if not login:
		raise SystemExit('could not resolve the logged-in `gh` user for --force')
	return login


def announce_force(actor, head_sha):
	"""
	Record on the PR which gates were skipped and who skipped them.

	A forced merge lands a commit no green CI ever covered, so the reason it was
	allowed must survive in the PR history rather than only in a job log that
	expires.
	"""
	run(
		'gh',
		'pr',
		'comment',
		pr_number(),
		'--repo',
		repo(),
		'--body',
		f'⚠️ Forced merge requested by `{actor}` (repo admin) on `{head_sha[:12]}`: '
		f'the review, full-CI and E2E gates are NOT being enforced. The base, '
		f'draft/open, title and 0-behind checks still apply.',
		check=False,
	)


def check_gates(pr):
	if pr['state'] != 'OPEN':
		block(f'PR is not open (state: {pr["state"]}).')
	if pr['isDraft']:
		block('PR is a draft.')

	base = pr['baseRefName']
	if not re.fullmatch(r'v.*-dev', base):
		block(f'base branch `{base}` is not a `v<X>-dev` branch.')

	head_sha = pr['headRefOid']

	# 1b. the title is about to become a commit subject verbatim. Checked even
	# under --force: an invalid subject would be rejected by the commit-message
	# hook on the very next push to the dev branch, so skipping it would land a
	# breakage rather than accept a risk.
	check_title(pr)

	# Gates 2-4 answer "is this commit known-good", and a forced merge is exactly
	# the decision to land it without that answer — an admin said so explicitly,
	# in a comment recorded on the PR. What stays enforced below is the set of
	# checks that keep the LAND itself correct (base shape, fast-forwardability),
	# which no amount of authority makes safe to skip.
	actor = force_actor()
	if actor is not None:
		announce_force(actor, head_sha)
		print(f'forced by {actor}: skipping the review, CI and E2E gates')
		return base, head_sha

	# 2. a maintainer approved THIS commit, or the PR is labelled ready-to-merge
	check_approval(head_sha, pr['labels'])

	# 3. full GenVM CI (queue.yaml) green on the head commit. The same head may
	# carry a failed markerless push run alongside the full panel run, and the
	# successful run may have been started by either a push
	# (event=pull_request) or the action panel (event=workflow_dispatch) — so we
	# query all events and require ANY completed run on this exact commit to have
	# succeeded, not just the most recent.
	runs = json.loads(
		gh(
			'api',
			f'repos/{repo()}/actions/workflows/queue.yaml/runs?head_sha={head_sha}',
		)
	)['workflow_runs']
	if not any(r['status'] == 'completed' and r['conclusion'] == 'success' for r in runs):
		got = ', '.join(f'{r["status"]}/{r["conclusion"]}' for r in runs) or 'no run'
		block(f'GenVM CI (queue.yaml) is not green on `{head_sha}` (runs: {got}).')

	# 4. cross-repo E2E check green on the head commit. The API defaults to
	# `filter=latest`, so a re-run replaces its predecessor rather than adding a
	# stale entry beside it.
	name, app_slug = e2e_identity()
	checks = [
		json.loads(line)
		for line in gh(
			'api',
			'--paginate',
			f'repos/{repo()}/commits/{head_sha}/check-runs?per_page=100',
			'--jq',
			'.check_runs[]',
		).splitlines()
		if line.strip()
	]
	e2e = [
		c
		for c in checks
		if c['name'] == name and ((c.get('app') or {}).get('slug')) == app_slug
	]
	if not e2e:
		seen = ', '.join(sorted({f'`{c["name"]}`' for c in checks})) or 'none'
		block(
			f'no `{name}` check from `{app_slug}` on `{head_sha}` (run E2E on this PR '
			f'first). Checks present: {seen}.'
		)
	if any(c['conclusion'] != 'success' for c in e2e):
		conclusions = ' '.join(c['conclusion'] or 'pending' for c in e2e)
		block(f'E2E is not green on `{head_sha}` (conclusions: {conclusions}).')

	return base, head_sha


def executor_remote_sha(submodule, branch):
	"""
	Current `origin/<branch>` in an executor checkout, or None if there is none.

	`plan_executor_line` fetched every executor head, so a branch is present locally
	iff it exists on the remote. Used to remember what a branch pointed at before we
	move it, so a failed land can put it back.
	"""
	r = egit(
		submodule,
		'rev-parse',
		'--verify',
		'--quiet',
		f'refs/remotes/origin/{branch}',
		check=False,
	)
	return r.stdout.strip() if r.returncode == 0 else None


def landing_ref(line=None):
	"""
	Scratch ref carrying one repo's landing objects.

	Both executor lines are checkouts of the SAME remote, so the ref has to be keyed
	by line as well as by PR — otherwise v0.3 force-overwrites v0.2's ref and that
	line's squashed commit is left unreferenced, which is exactly the object the
	recovery path assumes is still fetchable.
	"""
	suffix = '' if line is None else f'/{line}'
	return f'{LANDING_REF_PREFIX}/pr-{pr_number()}{suffix}'


def stage_objects(plans, push_sha):
	"""
	Upload every commit that is about to land, without moving a branch anyone reads.

	Manager and executors are separate repos, so the branch moves that finish a merge
	can never be one transaction — but they can be made short. Pushing the objects
	first under a scratch ref leaves each final push a ref update with nothing to
	transfer, so the window in which the manager can gitlink a commit no executor
	branch contains shrinks from "however long the upload takes" to a round trip.

	The scratch refs also keep those objects reachable if the job dies mid-way, so a
	repair is a ref move rather than a re-squash under a fresh sha. That is what makes
	the replay instructions on a partial land runnable at all, so a failure here is
	fatal rather than a warning — nothing has moved yet, so blocking costs nothing.
	"""
	for submodule, line, _base, _pinned, landed, needs_push in plans:
		if not needs_push:
			continue
		ref = landing_ref(line)
		r = egit(submodule, 'push', '--force', 'origin', f'{landed}:{ref}', check=False)
		if r.returncode != 0:
			block(
				f'could not stage `{submodule}` objects at `{ref}`, so a partial land '
				f'could not be repaired; nothing was moved. ({r.stderr.strip()})'
			)
	ref = landing_ref()
	r = git('push', '--force', 'origin', f'{push_sha}:{ref}', check=False)
	if r.returncode != 0:
		block(
			f'could not stage manager objects at `{ref}`; nothing was moved. '
			f'({r.stderr.strip()})'
		)


def unstage_objects(plans):
	"""
	Drop the scratch refs once every target branch holds the commits. Best-effort.

	Only reached on a fully successful land: after a partial one the refs are what
	keeps the un-pushed commits fetchable, so leaving them is the point.
	"""
	for submodule, line, _base, _pinned, _landed, needs_push in plans:
		if needs_push:
			egit(submodule, 'push', 'origin', '--delete', landing_ref(line), check=False)
	git('push', 'origin', '--delete', landing_ref(), check=False)


def move_executor_mirror_branch(submodule, line, head_ref, landed):
	"""
	Move the executor mirror branch `pr/<line>/<head_ref>` onto `landed`.

	The mirror PR (`pr/<line>/<head_ref>` -> `<line>-dev`, branch_executor_prs.yaml)
	is auto-closed as MERGED once its head is contained in its base, so we point the
	head at the very commit `<line>-dev` is about to receive. Doing this BEFORE the
	base moves is what makes GitHub read the result as a merge; doing it after races
	the base update and can land the PR as merely 'closed'. (Deleting the branch — the
	original behaviour — always dropped the merged link.)

	Only lines this PR actually moved were provisioned a mirror branch; an untouched
	line has none, and force-pushing would CREATE a stray dangling ref, so move only a
	branch that already exists. Returns the sha to restore on rollback, or None when
	there was nothing to move.
	"""
	branch = f'pr/{line}/{head_ref}'
	previous = executor_remote_sha(submodule, branch)
	if previous is None:
		print(f'no executor mirror branch {branch}; nothing to auto-merge for {line}')
		return None
	r = egit(
		submodule, 'push', '--force', 'origin', f'{landed}:refs/heads/{branch}', check=False
	)
	if r.returncode != 0:
		print(f'note: could not move executor mirror branch {branch}: {r.stderr.strip()}')
		return None
	print(f'moved executor mirror branch {branch} -> {landed[:12]} (auto-merges its PR)')
	return previous


def move_manager_pr_head(pr, head_sha, push_sha):
	"""
	Point the manager PR's head branch at `push_sha`, the commit `<base>` will get.

	Same trick as the executor mirrors, and again before the base moves: head contained
	in base is what GitHub reads as merged, unlike a bare `gh pr close` (merely
	'closed').

	The rollback target is `head_sha` rather than whatever `origin/<head_ref>` happens
	to hold locally: the gates already pinned the PR head to it and `merge()` re-checked
	it, so it is exact, and it does not silently depend on the workflow checking out
	with full history.

	A fork PR's head branch lives in another repo, so `<head_ref>` is not a branch on
	`origin` — moving it would create a stray branch and not touch the PR. Those never
	reach here (the merge gate needs a same-repo run), but be explicit and leave fork
	PRs to the explicit close after the land. Returns the sha to restore on rollback,
	or None when there was deliberately nothing to move.
	"""
	head_ref = pr['headRefName']
	if pr.get('isCrossRepository'):
		print(f'PR head `{head_ref}` is on a fork; cannot move it, will close explicitly')
		return None
	# A PR raised FROM one dev branch INTO another would make this force-push a
	# squashed commit onto a protected branch, using a key that bypasses protection.
	if re.fullmatch(r'v.*-dev', head_ref):
		block(f'PR head `{head_ref}` is itself a dev branch; refusing to force-move it.')
	r = git('push', '--force', 'origin', f'{push_sha}:refs/heads/{head_ref}', check=False)
	if r.returncode != 0:
		# Leaving it unmoved would land the base anyway and strand the PR open, with
		# no rollback entry to undo. Nothing has moved yet, so stop here.
		block(f'could not move PR head `{head_ref}`; nothing landed. ({r.stderr.strip()})')
	print(f'moved manager PR head `{head_ref}` -> {push_sha[:12]} (auto-merges the PR)')
	return head_sha


def restore_branches(moved):
	"""
	Put every branch we moved back where it was, after a target push was rejected.

	Without this a rejected land leaves the PR head sitting on the squashed commit:
	the head sha then no longer matches the one CI and E2E ran on, so re-ticking Merge
	would be refused by its own gates and the PR could not be landed at all.
	"""
	for submodule, branch, previous in moved:
		r = (
			git('push', '--force', 'origin', f'{previous}:refs/heads/{branch}', check=False)
			if submodule is None
			else egit(
				submodule,
				'push',
				'--force',
				'origin',
				f'{previous}:refs/heads/{branch}',
				check=False,
			)
		)
		where = 'manager' if submodule is None else submodule
		if r.returncode == 0:
			print(f'restored {where} branch {branch} -> {previous[:12]}')
		else:
			print(f'note: could not restore {where} branch {branch}: {r.stderr.strip()}')


def executor_gitlink(commit, submodule):
	"""
	Executor submodule commit (gitlink) recorded at `commit`'s tree.
	"""
	out = git('ls-tree', commit, submodule).stdout
	# "160000 commit <sha>\t<path>"
	m = re.match(r'\S+\s+commit\s+([0-9a-f]+)\t', out)
	if m is None:
		block(
			f'could not resolve the executor submodule pointer ({submodule}) at `{commit}`.'
		)
	else:
		return m.group(1)


def pr_reference(qualified=True):
	"""
	The `(#N)` suffix a squashed subject ends with.

	A commit landing in an executor repo has to name the manager repo for the
	reference to resolve; the manager's own bare `#N` resolves in-repo.
	"""
	return f'({repo()}#{pr_number()})' if qualified else f'(#{pr_number()})'


def commit_entries(repo, rev_range):
	"""
	`(subject, body)` per commit in `rev_range`, oldest first, merges excluded.

	`-z` terminates the records, a body being able to contain anything. The two
	halves need no separator: a subject is one line, so the first newline is it.
	"""
	args = ('log', '--reverse', '--no-merges', '-z', '--format=%s%n%b', rev_range)
	out = (git(*args) if repo is None else egit(repo, *args)).stdout
	entries = []
	for record in out.split('\x00'):
		if not record.strip():
			continue
		subject, _, body = record.partition('\n')
		entries.append((subject.strip(), body.strip('\n')))
	return entries


def flatten_body(body):
	"""
	A commit body as message lines, bullets kept at the top level.

	A body line starting with `*` IS a bullet -- that is the invariant the format
	rests on -- so an already-squashed commit's bullets continue the outer list
	instead of nesting under it. Prose keeps its own paragraph breaks, and gains
	the blank line on either side that separates it from the bullets around it.
	"""
	lines = []
	prose = []

	def flush():
		while prose and not prose[0].strip():
			prose.pop(0)
		while prose and not prose[-1].strip():
			prose.pop()
		if prose:
			lines.extend(['', *prose, ''])
			prose.clear()

	for line in body.split('\n'):
		# Indentation stripped, as the commit-message hook does.
		if line.strip().startswith('*'):
			flush()
			lines.append(line.strip())
		else:
			prose.append(line)
	flush()
	return lines


def compose_message(pr, submodule, rev_range, author, *, qualified=True):
	"""
	The squash message for `rev_range`: the PR title, one `* <subject>` bullet
	per squashed commit, each followed by that commit's own body, then
	`Co-authored-by:` for every other author.

	The bullets are already conventional commits (the commit-message hook saw
	them) and are what `make_release_notes` promotes into the changelog, whereas a
	PR description is written for reviewers and only rots on a dev branch. Bodies
	carry the `why`, which nothing else preserves once the originals are gone. A
	range whose one commit already says the title gets no bullets, only its body.

	Squashing a squash flattens the inner bullets into the outer list instead of
	nesting them, so every real entry stays at the level both readers of this
	format look at. The inner umbrella subject rides along as one extra bullet,
	and the changelog gains that one redundant line.
	"""
	entries = commit_entries(submodule, rev_range)
	if [subject for subject, _ in entries] == [pr['title']]:
		lines = flatten_body(entries[0][1])
	else:
		lines = []
		for subject, body in entries:
			lines.append(f'* {subject}')
			lines.extend(flatten_body(body))

	message = f'{pr["title"]} {pr_reference(qualified)}\n'
	body = '\n'.join(lines).strip('\n')
	if body:
		message += f'\n{body}\n'
	trailers = coauthor_trailers(submodule, rev_range, author)
	if trailers:
		message += '\n' + '\n'.join(trailers) + '\n'
	return message


def already_squashed(submodule, exec_base, tree):
	"""
	The commit on `<exec_base>` that this run already landed for `tree`, if any.

	A squash keeps only the TREE of the pinned commit, so that commit never becomes
	an ancestor of the dev branch and ancestry alone cannot answer "did this line
	already land?" — a re-tick after a partial land would see a tip that is neither
	ancestor nor descendant and block the PR as diverged, forever.

	A commit is this PR's own work when BOTH halves of its identity match: its
	subject references this PR, and its tree is the one being landed. Neither alone
	is enough — the subject alone would mistake an earlier landing for the current
	one after the author pushed more executor commits (silently dropping them), and
	the tree alone would mistake a revert that restored an older tree.
	"""
	out = egit(
		submodule,
		'log',
		f'refs/remotes/origin/{exec_base}',
		'-F',
		f'--grep={pr_reference()}',
		'--format=%H%x00%T%x00%s',
	).stdout
	for line in out.split('\n'):
		if line.count('\x00') != 2:
			continue
		sha, commit_tree, subject = line.split('\x00')
		if commit_tree == tree and subject.endswith(pr_reference()):
			return sha
	return None


def plan_executor_line(submodule, exec_base, exec_sha, pr):
	"""
	Work out what this line should land on its `<line>-dev`.

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

	tree = egit(submodule, 'rev-parse', f'{exec_sha}^{{tree}}').stdout.strip()

	landed = already_squashed(submodule, exec_base, tree)
	if landed is not None:
		# A previous run squashed this line and pushed it; only the manager push
		# is left. Reuse that commit rather than squashing a second one.
		print(f'Executor {exec_base} already carries {exec_sha[:12]} as {landed[:12]}')
		return landed, False

	if is_ancestor(submodule, exec_sha, tip):
		# Executor moved ahead of what this PR pins; the pinned commit is
		# already contained, so there is nothing to push.
		return exec_sha, False

	if not is_ancestor(submodule, tip, exec_sha):
		# A maintainer may have merged the auto-opened executor PR by hand: the tree
		# is already on the tip under a different sha and a subject of their own
		# choosing. Same content already landed, so mirror the tip rather than block.
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
	full_message = compose_message(pr, submodule, f'{tip}..{exec_sha}', (name, email))

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


def plan_executor_lines(head_sha, pr):
	"""
	Plan every active line off the PR head's gitlinks.

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
		landed, needs_push = plan_executor_line(submodule, exec_base, pinned, pr)
		plans.append((submodule, line, exec_base, pinned, landed, needs_push))
	return plans


def push_executor_lines(plans):
	"""
	Advance each line's `<line>-dev` onto its landed commit, after the manager push.

	Every line is attempted even if an earlier one fails, so one rejected line does
	not strand the rest: the manager commit already gitlinks all of them, and each
	push is independent. A failure is reported at the end with the exact ref update
	to replay, because by this point the PR is landed and closed — re-ticking Merge
	is no longer an option.
	"""
	failed = []
	for submodule, _line, exec_base, _pinned, landed, needs_push in plans:
		if not needs_push:
			print(f'Executor {exec_base} already contains {landed}; nothing to mirror')
			continue
		print(f'Mirroring executor {exec_base} -> {landed}')
		r = egit(
			submodule, 'push', 'origin', f'{landed}:refs/heads/{exec_base}', check=False
		)
		if r.returncode != 0:
			failed.append((submodule, exec_base, landed, r.stderr.strip()))

	if failed:
		details = '\n'.join(
			f'- `{submodule}`: `git push origin {landed}:refs/heads/{exec_base}` ({err})'
			for submodule, exec_base, landed, err in failed
		)
		block(
			'the manager side landed but these executor branches did not advance, so the '
			'new gitlinks point at commits missing from their dev branches. Replay:\n'
			f'{details}'
		)


def merge(pr, base, head_sha):
	# Fetch the exact head commit (works for fork PRs too) and live base tip.
	git(
		'fetch',
		'--no-tags',
		'origin',
		f'refs/pull/{pr_number()}/head:refs/prhead',
		f'+refs/heads/{base}:refs/remotes/origin/{base}',
	)

	fetched = git('rev-parse', 'refs/prhead').stdout.strip()
	if fetched != head_sha:
		block(
			f'head moved during merge (expected `{head_sha}`, got `{fetched}`); re-tick Merge.'
		)

	# 5. authoritative 0-commits-behind check at merge time. Same measurement as
	# `initial / behind-check` and rebase-watch, so the three cannot disagree.
	count = behind_lib.behind_by_git(f'origin/{base}', 'refs/prhead')
	if count:
		block(
			f'PR is {count} commit(s) behind `{base}`; update the branch and re-tick Merge.'
		)

	git('config', 'user.name', 'genvm-ci')
	git('config', 'user.email', 'genvm-ci@genlayer.com')

	# Plan every active executor line first: each line's commits are squashed into
	# one commit on its `<line>-dev`, so a line that was squashed lands under a
	# NEW sha and the manager commit has to gitlink that one instead of the sha the
	# PR pinned (which never lands on the dev branch).
	plans = plan_executor_lines(head_sha, pr)

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

		full_message = compose_message(
			pr, None, f'origin/{base}..{head_sha}', (name, email), qualified=False
		)

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

	# 1. Get every object onto both remotes while nothing is watching, so the two
	# branch moves below are ref updates with nothing left to transfer.
	stage_objects(plans, push_sha)

	# 2. Point the PR head branches at what is about to land. This has to precede
	# the base moves for GitHub to read the result as a merge rather than a close,
	# and it is undone if the land is then rejected — a PR head left on the squashed
	# commit no longer matches the sha CI ran on, which would lock the PR out of its
	# own gates.
	moved = []
	head_ref = pr['headRefName']
	previous = move_manager_pr_head(pr, head_sha, push_sha)
	if previous is not None:
		moved.append((None, head_ref, previous))
	for submodule, line, _base, _pinned, landed, _push in plans:
		previous = move_executor_mirror_branch(submodule, line, head_ref, landed)
		if previous is not None:
			moved.append((submodule, f'pr/{line}/{head_ref}', previous))

	time.sleep(SETTLE_SECONDS)

	# 3. Move the target branches. The manager goes first: it is the only push that
	# can lose a race (the executor dev branches only move through this same tool,
	# under the same PR gates), so a rejection here has nothing to unwind but the
	# branch moves above. Its commit gitlinks executor commits that are already on
	# the remote — reachable from the scratch ref and the mirror branches — so the
	# gitlinks resolve even in the window before the executor branches advance.
	if (
		git('push', 'origin', f'{push_sha}:refs/heads/{base}', check=False).returncode != 0
	):
		restore_branches(moved)
		block(
			f'fast-forward push to manager `{base}` was rejected (base advanced); '
			f'nothing landed. Update the branch and re-tick Merge.'
		)

	push_executor_lines(plans)

	unstage_objects(plans)

	run(
		'gh',
		'pr',
		'comment',
		pr_number(),
		'--repo',
		repo(),
		'--body',
		f'✅ Merged into `{base}` (`{push_sha}`) via fast-forward.'
		+ (
			f' Forced by `{force_actor()}` — the review, CI and E2E gates were not enforced.'
			if force_actor()
			else ''
		),
		check=False,
	)

	# A fork PR's head lives in another repo, so it was never moved and GitHub has
	# nothing to detect; close it explicitly (as merely 'closed' — unavoidable).
	if pr.get('isCrossRepository'):
		run('gh', 'pr', 'close', pr_number(), '--repo', repo(), check=False)


class GenvmMergeIntoDev(ci_lib.Tool):
	"""
	Gate and perform a Merge of a PR into a v<X>-dev branch.
	"""

	def name(self) -> str:
		return 'genvm-merge-into-dev'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, head_ref=False)
		parser.add_argument(
			'--force',
			action='store_true',
			help='merge without requiring an approving review, green CI or green '
			'E2E; local runs only, and recorded on the PR under your `gh` login',
		)

	def handler(self, args: argparse.Namespace) -> int:
		gh_common.set_ctx(gh_common.Ctx.from_args(args))
		if args.force:
			# One channel for "who forced this", whatever set it: the announcement
			# and the merge comment both read it back through `force_actor()`.
			os.environ['FORCE_ACTOR'] = local_force_actor()
		merge_into_dev()
		return 0


def merge_into_dev():
	pr = pr_view(
		'baseRefName',
		'headRefName',
		'headRefOid',
		'commits',
		'labels',
		'state',
		'isDraft',
		'title',
		'isCrossRepository',
	)
	base, head_sha = check_gates(pr)
	merge(pr, base, head_sha)


COMMANDS = [GenvmMergeIntoDev()]
