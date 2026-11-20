import type * as pup from 'puppeteer-core';
import { setTimeout as delay } from 'node:timers/promises';
import * as logger from '../logging.js';
import { formatDurationMs } from '../duration.js';
import type * as browser from './index.js';

export class BrowserHolder {
	browser: pup.Browser;
	counter: number;
	id: number;

	/** Settles once the browser is really closed; `false` if closing failed. */
	readonly closed: Promise<boolean>;
	private settleClosed!: (ok: boolean) => void;

	static nextId = 1;

	constructor(browser: pup.Browser) {
		this.browser = browser;
		this.counter = 1;
		this.id = BrowserHolder.nextId++;
		this.closed = new Promise((resolve) => {
			this.settleClosed = resolve;
		});
		logger.log('info', 'created browser', { id: this.id });
	}

	async close(): Promise<void> {
		this.counter--;
		if (this.counter === 0) {
			logger.log('info', 'closing browser instance', { id: this.id });
			try {
				await this.browser.close();
				this.settleClosed(true);
			} catch (error) {
				this.settleClosed(false);
				throw error;
			}
		}
	}

	get(): pup.Browser {
		return this.browser;
	}
}

export interface Options {
	checkIntervalMs: number;
	absoluteThresholdMB: number;
	idleDeltaMB: number;
	atLeastMs: number;
	// Consecutive browser-level render failures that force a rotation; 0 disables.
	failureThreshold: number;
	// How long the old browser may take to close before it is killed.
	closeTimeoutMs: number;
	launch: () => Promise<BrowserHolder>;
	memoryMB: (pid: number | undefined) => Promise<number>;
	now?: () => number;
	sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export class RotatingManager implements browser.Manager {
	private holder: BrowserHolder;
	private used = false;
	private lastMemoryMB: number | null = null;
	private lastRotationTime: number;
	private consecutiveFailures = 0;
	private checking: Promise<void> | undefined;
	private readonly stopper = new AbortController();
	private readonly now: () => number;
	private readonly sleep: (ms: number, signal: AbortSignal) => Promise<void>;

	private constructor(
		private readonly options: Options,
		holder: BrowserHolder,
		private readonly initialMemoryMB: number | undefined,
	) {
		this.holder = holder;
		this.now = options.now ?? Date.now;
		this.sleep =
			options.sleep ??
			((ms, signal) => delay(ms, undefined, { signal, ref: false }));
		this.lastRotationTime = this.now();
	}

	static async create(options: Options): Promise<RotatingManager> {
		const holder = await options.launch();
		const pid = holder.browser.process()?.pid;
		const initialMemoryMB = pid ? await options.memoryMB(pid) : undefined;
		return new RotatingManager(options, holder, initialMemoryMB);
	}

	/**
	 * Run the rotation check every interval. The next wait starts only once the
	 * current check has settled, so checks never overlap and never backlog.
	 */
	start(): void {
		void this.loop();
	}

	stop(): void {
		this.stopper.abort();
	}

	private async loop(): Promise<void> {
		for (;;) {
			try {
				await this.sleep(this.options.checkIntervalMs, this.stopper.signal);
			} catch {
				return;
			}
			await this.check().catch((error) => {
				logger.log('error', 'browser rotation check failed', { error });
			});
		}
	}

	/** Concurrent callers share the check in flight, so holders are never swapped twice at once. */
	check(): Promise<void> {
		this.checking ??= this.rotationCheck().finally(() => {
			this.checking = undefined;
		});
		return this.checking;
	}

	private async rotationCheck(): Promise<void> {
		const { options } = this;
		// Consumed before measuring so activity during the measurement counts for the next interval
		const used = this.used;
		this.used = false;
		const lastMemoryMB = this.lastMemoryMB;
		const old = this.holder;
		const usedRAMMB = await options.memoryMB(old.browser.process()?.pid);
		this.lastMemoryMB = usedRAMMB;

		const memoryDeltaMB = lastMemoryMB !== null ? usedRAMMB - lastMemoryMB : 0;
		const age = this.now() - this.lastRotationTime;

		const reasons: string[] = [];

		if (
			this.initialMemoryMB &&
			usedRAMMB > this.initialMemoryMB + options.absoluteThresholdMB
		) {
			reasons.push(
				`absolute memory ${usedRAMMB.toFixed(0)}MB exceeds initial ${this.initialMemoryMB.toFixed(0)}MB + ${options.absoluteThresholdMB}MB`,
			);
		}

		if (!used && memoryDeltaMB > options.idleDeltaMB) {
			reasons.push(
				`idle memory grew by ${memoryDeltaMB.toFixed(0)}MB (threshold ${options.idleDeltaMB}MB)`,
			);
		}

		if (options.atLeastMs > 0 && age >= options.atLeastMs) {
			reasons.push(
				`age ${formatDurationMs(age)} exceeds max ${formatDurationMs(options.atLeastMs)}`,
			);
		}

		if (
			options.failureThreshold > 0 &&
			this.consecutiveFailures >= options.failureThreshold
		) {
			reasons.push(
				`${this.consecutiveFailures} consecutive browser failures (threshold ${options.failureThreshold})`,
			);
		}

		logger.log('info', 'browser rotation check', {
			shouldRotate: reasons.length > 0,
			reasons,
			used,
			usedRAMMB,
			memoryDeltaMB,
			initialMemoryMB: this.initialMemoryMB,
			consecutiveFailures: this.consecutiveFailures,
			lastRotationAgo: formatDurationMs(age),
		});

		if (reasons.length === 0) {
			return;
		}

		const next = await options.launch();
		this.holder = next;
		this.lastMemoryMB = null;
		this.consecutiveFailures = 0;
		this.lastRotationTime = this.now();
		// Not awaited: the check is done once `next` serves, and the new holder may need a check meanwhile
		this.retire(old).catch((error) => {
			logger.log('error', 'retiring old browser failed', { id: old.id, error });
		});
	}

	/**
	 * Close the old browser once its last render lets go, killing its process if
	 * it is not really closed within the bound, which starts now.
	 */
	private async retire(old: BrowserHolder): Promise<void> {
		const bound = new AbortController();
		// Renders still holding `old` delay the real close; the bound covers them too
		old.close().catch((error) => {
			logger.log('error', 'closing old browser failed', { id: old.id, error });
		});
		const expired = this.sleep(this.options.closeTimeoutMs, bound.signal).then(
			() => 'timeout' as const,
			() => 'cancelled' as const,
		);
		const outcome = await Promise.race([
			old.closed.then((ok) => (ok ? ('closed' as const) : ('failed' as const))),
			expired,
		]);
		bound.abort();
		if (outcome === 'closed') {
			return;
		}

		const proc = old.browser.process();
		logger.log('warn', 'old browser did not close, killing it', {
			id: old.id,
			outcome,
			pid: proc?.pid,
		});
		if (!proc) {
			logger.log('error', 'cannot kill old browser: no process handle', {
				id: old.id,
			});
			return;
		}
		proc.kill('SIGKILL');
	}

	/**
	 * Report how a render on `handle` ended; `failed` means the browser itself
	 * failed, not the page. Reports for an already replaced browser are dropped.
	 */
	report(handle: browser.Handle, failed: boolean): void {
		if (handle !== this.holder) {
			return;
		}
		if (!failed) {
			this.consecutiveFailures = 0;
			return;
		}
		this.consecutiveFailures++;
		const { failureThreshold } = this.options;
		if (failureThreshold > 0 && this.consecutiveFailures >= failureThreshold) {
			void this.check().catch((error) => {
				logger.log('error', 'browser rotation check failed', { error });
			});
		}
	}

	getBrowser(): browser.Handle {
		this.holder.counter++;
		this.used = true;
		return this.holder;
	}
}
