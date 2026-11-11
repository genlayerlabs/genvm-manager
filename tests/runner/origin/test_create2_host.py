import asyncio
import contextlib
import socket
from unittest.mock import AsyncMock, Mock

import pytest

from . import base_host, host_fns
from .calldata import Address


# ethers 6.15.0: id('predictGhostAddress(uint256,address)').slice(0, 10)
SELECTOR = bytes.fromhex('d4fd3a27')
SENDER = Address('0x1111111111111111111111111111111111111111')
CREATE2 = base_host.DEFAULT_CREATE2


class Host(base_host.IHost):
	loop_enter = AsyncMock()
	storage_read = AsyncMock()
	consume_time_fee_gen_wei = AsyncMock()
	get_balance_gen_wei = AsyncMock()
	get_remaining_time_fee_gen_wei = AsyncMock()
	notify_nondet_disagreement = AsyncMock()

	def __init__(self):
		self.calls = []

	async def external_call(self, address, calldata, /):
		self.calls.append((address, calldata))
		return b'fallback'


def predict_calldata(salt_nonce=7, sender=SENDER):
	return SELECTOR + salt_nonce.to_bytes(32, 'big') + bytes(12) + sender.as_bytes


async def external_call(host, address, calldata, *, create2=CREATE2):
	server, client = socket.socketpair()
	server.setblocking(False)
	client.setblocking(False)
	loop = asyncio.get_running_loop()
	kwargs = {} if create2 is None else {'create2': create2}
	task = asyncio.create_task(base_host.host_loop_on(host, server, ctx=Mock(), **kwargs))

	async def read_exact(size):
		data = bytearray()
		while len(data) < size:
			chunk = await loop.sock_recv(client, size - len(data))
			assert chunk
			data.extend(chunk)
		return bytes(data)

	try:
		async with asyncio.timeout(2):
			await loop.sock_sendall(
				client,
				bytes([host_fns.Methods.EXTERNAL_CALL])
				+ address.as_bytes
				+ len(calldata).to_bytes(4, 'little')
				+ calldata,
			)
			status = await read_exact(1)
			if status != bytes([host_fns.Errors.OK]):
				return status, None
			size = int.from_bytes(await read_exact(4), 'little')
			return status, await read_exact(size)
	finally:
		client.close()
		task.cancel()
		try:
			with contextlib.suppress(asyncio.CancelledError):
				await task
		finally:
			server.close()


def test_default_hook_predicts_ghost_address():
	host = Host()
	status, result = asyncio.run(
		external_call(host, CREATE2['deployer'], predict_calldata())
	)
	assert status == bytes([host_fns.Errors.OK])
	assert result == bytes(12) + bytes.fromhex('f2920708d104d2bbff4382b8cefe9ab8a207caa2')
	assert host.calls == []


@pytest.mark.parametrize('salt_nonce', [0, 1, 2**256 - 1])
def test_hook_receives_decoded_arguments(salt_nonce):
	host = Host()
	host.predict_ghost_address_hook = AsyncMock(return_value=SENDER)
	status, result = asyncio.run(
		external_call(host, CREATE2['deployer'], predict_calldata(salt_nonce))
	)
	assert status == bytes([host_fns.Errors.OK])
	assert result == bytes(12) + SENDER.as_bytes
	host.predict_ghost_address_hook.assert_awaited_once_with(CREATE2, salt_nonce, SENDER)
	assert host.calls == []


@pytest.mark.parametrize(
	'address, calldata',
	[
		(SENDER, predict_calldata()),
		(CREATE2['deployer'], b''),
		(CREATE2['deployer'], b'\x00\x00\x00\x00' + predict_calldata()[4:]),
		(CREATE2['deployer'], SELECTOR),
		(CREATE2['deployer'], predict_calldata()[:-1]),
		(CREATE2['deployer'], predict_calldata()[:36] + b'\x01' + predict_calldata()[37:]),
	],
)
def test_nonmatching_or_malformed_call_falls_back(address, calldata):
	host = Host()
	host.predict_ghost_address_hook = AsyncMock(return_value=SENDER)
	assert asyncio.run(external_call(host, address, calldata)) == (
		bytes([host_fns.Errors.OK]),
		b'fallback',
	)
	host.predict_ghost_address_hook.assert_not_called()
	assert host.calls == [(address.as_bytes, calldata)]


@pytest.mark.parametrize('hook', [None, AsyncMock(return_value=NotImplemented)])
def test_disabled_or_unimplemented_hook_falls_back(hook):
	host = Host()
	host.predict_ghost_address_hook = hook
	calldata = predict_calldata()
	assert asyncio.run(external_call(host, CREATE2['deployer'], calldata)) == (
		bytes([host_fns.Errors.OK]),
		b'fallback',
	)
	assert host.calls == [(CREATE2['deployer'].as_bytes, calldata)]


def test_handler_without_hook_falls_back():
	host = Mock(spec=['external_call'])
	host.external_call = AsyncMock(return_value=b'fallback')
	calldata = predict_calldata()
	assert asyncio.run(external_call(host, CREATE2['deployer'], calldata)) == (
		bytes([host_fns.Errors.OK]),
		b'fallback',
	)
	host.external_call.assert_awaited_once_with(CREATE2['deployer'].as_bytes, calldata)


@pytest.mark.parametrize('code_hash', [CREATE2['code_hash'], bytes(32)])
def test_default_hook_returns_zero_for_unsalted_prediction(code_hash):
	host = Host()
	assert asyncio.run(
		external_call(
			host,
			CREATE2['deployer'],
			predict_calldata(0),
			create2={**CREATE2, 'code_hash': code_hash},
		)
	) == (
		bytes([host_fns.Errors.OK]),
		bytes(32),
	)
	assert host.calls == []


def test_default_hook_accepts_trailing_calldata_like_solidity():
	host = Host()
	assert asyncio.run(
		external_call(host, CREATE2['deployer'], predict_calldata() + b'extra')
	) == (
		bytes([host_fns.Errors.OK]),
		bytes(12) + bytes.fromhex('f2920708d104d2bbff4382b8cefe9ab8a207caa2'),
	)
	assert host.calls == []


def test_hook_host_exception_uses_normal_error_response():
	host = Host()
	error = next(code for code in host_fns.Errors if code != host_fns.Errors.OK)
	host.predict_ghost_address_hook = AsyncMock(
		side_effect=base_host.HostException(error)
	)
	assert asyncio.run(external_call(host, CREATE2['deployer'], predict_calldata())) == (
		bytes([error]),
		None,
	)
	assert host.calls == []


@pytest.mark.parametrize('deployer', [None, SENDER, SENDER.as_hex, SENDER.as_bytes])
def test_run_normalizes_create2_without_mutating_handler(monkeypatch, deployer):
	host = Host()
	initial_state = host.__dict__.copy()
	create2 = (
		{'deployer': deployer, 'code_hash': bytearray([42]) * 32}
		if deployer is not None
		else base_host.DEFAULT_CREATE2
	)
	expected = {
		'deployer': Address(create2['deployer']),
		'code_hash': bytes(create2['code_hash']),
	}
	client = Mock(run=AsyncMock(side_effect=RuntimeError('stop before execution')))
	host_loop = AsyncMock()
	monkeypatch.setattr(base_host, 'host_loop', host_loop)
	with pytest.raises(base_host.ManagerRunNotStarted, match='stop before execution'):
		asyncio.run(
			base_host.run_genvm(
				host,
				manager_client=client,
				ctx=Mock(),
				is_sync=False,
				message={'is_init': True},
				host='unused',
				bucket_totals={},
				calldata=b'',
				request_extra={'create2': create2} if deployer is not None else {},
			)
		)
	request_pair = client.run.call_args.args[0]['create2']
	assert request_pair == expected
	assert isinstance(request_pair['deployer'], Address)
	assert isinstance(request_pair['code_hash'], bytes)
	assert host_loop.call_args.kwargs['create2'] is request_pair
	assert host.__dict__ == initial_state


# ethers 6.15.0: getCreate2Address(factory, solidityPackedKeccak256(['address', 'uint256'], [sender, nonce]), hash)
@pytest.mark.parametrize(
	'salt_nonce, expected',
	[
		(1, '271441ccd7bba256d365975d06825c14e38029b0'),
		(7, 'f2920708d104d2bbff4382b8cefe9ab8a207caa2'),
		(2**256 - 1, '35d3dd0b90ce1586bb44318f4908c4147adc04da'),
	],
)
def test_default_hook_matches_node_preset_vectors(salt_nonce, expected):
	host = Host()
	assert asyncio.run(
		external_call(host, CREATE2['deployer'], predict_calldata(salt_nonce))
	) == (bytes([host_fns.Errors.OK]), bytes(12) + bytes.fromhex(expected))
	assert host.calls == []


@pytest.mark.parametrize('salt_nonce', [1, 7, 2**256 - 1])
def test_default_hook_reverts_for_zero_code_hash(salt_nonce):
	host = Host()
	assert asyncio.run(
		external_call(
			host,
			CREATE2['deployer'],
			predict_calldata(salt_nonce),
			create2={**CREATE2, 'code_hash': bytes(32)},
		)
	) == (bytes([host_fns.Errors.EVM_REVERTED]), None)
	assert host.calls == []


def test_async_hook_is_awaited():
	host = Host()
	host.predict_ghost_address_hook = AsyncMock(return_value=SENDER)
	assert asyncio.run(external_call(host, CREATE2['deployer'], predict_calldata())) == (
		bytes([host_fns.Errors.OK]),
		bytes(12) + SENDER.as_bytes,
	)
	host.predict_ghost_address_hook.assert_awaited_once_with(CREATE2, 7, SENDER)


@pytest.mark.parametrize('invalid', [None, SENDER.as_hex, SENDER.as_bytes])
def test_invalid_hook_result_fails_clearly(invalid):
	host = Host()
	host.predict_ghost_address_hook = AsyncMock(return_value=invalid)
	with pytest.raises(
		TypeError, match='predict_ghost_address_hook must return Address or NotImplemented'
	):
		asyncio.run(external_call(host, CREATE2['deployer'], predict_calldata()))


def test_nested_connections_inherit_their_run_create2_pair():
	async def exercise(create2):
		host = Host()
		host.predict_ghost_address_hook = AsyncMock(return_value=SENDER)
		nested = None
		server, client = socket.socketpair()
		server.setblocking(False)
		client.close()

		async def enter(cancellation):
			nonlocal nested
			nested = asyncio.create_task(
				external_call(host, create2['deployer'], predict_calldata(), create2=None)
			)
			await asyncio.sleep(0)
			return server

		host.loop_enter = enter
		try:
			await base_host.host_loop(host, asyncio.Event(), ctx=Mock(), create2=create2)
			assert await nested == (bytes([host_fns.Errors.OK]), bytes(12) + SENDER.as_bytes)
			host.predict_ghost_address_hook.assert_awaited_once_with(create2, 7, SENDER)
			assert host.calls == []
			assert await external_call(
				host, create2['deployer'], predict_calldata(), create2=None
			) == (bytes([host_fns.Errors.OK]), b'fallback')
		finally:
			server.close()
			if nested is not None:
				nested.cancel()
				with contextlib.suppress(asyncio.CancelledError):
					await nested

	async def run():
		await asyncio.gather(
			exercise({'deployer': SENDER, 'code_hash': bytes([42]) * 32}),
			exercise({'deployer': Address(bytes([34]) * 20), 'code_hash': bytes([43]) * 32}),
		)

	asyncio.run(run())


def test_default_create2_matches_node_preset():
	assert base_host.DEFAULT_CREATE2 == {
		'deployer': Address('0x1429859428C0aBc9C2C47C8Ee9FBaf82cFA0F20f'),
		'code_hash': bytes.fromhex(
			'aa5d6fdc0053688f0cb3b30dbde241e20d9031bf0f5b714582d4e09f3e103ff8'
		),
	}
