// genvm-tool-test-tags: feature-web-render

import { test, type TestContext } from 'node:test';
import assert from 'node:assert/strict';
import { setImmediate as nextTurn } from 'node:timers/promises';
import type * as pup from 'puppeteer-core';
import {
	ConnectionClosedError,
	ProtocolError,
	TimeoutError,
} from 'puppeteer-core';

import { BrowserHolder, RotatingManager, type Options } from './manager.js';
import { isBrowserFailure } from './index.js';

interface FakeBrowser {
	holder: BrowserHolder;
	kills: string[];
	closes: number;
}

type CloseMode = 'ok' | 'hang' | 'reject';

function fakeBrowser(
	closeMode: CloseMode = 'ok',
	hasProcess = true,
): FakeBrowser {
	const fake: FakeBrowser = { holder: undefined!, kills: [], closes: 0 };
	const browser = {
		close: async () => {
			fake.closes++;
			if (closeMode === 'hang') {
				await new Promise<never>(() => {});
			} else if (closeMode === 'reject') {
				throw new Error('close failed');
			}
		},
		process: () =>
			hasProcess
				? { pid: 100, kill: (signal: string) => fake.kills.push(signal) }
				: null,
	};
	fake.holder = new BrowserHolder(browser as unknown as pup.Browser);
	return fake;
}

/** Sleeps that only finish when the test says so, and cancel on abort. */
class FakeTimers {
	pending = new Set<() => void>();

	sleep = (_ms: number, signal: AbortSignal): Promise<void> =>
		new Promise((resolve, reject) => {
			const fire = () => resolve();
			this.pending.add(fire);
			signal.addEventListener('abort', () => {
				this.pending.delete(fire);
				reject(signal.reason);
			});
		});

	fireAll(): void {
		const fires = [...this.pending];
		this.pending.clear();
		fires.forEach((fire) => fire());
	}
}

function captureLogs(t: TestContext): () => any[] {
	const lines: string[] = [];
	t.mock.method(console, 'log', (line: string) => {
		lines.push(line);
	});
	return () => lines.slice();
}

async function setup(
	first: FakeBrowser,
	launches: (FakeBrowser | Error)[],
	overrides: Partial<Options> = {},
) {
	const timers = new FakeTimers();
	launches.unshift(first);
	const manager = await RotatingManager.create({
		checkIntervalMs: 600_000,
		absoluteThresholdMB: 2048,
		idleDeltaMB: 256,
		atLeastMs: 0,
		failureThreshold: 3,
		closeTimeoutMs: 120_000,
		launch: async () => {
			const next = launches.shift();
			assert.ok(next, 'unexpected launch');
			if (next instanceof Error) {
				throw next;
			}
			return next.holder;
		},
		memoryMB: async () => 100,
		sleep: timers.sleep,
		...overrides,
	});
	return { manager, timers };
}

function fail(manager: RotatingManager, times: number) {
	for (let i = 0; i < times; i++) {
		const handle = manager.getBrowser();
		manager.report(handle, true);
		void handle.close();
	}
}

test('consecutive browser failures replace the browser in-process', async (t) => {
	const logs = captureLogs(t);
	const first = fakeBrowser();
	const second = fakeBrowser();
	const { manager } = await setup(first, [second]);

	fail(manager, 3);
	await manager.check();

	assert.equal(manager.getBrowser().get(), second.holder.get());
	assert.equal(first.kills.length, 0);
	const check = logs().find((l) => l.includes('shouldRotate: true'));
	assert.ok(check, 'rotation check not logged');
	assert.match(check, /3 consecutive browser failures/);
});

test('reaching the threshold rotates without waiting for the interval', async (t) => {
	captureLogs(t);
	const first = fakeBrowser();
	const second = fakeBrowser();
	const { manager } = await setup(first, [second]);

	fail(manager, 3);
	await nextTurn();

	assert.equal(manager.getBrowser().get(), second.holder.get());
	assert.equal(first.closes, 1);
});

test('a single transient failure does not rotate', async (t) => {
	captureLogs(t);
	const first = fakeBrowser();
	const { manager } = await setup(first, []);

	fail(manager, 1);
	await manager.check();
	assert.equal(manager.getBrowser().get(), first.holder.get());
});

test('a success resets the failure counter', async (t) => {
	captureLogs(t);
	const first = fakeBrowser();
	const { manager } = await setup(first, []);

	fail(manager, 2);
	manager.report(manager.getBrowser(), false);
	fail(manager, 2);
	await manager.check();
	assert.equal(manager.getBrowser().get(), first.holder.get());
});

test('reports for a replaced browser are ignored', async (t) => {
	captureLogs(t);
	const first = fakeBrowser();
	const second = fakeBrowser();
	const { manager } = await setup(first, [second], { failureThreshold: 1 });

	const stale = manager.getBrowser();
	fail(manager, 1);
	await manager.check();
	manager.report(stale, true);
	await manager.check();

	assert.equal(
		manager.getBrowser().get(),
		second.holder.get(),
		'rotated twice',
	);
});

test('a rotation that fails to launch keeps the old browser and retries', async (t) => {
	captureLogs(t);
	const first = fakeBrowser();
	const second = fakeBrowser();
	const { manager } = await setup(first, [new Error('launch failed'), second], {
		failureThreshold: 1,
	});

	fail(manager, 1);
	await nextTurn();
	assert.equal(manager.getBrowser().get(), first.holder.get());
	fail(manager, 1);
	await nextTurn();
	assert.equal(manager.getBrowser().get(), second.holder.get());
});

test('a hung close of the old browser is killed after the bound and does not block the new one', async (t) => {
	captureLogs(t);
	const first = fakeBrowser('hang');
	const second = fakeBrowser();
	const { manager, timers } = await setup(first, [second], {
		failureThreshold: 1,
	});

	fail(manager, 1);
	const rotation = manager.check();
	await nextTurn();

	assert.equal(
		manager.getBrowser().get(),
		second.holder.get(),
		'swap must not wait',
	);
	assert.deepEqual(first.kills, []);
	timers.fireAll();
	await rotation;
	assert.deepEqual(first.kills, ['SIGKILL']);
	assert.equal(timers.pending.size, 0);
});

test('a rejecting close kills the old browser too', async (t) => {
	captureLogs(t);
	const first = fakeBrowser('reject');
	const second = fakeBrowser();
	const { manager } = await setup(first, [second], { failureThreshold: 1 });

	fail(manager, 1);
	await manager.check();
	assert.deepEqual(first.kills, ['SIGKILL']);
});

test('a clean close is not killed and leaves no timer behind', async (t) => {
	captureLogs(t);
	const first = fakeBrowser();
	const second = fakeBrowser();
	const { manager, timers } = await setup(first, [second], {
		failureThreshold: 1,
	});

	fail(manager, 1);
	await manager.check();
	assert.deepEqual(first.kills, []);
	assert.equal(timers.pending.size, 0);
});

test('a hung close without a process handle does not throw', async (t) => {
	captureLogs(t);
	const first = fakeBrowser('hang', false);
	const second = fakeBrowser();
	const { manager, timers } = await setup(first, [second], {
		failureThreshold: 1,
	});

	fail(manager, 1);
	const rotation = manager.check();
	await nextTurn();
	timers.fireAll();
	await rotation;
	assert.equal(manager.getBrowser().get(), second.holder.get());
});

test('concurrent checks share one rotation', async (t) => {
	captureLogs(t);
	const first = fakeBrowser();
	const second = fakeBrowser();
	const { manager } = await setup(first, [second], { failureThreshold: 1 });

	fail(manager, 1);
	await Promise.all([manager.check(), manager.check()]);
	assert.equal(manager.getBrowser().get(), second.holder.get());
});

test('the scheduled loop checks once per interval and stops', async (t) => {
	const logs = captureLogs(t);
	const first = fakeBrowser();
	const { manager, timers } = await setup(first, []);

	manager.start();
	assert.equal(timers.pending.size, 1);
	timers.fireAll();
	await nextTurn();
	assert.equal(
		logs().filter((l) => l.includes('browser rotation check')).length,
		1,
	);
	assert.equal(timers.pending.size, 1);
	manager.stop();
	assert.equal(timers.pending.size, 0);
});

test('only browser-level errors count as browser failures', () => {
	assert.equal(isBrowserFailure(new ConnectionClosedError('closed')), true);
	assert.equal(
		isBrowserFailure(
			new ProtocolError(
				"Target.createBrowserContext timed out. Increase the 'protocolTimeout' setting",
			),
		),
		true,
	);
	assert.equal(
		isBrowserFailure(new ProtocolError('Runtime.evaluate timed out.')),
		false,
	);
	assert.equal(isBrowserFailure(new TimeoutError('Navigation timeout')), false);
	assert.equal(isBrowserFailure(new Error('x')), false);
});
