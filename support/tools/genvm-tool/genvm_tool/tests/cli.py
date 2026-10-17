#!/usr/bin/env python3

"""Command-line interface for genvm-tool test."""

import argparse
import asyncio
import os
import signal
import sys
import typing
import xml.etree.ElementTree as ET
from pathlib import Path

import genvm_tool.tests
import genvm_tool.tests.stage
from genvm_tool import common, formatter

from .stage.pipeline import (
	CollectionStage,
	ExecutionStage,
	FilterStage,
	ReportStage,
	SchedulingStage,
	pipe,
	wrap,
)


class _ParserResult(typing.NamedTuple):
	parser: argparse.ArgumentParser
	run_parser: argparse.ArgumentParser
	filter_parser: argparse.ArgumentParser


def create_parser() -> _ParserResult:
	"""Create the command-line argument parser."""
	filter_parser = argparse.ArgumentParser(add_help=False)
	genvm_tool.tests.stage.filter.add_args(filter_parser)

	parser = argparse.ArgumentParser(
		prog='genvm-tool test',
		description='A test runner utility',
		parents=[filter_parser],
	)

	subparsers = parser.add_subparsers()

	run_parser = subparsers.add_parser('run', parents=[filter_parser], help='run tests')
	run_parser.set_defaults(func=workflow_run)

	run_parser.add_argument(
		'--fail-fast',
		action='store_true',
		default=False,
		help='Stop execution after the first test failure',
	)

	run_parser.add_argument(
		'--ci',
		action='store_true',
		default=False,
		help='Emit full failure and service log context for CI diagnostics',
	)

	default_cpu_count = os.cpu_count() or 1

	run_parser.add_argument(
		'--junit-xml',
		type=str,
		default=None,
		metavar='FILE',
		help='Write JUnit XML report to FILE (default: <artifacts_dir>/junit.xml)',
	)

	run_parser.add_argument(
		'--max-concurrent',
		type=int,
		default=default_cpu_count,
		metavar='N',
		help=f'Maximum number of tests to run concurrently (default: number of CPUs = {default_cpu_count})',
	)

	# 'show' subcommand with nested subcommands
	show_parser = subparsers.add_parser(
		'show', parents=[], help='show information without running'
	)
	show_subparsers = show_parser.add_subparsers()

	show_plan_parser = show_subparsers.add_parser(
		'plan', parents=[filter_parser], help='show execution plan'
	)
	show_plan_parser.set_defaults(func=workflow_plan)

	show_test_parser = show_subparsers.add_parser(
		'test', parents=[filter_parser], help='show available tests'
	)
	show_test_parser.set_defaults(func=workflow_list)

	show_services_parser = show_subparsers.add_parser(
		'services', parents=[], help='show service dependencies'
	)
	show_services_parser.set_defaults(func=workflow_services)

	show_tags_parser = show_subparsers.add_parser(
		'tags', parents=[], help='show available tags'
	)
	show_tags_parser.set_defaults(func=workflow_tags)

	return _ParserResult(parser, run_parser, filter_parser)


def _show_execution_plan(
	shared_context: genvm_tool.tests.SharedContext,
	scheduling_env: genvm_tool.tests.stage.scheduling.Env,
) -> None:
	"""Display the execution plan."""
	from genvm_tool.tests.stage.scheduling import (
		AwaitAllCases,
		StartCases,
		StartService,
		StopService,
	)

	# Find batches that are immediately awaited (start followed by await with nothing in between)
	actions = scheduling_env.actions
	immediate_await_batches: set[int] = set()
	for i, action in enumerate(actions):
		if isinstance(action, StartCases):
			if i + 1 < len(actions) and isinstance(actions[i + 1], AwaitAllCases):
				if actions[i + 1].id == action.id:
					immediate_await_batches.add(action.id)

	def _case_info(case: genvm_tool.tests.test.Case) -> str | dict:
		info: dict[str, typing.Any] = {}
		if case.description.depends_on:
			info['depends_on'] = sorted(case.description.depends_on)
		if case.description.permits > 1 and not case.description.console_pool:
			# Otherwise a case holding a third of the pool looks like any other one
			info['permits'] = case.description.permits
		if not info:
			return case.description.name
		return {'name': case.description.name, **info}

	plan_items = []
	for action in actions:
		if isinstance(action, StartService):
			plan_items.append(f'start service: {action.service.name}')
		elif isinstance(action, StopService):
			plan_items.append(f'stop service: {action.service.name}')
		elif isinstance(action, StartCases):
			case_infos = [_case_info(c) for c in action.cases]
			if action.id in immediate_await_batches:
				# Immediately awaited - use simple "run:" format
				if len(case_infos) == 1:
					plan_items.append({'run': case_infos[0]})
				else:
					plan_items.append({f'run parallel ({len(case_infos)} tests):': case_infos})
			else:
				# Deferred await - use "batch N := start" format
				if len(case_infos) == 1:
					plan_items.append({f'batch {action.id} := start': case_infos[0]})
				else:
					plan_items.append(
						{
							f'batch {action.id} := start parallel ({len(case_infos)} tests):': case_infos
						}
					)
		elif isinstance(action, AwaitAllCases):
			# Skip await for immediately awaited batches (already shown as "run")
			if action.id not in immediate_await_batches:
				plan_items.append(f'await batch {action.id}')

	shared_context.printer.put(
		'execution plan',
		total_actions=len(scheduling_env.actions),
		plan=plan_items,
	)


async def workflow_plan(
	shared_context: genvm_tool.tests.SharedContext,
	conf_env: genvm_tool.tests.stage.configuration.Env,
) -> None:
	"""Show execution plan without running tests."""
	to_scheduling = pipe(
		wrap(CollectionStage()), pipe(wrap(FilterStage()), wrap(SchedulingStage()))
	)
	scheduling_env = await to_scheduling(shared_context, conf_env)
	_show_execution_plan(shared_context, scheduling_env)


def workflow_run(
	shared_context: genvm_tool.tests.SharedContext,
	conf_env: genvm_tool.tests.stage.configuration.Env,
) -> None:
	# Set up Ctrl+C handling for graceful shutdown
	interrupted = False

	def signal_handler(signum, frame):
		nonlocal interrupted
		if interrupted:
			# Second Ctrl+C, force exit
			shared_context.logger.warning('Received second interrupt, forcing exit')
			sys.exit(130)
		interrupted = True
		shared_context.logger.warning(
			'Received interrupt, stopping gracefully (press Ctrl+C again to force)'
		)
		shared_context.interrupt()

	original_handler = signal.signal(signal.SIGINT, signal_handler)

	try:
		asyncio.run(_workflow_run_inner(shared_context, conf_env))
	except KeyboardInterrupt:
		shared_context.logger.warning('Interrupted')
		sys.exit(130)
	finally:
		signal.signal(signal.SIGINT, original_handler)


def _write_junit_xml(
	path: Path,
	shared: genvm_tool.tests.SharedContext,
	exec_env: genvm_tool.tests.stage.execution.Env,
) -> None:
	testsuites = ET.Element('testsuites')
	testsuite = ET.SubElement(testsuites, 'testsuite', name='genvm-tool test')

	total_time = 0.0
	failures = 0
	for record in exec_env.results:
		total_time += record.elapsed_seconds
		attrs = {
			'name': record.name,
			'time': f'{record.elapsed_seconds:.3f}',
		}
		testcase = ET.SubElement(testsuite, 'testcase', **attrs)
		if not record.passed:
			failures += 1
			failure = ET.SubElement(testcase, 'failure', message='Test failed')
			if record.failure_message:
				failure.text = record.failure_message

	testsuite.set('tests', str(len(exec_env.results)))
	testsuite.set('failures', str(failures))
	testsuite.set('time', f'{total_time:.3f}')

	path.parent.mkdir(parents=True, exist_ok=True)
	tree = ET.ElementTree(testsuites)
	ET.indent(tree)
	tree.write(path, xml_declaration=True, encoding='unicode')


async def _workflow_run_inner(
	shared_context: genvm_tool.tests.SharedContext,
	conf_env: genvm_tool.tests.stage.configuration.Env,
) -> None:
	to_execution = pipe(
		wrap(CollectionStage()),
		pipe(wrap(FilterStage()), pipe(wrap(SchedulingStage()), wrap(ExecutionStage()))),
	)
	execution_env = await to_execution(shared_context, conf_env)

	junit_xml_path = conf_env.args.junit_xml
	if junit_xml_path is None:
		junit_xml_path = shared_context.artifacts_dir / 'junit.xml'
	else:
		junit_xml_path = Path(junit_xml_path)
	conf_env.post_run_steps.append(
		lambda shared, env: _write_junit_xml(junit_xml_path, shared, env)
	)

	success = await wrap(ReportStage())(shared_context, execution_env)

	# Run plugin-registered post-run steps
	for step in conf_env.post_run_steps:
		try:
			step(shared_context, execution_env)
		except Exception as e:
			shared_context.logger.error('Post-run step failed', error=str(e))

	if success:
		sys.exit(0)
	else:
		sys.exit(1)


async def workflow_list(
	shared_context: genvm_tool.tests.SharedContext,
	conf_env: genvm_tool.tests.stage.configuration.Env,
):
	to_filter = pipe(wrap(CollectionStage()), wrap(FilterStage()))
	collection_env = await to_filter(shared_context, conf_env)

	shared_context.printer.put(
		'util stats',
		collectors_count=len(conf_env.collectors),
	)

	cases_info = []
	for case in collection_env.cases:
		info: dict[str, typing.Any] = {
			'name': case.description.name,
			'tags': case.description.tags,
		}
		if case.description.depends_on:
			info['depends_on'] = sorted(case.description.depends_on)
		cases_info.append(info)

	shared_context.printer.put(
		'available test cases',
		total=len(collection_env.cases),
		cases=cases_info,
	)


async def workflow_services(
	shared_context: genvm_tool.tests.SharedContext,
	conf_env: genvm_tool.tests.stage.configuration.Env,
) -> None:
	"""Show service dependencies."""
	collection_env = await wrap(CollectionStage())(shared_context, conf_env)

	# Collect all services from cases
	all_services: set[genvm_tool.tests.stage.collection.Service] = set()
	for case in collection_env.cases:
		for svc in case.description.needed_services:
			all_services.add(svc)
			if svc.depends_on:
				for dep in svc.depends_on:
					all_services.add(dep)

	# Build service info list
	services_info = []
	for svc in sorted(all_services, key=lambda s: s.name):
		info: dict[str, typing.Any] = {'name': svc.name}
		if svc.depends_on:
			info['depends_on'] = [dep.name for dep in svc.depends_on]
		if svc.semaphores:
			info['semaphores'] = sorted(semaphore.name for semaphore in svc.semaphores)
		services_info.append(info)

	shared_context.printer.put(
		'services',
		total=len(all_services),
		services=services_info,
	)


async def workflow_tags(
	shared_context: genvm_tool.tests.SharedContext,
	conf_env: genvm_tool.tests.stage.configuration.Env,
) -> None:
	"""Show available tags."""
	collection_env = await wrap(CollectionStage())(shared_context, conf_env)

	# Collect all tags and count usage
	tag_counts: dict[str, int] = {}
	for case in collection_env.cases:
		for tag in case.description.tags:
			tag_counts[tag] = tag_counts.get(tag, 0) + 1

	# Build tags info sorted by name
	tags_info = [
		{'tag': tag, 'count': count} for tag, count in sorted(tag_counts.items())
	]

	shared_context.printer.put(
		'tags',
		total=len(tag_counts),
		tags=tags_info,
	)


def run(
	logger: formatter.Formatter,
	printer: formatter.Sink,
	argv: list[str] | None = None,
	*,
	root: Path,
	suite: typing.Callable,
) -> None:
	"""
	Run the test runner under genvm-tool's shared ``logger`` / ``printer``.

	``argv`` is everything after ``genvm-tool test`` — the runner's own
	``run`` / ``show ...`` subcommands plus suite-provided filter flags. The
	top-level ``-C`` / ``--log-format`` / ``--log-level`` belong to genvm-tool
	and are already applied by the time we get here.

	``root`` is the manager root (the genvm-tool ``Context.root``) and ``suite``
	is the manager's ``.genvm-tool.py:tests`` — the root suite-config entry.
	Suites are collected before the args are fully parsed, because they may
	register extra arguments on the parser.
	"""

	remaining_args = list(argv) if argv else []

	logger.trace('test root directory', root_dir=root)

	shared_context = genvm_tool.tests.SharedContext(
		root_dir=root,
		logger=logger,
		printer=printer,
		suite=suite,
	)

	common.add_extra_python_paths(shared_context.root_dir)

	parser_result = create_parser()

	initial_env = genvm_tool.tests.stage.configuration.InitialEnv(
		parser=parser_result.parser,
		run_parser=parser_result.run_parser,
		filter_parser=parser_result.filter_parser,
		remaining_args=remaining_args,
	)

	conf_step = wrap(genvm_tool.tests.stage.pipeline.ConfigurationStage())

	async def foo():
		return await conf_step(shared_context, initial_env)

	conf_env = asyncio.run(foo())

	# --ci is a `run`-subcommand flag; absent for other subcommands (default False).
	shared_context.ci = getattr(conf_env.args, 'ci', False)

	if 'func' not in conf_env.args:
		logger.error('subcommand not given')
		parser_result.parser.print_help()
		sys.exit(1)

	try:
		func = conf_env.args.func
		if asyncio.iscoroutinefunction(func):
			asyncio.run(func(shared_context, conf_env))
		else:
			func(shared_context, conf_env)
	finally:
		shared_context.logger.debug('metrics', **shared_context.metrics)
		shared_context.watchdog.stop()
