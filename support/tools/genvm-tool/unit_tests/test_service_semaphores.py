from argparse import Namespace

import genvm_tool.tests
from genvm_tool.tests.stage import scheduling
from genvm_tool.tests.stage.collection import Env, Semaphore, Service


class _Service(genvm_tool.tests.exec.service.Service):
	async def start(self):
		raise NotImplementedError


def _case(
	name: str,
	service: Service | None,
	*,
	depends_on: frozenset[str] = frozenset(),
	console_pool: bool = False,
):
	return genvm_tool.tests.test.StepsCase(
		description=genvm_tool.tests.test.Description(
			name=name,
			needed_services=frozenset() if service is None else frozenset({service}),
			depends_on=depends_on,
			console_pool=console_pool,
		),
		steps=[],
	)


def _service_action_index(actions, action_type, service):
	return next(
		index
		for index, action in enumerate(actions)
		if isinstance(action, action_type) and action.service is service
	)


def test_mutually_exclusive_services_do_not_overlap():
	semaphore = Semaphore('manager-listener')
	first = Service('first', _Service(), semaphores=frozenset({semaphore}), order=0)
	second = Service('second', _Service(), semaphores=frozenset({semaphore}), order=1)

	env = scheduling.run(
		None,
		Env(
			cases=[_case('first-case', first), _case('second-case', second)], args=Namespace()
		),
	)

	actions = env.actions
	stop_first = _service_action_index(actions, scheduling.StopService, first)
	start_second = _service_action_index(actions, scheduling.StartService, second)
	assert stop_first < start_second
	assert isinstance(actions[stop_first - 1], scheduling.AwaitAllCases)


def test_dependents_finish_before_conflicting_service_starts():
	semaphore = Semaphore('manager-listener')
	manager = Service('manager', _Service(), semaphores=frozenset({semaphore}), order=0)
	replacement = Service(
		'replacement', _Service(), semaphores=frozenset({semaphore}), order=1
	)
	webdriver = Service('webdriver', _Service(), order=2)
	modules = Service('modules', _Service(), depends_on=[manager, webdriver], order=3)

	env = scheduling.run(
		None,
		Env(
			cases=[_case('integration', modules), _case('replacement-case', replacement)],
			args=Namespace(),
		),
	)

	actions = env.actions
	start_modules = _service_action_index(actions, scheduling.StartService, modules)
	stop_modules = _service_action_index(actions, scheduling.StopService, modules)
	stop_manager = _service_action_index(actions, scheduling.StopService, manager)
	start_replacement = _service_action_index(
		actions, scheduling.StartService, replacement
	)
	stop_webdriver = _service_action_index(actions, scheduling.StopService, webdriver)
	assert start_modules < stop_manager
	assert stop_modules < stop_manager
	assert stop_manager < start_replacement
	assert start_replacement < stop_webdriver


def test_case_cannot_require_mutually_exclusive_services():
	semaphore = Semaphore('manager-listener')
	first = Service('first', _Service(), semaphores=frozenset({semaphore}), order=0)
	second = Service('second', _Service(), semaphores=frozenset({semaphore}), order=1)
	case = genvm_tool.tests.test.StepsCase(
		description=genvm_tool.tests.test.Description(
			name='impossible',
			needed_services=frozenset({first, second}),
		),
		steps=[],
	)

	try:
		scheduling.run(None, Env(cases=[case], args=Namespace()))
	except ValueError as exc:
		assert 'mutually exclusive services' in str(exc)
	else:
		raise AssertionError('mutually exclusive service requirement was accepted')


def test_test_dependency_crosses_semaphore_transition():
	semaphore = Semaphore('manager-listener')
	first = Service('first', _Service(), semaphores=frozenset({semaphore}), order=0)
	second = Service('second', _Service(), semaphores=frozenset({semaphore}), order=1)
	first_case = _case('first-case', first)
	second_case = _case(
		'second-case',
		second,
		depends_on=frozenset({'first-case'}),
	)

	env = scheduling.run(
		None,
		Env(cases=[first_case, second_case], args=Namespace()),
	)
	actions = env.actions
	stop_first = _service_action_index(actions, scheduling.StopService, first)
	start_second = _service_action_index(actions, scheduling.StartService, second)
	assert isinstance(actions[stop_first - 1], scheduling.AwaitAllCases)
	assert stop_first < start_second


def test_test_dependency_can_reverse_service_registration_order():
	semaphore = Semaphore('manager-listener')
	dependent_service = Service(
		'dependent',
		_Service(),
		semaphores=frozenset({semaphore}),
		order=0,
	)
	prerequisite_service = Service(
		'prerequisite',
		_Service(),
		semaphores=frozenset({semaphore}),
		order=1,
	)
	prerequisite = _case('prerequisite-case', prerequisite_service)
	dependent = _case(
		'dependent-case',
		dependent_service,
		depends_on=frozenset({'prerequisite-case'}),
	)

	env = scheduling.run(
		None,
		Env(cases=[dependent, prerequisite], args=Namespace()),
	)
	actions = env.actions
	stop_prerequisite = _service_action_index(
		actions,
		scheduling.StopService,
		prerequisite_service,
	)
	start_dependent = _service_action_index(
		actions,
		scheduling.StartService,
		dependent_service,
	)
	assert stop_prerequisite < start_dependent


def test_console_dependency_is_started_before_scheduler_waits():
	service = Service('service', _Service(), order=0)
	prerequisite = _case('prerequisite', service, console_pool=True)
	dependent = _case(
		'dependent',
		service,
		depends_on=frozenset({'prerequisite'}),
		console_pool=True,
	)

	actions = scheduling.run(
		None,
		Env(cases=[dependent, prerequisite], args=Namespace()),
	).actions
	# Waiting for a case whose dependency has not started yet never ends
	batches: dict[int, list] = {}
	started: set[str] = set()
	for action in actions:
		if isinstance(action, scheduling.StartCases):
			batches[action.id] = action.cases
			started.update(case.description.name for case in action.cases)
		elif isinstance(action, scheduling.AwaitAllCases):
			for case in batches[action.id]:
				assert case.description.depends_on.issubset(started)
	assert started == {'prerequisite', 'dependent'}


def test_console_case_waits_until_later_service_dependency_is_scheduled():
	console_service = Service('console-service', _Service(), order=0)
	prerequisite_service = Service('prerequisite-service', _Service(), order=1)
	prerequisite = _case('prerequisite', prerequisite_service)
	dependent = _case(
		'dependent',
		console_service,
		depends_on=frozenset({'prerequisite'}),
		console_pool=True,
	)

	actions = scheduling.run(
		None,
		Env(cases=[dependent, prerequisite], args=Namespace()),
	).actions
	start_prerequisite_service = _service_action_index(
		actions,
		scheduling.StartService,
		prerequisite_service,
	)
	start_dependent = next(
		index
		for index, action in enumerate(actions)
		if isinstance(action, scheduling.StartCases) and action.cases == [dependent]
	)
	assert start_prerequisite_service < start_dependent


def test_no_service_console_case_waits_for_service_dependency():
	service = Service('service', _Service(), order=0)
	prerequisite = _case('prerequisite', service)
	dependent = _case(
		'dependent',
		None,
		depends_on=frozenset({'prerequisite'}),
		console_pool=True,
	)

	actions = scheduling.run(
		None,
		Env(cases=[dependent, prerequisite], args=Namespace()),
	).actions
	start_service = _service_action_index(actions, scheduling.StartService, service)
	start_dependent = next(
		index
		for index, action in enumerate(actions)
		if isinstance(action, scheduling.StartCases) and action.cases == [dependent]
	)
	assert start_service < start_dependent


def test_case_cannot_depend_on_a_case_of_a_later_service():
	# `replacement` displaces `first`, so `early-case` is over before `last-case`
	# can even start; waiting for it would hang the runner
	semaphore = Semaphore('manager-listener')
	first = Service('first', _Service(), semaphores=frozenset({semaphore}), order=0)
	replacement = Service(
		'replacement', _Service(), semaphores=frozenset({semaphore}), order=1
	)
	unrelated = Service('unrelated', _Service(), order=2)
	early = _case(
		'early-case',
		first,
		depends_on=frozenset({'last-case'}),
	)

	try:
		scheduling.run(
			None,
			Env(
				cases=[
					early,
					_case('replacement-case', replacement),
					_case('last-case', unrelated),
				],
				args=Namespace(),
			),
		)
	except ValueError as exc:
		assert 'could not be scheduled' in str(exc)
	else:
		raise AssertionError('a case waiting for a case that runs later was accepted')


def test_semaphore_handover_order_is_chosen_across_semaphores():
	# `both` conflicts with either other service, so it has to run on its own;
	# ordering the owners of one semaphore at a time claims that is impossible
	left = Semaphore('left')
	right = Semaphore('right')
	first = Service('first', _Service(), semaphores=frozenset({right}), order=0)
	both = Service('both', _Service(), semaphores=frozenset({left, right}), order=1)
	dependent = Service(
		'dependent',
		_Service(),
		semaphores=frozenset({left}),
		depends_on=[first],
		order=2,
	)

	actions = scheduling.run(
		None,
		Env(
			cases=[
				_case('first-case', first),
				_case('both-case', both),
				_case('dependent-case', dependent),
			],
			args=Namespace(),
		),
	).actions
	start_first = _service_action_index(actions, scheduling.StartService, first)
	start_dependent = _service_action_index(actions, scheduling.StartService, dependent)
	stop_dependent = _service_action_index(actions, scheduling.StopService, dependent)
	start_both = _service_action_index(actions, scheduling.StartService, both)
	assert start_first < start_dependent
	assert stop_dependent < start_both


def test_crossed_semaphore_dependencies_are_orderable():
	# Each semaphore's owners can be handed over in one order only, and the two
	# orders are opposite: the second `left` owner depends on the second `right`
	# one and vice versa
	left = Semaphore('left')
	right = Semaphore('right')
	first_left = Service('first-left', _Service(), semaphores=frozenset({left}), order=0)
	first_right = Service(
		'first-right', _Service(), semaphores=frozenset({right}), order=1
	)
	second_left = Service(
		'second-left',
		_Service(),
		semaphores=frozenset({left}),
		depends_on=[first_right],
		order=2,
	)
	second_right = Service(
		'second-right',
		_Service(),
		semaphores=frozenset({right}),
		depends_on=[first_left],
		order=3,
	)

	services = [first_left, first_right, second_left, second_right]
	actions = scheduling.run(
		None,
		Env(
			cases=[_case(f'{service.name}-case', service) for service in services],
			args=Namespace(),
		),
	).actions
	started = [
		action.service for action in actions if isinstance(action, scheduling.StartService)
	]
	assert set(started) == set(services)
	for service in services:
		start = _service_action_index(actions, scheduling.StartService, service)
		for dependency in service.depends_on or []:
			stop = _service_action_index(actions, scheduling.StopService, dependency)
			assert start < stop
