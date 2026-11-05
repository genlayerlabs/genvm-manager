import * as net from 'node:net';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ipIsBad, resolveHost } from './ssrf.js';
import * as logger from './logging.js';

interface ProxyOptions {
	resolve?: (host: string) => Promise<string[]>;
	connect?: (address: string, port: number) => net.Socket;
	timeoutMs?: number;
}

export async function startEgressProxy({
	resolve = resolveHost,
	connect = (host, port) => net.createConnection({ host, port }),
	timeoutMs = 90_000,
}: ProxyOptions = {}) {
	const clients = new Set<net.Socket>();
	const server = net.createServer((client) => {
		clients.add(client);
		let upstream: net.Socket | undefined;
		let greeted = false;
		let buffer: Buffer = Buffer.alloc(0);
		client.setTimeout(timeoutMs, () => client.destroy());
		client.on('error', () => client.destroy());
		client.on('close', () => {
			clients.delete(client);
			upstream?.destroy();
		});
		const reject = (status: number) => {
			client.end(Buffer.from([5, status, 0, 1, 0, 0, 0, 0, 0, 0]));
		};
		const open = async (host: string, port: number, initial: Buffer) => {
			let pending = initial;
			const collect = (chunk: Buffer) => {
				pending = Buffer.concat([pending, chunk]);
				if (pending.length > 65536) client.destroy();
			};
			client.on('data', collect);
			try {
				const addresses = net.isIP(host) ? [host] : await resolve(host);
				if (client.destroyed) return;
				if (!port || !addresses.length || addresses.some(ipIsBad)) {
					logger.log('debug', 'egress proxy blocked destination', {
						host,
						port,
					});
					reject(2);
					return;
				}
				const dial = (index: number) => {
					if (client.destroyed) return;
					if (index === addresses.length) {
						reject(5);
						return;
					}
					// Every retry uses the same validated answer, never another lookup.
					const socket = connect(addresses[index]!, port);
					upstream = socket;
					let connected = false;
					socket.setTimeout(Math.min(10_000, timeoutMs), () =>
						socket.destroy(new Error('connect timeout')),
					);
					socket.on('error', () => {
						if (connected) client.destroy();
						else {
							upstream = undefined;
							socket.destroy();
							dial(index + 1);
						}
					});
					socket.on('close', () => {
						if (upstream === socket) client.destroy();
					});
					socket.once('connect', () => {
						if (client.destroyed) {
							socket.destroy();
							return;
						}
						connected = true;
						socket.setTimeout(0);
						client.off('data', collect);
						client.write(Buffer.from([5, 0, 0, 1, 0, 0, 0, 0, 0, 0]));
						if (pending.length) socket.write(pending);
						client.pipe(socket).pipe(client);
					});
				};
				dial(0);
			} catch {
				if (!client.destroyed) reject(4);
			}
		};
		const handshake = (chunk: Buffer) => {
			buffer = Buffer.concat([buffer, chunk]);
			if (buffer.length > 65536) {
				client.destroy();
				return;
			}
			if (!greeted) {
				if (buffer.length < 2) return;
				const length = 2 + buffer[1]!;
				if (buffer[0] !== 5 || buffer[1] === 0) {
					client.destroy();
					return;
				}
				if (buffer.length < length) return;
				if (!buffer.subarray(2, length).includes(0)) {
					client.end(Buffer.from([5, 255]));
					return;
				}
				buffer = buffer.subarray(length);
				greeted = true;
				client.write(Buffer.from([5, 0]));
			}
			if (buffer.length < 5) return;
			if (buffer[0] !== 5 || buffer[1] !== 1 || buffer[2] !== 0) {
				client.off('data', handshake);
				reject(7);
				return;
			}
			const kind = buffer[3];
			const length =
				kind === 1 ? 10 : kind === 4 ? 22 : kind === 3 ? 7 + buffer[4]! : 0;
			if (!length) {
				client.off('data', handshake);
				reject(8);
				return;
			}
			if (buffer.length < length) return;
			let host: string;
			if (kind === 1) {
				host = [...buffer.subarray(4, 8)].join('.');
			} else if (kind === 4) {
				host = Array.from({ length: 8 }, (_, i) =>
					buffer.readUInt16BE(4 + i * 2).toString(16),
				).join(':');
			} else {
				host = buffer.subarray(5, length - 2).toString('utf8');
			}
			client.off('data', handshake);
			if (!host || host.includes('\0')) {
				reject(4);
				return;
			}
			void open(host, buffer.readUInt16BE(length - 2), buffer.subarray(length));
			buffer = Buffer.alloc(0);
		};
		client.on('data', handshake);
	});
	server.listen(0, '127.0.0.1');
	await once(server, 'listening');
	const address = server.address() as net.AddressInfo;
	return {
		url: `socks5://127.0.0.1:${address.port}`,
		close: async () => {
			const closed = new Promise<void>((resolve) =>
				server.close(() => resolve()),
			);
			for (const client of clients) client.destroy();
			await closed;
		},
	};
}

export function egressProxyArgs(
	url: string,
	extraArgs: string[] = [],
): string[] {
	const protectedFlags = new Set([
		'--no-proxy-server',
		'--proxy-server',
		'--proxy-bypass-list',
		'--proxy-pac-url',
		'--proxy-auto-detect',
		'--host-resolver-rules',
		'--force-webrtc-ip-handling-policy',
		'--enable-quic',
		'--user-data-dir',
		'--profile-directory',
		'--guest',
	]);
	for (const arg of extraArgs) {
		// Chromium also accepts single-dash switches.
		const name = arg.split('=')[0]!.toLowerCase().replace(/^-+/, '--');
		if (protectedFlags.has(name)) {
			throw new Error(
				`Chrome argument conflicts with the egress guard: ${arg}`,
			);
		}
	}
	return [
		...extraArgs,
		`--proxy-server=${url}`,
		'--proxy-bypass-list=<-loopback>',
		'--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
		'--disable-quic',
		'--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
	];
}

export async function createEgressProfile() {
	const path = await mkdtemp(join(tmpdir(), 'genvm-chromium-'));
	const close = () => rm(path, { recursive: true, force: true });
	try {
		await mkdir(join(path, 'Default'));
		// Chrome's profile preference overrides the content-layer command-line flag.
		await writeFile(
			join(path, 'Default', 'Preferences'),
			JSON.stringify({
				webrtc: { ip_handling_policy: 'disable_non_proxied_udp' },
			}),
		);
		return { path, close };
	} catch (error) {
		await close();
		throw error;
	}
}
