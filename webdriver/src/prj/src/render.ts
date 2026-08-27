/**
 * Rendering a page with an already-running browser.
 *
 * Split out of `index.ts` so it can be exercised without launching Chromium or
 * binding a port: importing `index.ts` starts the HTTP server, and importing
 * `browser/chrome.js` launches a browser at module scope.
 *
 * The contract this file upholds: everything it *returns* is an observation
 * about the page, reported as a status the caller puts in `Resulting-Status`
 * alongside a `200`, which the module surfaces to the contract as a catchable
 * `WEBPAGE_LOAD_FAILED`. Everything it *throws* is this sidecar failing. The
 * caller turns a throw into a `500`, which the module classifies as fatal, so
 * the validator abstains rather than voting on a page it never observed. "This
 * sidecar failing" includes the host it runs on: a machine with no network is
 * ours too, and `LocalNetworkUnavailable` below is that case.
 *
 * The two channels must not be mixed. Folding a sidecar failure into a
 * `Resulting-Status` -- 503 is the tempting one, since a page whose host
 * refused the connection already reports 503 -- would make a broken sidecar
 * indistinguishable from a real observation, and let the validator vote on it.
 */

import type * as pup from 'puppeteer-core';

import * as logger from './logging.js';
import * as ssrf from './ssrf.js';
import {
	envDurationMs,
	envInt,
	envSize,
	formatDurationMs,
} from './duration.js';

interface NavigationOptions {
	waitUntil?: pup.PuppeteerLifeCycleEvent;
	timeout?: number;
}

export interface RenderOptions {
	loadTimeout?: number;
	waitAfterLoaded?: number;
	waitUntil?: pup.PuppeteerLifeCycleEvent;
	maxPageHeapMB?: number;
	maxResponseChars?: number;
}

const STATUS_I_AM_A_TEAPOT = 418;
const STATUS_INSUFFICIENT_STORAGE = 507;

const DEFAULT_MAX_PAGE_HEAP_MB = envInt('GVM_WEBDRIVER_MAX_PAGE_HEAP_MB', 1024);

// Counted in characters, which is what bounds the string materialized in this
// process. One character encodes to at most 4 UTF-8 bytes on the wire, and
// occupies 2 bytes in V8 for the common (BMP) case.
const MAX_RESPONSE_CHARS = envSize('GVM_WEBDRIVER_MAX_RESPONSE', '5MiB');

const MAX_WAIT_AFTER_LOADED_MS = envDurationMs(
	'GVM_WEBDRIVER_MAX_WAIT_AFTER_LOADED',
	'60s',
);

function normalizeWhitespace(contents: string): string {
	return contents
		.split('\n')
		.map((line) => line.trim().replace(/\s+/g, ' '))
		.join('\n')
		.replace(/\n{2,}/g, '\n\n');
}

/**
 * This machine has no network at all, which is not something we observed about
 * the page: `net::ERR_INTERNET_DISCONNECTED` is Chrome saying the request never
 * left the host.
 *
 * It is the one navigation error that leaves through the thrown channel. The
 * others are ambiguous between the site and us, and stay returned: a name that
 * does not resolve is either a domain that does not exist or a resolver of ours
 * that is broken, and a timeout is either a slow site or a browser of ours that
 * is wedged. Settling that is what several validators and an equivalence
 * principle are for, so deciding it here would suppress the very disagreement
 * consensus exists to reconcile. This case is different only because it is
 * unambiguous -- there is no observation to reconcile.
 *
 * The wording says *local network* rather than *webdriver*: the browser and
 * this sidecar are answering normally, and the `500` the caller sends carries
 * only this text.
 */
export class LocalNetworkUnavailable extends Error {
	constructor() {
		super(
			'Local network fault: this host has no network route ' +
				'(net::ERR_INTERNET_DISCONNECTED). The browser answered, so it is ' +
				'the local network that failed rather than the webdriver, and ' +
				'nothing about the page was observed.',
		);
		this.name = 'LocalNetworkUnavailable';
	}
}

/**
 * What we observed happening to the page, as a status for `Resulting-Status`.
 *
 * `net::ERR_INTERNET_DISCONNECTED` is deliberately absent: it is ours rather
 * than the page's, so `navigateToPage` throws it before reaching here. See
 * [`LocalNetworkUnavailable`] for why the neighbouring cases are NOT treated
 * the same way.
 */
function getNavigationErrorStatus(error: any): number {
	if (error.name === 'TimeoutError') {
		// Ambiguous on purpose: a slow site and a browser of ours that wedged
		// are indistinguishable here, so the validators settle it, not us.
		return 408; // Request Timeout
	} else if (error.message?.includes('net::ERR_NAME_NOT_RESOLVED')) {
		// Ambiguous in the same way: a domain that does not exist and a broken
		// resolver of ours look identical from this side.
		return 502; // Bad Gateway
	} else if (error.message?.includes('net::ERR_CONNECTION_REFUSED')) {
		return 503; // Service Unavailable
	} else if (error.message?.includes('net::ERR_CERT_')) {
		return 495; // SSL Certificate Error
	} else if (error.message?.includes('net::ERR_BLOCKED_BY_CLIENT')) {
		return 403; // Forbidden (SSRF guard)
	}
	return STATUS_I_AM_A_TEAPOT; // Unknown error
}

function getNavigationErrorMessage(error: any): string {
	if (error.name === 'TimeoutError') {
		return 'Navigation timeout';
	} else if (error.message?.includes('net::ERR_NAME_NOT_RESOLVED')) {
		return 'DNS resolution failed';
	} else if (error.message?.includes('net::ERR_CONNECTION_REFUSED')) {
		return 'Connection refused';
	} else if (error.message?.includes('net::ERR_CERT_')) {
		return 'SSL certificate error';
	} else if (error.message?.includes('net::ERR_BLOCKED_BY_CLIENT')) {
		return 'Blocked by SSRF guard: address not allowed';
	}
	return `Navigation error: ${error.message || 'Unknown error'}`;
}

async function navigateToPage(
	page: pup.Page,
	targetUrl: string,
	options: NavigationOptions = {},
): Promise<{ status: number; error?: string; response?: pup.HTTPResponse }> {
	const { waitUntil = 'domcontentloaded', timeout = 30000 } = options;

	try {
		const response = await page.goto(targetUrl, {
			waitUntil,
			timeout,
		});

		if (!response) {
			return {
				status: STATUS_I_AM_A_TEAPOT,
				error: 'Navigation did not result in a valid HTTP response',
			};
		}

		return { status: response.status(), response };
	} catch (navigationError: any) {
		logger.log('error', 'navigation Error', navigationError);
		if (navigationError.message?.includes('net::ERR_INTERNET_DISCONNECTED')) {
			// Thrown, not returned: this one is our fault, and the caller's `500`
			// is what makes the module abort, so the validator abstains rather
			// than voting on a page the request never reached.
			throw new LocalNetworkUnavailable();
		}
		const statusCode = getNavigationErrorStatus(navigationError);
		const errorMessage = getNavigationErrorMessage(navigationError);
		return { status: statusCode, error: errorMessage };
	}
}

/**
 * Read `innerText` or `innerHTML`, rejecting oversized documents *in the page*.
 *
 * The size test has to happen on the browser side. Pulling the string across
 * first and measuring it here would already have paid the memory cost we are
 * trying to avoid -- this process would hold the whole document no matter what
 * any downstream limit says.
 */
async function extractBounded(
	page: pup.Page,
	kind: 'innerText' | 'innerHTML',
	maxChars: number,
): Promise<string> {
	const extracted = await page.evaluate(
		(k, limit) => {
			const raw =
				k === 'innerText' ? document.body.innerText : document.body.innerHTML;
			return raw.length > limit
				? { tooLarge: true as const, length: raw.length }
				: { tooLarge: false as const, content: raw };
		},
		kind,
		maxChars,
	);

	if (extracted.tooLarge) {
		throw new ResponseLimitExceeded(extracted.length, maxChars);
	}
	return extracted.content;
}

async function asText(page: pup.Page, maxChars: number) {
	return normalizeWhitespace(await extractBounded(page, 'innerText', maxChars));
}

async function asHTML(page: pup.Page, maxChars: number) {
	return await extractBounded(page, 'innerHTML', maxChars);
}

async function asScreenshot(page: pup.Page, maxChars: number) {
	// Screenshots are viewport-sized rather than full-page, so this is a
	// backstop rather than a limit anyone should reach.
	const image = await page.screenshot();
	if (image.length > maxChars) {
		throw new ResponseLimitExceeded(image.length, maxChars);
	}
	return image;
}

export class HeapLimitExceeded extends Error {
	constructor(heapMB: number, maxHeapMB: number) {
		super(`Page JS heap ${heapMB.toFixed(1)}MB exceeds limit ${maxHeapMB}MB`);
	}
}

export class ResponseLimitExceeded extends Error {
	constructor(size: number, maxSize: number) {
		super(`Rendered response ${size} exceeds limit ${maxSize}`);
	}
}

const HEAP_CHECK_INTERVAL_MS = envInt(
	'GVM_WEBDRIVER_HEAP_CHECK_INTERVAL_MS',
	200,
);

async function withHeapMonitor<T>(
	page: pup.Page,
	maxHeapMB: number,
	fn: () => Promise<T>,
): Promise<T> {
	let stopped = false;
	let stopResolve: () => void;
	const stopPromise = new Promise<void>((r) => {
		stopResolve = r;
	});
	const monitor = (async () => {
		while (!stopped) {
			await new Promise((r) => setTimeout(r, HEAP_CHECK_INTERVAL_MS));
			if (stopped) break;
			let totalMB: number;
			try {
				totalMB = await page.evaluate(
					() => (performance as any).memory?.totalJSHeapSize / 1024 / 1024,
				);
			} catch {
				await stopPromise;
				return;
			}
			logger.log('debug', 'heap monitor check', {
				heapMB: totalMB.toFixed(1),
			});
			if (totalMB > maxHeapMB) {
				throw new HeapLimitExceeded(totalMB, maxHeapMB);
			}
		}
	})();

	try {
		const result = await Promise.race([
			fn(),
			monitor.then(() => undefined as never),
		]);
		let totalMB: number;
		try {
			totalMB = await page.evaluate(
				() => (performance as any).memory?.totalJSHeapSize / 1024 / 1024,
			);
		} catch {
			return result;
		}
		if (totalMB > maxHeapMB) {
			throw new HeapLimitExceeded(totalMB, maxHeapMB);
		}
		return result;
	} finally {
		stopped = true;
		stopResolve!();
		await monitor.catch(() => {});
	}
}

export function statusIsGood(status: number): boolean {
	return (status >= 200 && status < 300) || status === 304;
}

/**
 * Acquire the per-render browsing context and its page.
 *
 * Both calls are CDP round-trips (`Target.createBrowserContext`,
 * `Target.createTarget`) that can hang or fail on their own, so they are kept
 * in one place with the context closed if the page never materializes. The
 * failure itself is re-raised, not translated -- see the file header.
 */
async function newRenderTarget(
	browserInstance: pup.Browser,
): Promise<{ context: pup.BrowserContext; page: pup.Page }> {
	// Each render runs in its own browser context so cookies, localStorage,
	// IndexedDB, service workers and HSTS state never leak between tenants
	// sharing this long-lived browser.
	const context = await browserInstance.createBrowserContext();
	try {
		return { context, page: await context.newPage() };
	} catch (error) {
		await context.close().catch(() => {});
		// Logged here as well as re-raised: this is the last point that still
		// knows the failure was ours rather than the page's, and the `500` the
		// caller sends carries only a message.
		logger.log('error', 'could not open a page for rendering', {
			name: (error as Error).name,
			error: (error as Error).message,
		});
		throw error;
	}
}

export async function renderPageWithBrowser(
	browserInstance: pup.Browser,
	targetUrl: string,
	mode: 'text' | 'html' | 'screenshot',
	options: RenderOptions = {},
): Promise<{ status: number; body: any }> {
	const {
		loadTimeout = 30000,
		waitAfterLoaded = 0,
		waitUntil = 'domcontentloaded',
		maxPageHeapMB = DEFAULT_MAX_PAGE_HEAP_MB,
		maxResponseChars = MAX_RESPONSE_CHARS,
	} = options;

	// No contract input reaches `newRenderTarget` -- the target URL is not
	// passed to it -- so anything it throws is this sidecar failing, never an
	// observation about the page. It is therefore allowed to propagate: the
	// caller turns it into a `500`, which the module classifies as fatal, and
	// the validator abstains instead of reporting a page outcome it never
	// observed. It must NOT be folded into a `Resulting-Status`, which is the
	// channel reserved for what actually happened to the page.
	const { context, page } = await newRenderTarget(browserInstance);

	try {
		await ssrf.installSsrfGuard(page);
		context.on('targetcreated', async (target) => {
			const p = await target.page();
			if (p && p !== page) {
				await ssrf.installSsrfGuard(p);
			}
		});
		page.setViewport({ width: 1920 / 2, height: 1080 / 2 });

		return await withHeapMonitor(page, maxPageHeapMB, async () => {
			const navigationResult = await navigateToPage(page, targetUrl, {
				waitUntil,
				timeout: loadTimeout,
			});

			if (navigationResult.error) {
				return {
					status: navigationResult.status,
					body: navigationResult.error,
				};
			}

			const statusCode = navigationResult.status;

			if (statusIsGood(statusCode) && waitAfterLoaded > 0) {
				let waitMs = Math.floor(waitAfterLoaded * 1000);
				if (waitMs > MAX_WAIT_AFTER_LOADED_MS) {
					logger.log('warn', 'waitAfterLoaded clamped to maximum', {
						url: targetUrl,
						requested: formatDurationMs(waitMs),
						max: formatDurationMs(MAX_WAIT_AFTER_LOADED_MS),
					});
					waitMs = MAX_WAIT_AFTER_LOADED_MS;
				}
				await new Promise((resolve) => setTimeout(resolve, waitMs));
			}

			let data;
			switch (mode) {
				case 'text':
					data = await asText(page, maxResponseChars);
					break;
				case 'html':
					data = await asHTML(page, maxResponseChars);
					break;
				case 'screenshot':
					data = await asScreenshot(page, maxResponseChars);
					break;
				default:
					data = 'Invalid mode';
			}

			return { status: statusCode, body: data };
		});
	} catch (e) {
		// Both limits are a property of the page rather than of this sidecar --
		// the same document would exceed them anywhere -- so they belong on the
		// returned channel, like any other page the contract could not load.
		if (e instanceof HeapLimitExceeded) {
			logger.log('warn', 'page heap limit exceeded', {
				url: targetUrl,
				error: e.message,
			});
		} else if (e instanceof ResponseLimitExceeded) {
			logger.log('warn', 'rendered response limit exceeded', {
				url: targetUrl,
				mode,
				error: e.message,
			});
		} else {
			throw e;
		}

		return { status: STATUS_INSUFFICIENT_STORAGE, body: e.message };
	} finally {
		// Close the page and its context together; the context teardown is what
		// actually discards the per-request browsing state.
		await Promise.allSettled([page.close(), context.close()]);
	}
}
