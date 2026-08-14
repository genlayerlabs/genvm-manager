local msg = import './message.json';
{
	// a deploy that errors persists nothing, so a contract whose constructor
	// takes arguments must get them here or the `next` step has no contract
	run(scriptfile, method, args=[], ctor_args=[])::
		{
			"vars": {},
			"code": scriptfile,
			"message": msg + {
				"is_init": true,
			},
			"calldata": if std.length(ctor_args) == 0 then "{}" else std.manifestJsonEx({
				"args": ctor_args,
			}, "    "),
			next: [
				{
					"vars": {},
					"code": null,
					"message": msg,
					"calldata": std.manifestJsonEx({
						"": method,
						"args": args,
					}, "    "),
				},
			],
		}
}
