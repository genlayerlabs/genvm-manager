# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.2_py-genlayer@" }
from genlayer import *


class Contract(gl.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def answer(self) -> int:
		raise gl.vm.UserError('boom')
