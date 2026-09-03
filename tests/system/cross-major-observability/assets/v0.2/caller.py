# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.2_py-genlayer@" }
from genlayer import *


class Contract(gl.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def context(self, target: Address):
		return gl.get_contract_at(target).view().context()

	@gl.public.view
	def permissions(self, target: Address, helper: Address):
		return gl.get_contract_at(target).view().permissions(helper)

	@gl.public.view
	def debug_alias(self, target: Address):
		return gl.get_contract_at(target).view().debug_alias()

	@gl.public.view
	def read(self, target: Address) -> int:
		return gl.get_contract_at(target).view().read()

	@gl.public.view
	def answer(self, target: Address) -> int:
		return gl.get_contract_at(target).view().answer()
