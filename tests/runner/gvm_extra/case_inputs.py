import origin.calldata as gvm_calldata


def encode_calldata(conf: dict) -> bytes:
	"""
	Calldata bytes an integration case config asks for.

	``calldata`` is a python expression, evaluated against the case's ``vars``:
	the jsonnet side keeps it as source so a case can build an
	:class:`~origin.calldata.Address` or reuse a var.
	"""
	names = {
		'Address': gvm_calldata.Address,
		'true': True,
		'false': False,
	}
	return gvm_calldata.encode(eval(conf['calldata'], names, dict(conf.get('vars', {}))))
