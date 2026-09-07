#!/usr/bin/env python3
"""
Report published advisories against the upstream sources the manager vendors.

A pin does not have to move to become vulnerable, so no PR-triggered check can
catch this — the day an advisory is published, every pin that stood still
becomes a finding and no PR is involved. This sweeps on a schedule instead and
keeps a single tracking issue in sync with what OSV currently says. It gates
nothing and writes no branch; the measurement is `wasmtime_pins`.

The automation never closes and never reopens an issue. Closing is the reader's
ruling on the findings that issue names — GenVM disables features and carries a
local patch stack, so whether an upstream advisory is reachable here is a
judgement OSV cannot make. So the advisory ids a closed issue covered are
recorded in its own marker, and a later sweep stays quiet while it finds nothing
outside that set. A new id files a fresh issue that supersedes it.

The issue is found by label (the query GitHub can filter server-side) and
identified by a marker in its body: a label is not a namespace, so anything else
that adopts this label — a person filing by hand, another tool — must not be
mistaken for this tool's issue and rewritten. The marker is also where the
issue's own state lives, since GitHub gives an issue no field this tool owns.
"""

import argparse
import dataclasses
import json
import os
import re

import ci_lib
import gh_common
import wasmtime_pins
from gh_common import gh_manager as gh
from gh_common import repo
from tools.versions import active_lines
from wasmtime_pins import Advisory, Subject

LABEL = 'wasmtime-maintenance'
LABEL_COLOR = 'b60205'
LABEL_DESCRIPTION = 'published advisories against vendored upstream sources'

MARKER_NAME = 'genvm-ci/wasmtime-watch'
MARKER_RE = re.compile(
	rf'<!-- {re.escape(MARKER_NAME)} advisories=(\S*)(?: supersedes=(\d+))? -->'
)

OWNER_ENV = 'WASMTIME_REBASE_OWNER'


@dataclasses.dataclass(frozen=True)
class Marker:
	"""
	The state this tool keeps in an issue body.
	"""

	ids: frozenset[str]
	supersedes: int | None = None

	def render(self) -> str:
		trailer = f' supersedes={self.supersedes}' if self.supersedes else ''
		return f'<!-- {MARKER_NAME} advisories={",".join(sorted(self.ids))}{trailer} -->'


def parse_marker(body: str | None) -> Marker | None:
	"""
	The marker an issue body carries, or None if it carries none.
	"""
	match = MARKER_RE.search(body or '')
	if match is None:
		return None
	return Marker(
		ids=frozenset(id for id in match.group(1).split(',') if id),
		supersedes=int(match.group(2)) if match.group(2) else None,
	)


def body_for(
	findings: dict[Advisory, list[Subject]], owner: str | None, marker: Marker
) -> str:
	if findings:
		rows = '\n'.join(
			f'| [{advisory.id}]({advisory.url}) '
			f'| {advisory.summary.replace("|", r"\|")} '
			f'| {", ".join(str(subject) for subject in subjects)} |'
			for advisory, subjects in findings.items()
		)
		table = f'| Advisory | Summary | Affected |\n|---|---|---|\n{rows}'
	else:
		table = 'No vendored source currently matches a published advisory.'

	owner_line = f'@{owner}' if owner else f'**unset** (`{OWNER_ENV}`)'
	supersedes = f'\nSupersedes #{marker.supersedes}.\n' if marker.supersedes else ''

	return f"""{marker.render()}
## Published advisories against vendored upstream sources

{table}
{supersedes}
**Owner:** {owner_line}

Each vendored repo is matched by its pinned upstream commit, and every crate
that comes out of one by its locked version. OSV knows nothing about the
features GenVM disables or the patches it carries, so a row is a finding to rule
on rather than a proven exposure.

Fixing one goes through the usual flow — a manager feature branch, an executor
mirror at `pr/<line>/<feature>` targeting `<line>-dev`, then the standing
release gate. Nothing here writes to a branch.

This issue is edited in place while it is open. **Close it once you have ruled
on the findings above**; it is never closed or reopened automatically, and a new
issue is filed only when an advisory outside this list appears.
"""


def title_for(findings: dict[Advisory, list[Subject]]) -> str:
	return f'[wasmtime] {len(findings)} published advisory(ies) affect vendored sources'


def marker_issues(state: str) -> list[dict]:
	"""
	This tool's own issues in `state`, newest first.

	`sort=created`, because the newest closed issue is the one whose ruling is
	current.
	"""
	out = gh(
		'api',
		gh_common.api_path(
			f'repos/{repo()}/issues',
			state=state,
			labels=LABEL,
			per_page=100,
			sort='created',
			direction='desc',
		),
		'--jq',
		# The issues endpoint returns PRs too; they carry a `pull_request` key.
		'.[] | select(has("pull_request") | not) | {number, title, body}',
	)
	issues = [json.loads(line) for line in out.splitlines() if line.strip()]
	return [issue for issue in issues if parse_marker(issue['body']) is not None]


def create_issue(title: str, body: str, owner: str | None) -> None:
	gh_common.ensure_label(LABEL, LABEL_COLOR, LABEL_DESCRIPTION)
	payload: dict = {'title': title, 'body': body, 'labels': [LABEL]}
	if owner:
		payload['assignees'] = [owner]
	gh_common.api_json('POST', f'repos/{repo()}/issues', payload)


def update_issue(issue: dict, title: str, body: str) -> None:
	"""
	Edit the open issue in place, and only when something actually differs.

	A no-op PATCH still notifies everyone subscribed to the issue, and this runs
	daily. Bodies are compared with line endings normalized, so a round-trip
	through GitHub cannot make an unchanged body look different every morning.

	Assignees are deliberately left out: they are set once at creation, and a
	sweep that re-asserted them would undo every hand-off.
	"""

	def normalized(text: str) -> str:
		return text.replace('\r\n', '\n')

	payload = {}
	if issue['title'] != title:
		payload['title'] = title
	if normalized(issue['body']) != normalized(body):
		payload['body'] = body
	if not payload:
		print(f'#{issue["number"]} is already up to date')
		return
	gh_common.api_json('PATCH', f'repos/{repo()}/issues/{issue["number"]}', payload)


def sweep(dry_run: bool) -> int:
	owner = os.environ.get(OWNER_ENV) or None
	if owner is None:
		print(f'::warning::{OWNER_ENV} is unset; the issue will be unassigned')

	findings = wasmtime_pins.scan(active_lines())
	current = frozenset(advisory.id for advisory in findings)

	open_issues = marker_issues('open')
	if len(open_issues) > 1:
		# A past race, or someone filing by hand. Report it rather than picking
		# one silently: two issues means two places a ruling can be recorded.
		numbers = ', '.join(f'#{issue["number"]}' for issue in open_issues)
		ci_lib.github_error(f'several open {LABEL} issues ({numbers}); resolve by hand')
		return 1

	if open_issues:
		issue = open_issues[0]
		# The supersede link is carried forward from the issue's own marker: it is
		# a fact about which issue this one replaced, not about today's findings.
		previous = parse_marker(issue['body'])
		assert previous is not None, 'marker_issues returns only issues with a marker'
		marker = Marker(ids=current, supersedes=previous.supersedes)
		print(f'updating #{issue["number"]} ({len(findings)} finding(s))')
		if not dry_run:
			update_issue(issue, title_for(findings), body_for(findings, owner, marker))
		return 0

	if not current:
		print('no advisories and no open issue: nothing to report')
		return 0

	# Nothing open: the newest closed issue carries the last ruling. Everything it
	# already named has been ruled on, so only something outside that set is news.
	closed = marker_issues('closed')
	ruled_on = parse_marker(closed[0]['body']).ids if closed else frozenset()
	new = current - ruled_on
	if not new:
		print(
			f'{len(current)} advisory(ies), all already ruled on in '
			f'#{closed[0]["number"]}: staying quiet'
		)
		return 0

	marker = Marker(ids=current, supersedes=closed[0]['number'] if closed else None)
	print(f'filing an issue for {len(new)} new advisory(ies): {", ".join(sorted(new))}')
	if not dry_run:
		create_issue(title_for(findings), body_for(findings, owner, marker), owner)
	return 0


class WasmtimeWatch(ci_lib.Tool):
	"""
	Track published advisories against the vendored upstream sources.
	"""

	def name(self) -> str:
		return 'wasmtime-watch'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, pr=False, head_ref=False)
		parser.add_argument(
			'--dry-run',
			action='store_true',
			help='report what would be filed or edited without touching any issue',
		)

	def handler(self, args: argparse.Namespace) -> int:
		gh_common.set_ctx(gh_common.Ctx.from_args(args))
		return sweep(args.dry_run)


COMMANDS = [WasmtimeWatch()]
