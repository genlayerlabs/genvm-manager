// genvm-tool-test-tags: feature-web-render

import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { test } from 'node:test';
import { setImmediate as nextTurn } from 'node:timers/promises';
import { abortable, disconnectSignal } from './cancellation.js';

for (const disconnect of [false, true]) {
	test(
		`response signal ${disconnect ? 'aborts on disconnect' : 'survives normal completion'}`,
		{ timeout: 2000 },
		async (t) => {
			let received!: (value: {
				signal: AbortSignal;
				res: http.ServerResponse;
			}) => void;
			const requestEnded = new Promise<{
				signal: AbortSignal;
				res: http.ServerResponse;
			}>((resolve) => {
				received = resolve;
			});
			const server = http.createServer((req, res) => {
				const signal = disconnectSignal(res);
				req.on('end', () => {
					received({ signal, res });
				});
				req.resume();
			});
			server.listen(0, '127.0.0.1');
			await once(server, 'listening');
			t.after(() => {
				server.closeAllConnections();
				server.close();
			});
			const address = server.address();
			assert.ok(address && typeof address !== 'string');
			const client = http.get(`http://127.0.0.1:${address.port}`, {
				agent: false,
			});
			client.on('error', () => {});
			t.after(() => client.destroy());
			const { signal, res } = await requestEnded;
			await nextTurn();
			assert.equal(
				signal.aborted,
				false,
				'finishing the incoming request is not a disconnect',
			);
			const closed = once(res, 'close');
			if (disconnect) {
				client.destroy();
			} else {
				const response = once(client, 'response');
				res.end('ok');
				const [incoming] = await response;
				incoming.resume();
				await once(incoming, 'end');
			}
			await closed;
			assert.equal(signal.aborted, disconnect);
		},
	);
}

test('cancelling a browser startup wait does not start rendering later', async () => {
	const controller = new AbortController();
	let ready!: () => void;
	const startup = new Promise<void>((resolve) => {
		ready = resolve;
	});
	let renders = 0;
	const result = (async () => {
		await abortable(controller.signal, () => startup);
		renders++;
	})();
	const rejected = assert.rejects(result, { name: 'AbortError' });
	controller.abort();
	await rejected;
	ready();
	await nextTurn();
	assert.equal(renders, 0);
});
