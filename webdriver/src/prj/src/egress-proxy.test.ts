// genvm-tool-test-tags: feature-web-render

import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as net from 'node:net';
import { once } from 'node:events';
import { setImmediate as nextTurn } from 'node:timers/promises';
import { egressProxyArgs, startEgressProxy } from './egress-proxy.js';

function reader(socket: net.Socket) {
	const chunks = socket[Symbol.asyncIterator]();
	let buffer: Buffer = Buffer.alloc(0);
	return async (length: number) => {
		while (buffer.length < length) {
			const chunk = await chunks.next();
			assert.equal(chunk.done, false, 'socket closed before its reply');
			buffer = Buffer.concat([buffer, chunk.value]);
		}
		const result = buffer.subarray(0, length);
		buffer = buffer.subarray(length);
		return result;
	};
}

function request(host: string, port = 443) {
	const name = Buffer.from(host);
	const result = Buffer.concat([
		Buffer.from([5, 1, 0, 3, name.length]),
		name,
		Buffer.alloc(2),
	]);
	result.writeUInt16BE(port, result.length - 2);
	return result;
}

async function client(url: string) {
	const socket = net.createConnection({
		host: '127.0.0.1',
		port: Number(new URL(url).port),
	});
	await once(socket, 'connect');
	const read = reader(socket);
	socket.write(Buffer.from([5, 1, 0]));
	assert.deepEqual(await read(2), Buffer.from([5, 0]));
	return { socket, read };
}

test(
	'DNS rebinding cannot change the validated socket destination',
	{ timeout: 3000 },
	async (t) => {
		const echo = net.createServer((socket) => socket.pipe(socket));
		echo.listen(0, '127.0.0.1');
		await once(echo, 'listening');
		t.after(() => echo.close());
		const port = (echo.address() as net.AddressInfo).port;
		let lookups = 0;
		const destinations: string[] = [];
		const proxy = await startEgressProxy({
			resolve: async () => (++lookups === 1 ? ['8.8.8.8'] : ['127.0.0.1']),
			connect: (address, requestedPort) => {
				assert.equal(net.isIP(address), 4);
				assert.equal(requestedPort, port);
				destinations.push(address);
				// Map the approved public destination to a local fixture.
				return net.createConnection({ host: '127.0.0.1', port });
			},
		});
		t.after(() => proxy.close());
		const first = await client(proxy.url);
		t.after(() => first.socket.destroy());
		for (const byte of request('changing.test', port)) {
			first.socket.write(Buffer.from([byte]));
			await nextTurn();
		}
		assert.equal((await first.read(10))[1], 0);
		first.socket.write('ping');
		assert.equal((await first.read(4)).toString(), 'ping');
		assert.equal(lookups, 1);
		assert.deepEqual(destinations, ['8.8.8.8']);
		const second = await client(proxy.url);
		t.after(() => second.socket.destroy());
		second.socket.write(request('changing.test', port));
		assert.equal((await second.read(10))[1], 2);
		assert.equal(lookups, 2);
		assert.deepEqual(
			destinations,
			['8.8.8.8'],
			'a later private DNS answer must never reach connect',
		);
	},
);

for (const addresses of [
	[],
	['127.0.0.1'],
	['169.254.169.254'],
	['::1'],
	['::ffff:7f00:1'],
	['8.8.8.8', '10.0.0.1'],
	['not-an-ip'],
]) {
	test(
		`rejects DNS answers ${JSON.stringify(addresses)}`,
		{ timeout: 2000 },
		async (t) => {
			let connections = 0;
			const proxy = await startEgressProxy({
				resolve: async () => addresses,
				connect: () => {
					connections++;
					throw new Error('must not connect');
				},
			});
			t.after(() => proxy.close());
			const { socket, read } = await client(proxy.url);
			t.after(() => socket.destroy());
			socket.write(request('blocked.test'));
			assert.equal((await read(10))[1], 2);
			assert.equal(connections, 0);
		},
	);
}

for (const address of [
	Buffer.from([1, 127, 0, 0, 1]),
	Buffer.from([4, ...new Array(15).fill(0), 1]),
]) {
	test(
		`blocks private IP literals of address type ${address[0]}`,
		{ timeout: 2000 },
		async (t) => {
			let lookups = 0;
			const proxy = await startEgressProxy({
				resolve: async () => {
					lookups++;
					return ['8.8.8.8'];
				},
			});
			t.after(() => proxy.close());
			const { socket, read } = await client(proxy.url);
			t.after(() => socket.destroy());
			socket.write(
				Buffer.concat([Buffer.from([5, 1, 0]), address, Buffer.from([0, 80])]),
			);
			assert.equal((await read(10))[1], 2);
			assert.equal(lookups, 0);
		},
	);
}

test(
	'disconnect during DNS lookup does not open a late connection',
	{ timeout: 2000 },
	async (t) => {
		let resolve!: (addresses: string[]) => void;
		const pending = new Promise<string[]>((r) => {
			resolve = r;
		});
		let entered!: () => void;
		const resolving = new Promise<void>((r) => {
			entered = r;
		});
		let connections = 0;
		const proxy = await startEgressProxy({
			resolve: () => {
				entered();
				return pending;
			},
			connect: () => {
				connections++;
				throw new Error('must not connect');
			},
		});
		t.after(() => proxy.close());
		const socket = net.createConnection({
			host: '127.0.0.1',
			port: Number(new URL(proxy.url).port),
		});
		t.after(() => socket.destroy());
		await once(socket, 'connect');
		socket.resume();
		socket.write(Buffer.concat([Buffer.from([5, 1, 0]), request('slow.test')]));
		await resolving;
		const ended = once(socket, 'end');
		socket.end();
		await ended;
		resolve(['8.8.8.8']);
		await nextTurn();
		assert.equal(connections, 0);
	},
);

test(
	'DNS failure is rejected without opening a socket',
	{ timeout: 2000 },
	async (t) => {
		const proxy = await startEgressProxy({
			resolve: async () => {
				throw new Error('DNS unavailable');
			},
			connect: () => assert.fail('must not connect'),
		});
		t.after(() => proxy.close());
		const { socket, read } = await client(proxy.url);
		t.after(() => socket.destroy());
		socket.write(request('missing.test'));
		assert.equal((await read(10))[1], 4);
	},
);

test(
	'UDP association and unsupported authentication fail closed',
	{ timeout: 2000 },
	async (t) => {
		const proxy = await startEgressProxy({
			connect: () => assert.fail('must not connect'),
		});
		t.after(() => proxy.close());
		const { socket, read } = await client(proxy.url);
		t.after(() => socket.destroy());
		const udp = request('test.invalid');
		udp[1] = 3;
		socket.write(udp);
		assert.equal((await read(10))[1], 7);
		const auth = net.createConnection({
			host: '127.0.0.1',
			port: Number(new URL(proxy.url).port),
		});
		t.after(() => auth.destroy());
		await once(auth, 'connect');
		const authRead = reader(auth);
		auth.write(Buffer.from([5, 1, 2]));
		assert.deepEqual(await authRead(2), Buffer.from([5, 255]));
	},
);

test(
	'connection failure retries only the already validated addresses',
	{ timeout: 2000 },
	async (t) => {
		const echo = net.createServer((socket) => socket.pipe(socket));
		echo.listen(0, '127.0.0.1');
		await once(echo, 'listening');
		t.after(() => echo.close());
		const port = (echo.address() as net.AddressInfo).port;
		const addresses = ['2606:4700:4700::1111', '8.8.8.8'];
		const attempted: string[] = [];
		let lookups = 0;
		const proxy = await startEgressProxy({
			resolve: async () => {
				lookups++;
				return addresses;
			},
			connect: (address) => {
				attempted.push(address);
				if (attempted.length === 1) {
					const failed = new net.Socket();
					queueMicrotask(() => failed.destroy(new Error('ENETUNREACH')));
					return failed;
				}
				return net.createConnection({ host: '127.0.0.1', port });
			},
		});
		t.after(() => proxy.close());
		const { socket, read } = await client(proxy.url);
		t.after(() => socket.destroy());
		socket.write(request('dual-stack.test'));
		assert.equal((await read(10))[1], 0);
		socket.write('ok');
		assert.equal((await read(2)).toString(), 'ok');
		assert.deepEqual(attempted, addresses);
		assert.equal(lookups, 1);
	},
);

test('extra Chrome flags cannot override the egress guard', () => {
	for (const flag of [
		'--no-proxy-server',
		'--proxy-server=direct://',
		'--proxy-bypass-list=*',
		'--proxy-pac-url=file:///tmp/proxy.pac',
		'--proxy-auto-detect',
		'--host-resolver-rules=MAP * 127.0.0.1',
		'--force-webrtc-ip-handling-policy=default',
		'--enable-quic',
		'--user-data-dir=/tmp/unguarded',
		'--profile-directory=unguarded',
		'--guest',
	]) {
		assert.throws(
			() => egressProxyArgs('socks5://127.0.0.1:1080', [flag]),
			/conflicts with the egress guard/,
		);
	}
});
