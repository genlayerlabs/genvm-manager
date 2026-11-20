import puppeteer, * as pup from 'puppeteer-core';
import * as logger from '../logging.js';
import * as util from 'util';
import * as child_process from 'child_process';
import {
	envBool,
	envDurationMs,
	envInt,
	envStr,
	envStrList,
} from '../duration.js';
import type * as browser from './index.js';
import { BrowserHolder, RotatingManager } from './manager.js';
import {
	createEgressProfile,
	egressProxyArgs,
	startEgressProxy,
} from '../egress-proxy.js';

const ROTATION_CHECK_INTERVAL_MS = envDurationMs(
	'GVM_WEBDRIVER_ROTATION_CHECK_INTERVAL',
	'10m',
);
const ROTATION_ABSOLUTE_THRESHOLD_MB = envInt(
	'GVM_WEBDRIVER_ROTATION_ABSOLUTE_MB',
	2048,
);
const ROTATION_IDLE_DELTA_MB = envInt(
	'GVM_WEBDRIVER_ROTATION_IDLE_DELTA_MB',
	256,
);
const ROTATION_AT_LEAST_MS = envDurationMs(
	'GVM_WEBDRIVER_ROTATION_AT_LEAST',
	0,
);
const ROTATION_FAILURE_THRESHOLD = envInt('GVM_WEBDRIVER_ROTATION_FAILURES', 3);
const ROTATION_CLOSE_TIMEOUT_MS = envDurationMs(
	'GVM_WEBDRIVER_ROTATION_CLOSE_TIMEOUT',
	'2m',
);
const RENDERER_MAX_OLD_SPACE_MB = envInt(
	'GVM_WEBDRIVER_RENDERER_MAX_OLD_SPACE_MB',
	1024,
);
const RENDERER_PROCESS_LIMIT = envInt(
	'GVM_WEBDRIVER_RENDERER_PROCESS_LIMIT',
	4,
);

const CHROME_EXECUTABLE = envStr(
	'GVM_WEBDRIVER_CHROME_EXECUTABLE',
	'/usr/bin/chromium',
);
const CHROME_HEADLESS = envBool('GVM_WEBDRIVER_CHROME_HEADLESS', true);
// Extra flags appended to the defaults below. See `envStrList` for the format.
const CHROME_EXTRA_ARGS = envStrList('GVM_WEBDRIVER_CHROME_ARGS', []);

/**
 * Ceiling on a single CDP round-trip. Puppeteer's own default is 180s, which
 * sits above the whole render budget (a 30s navigation plus at most 60s of
 * `waitAfterLoaded`), so a wedged command used to be noticed long after the
 * render should have finished. It must also stay below the module's HTTP
 * deadline (`base_client_builder` in `implementation/src/common/mod.rs`, 300s)
 * so that a stuck browser produces an answer from us rather than a client-side
 * timeout with nothing to report.
 */
const PROTOCOL_TIMEOUT_MS = envDurationMs(
	'GVM_WEBDRIVER_PROTOCOL_TIMEOUT',
	'90s',
);

async function newBrowser(): Promise<BrowserHolder> {
	const proxy = await startEgressProxy({ timeoutMs: PROTOCOL_TIMEOUT_MS });
	let profile: Awaited<ReturnType<typeof createEgressProfile>> | undefined;
	let realBrowser: pup.Browser;
	try {
		profile = await createEgressProfile();
		const args = [
			'--no-sandbox',
			'--disable-dev-shm-usage',
			'--disable-accelerated-2d-canvas',
			'--no-first-run',
			'--no-zygote',
			'--disable-gpu',
			// No popup can be SSRF-guarded before its first navigation, so none is
			// allowed to exist -- see the `targetcreated` handler in `render.ts`.
			'--block-new-web-contents',
			'--enable-precise-memory-info',
			`--js-flags=--max-old-space-size=${RENDERER_MAX_OLD_SPACE_MB}`,
			`--renderer-process-limit=${RENDERER_PROCESS_LIMIT}`,
			...egressProxyArgs(proxy.url, CHROME_EXTRA_ARGS),
		];
		realBrowser = await puppeteer.launch({
			headless: CHROME_HEADLESS,
			args,
			executablePath: CHROME_EXECUTABLE,
			protocolTimeout: PROTOCOL_TIMEOUT_MS,
			userDataDir: profile.path,
		});
	} catch (error) {
		await proxy.close();
		await profile?.close();
		throw error;
	}

	logger.log('info', 'created new raw browser', {
		pid: realBrowser.process()?.pid,
	});

	realBrowser.on('disconnected', () => {
		void proxy.close();
		const cleanup = () => {
			void profile!
				.close()
				.catch((error) =>
					logger.log('warn', 'could not remove browser profile', { error }),
				);
		};
		const process = realBrowser.process();
		if (process && process.exitCode === null && process.signalCode === null) {
			process.once('exit', cleanup);
		} else {
			cleanup();
		}
		logger.log('info', 'browser disconnected');
	});

	return new BrowserHolder(realBrowser);
}

async function getRssMBByPid(pid: number): Promise<number> {
	try {
		const output = await util.promisify(child_process.exec)(
			`ps -o rss= -p ${pid}`,
		);
		return parseInt(output.stdout.trim(), 10) / 1024;
	} catch (error) {
		logger.log('error', 'Failed to get RSS memory usage', { pid, error });
		return 0;
	}
}

async function getSelfRssMB(): Promise<number> {
	try {
		return process.memoryUsage().rss / 1024 / 1024;
	} catch (error) {
		logger.log('error', 'Failed to get self RSS memory usage', { error });
		return 0;
	}
}

async function getTotalRssMB(pid: number): Promise<number> {
	return (await getSelfRssMB()) + (await getRssMBByPid(pid));
}

export const INSTANCE: Promise<browser.Manager> = RotatingManager.create({
	checkIntervalMs: ROTATION_CHECK_INTERVAL_MS,
	absoluteThresholdMB: ROTATION_ABSOLUTE_THRESHOLD_MB,
	idleDeltaMB: ROTATION_IDLE_DELTA_MB,
	atLeastMs: ROTATION_AT_LEAST_MS,
	failureThreshold: ROTATION_FAILURE_THRESHOLD,
	closeTimeoutMs: ROTATION_CLOSE_TIMEOUT_MS,
	launch: newBrowser,
	memoryMB: (pid) => (pid ? getTotalRssMB(pid) : getSelfRssMB()),
})
	.then((manager) => {
		manager.start();
		return manager;
	})
	.catch((error) => {
		logger.log('error', 'failed to create initial browser', { error });
		process.exit(1);
	});
