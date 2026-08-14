# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@" }
import genlayer as gl
import _genlayer_wasi as wasi
from genlayer.types import Address, u32


class Contract(gl.contract.Contract):
	value: u32

	def __init__(self):
		self.value = 777

	@gl.public.view
	def context(self):
		return {
			'contract': gl.message.contract_address.as_hex,
			'sender': gl.message.sender_address.as_hex,
			'origin': gl.message.origin_address.as_hex,
			'signer': gl.message.signer_address.as_hex,
			'stack': [address.as_hex for address in gl.message.stack],
			'value': int(gl.message.value),
			'is_init': gl.message.is_init,
		}

	@gl.public.view
	def read(self) -> int:
		return self.value

	@gl.public.view
	def permissions(self, helper: Address):
		def attempt(action):
			try:
				value = action()
				return {'kind': 'allowed', 'value': str(value)}
			except OSError as exc:
				return {'kind': 'oserror', 'errno': exc.errno}
			except Exception as exc:
				return {'kind': type(exc).__name__, 'value': str(exc)}

		write = attempt(lambda: wasi.storage_write(b'\x77' * 32, 0, b'forbidden'))
		send = attempt(lambda: gl.contract.get_at(Address(b'\x66' * 20)).emit().ping())
		nondet = attempt(lambda: gl.vm.run_nondet(lambda: 1, lambda _result: True))

		registered = gl.vm.register_runner(
			b'# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@" }\n'
			b'print("registered")\n'
		)
		called = gl.contract.get_at(helper).view().answer()
		return {
			'write': write,
			'send': send,
			'nondet': nondet,
			'registered': registered,
			'called': called,
		}

	@gl.public.view
	def debug_alias(self) -> int:
		result = gl.vm.spawn_sandbox(lambda: 1, runner='py-genlayer:test')
		return gl.vm.unpack_result(result)
