/**
 * Failures of this sidecar must leave `renderPageWithBrowser` by being thrown,
 * and must not be dressed up as a page status.
 *
 * `handleRenderRequest` turns anything thrown out of here into a bare `500`,
 * and the module classifies a `500` from its own webdriver as a fatal
 * `WEBDRIVER_UNAVAILABLE`. That is the intended outcome: a validator whose own
 * sidecar is broken has no observation of the page, so it must abstain (the
 * node votes Timeout) rather than assert a result it never computed.
 *
 * A page that merely fails to load is the opposite case -- a real observation.
 * It comes back as a *returned* status the caller puts in `Resulting-Status`
 * next to a `200`, which the module reports as a catchable, non-fatal
 * `WEBPAGE_LOAD_FAILED`. The pairs below pin both directions, so reclassifying
 * either one fails the suite.
 *
 * The browser is faked outright: no Chromium is launched and nothing leaves
 * the process. `render.ts` is imported directly rather than through
 * `index.ts`, which would start the HTTP server and launch a browser at module
 * scope.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import type * as pup from 'puppeteer-core';
import { ProtocolError, TimeoutError } from 'puppeteer-core';

import { renderPageWithBrowser, statusIsGood } from '../src/render.js';

interface FakeContext {
	closes: number;
}

/**
 * A browser whose page creation fails the way a wedged or dying Chromium does.
 * Records context closes so a leak shows up as a failing assertion.
 */
function browserFailingOn(
	where: 'createBrowserContext' | 'newPage',
	error: Error,
): { browser: pup.Browser; context: FakeContext } {
	const context: FakeContext = { closes: 0 };

	const fakeContext = {
		newPage: async () => {
			if (where === 'newPage') {
				throw error;
			}
			throw new Error('unreachable');
		},
		close: async () => {
			context.closes++;
		},
		on: () => {},
	};

	const browser = {
		createBrowserContext: async () => {
			if (where === 'createBrowserContext') {
				throw error;
			}
			return fakeContext;
		},
	};

	return { browser: browser as unknown as pup.Browser, context };
}

async function render(browser: pup.Browser) {
	return renderPageWithBrowser(browser, 'https://example.com/', 'text');
}

// -- a broken sidecar raises, it does not report ----------------------

test('a protocol timeout opening a page is thrown, not reported', async () => {
	// what puppeteer raises once `Target.createTarget` exceeds protocolTimeout
	const { browser, context } = browserFailingOn(
		'newPage',
		new ProtocolError('Target.createTarget timed out'),
	);

	await assert.rejects(render(browser), /Target.createTarget timed out/);
	assert.equal(context.closes, 1, 'the browser context must not be leaked');
});

test('a TimeoutError opening a page is thrown, not reported', async () => {
	const { browser, context } = browserFailingOn(
		'newPage',
		new TimeoutError('waiting for target failed'),
	);

	await assert.rejects(render(browser), /waiting for target failed/);
	assert.equal(context.closes, 1, 'the browser context must not be leaked');
});

test('a failure creating the context is thrown, not reported', async () => {
	const { browser } = browserFailingOn(
		'createBrowserContext',
		new ProtocolError('Target.createBrowserContext timed out'),
	);

	await assert.rejects(
		render(browser),
		/Target.createBrowserContext timed out/,
	);
});

test('a failure after the page exists still propagates', async () => {
	// `installSsrfGuard` is the first thing done with a live page, and a page
	// that cannot be guarded is this sidecar failing too
	const closes = { page: 0, context: 0 };
	const page = {
		setRequestInterception: async () => {
			throw new Error('interception unavailable');
		},
		close: async () => {
			closes.page++;
		},
		on: () => {},
		setViewport: () => {},
	};
	const browser = {
		createBrowserContext: async () => ({
			newPage: async () => page,
			close: async () => {
				closes.context++;
			},
			on: () => {},
		}),
	} as unknown as pup.Browser;

	await assert.rejects(render(browser), /interception unavailable/);
	assert.deepEqual(
		closes,
		{ page: 1, context: 1 },
		'the page and its context must still be torn down',
	);
});

// -- and a failing remote page still reports, it does not raise -------

/**
 * The other half of the pair, so the tests above are not satisfied by "throw
 * everything". A site that refuses the connection is something we *did*
 * observe, so it comes back as a status rather than an exception -- and it is
 * a 503, the very code the sidecar-failure path must not borrow.
 */
test('an unreachable remote site is reported, not thrown', async () => {
	const closes = { page: 0, context: 0 };
	const page = {
		setRequestInterception: async () => {},
		goto: async () => {
			throw new Error('net::ERR_CONNECTION_REFUSED at https://example.com/');
		},
		evaluate: async () => 0,
		close: async () => {
			closes.page++;
		},
		on: () => {},
		off: () => {},
		setViewport: () => {},
	};
	const browser = {
		createBrowserContext: async () => ({
			newPage: async () => page,
			close: async () => {
				closes.context++;
			},
			on: () => {},
		}),
	} as unknown as pup.Browser;

	const result = await render(browser);

	assert.equal(result.status, 503);
	assert.match(String(result.body), /Connection refused/);
	assert.equal(
		statusIsGood(result.status),
		false,
		'the module must see this as a failed load',
	);
	assert.deepEqual(closes, { page: 1, context: 1 });
});
