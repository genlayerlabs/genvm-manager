local msg = import './message.json';
{
	run(scriptfile)::
		{
			"vars": {},
			"code": scriptfile,
			"message": msg,
			"calldata": "{}",
		}
}
