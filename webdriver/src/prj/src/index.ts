import type * as pup from 'puppeteer-core';
import http from 'http';
import { Command } from 'commander';

import * as logger from './logging.js';
import * as chromeBrowser from './browser/chrome.js';
import {
	renderPageWithBrowser,
	statusIsGood,
	type RenderOptions,
} from './render.js';
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

const program = new Command();
program
	.name('puppeteer-webdriver')
	.description('Puppeteer-based web scraping server')
	.version('1.0.0')
	.option('-p, --port <number>', 'port to run the server on', '4444')
	.parse();

const options = program.opts();

const STATUS_SERVICE_UNAVAILABLE = 503;

// peak_mem ~= MAX_CONCURRENT_RENDERS * (page heap + extracted response) + browser baseline
//
// we deem that by default we are limited by manager permits,
// so this knob is for extra tuning and default 128 is essentially "unlimited"
const MAX_CONCURRENT_RENDERS = envPositiveInt(
	'GVM_WEBDRIVER_MAX_CONCURRENT_RENDERS',
	128,
);

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

const HEALTHCHECK_CACHE_DURATION_MS = envDurationMs(
	'GVM_WEBDRIVER_HEALTHCHECK_CACHE_DURATION',
	'5m',
);
let lastSuccessfulRenderTime: number = 0;

function updateLastSuccessfulRenderTime() {
	lastSuccessfulRenderTime = Date.now();
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
		// How loaded this host happens to be is not a property of the page, so it
		// must not reach the contract: two hosts under different load would
		// answer the same request differently, both answers looking legitimate.
		// A transport failure says plainly that this host could not run it.
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
					message: error.message,
				}),
			);
			return;
		}
		// Everything still reaching here is this sidecar failing, not the page:
		// page outcomes are *returned* as a status. The module reads this `500`
		// as a fatal `WEBDRIVER_UNAVAILABLE` and the validator abstains, so leave
		// a local trace -- otherwise the only record is the message below.
		logger.log('error', 'render request failed', {
			url: query.get('url') ?? '',
			error: (error as Error).message,
		});
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
