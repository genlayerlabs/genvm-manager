# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@" }
import genlayer as gl
from genlayer.types import Address


class Contract(gl.contract.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def call(self, target: Address, final: bool) -> int:
		mode = (
			gl.vm.public_abi.StorageType.LATEST_FINAL
			if final
			else gl.vm.public_abi.StorageType.LATEST_NON_FINAL
		)
		return gl.contract.get_at(target).view(state=mode).answer()
