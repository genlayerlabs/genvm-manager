import puppeteer, * as pup from 'puppeteer-core';
import http from 'http';
import { Command } from 'commander';

import * as logger from './logging.js';
import * as chromeBrowser from './browser/chrome.js';
import * as ssrf from './ssrf.js';
import {
	envDurationMs,
	envInt,
	envPositiveInt,
	formatDurationMs,
} from './duration.js';
import {
	Semaphore,
	SemaphoreAborted,
	SemaphoreQueueFull,
	SemaphoreTimeout,
} from './semaphore.js';

interface NavigationOptions {
	waitUntil?: pup.PuppeteerLifeCycleEvent;
	timeout?: number;
}

interface RenderOptions {
	loadTimeout?: number;
	waitAfterLoaded?: number;
	waitUntil?: pup.PuppeteerLifeCycleEvent;
	maxPageHeapMB?: number;
	maxResponseChars?: number;
}

const program = new Command();
program
	.name('puppeteer-webdriver')
	.description('Puppeteer-based web scraping server')
	.version('1.0.0')
	.option('-p, --port <number>', 'port to run the server on', '4444')
	.parse();

const options = program.opts();

const STATUS_I_AM_A_TEAPOT = 418;
const STATUS_INSUFFICIENT_STORAGE = 507;
const STATUS_SERVICE_UNAVAILABLE = 503;

const DEFAULT_MAX_PAGE_HEAP_MB = envInt('GVM_WEBDRIVER_MAX_PAGE_HEAP_MB', 1024);

// Peak memory of this service is bounded by the product of the two limits
// below, so both have to exist for either to mean anything:
//
//   peak ~= MAX_CONCURRENT_RENDERS * (page heap + extracted response) + browser baseline
//
// Every in-flight render is guaranteed its full per-request allocation. A
// caller that has a slot is never squeezed by how many other callers are
// active; callers beyond the limit queue instead.
const MAX_CONCURRENT_RENDERS = envPositiveInt(
	'GVM_WEBDRIVER_MAX_CONCURRENT_RENDERS',
	4,
);

// Counted in characters, which is what bounds the string materialized in this
// process. One character encodes to at most 4 UTF-8 bytes on the wire, and
// occupies 2 bytes in V8 for the common (BMP) case.
const MAX_RESPONSE_MB = envPositiveInt('GVM_WEBDRIVER_MAX_RESPONSE_MB', 32);
const MAX_RESPONSE_CHARS = MAX_RESPONSE_MB * 1024 * 1024;

// Waiting callers are cheap but not free: each holds a live connection. Past
// this depth the service is not going to work through the backlog before
// callers give up anyway, so refusing immediately beats refusing slowly.
const MAX_RENDER_QUEUE = envInt('GVM_WEBDRIVER_MAX_RENDER_QUEUE', 64);

// Kept well below the caller's own transport timeout: a queue wait that
// outlives it converts a condition we can report cleanly into a transport
// failure, which callers treat far more harshly.
const RENDER_QUEUE_TIMEOUT_MS = envDurationMs(
	'GVM_WEBDRIVER_RENDER_QUEUE_TIMEOUT',
	'120s',
);

const renderSlots = new Semaphore({
	permits: MAX_CONCURRENT_RENDERS,
	maxQueued: MAX_RENDER_QUEUE,
});

const MAX_WAIT_AFTER_LOADED_MS = envDurationMs(
	'GVM_WEBDRIVER_MAX_WAIT_AFTER_LOADED',
	'60s',
);

const HEALTHCHECK_CACHE_DURATION_MS = envDurationMs(
	'GVM_WEBDRIVER_HEALTHCHECK_CACHE_DURATION',
	'5m',
);
let lastSuccessfulRenderTime: number = 0;

function updateLastSuccessfulRenderTime() {
	lastSuccessfulRenderTime = Date.now();
}

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

/**
 * Read `innerText` or `innerHTML`, rejecting oversized documents *in the page*.
 *
 * The size test has to happen on the browser side. Pulling the string across
 * first and measuring it here would already have paid the memory cost we are
 * trying to avoid — this process would hold the whole document no matter what
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

class HeapLimitExceeded extends Error {
	constructor(heapMB: number, maxHeapMB: number) {
		super(`Page JS heap ${heapMB.toFixed(1)}MB exceeds limit ${maxHeapMB}MB`);
	}
}

class ResponseLimitExceeded extends Error {
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

function statusIsGood(status: number): boolean {
	return (status >= 200 && status < 300) || status === 304;
}

async function renderPage(
	targetUrl: string,
	mode: 'text' | 'html' | 'screenshot',
	options: RenderOptions = {},
	signal?: AbortSignal,
): Promise<{ status: number; body: any }> {
	// The slot is taken before the browser is touched, so a queued request costs
	// nothing but the open connection.
	try {
		await renderSlots.acquire(RENDER_QUEUE_TIMEOUT_MS, signal);
	} catch (e) {
		// Saturation escapes as a transport failure rather than a render result,
		// and the distinction is deliberate. How busy this host happens to be is
		// not a property of the page, so it must not reach the contract: two
		// hosts under different load would otherwise return different answers
		// for the same request and each would look legitimate. Failing at the
		// transport level keeps the outcome an honest "this host could not run
		// it" instead of a fabricated observation about the web.
		if (e instanceof SemaphoreTimeout || e instanceof SemaphoreQueueFull) {
			logger.log('warn', 'render rejected: saturated', {
				url: targetUrl,
				reason: e.name,
				inFlight: renderSlots.inFlight,
				queued: renderSlots.queued,
				timeout: formatDurationMs(RENDER_QUEUE_TIMEOUT_MS),
			});
		} else if (e instanceof SemaphoreAborted) {
			logger.log('debug', 'render abandoned while queued', { url: targetUrl });
		}
		throw e;
	}

	try {
		const browserManager = await chromeBrowser.INSTANCE;
		const browserInstance = browserManager.getBrowser();
		try {
			return await renderPageWithBrowser(
				browserInstance.get(),
				targetUrl,
				mode,
				options,
			);
		} finally {
			browserInstance.close();
		}
	} finally {
		renderSlots.release();
	}
}

async function renderPageWithBrowser(
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

	// Each render runs in its own browser context so cookies, localStorage,
	// IndexedDB, service workers and HSTS state never leak between tenants
	// sharing this long-lived browser.
	const context = await browserInstance.createBrowserContext();

	// `newPage` fails when the shared browser is under pressure, and the cleanup
	// below only runs once execution is inside the try. Without this the context
	// survives for the browser's whole lifetime — and since pressure is exactly
	// what makes `newPage` fail, the response to it would be to leak more.
	let page: pup.Page;
	try {
		page = await context.newPage();
	} catch (e) {
		await context.close().catch(() => {});
		throw e;
	}

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
		// Both are reported through the status body rather than as transport
		// failures, because both are properties of the page itself: any host
		// with the same configuration reaches the same verdict, so it is a real
		// observation the contract is entitled to see and handle. Contrast the
		// saturation checks in `renderPage`, which depend on this host's load
		// and so must never reach the contract.
		//
		// This does mean the two limits have to be configured identically
		// everywhere; a host that disagrees about them disagrees about results.
		if (e instanceof HeapLimitExceeded) {
			logger.log('warn', 'page heap limit exceeded', {
				url: targetUrl,
				error: e.message,
			});
			return { status: STATUS_INSUFFICIENT_STORAGE, body: e.message };
		}
		if (e instanceof ResponseLimitExceeded) {
			logger.log('warn', 'rendered response limit exceeded', {
				url: targetUrl,
				mode,
				error: e.message,
			});
			return { status: STATUS_INSUFFICIENT_STORAGE, body: e.message };
		}
		throw e;
	} finally {
		// Close the page and its context together; the context teardown is what
		// actually discards the per-request browsing state.
		await Promise.allSettled([page.close(), context.close()]);
	}
}

/**
 * Fires when the caller goes away before it has been answered, so a request
 * nobody is waiting for stops occupying a queue slot.
 */
function disconnectSignal(
	req: http.IncomingMessage,
	res: http.ServerResponse,
): AbortSignal {
	const controller = new AbortController();
	req.on('close', () => {
		if (!res.writableEnded) {
			controller.abort();
		}
	});
	return controller.signal;
}

async function handleRenderRequest(
	parsedUrl: URL,
	req: http.IncomingMessage,
	res: http.ServerResponse,
) {
	const query = parsedUrl.searchParams;

	try {
		const targetUrl = query.get('url') ?? '';
		const mode = query.get('mode') as 'text' | 'html' | 'screenshot';

		if (!targetUrl) {
			res.writeHead(400, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({ error: 'Missing url parameter' }));
			return;
		}

		if (!['text', 'html', 'screenshot'].includes(mode)) {
			res.writeHead(400, { 'Content-Type': 'application/json' });
			res.end(
				JSON.stringify({
					error: 'Invalid mode. Must be text, html, or screenshot',
				}),
			);
			return;
		}

		const options: RenderOptions = {
			waitAfterLoaded: parseFloat(query.get('waitAfterLoaded') || '0'),
			loadTimeout: parseInt(query.get('loadTimeout') || '30000'),
			waitUntil:
				(query.get('waitUntil') as pup.PuppeteerLifeCycleEvent) ||
				'domcontentloaded',
			...(query.get('maxPageHeapMB')
				? { maxPageHeapMB: parseInt(query.get('maxPageHeapMB')!) }
				: {}),
		};

		const result = await renderPage(
			targetUrl,
			mode,
			options,
			disconnectSignal(req, res),
		);

		if (statusIsGood(result.status)) {
			updateLastSuccessfulRenderTime();
		}

		res.setHeader('Resulting-Status', result.status.toString());

		if (mode === 'screenshot') {
			res.writeHead(200, { 'Content-Type': 'image/png' });
		} else {
			res.writeHead(200, { 'Content-Type': 'application/json' });
		}
		res.end(result.body);
	} catch (error) {
		if (error instanceof SemaphoreAborted) {
			// The caller hung up while queued; there is nobody left to answer.
			return;
		}
		if (
			error instanceof SemaphoreTimeout ||
			error instanceof SemaphoreQueueFull
		) {
			res.writeHead(STATUS_SERVICE_UNAVAILABLE, {
				'Content-Type': 'application/json',
			});
			res.end(
				JSON.stringify({
					error: 'Service unavailable',
					message: (error as Error).message,
				}),
			);
			return;
		}
		res.writeHead(500, { 'Content-Type': 'application/json' });
		res.end(
			JSON.stringify({
				error: 'Internal server error',
				message: (error as Error).message,
			}),
		);
	}
}

async function handleHealthcheck(
	parsedUrl: URL,
	req: http.IncomingMessage,
	res: http.ServerResponse,
) {
	const now = Date.now();
	const sinceLastSuccessMs = now - lastSuccessfulRenderTime;
	if (sinceLastSuccessMs < HEALTHCHECK_CACHE_DURATION_MS) {
		logger.log('debug', 'healthcheck cached ok', {
			lastSuccessAgo: formatDurationMs(sinceLastSuccessMs),
		});
		res.writeHead(200, { 'Content-Type': 'text/plain' });
		res.end('ok');
		return;
	}

	const query = parsedUrl.searchParams;
	const targetUrl = query.get('url') ?? '';
	const VALID_MODES = ['text', 'html', 'screenshot'] as const;
	const rawMode = query.get('mode');
	const mode = VALID_MODES.find((m) => m === rawMode);

	if (!targetUrl || !mode) {
		logger.log('warn', 'healthcheck missing parameters', {
			url: targetUrl,
			mode: rawMode,
		});
		res.writeHead(400, { 'Content-Type': 'text/plain' });
		res.end('missing url or mode query parameters');
		return;
	}

	logger.log('info', 'healthcheck performing render', {
		url: targetUrl,
		mode,
		sinceLast: formatDurationMs(sinceLastSuccessMs),
		cacheDuration: formatDurationMs(HEALTHCHECK_CACHE_DURATION_MS),
	});
	try {
		const result = await renderPage(
			targetUrl,
			mode,
			{},
			disconnectSignal(req, res),
		);
		if (statusIsGood(result.status)) {
			updateLastSuccessfulRenderTime();
			logger.log('info', 'healthcheck ok', { status: result.status });
			res.writeHead(200, { 'Content-Type': 'text/plain' });
			res.end('ok');
		} else {
			logger.log('warn', 'healthcheck unhealthy', { status: result.status });
			res.writeHead(503, { 'Content-Type': 'text/plain' });
			res.end('unhealthy');
		}
	} catch (error) {
		if (error instanceof SemaphoreAborted) {
			return;
		}
		// Saturation lands here too, and reporting unhealthy is right: the probe
		// only renders when nothing has succeeded for the cache duration, so a
		// host that is both saturated and has completed nothing in that window
		// genuinely is not serving.
		logger.log('error', 'healthcheck error', {
			error: (error as Error).message,
		});
		res.writeHead(503, { 'Content-Type': 'text/plain' });
		res.end('unhealthy');
	}
}

const server = http.createServer(async (req, res) => {
	const parsedUrl = new URL(req.url || '/', 'http://localhost');
	const pathname = parsedUrl.pathname;

	if (pathname === '/render') {
		await handleRenderRequest(parsedUrl, req, res);
	} else if (pathname === '/healthcheck') {
		await handleHealthcheck(parsedUrl, req, res);
	} else if (pathname === '/log-level') {
		if (req.method === 'GET') {
			res.writeHead(200, { 'Content-Type': 'text/plain' });
			res.end(logger.MIN_LEVEL);
			return;
		}
		if (req.method !== 'POST') {
			res.writeHead(405, { 'Content-Type': 'text/plain' });
			res.end('method not allowed');
			return;
		}
		const level = parsedUrl.searchParams.get('level');
		if (!level || !logger.VALID_LEVELS.includes(level as logger.LogLevel)) {
			res.writeHead(400, { 'Content-Type': 'text/plain' });
			res.end(
				`invalid level, must be one of: ${logger.VALID_LEVELS.join(', ')}`,
			);
			return;
		}
		const prev = logger.MIN_LEVEL;
		logger.setMinLevel(level as logger.LogLevel);
		logger.log('info', 'log level changed', { from: prev, to: level });
		res.writeHead(200, { 'Content-Type': 'text/plain' });
		res.end(level);
	} else {
		res.writeHead(404, { 'Content-Type': 'text/plain' });
		res.end(
			'Puppeteer webdriver server. Use /render endpoint with url, mode, waitAfterLoaded, and waitUntil parameters.',
		);
	}
});

const port = parseInt(options.port);

import * as proc from 'process';
import { log } from 'console';

server.listen(port, () => {
	logger.log('info', 'server started', {
		port,
		url: `http://localhost:${port}/`,
		pid: proc.pid,
	});
});
