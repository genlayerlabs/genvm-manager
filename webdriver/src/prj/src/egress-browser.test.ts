// genvm-tool-test-tags: feature-web-render

import assert from 'node:assert/strict';
import http from 'node:http';
import https from 'node:https';
import * as net from 'node:net';
import * as dgram from 'node:dgram';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import puppeteer from 'puppeteer-core';
import {
	createEgressProfile,
	egressProxyArgs,
	startEgressProxy,
} from './egress-proxy.js';
import { renderPageWithBrowser } from './render.js';
import { Semaphore } from './semaphore.js';

const executablePath = process.env.GVM_WEBDRIVER_CHROME_EXECUTABLE;

test(
	'Chromium uses the checked proxy for HTTP, HTTPS, redirects and WebSockets',
	{
		skip:
			!executablePath &&
			'set GVM_WEBDRIVER_CHROME_EXECUTABLE to run Chromium coverage',
		timeout: 30_000,
	},
	async (t) => {
		const requests: string[] = [];
		const upgrades: string[] = [];
		const fixture = http.createServer((req, res) => {
			requests.push(`${req.headers.host}${req.url}`);
			if (req.url === '/redirect') {
				res.writeHead(302, { Location: `http://127.0.0.1:${port}/private` });
				res.end();
			} else {
				res.end('<html><body>public fixture</body></html>');
			}
		});
		fixture.on('upgrade', (req, socket) => {
			upgrades.push(req.headers.host!);
			const accept = createHash('sha1')
				.update(
					`${req.headers['sec-websocket-key']}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`,
				)
				.digest('base64');
			socket.end(
				`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
			);
		});
		fixture.listen(0, '127.0.0.1');
		await once(fixture, 'listening');
		const port = (fixture.address() as net.AddressInfo).port;
		t.after(() => {
			fixture.closeAllConnections();
			fixture.close();
		});
		let ipv6Requests = 0;
		const ipv6 = http.createServer((_req, res) => {
			ipv6Requests++;
			res.end('private IPv6 fixture');
		});
		ipv6.listen(0, '::1');
		await once(ipv6, 'listening');
		const ipv6Port = (ipv6.address() as net.AddressInfo).port;
		t.after(() => {
			ipv6.closeAllConnections();
			ipv6.close();
		});
		const dir = await mkdtemp(join(tmpdir(), 'webdriver-tls-'));
		t.after(() => rm(dir, { recursive: true, force: true }));
		execFileSync(
			'openssl',
			[
				'req',
				'-x509',
				'-newkey',
				'rsa:2048',
				'-nodes',
				'-keyout',
				join(dir, 'key.pem'),
				'-out',
				join(dir, 'cert.pem'),
				'-days',
				'1',
				'-subj',
				'/CN=allowed.test',
			],
			{ stdio: 'ignore' },
		);
		let serverName = '';
		const secure = https.createServer(
			{
				key: await readFile(join(dir, 'key.pem')),
				cert: await readFile(join(dir, 'cert.pem')),
			},
			(req, res) => {
				serverName = req.headers.host!;
				res.end('secure fixture');
			},
		);
		secure.listen(0, '127.0.0.1');
		await once(secure, 'listening');
		const tlsPort = (secure.address() as net.AddressInfo).port;
		t.after(() => {
			secure.closeAllConnections();
			secure.close();
		});
		let rebind = false;
		const destinations: string[] = [];
		const proxy = await startEgressProxy({
			resolve: async (host) =>
				host === 'allowed.test' || (host === 'changing.test' && !rebind)
					? ['8.8.8.8']
					: ['127.0.0.1'],
			connect: (address, targetPort) => {
				assert.equal(address, '8.8.8.8');
				assert.ok(targetPort === port || targetPort === tlsPort);
				destinations.push(address);
				return net.createConnection({ host: '127.0.0.1', port: targetPort });
			},
		});
		t.after(() => proxy.close());
		const profile = await createEgressProfile();
		const browser = await puppeteer.launch({
			userDataDir: profile.path,
			executablePath: executablePath!,
			headless: true,
			acceptInsecureCerts: true,
			args: ['--no-sandbox', ...egressProxyArgs(proxy.url)],
		});
		t.after(async () => {
			await browser.close();
			await profile.close();
		});
		const page = await browser.newPage();
		assert.equal(
			(await page.goto(`http://allowed.test:${port}/`))?.status(),
			200,
		);
		assert.equal(
			await page.evaluate(() => document.body.innerText),
			'public fixture',
		);
		assert.equal(
			(await page.goto(`https://allowed.test:${tlsPort}/`))?.status(),
			200,
		);
		assert.equal(
			await page.evaluate(() => document.body.innerText),
			'secure fixture',
		);
		assert.equal(serverName, `allowed.test:${tlsPort}`);
		await page.goto(`http://allowed.test:${port}/`);
		const webSocket = (url: string) =>
			page.evaluate(
				(address) =>
					new Promise<string>((resolve) => {
						const socket = new WebSocket(address);
						socket.onopen = () => {
							socket.close();
							resolve('open');
						};
						socket.onerror = () => resolve('blocked');
					}),
				url,
			);
		assert.equal(await webSocket(`ws://allowed.test:${port}/`), 'open');
		assert.equal(await webSocket(`ws://127.0.0.1:${port}/`), 'blocked');
		assert.deepEqual(upgrades, [`allowed.test:${port}`]);
		await page.evaluate(
			(target) =>
				new Promise<void>((resolve) => {
					const image = new Image();
					image.onload = image.onerror = () => resolve();
					image.src = target;
				}),
			`http://127.0.0.1:${port}/private-image`,
		);
		await assert.rejects(page.goto(`http://allowed.test:${port}/redirect`));
		for (const host of ['127.0.0.1', 'localhost']) {
			await assert.rejects(page.goto(`http://${host}:${port}/private`));
		}
		await assert.rejects(page.goto(`http://[::1]:${ipv6Port}/private`));
		assert.equal(ipv6Requests, 0);
		assert.ok(requests.every((url) => !url.includes('/private')));
		await t.test(
			'WebRTC cannot reach private STUN or TURN servers directly',
			async (t) => {
				const context = await browser.createBrowserContext();
				t.after(() => context.close());
				const rtcPage = await context.newPage();
				let packets = 0;
				const udp = dgram.createSocket('udp4');
				udp.on('message', () => {
					packets++;
				});
				udp.bind(0, '127.0.0.1');
				await once(udp, 'listening');
				t.after(() => udp.close());
				let connections = 0;
				const tcp = net.createServer((socket) => {
					connections++;
					socket.destroy();
				});
				tcp.listen(0, '127.0.0.1');
				await once(tcp, 'listening');
				t.after(() => tcp.close());
				await rtcPage.goto(`http://allowed.test:${port}/`);
				await rtcPage.evaluate(
					async ({ udpPort, tcpPort }) => {
						const peer = new RTCPeerConnection({
							iceServers: [
								{ urls: `stun:127.0.0.1:${udpPort}` },
								{
									urls: `turn:127.0.0.1:${tcpPort}?transport=tcp`,
									username: 'test',
									credential: 'test',
								},
							],
						});
						try {
							peer.createDataChannel('probe');
							await peer.setLocalDescription(await peer.createOffer());
							await new Promise<void>((resolve) => {
								const timeout = setTimeout(resolve, 3000);
								peer.onicegatheringstatechange = () => {
									if (peer.iceGatheringState === 'complete') {
										clearTimeout(timeout);
										resolve();
									}
								};
							});
						} finally {
							peer.close();
						}
					},
					{
						udpPort: udp.address().port,
						tcpPort: (tcp.address() as net.AddressInfo).port,
					},
				);
				assert.equal(packets, 0);
				assert.equal(connections, 0);
			},
		);
		const first = await browser.createBrowserContext();
		await (await first.newPage()).goto(`http://changing.test:${port}/`);
		await first.close();
		rebind = true;
		const count = destinations.length;
		const second = await browser.createBrowserContext();
		await assert.rejects(
			(await second.newPage()).goto(`http://changing.test:${port}/`),
		);
		assert.equal(
			destinations.length,
			count,
			'rebinding must not open another socket',
		);
		await second.close();
		await t.test(
			'disconnect stops a real render and releases its permit',
			async () => {
				const controller = new AbortController();
				const slots = new Semaphore({ permits: 1, maxQueued: 1 });
				const url = 'data:text/html,<body>cancel-this-render</body>';
				const contexts = browser.browserContexts().length;
				const rendering = slots.withPermit(
					1000,
					() =>
						renderPageWithBrowser(
							browser,
							url,
							'text',
							{ waitAfterLoaded: 60 },
							controller.signal,
						),
					controller.signal,
				);
				const rejected = assert.rejects(rendering, { name: 'AbortError' });
				const target = await browser.waitForTarget(
					(candidate) => candidate.url() === url,
				);
				const renderingPage = await target.page();
				assert.ok(renderingPage);
				assert.equal(
					await renderingPage.evaluate(() => document.body.innerText),
					'cancel-this-render',
				);
				const next = slots.acquire(1000);
				controller.abort();
				await rejected;
				await next;
				assert.equal(renderingPage.isClosed(), true);
				assert.equal(browser.browserContexts().length, contexts);
				slots.release();
				assert.equal(slots.inFlight, 0);
			},
		);
		await proxy.close();
		await assert.rejects(
			page.goto(`http://127.0.0.1:${port}/no-direct-fallback`),
		);
		assert.ok(requests.every((url) => !url.endsWith('/no-direct-fallback')));
	},
);
