# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@" }
import genlayer as gl


class Contract(gl.contract.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def sink(self, blob: str) -> int:
		return len(blob)
