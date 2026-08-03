import json
import subprocess
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

CI_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI_DIR))

import executor_pr_link  # noqa: E402
import gh_common  # noqa: E402
import pr_branches_info  # noqa: E402
from tools import open_executor_prs, pr_branches  # noqa: E402


class ExecutorPrLinkTests(unittest.TestCase):
	def section(self, manager_pr: str = '42') -> str:
		return executor_pr_link.render(
			'genlayerlabs/genvm-manager',
			manager_pr,
			'v0.3',
			'pr/v0.3/feat/example',
			'v0.3-dev',
		)

	def test_render_is_machine_readable_and_explains_e2e_ownership(self):
		section = self.section()
		encoded = section.split('\n', 2)[1]
		data = json.loads(encoded)

		self.assertEqual(data['schema_version'], 1)
		self.assertEqual(data['manager_pr'], 42)
		self.assertEqual(data['executor_line'], 'v0.3')
		self.assertEqual(data['gitlink_path'], 'executors/v0.3.x')
		self.assertEqual(data['cross_repo_e2e_source'], 'manager-pr-gitlink')
		self.assertIn('This executor PR alone is not enrolled in cross-repo E2E', section)

	def test_upsert_preserves_unmanaged_body_text(self):
		body = 'Reviewer notes stay here.'
		updated = executor_pr_link.upsert(body, self.section())

		self.assertTrue(updated.startswith(body))
		self.assertIn(executor_pr_link.LINK_START, updated)

	def test_upsert_replaces_stale_link_idempotently(self):
		body = f'Notes\n\n{self.section("41")}\n\nFooter'
		current = self.section('42')
		updated = executor_pr_link.upsert(body, current)

		self.assertNotIn('/pull/41', updated)
		self.assertIn('/pull/42', updated)
		self.assertEqual(updated.count(executor_pr_link.LINK_START), 1)
		self.assertEqual(executor_pr_link.upsert(updated, current), updated)

	def test_reconcile_patches_existing_pr_once(self):
		calls = []

		def fake_gh(*args, **kwargs):
			calls.append((args, kwargs))
			if '--method' not in args:
				return subprocess.CompletedProcess(args, 0, stdout='Existing notes', stderr='')
			return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

		executor_pr_link.reconcile(
			executor_repo='genlayerlabs/genvm-executor',
			executor_pr_url='https://github.com/genlayerlabs/genvm-executor/pull/7',
			manager_repo='genlayerlabs/genvm-manager',
			manager_pr='42',
			line='v0.3',
			head='pr/v0.3/feat/example',
			base='v0.3-dev',
			token='token',
			gh=fake_gh,
		)

		self.assertEqual(len(calls), 2)
		self.assertIn('repos/genlayerlabs/genvm-executor/pulls/7', calls[0][0])
		self.assertIn('PATCH', calls[1][0])
		self.assertFalse(calls[1][1]['retry'])
		body_arg = next(arg for arg in calls[1][0] if arg.startswith('body='))
		self.assertIn('Existing notes', body_arg)
		self.assertIn('manager-pr-gitlink', body_arg)

	def test_open_executor_prs_reconciles_reverse_link(self):
		ctx = gh_common.Ctx(
			'genlayerlabs/genvm-manager',
			'genlayerlabs/genvm-executor',
			'42',
			'feat/example',
		)
		url = 'https://github.com/genlayerlabs/genvm-executor/pull/7'
		with (
			mock.patch.object(open_executor_prs, 'active_lines', return_value=['v0.3']),
			mock.patch.object(open_executor_prs, 'executor_branch_exists', return_value=True),
			mock.patch.object(open_executor_prs, 'existing_pr', return_value=url),
			mock.patch.object(open_executor_prs, 'upsert_comment'),
			mock.patch.object(executor_pr_link, 'reconcile') as reconcile,
		):
			open_executor_prs.open_executor_prs(ctx)

		reconcile.assert_called_once_with(
			executor_repo='genlayerlabs/genvm-executor',
			executor_pr_url=url,
			manager_repo='genlayerlabs/genvm-manager',
			manager_pr='42',
			line='v0.3',
			head='pr/v0.3/feat/example',
			base='v0.3-dev',
			token=mock.ANY,
		)

	def test_provision_reconciles_reverse_link_on_existing_pr(self):
		ctx = gh_common.Ctx(
			'genlayerlabs/genvm-manager',
			'genlayerlabs/genvm-executor',
			'42',
			'feat/example',
		)
		manager = pr_branches_info.RepoInfo(
			path='.',
			repo=ctx.manager_repo,
			line=None,
			base_ref='v0.6-dev',
			head_ref='feat/example',
			base_sha='a' * 40,
			head_sha='b' * 40,
			ahead_by=1,
			behind_by=0,
			pr_url='https://github.com/genlayerlabs/genvm-manager/pull/42',
		)
		url = 'https://github.com/genlayerlabs/genvm-executor/pull/7'
		executor = pr_branches_info.RepoInfo(
			path='executors/v0.3.x',
			repo=ctx.executor_repo,
			line='v0.3',
			base_ref='v0.3-dev',
			head_ref='pr/v0.3/feat/example',
			base_sha='c' * 40,
			head_sha='d' * 40,
			ahead_by=1,
			behind_by=0,
			pr_url=url,
		)
		result = subprocess.CompletedProcess((), 0, stdout='Example title\n', stderr='')
		with (
			mock.patch.object(gh_common.Ctx, 'from_args', return_value=ctx),
			mock.patch.object(
				pr_branches_info,
				'from_ctx',
				return_value={'.': manager, executor.path: executor},
			),
			mock.patch.object(pr_branches_info, 'gh', return_value=result),
			mock.patch.object(pr_branches, 'force_push_head', return_value='updated'),
			mock.patch.object(executor_pr_link, 'reconcile') as reconcile,
		):
			rc = pr_branches.PrBranches().provision(Namespace())

		self.assertEqual(rc, 0)
		reconcile.assert_called_once_with(
			executor_repo=ctx.executor_repo,
			executor_pr_url=url,
			manager_repo=ctx.manager_repo,
			manager_pr='42',
			line='v0.3',
			head='pr/v0.3/feat/example',
			base='v0.3-dev',
			token=mock.ANY,
		)

	def test_rejects_noncanonical_pr_url(self):
		with self.assertRaisesRegex(ValueError, 'not a canonical GitHub pull request URL'):
			executor_pr_link.pr_number_from_url(
				'https://github.com/genlayerlabs/genvm-executor'
			)


if __name__ == '__main__':
	unittest.main()
