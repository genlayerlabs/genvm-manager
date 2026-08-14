import asyncio
import json
import os
import typing
from collections.abc import Mapping
from pathlib import Path

import aiohttp
import genvm_tool.io as gvm_io
import genvm_tool.tests
import genvm_tool.tests.exec.process
import genvm_tool.tests.exec.service
import genvm_tool_plugins.docker as docker

local_ctx = genvm_tool.tests.stage.configuration.current_context()


def get_webdriver_port(env: genvm_tool.tests.stage.configuration.Env) -> int:
	return 4444


def get_manager_port(env: genvm_tool.tests.stage.configuration.Env) -> int:
	return 3999


async def start_webdriver_service(
	env: genvm_tool.tests.stage.configuration.Env,
	*,
	ci: bool = False,
) -> genvm_tool.tests.exec.service.Handle:
	context_dir = local_ctx.shared.root_dir.joinpath('webdriver')

	build_data = {}
	image_sha = await docker.build(context_dir=context_dir, log_context=build_data)

	port = get_webdriver_port(env)

	proc_env = docker.DEFAULT_ENV.copy()
	for k, v in os.environ.items():
		if k.lower().endswith('key') or k.lower().endswith('token'):
			proc_env[k] = v

	return await docker.ContainerHandle.run(
		['-p', f'{port}:4444', image_sha],
		cwd=local_ctx.shared.root_dir,
		env=proc_env,
		ci=ci,
	)


_LOG_LEVEL_PRIORITY = {
	'trace': 0,
	'debug': 10,
	'info': 20,
	'warn': 30,
	'warning': 30,
	'error': 40,
}


async def _read_log_pipe(
	stream: asyncio.StreamReader,
	log_file: typing.IO[str],
	logger: genvm_tool.tests.Formatter,
	*,
	ci: bool,
) -> None:
	while True:
		line_bytes = await stream.readline()
		if not line_bytes:
			break
		line = line_bytes.decode('utf-8', errors='replace').rstrip('\n')
		log_file.write(line + '\n')
		log_file.flush()
		try:
			parsed = json.loads(line)
			level = parsed.get('level', '')
			min_level = 'info' if ci else 'warning'
			if _LOG_LEVEL_PRIORITY.get(level, -1) >= _LOG_LEVEL_PRIORITY[min_level]:
				message = parsed.get('message', line)
				extra = {k: v for k, v in parsed.items() if k not in ('level', 'message')}
				fmt_level = genvm_tool.tests.Formatter.Level.from_str(level)
				logger.log(fmt_level, f'[manager] {message}', **extra)
		except (json.JSONDecodeError, ValueError):
			if ci:
				logger.warning(f'[manager] {line}')


class ManagerHandle(genvm_tool.tests.exec.service.Handle):
	def __init__(
		self,
		port: int | None,
		process: asyncio.subprocess.Process | None,
		log_file: gvm_io.AsyncFile[str] | None,
		log_tasks: list[asyncio.Task] | None,
		socket_path: Path | None = None,
		restart: typing.Callable[[], typing.Awaitable['ManagerHandle']] | None = None,
	):
		self._port = port
		self._socket_path = socket_path
		self._process = process
		self._log_file = log_file
		self._log_tasks = log_tasks
		self._restart = restart

	@property
	def port(self) -> int | None:
		return self._port

	@property
	def uri(self) -> str:
		if self._socket_path is not None:
			return f'unix://{self._socket_path}'
		return f'http://localhost:{self._port}'

	@property
	def socket_path(self) -> Path | None:
		return self._socket_path

	@property
	def process(self) -> asyncio.subprocess.Process | None:
		return self._process

	async def healthy(self) -> bool:
		try:
			connector = (
				aiohttp.UnixConnector(path=str(self._socket_path))
				if self._socket_path is not None
				else None
			)
			async with aiohttp.ClientSession(connector=connector) as session:
				async with session.get(
					'http://localhost/status'
					if self._socket_path is not None
					else f'{self.uri}/status',
					timeout=aiohttp.ClientTimeout(total=5),
				) as response:
					return response.status == 200
		except Exception:
			return False

	async def death_reason(self) -> str | None:
		if self._process is None or self._process.returncode is None:
			return None
		return f'manager exited with code {self._process.returncode}'

	async def interrupt(self) -> None:
		if self._process is not None:
			await genvm_tool.tests.exec.process.stop(self._process)
		if self._log_tasks is not None:
			await genvm_tool.tests.exec.process.drain(self._log_tasks)
		if self._log_file is not None:
			await self._log_file.close()

	async def restart(self) -> None:
		if self._restart is None:
			raise RuntimeError('manager service is not restartable')
		await self.interrupt()
		replacement = await self._restart()
		self._port = replacement._port
		self._socket_path = replacement._socket_path
		self._process = replacement._process
		self._log_file = replacement._log_file
		self._log_tasks = replacement._log_tasks
		await self.await_startup()


class ManagerService(genvm_tool.tests.exec.service.Service):
	def __init__(
		self,
		*,
		env: genvm_tool.tests.stage.configuration.Env,
		bin_path: Path,
		log_path: Path | None = None,
		ci: bool = False,
		port: int | None = None,
		config: Mapping[str, typing.Any] | None = None,
		socket_path: Path | None = None,
	):
		self._bin_path = bin_path
		self._log_path = log_path
		self._env = env
		self._ci = ci
		self._port = port
		self._config = dict(config or {})
		self._socket_path = socket_path
		self._has_started = False

	async def start(self) -> ManagerHandle:
		handle = await self._start_once()
		handle._restart = self._start_once
		return handle

	async def _start_once(self) -> ManagerHandle:
		log_file = None
		log_tasks = None
		use_pipe = self._log_path is not None

		if use_pipe and self._log_path:
			self._log_path.parent.mkdir(parents=True, exist_ok=True)
			log_file = await gvm_io.open_file(
				self._log_path,
				'a' if self._has_started else 'w',
			)
		self._has_started = True

		port = self._port if self._port is not None else get_manager_port(self._env)
		config_dir = self._log_path.parent if self._log_path is not None else Path('/tmp')
		config_dir.mkdir(parents=True, exist_ok=True)
		config_path = config_dir / 'genvm-manager.yaml'
		manifest_path = self._bin_path.parent.parent / 'data' / 'manifest.yaml'
		config = {
			'threads': 4,
			'blocking_threads': 48,
			'log_disable': 'tracing*,polling*,tungstenite*,tokio_tungstenite*',
			'log_level': 'info',
			'manifest_path': str(manifest_path),
			'permits': None,
			'execution_retention': '5m',
			'max_message_bytes': 67108864,
		}
		config.update(self._config)
		config_path.write_text(
			'\n'.join(f'{key}: {json.dumps(value)}' for key, value in config.items()) + '\n'
		)

		listener_args = (
			['--socket', str(self._socket_path)]
			if self._socket_path is not None
			else ['--port', str(port)]
		)
		process = await asyncio.subprocess.create_subprocess_exec(
			str(self._bin_path),
			'manager',
			*listener_args,
			'--die-with-parent',
			'--config',
			str(config_path),
			stdin=asyncio.subprocess.DEVNULL,
			stdout=asyncio.subprocess.PIPE if use_pipe else asyncio.subprocess.DEVNULL,
			stderr=asyncio.subprocess.PIPE if use_pipe else asyncio.subprocess.DEVNULL,
			start_new_session=True,
		)

		if use_pipe:
			logger = local_ctx.shared.logger
			log_tasks = [
				asyncio.create_task(
					_read_log_pipe(process.stdout, log_file.raw, logger, ci=self._ci)
				),
				asyncio.create_task(
					_read_log_pipe(process.stderr, log_file.raw, logger, ci=self._ci)
				),
			]

		handle = ManagerHandle(
			None if self._socket_path is not None else port,
			process,
			log_file,
			log_tasks,
			socket_path=self._socket_path,
		)

		return handle


class ModulesHandle(genvm_tool.tests.exec.service.Handle):
	"""Handle for started modules - no cleanup needed as modules stop with manager."""

	def __init__(self, manager_uri: str):
		self._manager_uri = manager_uri

	async def healthy(self) -> bool:
		return True  # Modules are healthy if they started successfully

	async def interrupt(self) -> None:
		pass  # Modules stop when manager stops


class ModulesService(genvm_tool.tests.exec.service.Service):
	"""
	Service that starts LLM and Web modules on the manager.

	This service depends on ManagerService being started first.
	"""

	def __init__(self, *, manager_uri: str):
		self._manager_uri = manager_uri

	async def start(self) -> ModulesHandle:
		async def start_module(name: str) -> None:
			async with aiohttp.request(
				'POST',
				f'{self._manager_uri}/module/start',
				json={'module_type': name, 'config': None},
			) as resp:
				body = await resp.json()
				if resp.status != 200 and body != {'error': 'module_already_running'}:
					raise RuntimeError(f'Starting module {name} failed: {resp.status} {body}')

		# Start both modules
		await asyncio.gather(
			start_module('Llm'),
			start_module('Web'),
		)

		return ModulesHandle(self._manager_uri)


class _NoOpHandle(genvm_tool.tests.exec.service.Handle):
	async def healthy(self) -> bool:
		return True

	async def interrupt(self) -> None:
		pass


class NoOpService(genvm_tool.tests.exec.service.Service):
	async def start(self) -> _NoOpHandle:
		return _NoOpHandle()


class ExternalManagerService(genvm_tool.tests.exec.service.Service):
	def __init__(self, *, port: int):
		self._port = port

	async def start(self) -> ManagerHandle:
		return ManagerHandle(
			self._port,
			process=None,
			log_file=None,
			log_tasks=None,
		)
