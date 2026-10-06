# { "Depends": "py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@" }
import genlayer as gl


class Contract(gl.contract.Contract):
	def __init__(self):
		pass

	@gl.public.view
	def recurse(self, depth: int) -> int:
		if depth == 0:
			return 1
		return 1 + gl.contract.get_at(self.address).view().recurse(depth - 1)

	@gl.public.view
	def observe_recursion(self, depth: int) -> str:
		try:
			return 'ok:' + str(self.recurse(depth))
		except gl.vm.UserError as exc:
			return 'caught:' + str(exc.data)
