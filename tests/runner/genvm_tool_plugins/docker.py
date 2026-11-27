import asyncio
import copy
import functools
import os
import re
import subprocess
import sys
from pathlib import Path

import genvm_tool.tests
import genvm_tool.tests.exec.service

local_ctx = genvm_tool.tests.stage.configuration.current_context()


class DockerBuildError(Exception):
	pass


class DockerRunError(Exception):
	pass


DEFAULT_ENV = {
	k: v
	for k, v in os.environ.items()
	if genvm_tool.tests.util.environ.DEFAULT_FILTER(k, v)
}
DEFAULT_ENV.pop('LD_LIBRARY_PATH', None)


async def build(
	*,
	context_dir: Path,
	dockerfile: Path | None = None,
	tag: str | None = None,
	build_args: dict[str, str] | None = None,
	log_context: dict | None = None,
	env: dict[str, str] | None = None,
) -> str:
	"""
	Build a Docker image and return its sha256 digest.

	Args:
		context_dir: The directory to use as the build context
		dockerfile: Optional path to the Dockerfile
		tag: Optional tag for the built image
		build_args: Optional build arguments to pass to docker build

	Returns:
		The sha256 digest of the built image (e.g., "sha256:abc123...")

	Raises:
		DockerBuildError: If the build fails or sha256 cannot be parsed
	"""
	args: list[str] = [
		'docker',
		'build',
		'--progress=plain',
	]

	if dockerfile is not None:
		args.extend(['-f', str(dockerfile)])

	if tag is not None:
		args.extend(['-t', tag])

	if build_args:
		for key, value in build_args.items():
			args.extend(['--build-arg', f'{key}={value}'])

	args.append(str(context_dir))

	if env is None:
		env = copy.deepcopy(DEFAULT_ENV)

	cmd = genvm_tool.tests.exec.command.Command(args=args, cwd=context_dir, env=env)

	res = await cmd.run(
		ctx=local_ctx.shared, mode=genvm_tool.tests.exec.command.RunMode.SILENT
	)

	combined_output = res.stdout + res.stderr

	if log_context is not None:
		log_context['docker_build_stdout'] = res.stdout
		log_context['docker_build_stderr'] = res.stderr

	if res.exit_code != 0:
		local_ctx.shared.logger.error(
			'Docker build failed',
			context_dir=str(context_dir),
			args=args,
			env=env,
			exit_code=res.exit_code,
			combined_output=combined_output,
		)
		raise DockerBuildError(f'Docker build failed with exit code {res.exit_code}')

	sha256_matches = re.findall(
		r'sha256:([a-f0-9]{64})', combined_output, flags=re.IGNORECASE
	)

	if log_context is not None:
		log_context['docker_build_sha256_matches'] = sha256_matches

	if not sha256_matches:
		print(combined_output, file=sys.stderr)
		raise DockerBuildError('Could not find sha256 in docker build output')

	return f'sha256:{sha256_matches[-1]}'


def _stop_cleanup(container_id: str) -> functools.partial:
	"""Build the picklable cleanup the watchdog runs to tear down a container."""
	return functools.partial(
		subprocess.run,
		['docker', 'stop', container_id],
		timeout=30,
		capture_output=True,
	)


class ContainerHandle(genvm_tool.tests.exec.service.Handle):
	# The webdriver image asks for a 10s start-period and probes every 30s, so a
	# first verdict lands well after half a minute and each retry costs another
	# interval. Room for several of them, not for all ten the image allows.
	STARTUP_TIMEOUT = 180.0
	# Outlasts the teardown chain: the watchdog's ack budget, and behind it a
	# `docker stop` capped at 30s.
	INTERRUPT_TIMEOUT = 120.0

	def __init__(self, container_id: str, *, ci: bool):
		self._container_id = container_id
		self._ci = ci
		self._last_inspect: 'genvm_tool.tests.exec.command.Result | None' = None
		self._watchdog_token = local_ctx.shared.watchdog.register(
			_stop_cleanup(container_id)
		)

	async def get_logs(self, tail: int = 100) -> str:
		"""Get recent logs from the container."""
		cmd = genvm_tool.tests.exec.command.Command(
			args=['docker', 'logs', '--tail', str(tail), self._container_id],
			cwd=local_ctx.shared.root_dir,
			env=DEFAULT_ENV,
		)
		res = await cmd.run(
			ctx=local_ctx.shared, mode=genvm_tool.tests.exec.command.RunMode.SILENT
		)
		return res.stdout + res.stderr

	@staticmethod
	async def run(
		args: list[str | Path],
		*,
		cwd: Path,
		env: dict[str, str] | None = None,
		log_context: dict | None = None,
		ci: bool = False,
	) -> 'ContainerHandle':
		if env is None:
			env = copy.deepcopy(DEFAULT_ENV)
		cmd = genvm_tool.tests.exec.command.Command(
			args=[
				'docker',
				'run',
				'--rm',
				'-d',
				*args,
			],
			cwd=cwd,
			env=env,
		)

		res = await cmd.run(
			ctx=local_ctx.shared, mode=genvm_tool.tests.exec.command.RunMode.SILENT
		)

		combined_output = res.stdout + res.stderr

		if log_context is not None:
			log_context['docker_run_stdout'] = res.stdout
			log_context['docker_run_stderr'] = res.stderr

		if res.exit_code != 0:
			local_ctx.shared.logger.error(
				'Docker run failed',
				cwd=str(cwd),
				args=args,
				env=env,
				exit_code=res.exit_code,
				combined_output=combined_output,
			)
			raise DockerRunError(f'Docker run failed with exit code {res.exit_code}')
		container_id = res.stdout.strip()
		return ContainerHandle(container_id=container_id, ci=ci)

	async def _inspect(self) -> 'genvm_tool.tests.exec.command.Result':
		"""
		State and health of the container in one call, cached for one probe.

		:meth:`healthy` runs immediately before :meth:`death_reason` on every
		poll, so the second reads what the first fetched rather than forking a
		docker of its own.
		"""
		cmd = genvm_tool.tests.exec.command.Command(
			args=[
				'docker',
				'inspect',
				'--format',
				'{{.State.Status}}|'
				'{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}',
				self._container_id,
			],
			cwd=local_ctx.shared.root_dir,
			env=DEFAULT_ENV,
		)
		self._last_inspect = await cmd.run(
			ctx=local_ctx.shared, mode=genvm_tool.tests.exec.command.RunMode.SILENT
		)
		return self._last_inspect

	@staticmethod
	def _split_inspect(res: 'genvm_tool.tests.exec.command.Result') -> tuple[str, str]:
		state, _, health = res.stdout.strip().partition('|')
		return state, health

	async def healthy(self) -> bool:
		try:
			res = await self._inspect()
			if res.exit_code != 0:
				if self._ci:
					logs = await self.get_logs()
					local_ctx.shared.logger.warning(
						'Docker health check failed',
						container_id=self._container_id,
						returncode=res.exit_code,
						logs=logs,
					)
				return False
			_, health = self._split_inspect(res)
			if health != 'healthy':
				if self._ci:
					logs = await self.get_logs()
					local_ctx.shared.logger.warning(
						'Container not healthy',
						container_id=self._container_id,
						status=health,
						logs=logs,
					)
			return health == 'healthy'
		except Exception as e:
			local_ctx.shared.logger.error(
				'Failed to check container health',
				container_id=self._container_id,
				error=e,
			)
			return False

	async def death_reason(self) -> str | None:
		res, self._last_inspect = self._last_inspect, None
		try:
			if res is None:
				res = await self._inspect()
				# a probe of our own is not a probe the next call may reuse
				self._last_inspect = None
		except Exception:
			return None  # healthy() reports the probe itself failing
		if res.exit_code != 0:
			# `docker run --rm` drops the container the moment it exits; any
			# other inspect failure is the daemon being flaky, so keep waiting
			if 'no such' in (res.stdout + res.stderr).lower():
				return f'container {self._container_id} is gone'
			return None
		state, health = self._split_inspect(res)
		if state in ('exited', 'dead'):
			return f'container {state}, logs: {await self.get_logs()}'
		if health == 'unhealthy':
			return f'container unhealthy, logs: {await self.get_logs()}'
		return None

	async def interrupt(self) -> None:
		# Tear the container down through the watchdog so creation and teardown
		# always go through the same owner; blocks until the stop completes.
		await asyncio.to_thread(local_ctx.shared.watchdog.kill, self._watchdog_token)
