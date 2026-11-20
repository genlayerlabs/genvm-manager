# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.2_py-genlayer@" }
from genlayer import *


class Contract(gl.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def loop(self, first: Address, second: Address) -> int:
		return gl.get_contract_at(first).view().loop(0, first, second)
