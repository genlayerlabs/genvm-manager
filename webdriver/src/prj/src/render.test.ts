/**
 * Failures of this sidecar must leave `renderPageWithBrowser` by being thrown,
 * and must not be dressed up as a page status.
 *
 * Except for disconnected callers, `handleRenderRequest` turns throws into a bare `500`,
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
 * One navigation error crosses that line, and only one:
 * `net::ERR_INTERNET_DISCONNECTED` says the request never left this host, so
 * there is nothing observed to report and it is thrown. The neighbouring
 * errors are deliberately NOT treated that way, and the tests that pin them
 * are guards rather than descriptions -- see each one for why the ambiguity is
 * left for the validators to settle.
 *
 * The browser is faked outright: no Chromium is launched and nothing leaves
 * the process. `render.ts` is imported directly rather than through
 * `index.ts`, which would start the HTTP server and launch a browser at module
 * scope.
 */

// genvm-tool-test-tags: feature-web-render

import { test } from 'node:test';
import assert from 'node:assert/strict';
import type * as pup from 'puppeteer-core';
import { ProtocolError, TimeoutError } from 'puppeteer-core';

import { renderPageWithBrowser, statusIsGood } from './render.js';
import { Semaphore } from './semaphore.js';
import { setImmediate as nextTurn } from 'node:timers/promises';
import http from 'node:http';
import { once } from 'node:events';
import { disconnectSignal } from './cancellation.js';

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

for (const stage of [
	'newPage',
	'guard',
	'viewport',
	'navigation',
	'wait',
	'extraction',
	'heap',
	'finalHeap',
	'screenshot',
] as const) {
	test(
		`disconnect during ${stage} closes the target and releases its permit`,
		{ timeout: 2000 },
		async () => {
			const controller = new AbortController();
			const slots = new Semaphore({ permits: 1, maxQueued: 1 });
			let entered!: () => void;
			const ready = new Promise<void>((resolve) => {
				entered = resolve;
			});
			const pending = () => {
				entered();
				return new Promise<never>(() => {});
			};
			const closes = { page: 0, context: 0 };
			let extractions = 0;
			const page = {
				setRequestInterception: async () => {
					if (stage === 'guard') await pending();
				},
				setViewport: async () => {
					if (stage === 'viewport') await pending();
				},
				goto: async () => {
					if (stage === 'navigation') await pending();
					if (stage === 'wait') entered();
					return { status: () => 200 };
				},
				evaluate: async (_fn: unknown, ...args: unknown[]) => {
					if (args.length === 0) {
						if (stage === 'heap' || stage === 'finalHeap') await pending();
						return 0;
					}
					extractions++;
					if (stage === 'extraction') await pending();
					return { tooLarge: false, content: 'ok' };
				},
				close: async () => {
					closes.page++;
				},
				screenshot: pending,
				on: () => {},
			};
			const context = {
				newPage: async () => {
					if (stage === 'newPage') await pending();
					return page;
				},
				close: async () => {
					closes.context++;
				},
				on: () => {},
			};
			const browser = {
				createBrowserContext: async () => context,
			} as unknown as pup.Browser;
			const result = slots.withPermit(
				1000,
				() =>
					renderPageWithBrowser(
						browser,
						'https://example.com/',
						stage === 'screenshot' ? 'screenshot' : 'text',
						{ waitAfterLoaded: stage === 'wait' || stage === 'heap' ? 60 : 0 },
						controller.signal,
					),
				controller.signal,
			);
			const rejected = assert.rejects(result, { name: 'AbortError' });
			await ready;
			// Let the navigation continuation enter its post-load delay.
			await nextTurn();
			const next = slots.acquire(1000);
			assert.equal(slots.queued, 1);
			controller.abort();
			await rejected;
			await next;
			assert.deepEqual(closes, {
				page: stage === 'newPage' ? 0 : 1,
				context: 1,
			});
			assert.equal(
				extractions,
				stage === 'extraction' || stage === 'finalHeap' ? 1 : 0,
			);
			assert.equal(slots.queued, 0);
			slots.release();
			assert.equal(slots.inFlight, 0);
		},
	);
}

test(
	'disconnect during context creation disposes a late context',
	{ timeout: 2000 },
	async () => {
		const controller = new AbortController();
		let created!: (context: pup.BrowserContext) => void;
		const pending = new Promise<pup.BrowserContext>((resolve) => {
			created = resolve;
		});
		const browser = {
			createBrowserContext: () => pending,
		} as unknown as pup.Browser;
		const result = renderPageWithBrowser(
			browser,
			'https://example.com/',
			'text',
			{},
			controller.signal,
		);
		const rejected = assert.rejects(result, { name: 'AbortError' });
		controller.abort();
		await rejected;
		let closes = 0;
		created({
			close: async () => {
				closes++;
			},
			newPage: () => assert.fail('cancelled render opened a page'),
		} as unknown as pup.BrowserContext);
		await nextTurn();
		assert.equal(closes, 1);
	},
);

test(
	'HTTP disconnect frees an active render slot for the queued request',
	{ timeout: 3000 },
	async (t) => {
		const slots = new Semaphore({ permits: 1, maxQueued: 1 });
		let entered!: () => void;
		const navigating = new Promise<void>((resolve) => {
			entered = resolve;
		});
		let queued!: () => void;
		const waiting = new Promise<void>((resolve) => {
			queued = resolve;
		});
		let contexts = 0;
		let closes = 0;
		const browser = {
			createBrowserContext: async () => {
				const first = ++contexts === 1;
				if (!first)
					assert.equal(
						closes,
						1,
						'dispose the first context before reusing its permit',
					);
				return {
					newPage: async () => ({
						setRequestInterception: async () => {},
						setViewport: async () => {},
						on: () => {},
						goto: async () => {
							if (first) {
								entered();
								await new Promise(() => {});
							}
							return { status: () => 200 };
						},
						evaluate: async (_fn: unknown, ...args: unknown[]) =>
							args.length ? { tooLarge: false, content: 'ok' } : 0,
						close: async () => {},
					}),
					close: async () => {
						closes++;
					},
					on: () => {},
				};
			},
		} as unknown as pup.Browser;
		const errors: unknown[] = [];
		const server = http.createServer(async (req, res) => {
			const signal = disconnectSignal(res);
			req.resume();
			try {
				const rendering = slots.withPermit(
					2000,
					() =>
						renderPageWithBrowser(
							browser,
							'https://example.com/',
							'text',
							{},
							signal,
						),
					signal,
				);
				if (slots.queued) queued();
				const result = await rendering;
				res.end(result.body);
			} catch (error) {
				if (!signal.aborted) errors.push(error);
				res.destroy();
			}
		});
		server.listen(0, '127.0.0.1');
		await once(server, 'listening');
		t.after(() => {
			server.closeAllConnections();
			server.close();
		});
		const address = server.address();
		assert.ok(address && typeof address !== 'string');
		const url = `http://127.0.0.1:${address.port}`;
		const first = http.get(url, { agent: false });
		first.on('error', () => {});
		t.after(() => first.destroy());
		await navigating;
		const second = http.get(url, { agent: false });
		t.after(() => second.destroy());
		const response = once(second, 'response');
		await waiting;
		assert.equal(contexts, 1);
		first.destroy();
		const [incoming] = await response;
		let body = '';
		for await (const chunk of incoming) body += chunk;
		assert.equal(body, 'ok');
		assert.equal(contexts, 2);
		assert.equal(closes, 2);
		assert.equal(slots.inFlight, 0);
		assert.equal(slots.queued, 0);
		assert.deepEqual(errors, []);
	},
);

test('an already disconnected caller never creates a context', async () => {
	const browser = {
		createBrowserContext: () => assert.fail('created a context'),
	} as unknown as pup.Browser;
	await assert.rejects(
		renderPageWithBrowser(
			browser,
			'https://example.com/',
			'text',
			{},
			AbortSignal.abort(),
		),
		{ name: 'AbortError' },
	);
});

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
 * A browser that opens a page normally and then fails the navigation with
 * `error`, which is how a healthy sidecar meets a page it cannot load.
 * Records page and context closes so a leak shows up as a failing assertion.
 */
function browserWhoseNavigationFails(error: Error): {
	browser: pup.Browser;
	closes: { page: number; context: number };
} {
	const closes = { page: 0, context: 0 };
	const page = {
		setRequestInterception: async () => {},
		goto: async () => {
			throw error;
		},
		// the heap monitor polls this
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

	return { browser, closes };
}

/** A Chrome navigation failure, which arrives as a plain `Error`. */
function netError(code: string): Error {
	return new Error(`${code} at https://example.com/`);
}

/**
 * The other half of the pair, so the tests above are not satisfied by "throw
 * everything". A site that refuses the connection is something we *did*
 * observe, so it comes back as a status rather than an exception -- and it is
 * a 503, the very code the sidecar-failure path must not borrow.
 */
test('an unreachable remote site is reported, not thrown', async () => {
	const { browser, closes } = browserWhoseNavigationFails(
		netError('net::ERR_CONNECTION_REFUSED'),
	);

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

/**
 * A certificate the site presents badly is likewise the site's doing, and the
 * contract is entitled to see it: a contract may legitimately branch on "that
 * host is not serving valid TLS".
 */
test('a certificate error is reported, not thrown', async () => {
	const { browser, closes } = browserWhoseNavigationFails(
		netError('net::ERR_CERT_AUTHORITY_INVALID'),
	);

	const result = await render(browser);

	assert.equal(result.status, 495);
	assert.match(String(result.body), /SSL certificate error/);
	assert.deepEqual(closes, { page: 1, context: 1 });
});

// -- the two ambiguous cases stay on the observation channel ----------

/**
 * Regression guard, not a description of a certainty.
 *
 * A name that does not resolve is either a domain that does not exist -- the
 * site -- or a resolver of *ours* that is broken. The two are indistinguishable
 * from inside one validator, and resolving that ambiguity is exactly what
 * several validators and an equivalence principle are for: pre-judging it here
 * would suppress the disagreement consensus exists to reconcile. So it stays
 * returned and contract-visible, and this test fails if someone later
 * "helpfully" reclassifies it as ours the way `ERR_INTERNET_DISCONNECTED` is.
 */
test('a name that does not resolve is still reported, not thrown', async () => {
	const { browser, closes } = browserWhoseNavigationFails(
		netError('net::ERR_NAME_NOT_RESOLVED'),
	);

	const result = await render(browser);

	assert.equal(result.status, 502);
	assert.match(String(result.body), /DNS resolution failed/);
	assert.equal(
		statusIsGood(result.status),
		false,
		'the module must see this as a failed load',
	);
	assert.deepEqual(closes, { page: 1, context: 1 });
});

/**
 * The same guard for the other ambiguous case: a navigation timeout is either
 * a slow site or a browser of ours that wedged, and it is not this process's
 * place to decide which. Returned, not thrown.
 */
test('a navigation timeout is still reported, not thrown', async () => {
	const { browser, closes } = browserWhoseNavigationFails(
		new TimeoutError('Navigation timeout of 30000 ms exceeded'),
	);

	const result = await render(browser);

	assert.equal(result.status, 408);
	assert.match(String(result.body), /Navigation timeout/);
	assert.equal(
		statusIsGood(result.status),
		false,
		'the module must see this as a failed load',
	);
	assert.deepEqual(closes, { page: 1, context: 1 });
});

// -- and the one navigation error that is ours ------------------------

/**
 * `net::ERR_INTERNET_DISCONNECTED` is not ambiguous: the request never left
 * this host, so there is no observation to reconcile with anyone. It must
 * leave by the thrown channel, which the caller turns into a `500` and the
 * module into a fatal error, so the validator abstains.
 *
 * The message must say so in its own words. It is all that reaches the `500`
 * body, and from there the module error's `ctx` and the operator-facing abort
 * string; "webdriver unavailable" would send an operator to look at a browser
 * that is working fine.
 */
test('a local network outage is thrown, not reported', async () => {
	const { browser, closes } = browserWhoseNavigationFails(
		netError('net::ERR_INTERNET_DISCONNECTED'),
	);

	await assert.rejects(render(browser), (e: Error) => {
		assert.match(
			e.message,
			/local network/i,
			'the message must name the fault an operator has to go and fix',
		);
		assert.match(e.message, /net::ERR_INTERNET_DISCONNECTED/);
		assert.doesNotMatch(
			e.message,
			/webdriver unavailable/i,
			'the browser answered, so it must not be reported as the broken part',
		);
		return true;
	});
	assert.deepEqual(
		closes,
		{ page: 1, context: 1 },
		'the page and its context must still be torn down',
	);
});
