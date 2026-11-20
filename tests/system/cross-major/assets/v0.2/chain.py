# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.2_py-genlayer@" }
from genlayer import *


class Contract(gl.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def hop(self, depth: int, other: Address) -> int:
		if depth <= 0:
			return 2
		me = gl.message.contract_address
		return 2 + gl.get_contract_at(other).view().hop(depth - 1, me)
