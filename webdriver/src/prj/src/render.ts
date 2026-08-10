/**
 * Rendering a page with an already-running browser.
 *
 * Split out of `index.ts` so it can be exercised without launching Chromium or
 * binding a port: importing `index.ts` starts the HTTP server, and importing
 * `browser/chrome.js` launches a browser at module scope.
 *
 * The contract this file upholds: everything it returns is an observation
 * about the *page*, reported as a status the caller puts in `Resulting-Status`
 * alongside a `200`. A failure of the sidecar itself is reported the same way
 * rather than raised, because the caller turns a raised error into a `500`,
 * and the module classifies a `500` from us as fatal.
 */

import type * as pup from 'puppeteer-core';

import * as logger from './logging.js';
import * as ssrf from './ssrf.js';
import { envDurationMs, envInt, formatDurationMs } from './duration.js';

interface NavigationOptions {
	waitUntil?: pup.PuppeteerLifeCycleEvent;
	timeout?: number;
}

export interface RenderOptions {
	loadTimeout?: number;
	waitAfterLoaded?: number;
	waitUntil?: pup.PuppeteerLifeCycleEvent;
	maxPageHeapMB?: number;
}

const STATUS_I_AM_A_TEAPOT = 418;

/**
 * Reported when this sidecar cannot render at all, as opposed to the page
 * failing to load. It joins the codes `getNavigationErrorStatus` already
 * returns for an unreachable target, so the module treats it as a non-fatal
 * `WEBPAGE_LOAD_FAILED` and the contract can catch it.
 */
export const STATUS_SERVICE_UNAVAILABLE = 503;

const DEFAULT_MAX_PAGE_HEAP_MB = envInt('GVM_WEBDRIVER_MAX_PAGE_HEAP_MB', 1024);

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

function getNavigationErrorStatus(error: any): number {
	if (error.name === 'TimeoutError') {
		return 408; // Request Timeout
	} else if (error.message?.includes('net::ERR_NAME_NOT_RESOLVED')) {
		return 502; // Bad Gateway
	} else if (error.message?.includes('net::ERR_CONNECTION_REFUSED')) {
		return 503; // Service Unavailable
	} else if (error.message?.includes('net::ERR_CERT_')) {
		return 495; // SSL Certificate Error
	} else if (error.message?.includes('net::ERR_INTERNET_DISCONNECTED')) {
		return 503; // Service Unavailable
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
	} else if (error.message?.includes('net::ERR_INTERNET_DISCONNECTED')) {
		return 'No internet connection';
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
		const statusCode = getNavigationErrorStatus(navigationError);
		const errorMessage = getNavigationErrorMessage(navigationError);
		return { status: statusCode, error: errorMessage };
	}
}

async function asText(page: pup.Page) {
	const bodyText = await page.evaluate(() => {
		return document.body.innerText;
	});

	return normalizeWhitespace(bodyText);
}

async function asHTML(page: pup.Page) {
	return await page.evaluate(() => {
		return document.body.innerHTML;
	});
}

async function asScreenshot(page: pup.Page) {
	return await page.screenshot();
}

export class HeapLimitExceeded extends Error {
	constructor(heapMB: number, maxHeapMB: number) {
		super(`Page JS heap ${heapMB.toFixed(1)}MB exceeds limit ${maxHeapMB}MB`);
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
 * in one place with the context closed if the page never materializes.
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
	} = options;

	// No contract input reaches `newRenderTarget` -- the target URL is not
	// passed to it -- so anything it throws is this sidecar failing, never an
	// observation about the page. Reported on the same channel a failed page
	// load uses (a `200` carrying `Resulting-Status`) rather than being left to
	// escape into the caller's `500` handler, because a `500` from us is
	// classified as a fatal internal error that aborts the whole contract run.
	let renderTarget: { context: pup.BrowserContext; page: pup.Page };
	try {
		renderTarget = await newRenderTarget(browserInstance);
	} catch (error) {
		logger.log('error', 'could not open a page for rendering', {
			url: targetUrl,
			name: (error as Error).name,
			error: (error as Error).message,
		});
		return {
			status: STATUS_SERVICE_UNAVAILABLE,
			body: `Webdriver unavailable: ${(error as Error).message}`,
		};
	}
	const { context, page } = renderTarget;

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
					data = await asText(page);
					break;
				case 'html':
					data = await asHTML(page);
					break;
				case 'screenshot':
					data = await asScreenshot(page);
					break;
				default:
					data = 'Invalid mode';
			}

			return { status: statusCode, body: data };
		});
	} catch (e) {
		if (e instanceof HeapLimitExceeded) {
			logger.log('warn', 'page heap limit exceeded', {
				url: targetUrl,
				error: e.message,
			});
			return { status: 507, body: e.message };
		}
		throw e;
	} finally {
		// Close the page and its context together; the context teardown is what
		// actually discards the per-request browsing state.
		await Promise.allSettled([page.close(), context.close()]);
	}
}
