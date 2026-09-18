# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@" }
import genlayer as gl
from genlayer.types import Address


class Contract(gl.contract.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def loop(self, remaining: int, first: Address, second: Address) -> int:
		if remaining > 0:
			return gl.contract.get_at(first).view().loop(remaining - 1, first, second)
		return gl.contract.get_at(second).view().loop(first, second)
