# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.2_py-genlayer@" }
from genlayer import *


class Contract(gl.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def signer(self, target: Address) -> str:
		return gl.get_contract_at(target).view().signer()
