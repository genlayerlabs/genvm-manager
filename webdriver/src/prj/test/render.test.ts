/**
 * Failures of this sidecar must not leave `renderPageWithBrowser` by being
 * thrown.
 *
 * `handleRenderRequest` turns anything thrown out of here into a bare `500`,
 * and the module classifies a `500` from its own webdriver as a fatal
 * `STATUS_NOT_OK`, which aborts the whole contract run as an internal error.
 * A page that merely fails to load takes a different route: it comes back as a
 * status the caller puts in `Resulting-Status` next to a `200`, which the
 * module reports as a catchable, non-fatal `WEBPAGE_LOAD_FAILED`.
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

import {
	renderPageWithBrowser,
	statusIsGood,
	STATUS_SERVICE_UNAVAILABLE,
} from '../src/render.js';

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

// -- a broken sidecar reports, it does not raise ----------------------

test('a protocol timeout opening a page is reported, not thrown', async () => {
	// what puppeteer raises once `Target.createTarget` exceeds protocolTimeout
	const { browser, context } = browserFailingOn(
		'newPage',
		new ProtocolError('Target.createTarget timed out'),
	);

	const result = await render(browser);

	assert.equal(result.status, STATUS_SERVICE_UNAVAILABLE);
	assert.match(String(result.body), /Webdriver unavailable/);
	assert.equal(context.closes, 1, 'the browser context must not be leaked');
});

test('a TimeoutError opening a page is reported, not thrown', async () => {
	const { browser, context } = browserFailingOn(
		'newPage',
		new TimeoutError('waiting for target failed'),
	);

	const result = await render(browser);

	assert.equal(result.status, STATUS_SERVICE_UNAVAILABLE);
	assert.equal(context.closes, 1, 'the browser context must not be leaked');
});

test('a failure creating the context is reported, not thrown', async () => {
	const { browser } = browserFailingOn(
		'createBrowserContext',
		new ProtocolError('Target.createBrowserContext timed out'),
	);

	const result = await render(browser);

	assert.equal(result.status, STATUS_SERVICE_UNAVAILABLE);
});

test('the reported status is one the module treats as a failed load', () => {
	// `statusIsGood` is what both this sidecar and `Render` in
	// `genvm-web-default.lua` use to decide whether a render succeeded
	assert.equal(statusIsGood(STATUS_SERVICE_UNAVAILABLE), false);
});

// -- and the mapping stays scoped to setup ----------------------------

test('a failure after the page exists still propagates', async () => {
	// Not vacuous: the change must not swallow every error. `installSsrfGuard`
	// is the first thing done with a live page, and a page that cannot be
	// guarded is a bug, not an environment blip
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
