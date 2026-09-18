export interface SemaphoreOptions {
	/** Concurrent holders. */
	permits: number;
	/** Callers allowed to wait for a permit before further ones are refused. */
	maxQueued: number;
}

/**
 * Counting semaphore with FIFO waiters, a bounded queue and a bounded wait.
 *
 * Callers past the permit count queue rather than being refused outright:
 * refusing on any contention would make the outcome depend on whatever else the
 * host happened to be running at the time.
 *
 * The queue is still finite, and the wait is still bounded. Both exist because
 * an unbounded queue only defers the problem — a caller parked indefinitely
 * eventually trips its own transport timeout, which fails far more harshly than
 * a refusal we issue ourselves, and until then it costs a live connection.
 */
export class Semaphore {
	private readonly permits: number;
	private readonly maxQueued: number;
	private available: number;
	private readonly waiters: Array<{
		resolve: () => void;
		reject: (e: Error) => void;
		timer: NodeJS.Timeout;
		cleanup: () => void;
	}> = [];

	constructor(options: SemaphoreOptions) {
		const { permits, maxQueued } = options;
		if (!Number.isInteger(permits) || permits < 1) {
			throw new Error(
				`semaphore permits must be a positive integer, got ${permits}`,
			);
		}
		if (!Number.isInteger(maxQueued) || maxQueued < 0) {
			throw new Error(
				`semaphore maxQueued must be a non-negative integer, got ${maxQueued}`,
			);
		}
		this.permits = permits;
		this.maxQueued = maxQueued;
		this.available = permits;
	}

	get inFlight(): number {
		return this.permits - this.available;
	}

	get queued(): number {
		return this.waiters.length;
	}

	/**
	 * Acquire a permit.
	 *
	 * @param timeoutMs how long to wait before giving up.
	 * @param signal aborts the wait — pass the caller's disconnect signal so a
	 *        vanished caller stops occupying a queue slot it will never use.
	 * @throws {SemaphoreQueueFull} if the queue is already at capacity.
	 * @throws {SemaphoreTimeout} if no permit became available in time.
	 * @throws {SemaphoreAborted} if `signal` fired while waiting.
	 */
	acquire(timeoutMs: number, signal?: AbortSignal): Promise<void> {
		if (this.available > 0) {
			this.available -= 1;
			return Promise.resolve();
		}
		if (this.waiters.length >= this.maxQueued) {
			return Promise.reject(new SemaphoreQueueFull(this.maxQueued));
		}
		if (signal?.aborted) {
			return Promise.reject(new SemaphoreAborted());
		}

		return new Promise<void>((resolve, reject) => {
			const remove = () => {
				const idx = this.waiters.indexOf(entry);
				if (idx !== -1) {
					this.waiters.splice(idx, 1);
				}
			};
			const onAbort = () => {
				remove();
				clearTimeout(entry.timer);
				reject(new SemaphoreAborted());
			};
			const entry = {
				resolve,
				reject,
				timer: setTimeout(() => {
					remove();
					entry.cleanup();
					reject(new SemaphoreTimeout(timeoutMs));
				}, timeoutMs),
				cleanup: () => signal?.removeEventListener('abort', onAbort),
			};
			signal?.addEventListener('abort', onAbort, { once: true });
			this.waiters.push(entry);
		});
	}

	release(): void {
		const next = this.waiters.shift();
		if (next === undefined) {
			// Guard against a double release leaking permits, which would silently
			// raise the concurrency ceiling above the configured one.
			if (this.available < this.permits) {
				this.available += 1;
			}
			return;
		}
		clearTimeout(next.timer);
		next.cleanup();
		// The permit passes straight to the waiter; `available` deliberately stays
		// where it is.
		next.resolve();
	}

	/** Run `fn` holding a permit, releasing it however `fn` settles. */
	async withPermit<T>(
		timeoutMs: number,
		fn: () => Promise<T>,
		signal?: AbortSignal,
	): Promise<T> {
		await this.acquire(timeoutMs, signal);
		try {
			return await fn();
		} finally {
			this.release();
		}
	}
}

export class SemaphoreTimeout extends Error {
	constructor(timeoutMs: number) {
		super(`timed out after ${timeoutMs}ms waiting for a render slot`);
		this.name = 'SemaphoreTimeout';
	}
}

export class SemaphoreQueueFull extends Error {
	constructor(maxQueued: number) {
		super(`render queue is full (${maxQueued} waiting)`);
		this.name = 'SemaphoreQueueFull';
	}
}

export class SemaphoreAborted extends Error {
	constructor() {
		super('caller went away while waiting for a render slot');
		this.name = 'SemaphoreAborted';
	}
}
