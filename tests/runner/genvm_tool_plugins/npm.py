"""
npm projects: one case per `*.test.ts` file, plus the install and typecheck
they share.

Per-file cases mean a failure names the file and `--filter-name` can select
one, matching how Rust integration tests collect. What they cannot each do is
install: `node_modules` is one directory per project, so `npm ci` is its own
case and every other case in the project `depends_on` it. A dependency is
pulled in even when the filter excludes it, and its failure skips the
dependents rather than letting them fail confusingly.
"""

import json
import os
from pathlib import Path

import genvm_tool.tests
import genvm_tool_plugins.source_tags as source_tags

default_env = {
	k: v
	for k, v in os.environ.items()
	if genvm_tool.tests.util.environ.DEFAULT_FILTER(k, v)
}


def scripts(package_json: Path) -> dict:
	"""
	A `package.json`'s `scripts`, or empty when it has none or is unreadable.

	Unreadable is not an error here: collection walks every tracked
	`package.json`, and one that cannot be parsed simply declares no cases.
	"""
	try:
		found = json.loads(package_json.read_text()).get('scripts', {})
	except (OSError, json.JSONDecodeError):
		return {}
	return found if isinstance(found, dict) else {}


def _case(
	ctx: genvm_tool.tests.stage.collection.Context,
	desc: genvm_tool.tests.test.Description,
	*,
	project_root_dir: Path,
	command: list[str],
):
	ctx.add_case(
		genvm_tool.tests.test.SimpleCommandCase(
			description=desc,
			command=command,
			cwd=project_root_dir,
			env=default_env,
			mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
		)
	)


def npm_project(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	project_root_dir: Path,
	test_files: list[Path],
):
	"""
	Collect one project: its install, its typecheck, and one case per test file.

	`needs-web` is not incidental -- `npm ci` reaches the registry, so a cell
	without egress can only fail. The dependents carry it too, since they
	cannot run without what it installs.
	"""
	rel_dir = project_root_dir.relative_to(ctx.shared.root_dir)
	base_tags = ['typescript', 'needs-web']

	install_name = f'{rel_dir}/npm-ci'
	_case(
		ctx,
		genvm_tool.tests.test.Description(install_name).with_tags(base_tags),
		project_root_dir=project_root_dir,
		command=['npm', 'ci'],
	)

	shared_desc = genvm_tool.tests.test.Description('').with_tags(base_tags + ['unit'])
	shared_desc = shared_desc.with_depends_on([install_name])

	if 'typecheck' in scripts(project_root_dir / 'package.json'):
		_case(
			ctx,
			shared_desc._replace(name=f'{rel_dir}/typecheck'),
			project_root_dir=project_root_dir,
			command=['npm', 'run', 'typecheck'],
		)

	for test_file in test_files:
		rel_file = test_file.relative_to(ctx.shared.root_dir)
		_case(
			ctx,
			shared_desc._replace(name=str(rel_file)).with_tags(
				source_tags.from_source(ctx, test_file)
			),
			project_root_dir=project_root_dir,
			# `npm test` forwards what follows `--`, and the project's runner
			# takes a file to restrict itself to. Going through the script
			# rather than spelling the runner here keeps the loader flags in
			# `package.json`, where a human running one file also finds them.
			command=['npm', 'test', '--', str(test_file.relative_to(project_root_dir))],
		)
