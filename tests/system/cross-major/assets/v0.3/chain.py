# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@" }
import genlayer as gl
from genlayer.types import Address


class Contract(gl.contract.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def hop(self, depth: int, other: Address) -> int:
		if depth <= 0:
			return 3
		me = gl.message.contract_address
		return 3 + gl.contract.get_at(other).view().hop(depth - 1, me)
