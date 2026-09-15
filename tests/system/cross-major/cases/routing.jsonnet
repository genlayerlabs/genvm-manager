local util = import 'templates/util.jsonnet';
local message = import 'templates/message.json';

local route02 = '${executorV02}';
local route03 = '${executorV03}';

local deploy(slug, address, code) = {
	slug: slug,
	code: '${jsonnetDir}/../assets/' + code,
	message: message + {
		contract_address: address,
		is_init: true,
	},
	calldata: '{}',
	modes: 'l',
	stable_hash: false,
	expected_semantics_components: [],
	reroute_to: if std.startsWith(code, 'v0.2/') then route02 else route03,
};

local call(slug, caller, target, method, route, extra={}) = {
	slug: slug,
	code: null,
	vars: { target: target },
	message: message + { contract_address: caller },
	calldata: '{"": "' + method + '", "args": [Address(target)]}',
	executor_routes: if route == null then {} else { [target]: route },
	hook_cross_contract_calls: true,
	stable_hash: false,
	expected_semantics_components: ['return', 'kind'],
} + extra;

local caller03 = '0x1111111111111111111111111111111111111111';
local caller02 = '0x1212121212121212121212121212121212121212';
local callee03 = '0x1313131313131313131313131313131313131313';
local callee02 = '0x1414141414141414141414141414141414141414';
local signer03 = '0x1717171717171717171717171717171717171717';
local signerCaller02 = '0x1818181818181818181818181818181818181818';
local structured03 = '0x1919191919191919191919191919191919191919';
local userError03 = '0x2020202020202020202020202020202020202020';
local userError02 = '0x2121212121212121212121212121212121212121';

local scenarios = [
	util.chain([
		deploy('v03-to-v02-caller', caller03, 'v0.3/caller.py'),
		deploy('v03-to-v02-callee', callee02, 'v0.2/callee.py'),
		call('v03-to-v02', caller03, callee02, 'call', route02),
	]),
	util.chain([
		deploy('v02-to-v03-caller', caller02, 'v0.2/caller.py'),
		deploy('v02-to-v03-callee', callee03, 'v0.3/callee.py'),
		call('v02-to-v03', caller02, callee03, 'call', route03, { reroute_to: route02 }),
	]),
	util.chain([
		deploy('signer-caller', signerCaller02, 'v0.2/signer_caller.py'),
		deploy('signer-callee', signer03, 'v0.3/signer.py'),
		call('signer', signerCaller02, signer03, 'signer', route03, { reroute_to: route02 }),
	]),
	util.chain([
		deploy('structured-error-caller', caller02, 'v0.2/caller.py'),
		deploy('structured-error-callee', structured03, 'v0.3/structured_error.py'),
		call('structured-error', caller02, structured03, 'call', route03, { reroute_to: route02 }),
	]),
	util.chain([
		deploy('plain-error-caller', caller02, 'v0.2/caller.py'),
		deploy('plain-error-callee', userError03, 'v0.3/user_error.py'),
		call('plain-error', caller02, userError03, 'call', route03, { reroute_to: route02 }),
	]),
	util.chain([
		deploy('plain-error-reverse-caller', caller03, 'v0.3/caller.py'),
		deploy('plain-error-reverse-callee', userError02, 'v0.2/user_error.py'),
		call('plain-error-reverse', caller03, userError02, 'call', route02),
	]),
	util.chain([
		deploy('null-same-caller', caller03, 'v0.3/caller.py'),
		deploy('null-same-callee', callee03, 'v0.3/callee.py'),
		call('null-same-major', caller03, callee03, 'call', null, {
			expected_executor_route_requests: [{
				contract_address: callee03,
				state_mode: 2,
				advisory_major: 0,
			}],
		}),
	]),
	util.chain([
		deploy('null-mismatch-caller', caller03, 'v0.3/caller.py'),
		deploy('null-mismatch-callee', callee02, 'v0.2/callee.py'),
		call('null-mismatch', caller03, callee02, 'call', null),
	]),
];

{
	tags: util.features([['version-routing', 'cross-major']], 'stable') + ['python'],
	entry: util.addPaths(scenarios),
}
