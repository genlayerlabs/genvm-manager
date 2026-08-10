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
import { envDurationMs, formatDurationMs } from './duration.js';

const program = new Command();
program
	.name('puppeteer-webdriver')
	.description('Puppeteer-based web scraping server')
	.version('1.0.0')
	.option('-p, --port <number>', 'port to run the server on', '4444')
	.parse();

const options = program.opts();

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
): Promise<{ status: number; body: any }> {
	const browserManager = await chromeBrowser.INSTANCE;
	const browserInstance = browserManager.getBrowser();
	try {
		return renderPageWithBrowser(
			browserInstance.get(),
			targetUrl,
			mode,
			options,
		);
	} finally {
		browserInstance.close();
	}
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

		const result = await renderPage(targetUrl, mode, options);

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
		res.writeHead(500, { 'Content-Type': 'application/json' });
		res.end(
			JSON.stringify({
				error: 'Internal server error',
				message: (error as Error).message,
			}),
		);
	}
}

async function handleHealthcheck(parsedUrl: URL, res: http.ServerResponse) {
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
		const result = await renderPage(targetUrl, mode, {});
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
		await handleHealthcheck(parsedUrl, res);
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
