/**
 * Counting semaphore with FIFO waiters and a bounded wait.
 *
 * Renders queue rather than being rejected: shedding load would turn saturation
 * into a caller-visible error that differs from node to node depending on what
 * else happened to be scheduled there. Queueing keeps the outcome a function of
 * the request alone.
 *
 * The wait is bounded anyway, because a caller blocked forever eventually trips
 * its own transport timeout, which is a harsher failure than one we report
 * ourselves.
 */
export class Semaphore {
	private available: number;
	private readonly waiters: Array<{
		resolve: () => void;
		reject: (e: Error) => void;
		timer: NodeJS.Timeout;
	}> = [];

	constructor(private readonly permits: number) {
		if (!Number.isInteger(permits) || permits < 1) {
			throw new Error(`semaphore permits must be a positive integer, got ${permits}`);
		}
		this.available = permits;
	}

	get inFlight(): number {
		return this.permits - this.available;
	}

	get queued(): number {
		return this.waiters.length;
	}

	/**
	 * Acquire a permit, waiting at most `timeoutMs`.
	 *
	 * @throws {SemaphoreTimeout} if no permit became available in time.
	 */
	acquire(timeoutMs: number): Promise<void> {
		if (this.available > 0) {
			this.available -= 1;
			return Promise.resolve();
		}

		return new Promise<void>((resolve, reject) => {
			const entry = {
				resolve,
				reject,
				timer: setTimeout(() => {
					const idx = this.waiters.indexOf(entry);
					if (idx !== -1) {
						this.waiters.splice(idx, 1);
					}
					reject(new SemaphoreTimeout(timeoutMs));
				}, timeoutMs),
			};
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
		// The permit passes straight to the waiter; `available` deliberately stays
		// where it is.
		next.resolve();
	}

	/** Run `fn` holding a permit, releasing it however `fn` settles. */
	async withPermit<T>(timeoutMs: number, fn: () => Promise<T>): Promise<T> {
		await this.acquire(timeoutMs);
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
