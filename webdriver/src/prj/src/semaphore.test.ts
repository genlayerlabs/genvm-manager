import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import {
	Semaphore,
	SemaphoreAborted,
	SemaphoreQueueFull,
	SemaphoreTimeout,
} from './semaphore.js';

const NEVER = 60_000;

/** Queue depth is irrelevant to most cases, so default it out of the way. */
function sem(permits: number, maxQueued = 1024) {
	return new Semaphore({ permits, maxQueued });
}

function deferred<T = void>() {
	let resolve!: (v: T) => void;
	const promise = new Promise<T>((r) => {
		resolve = r;
	});
	return { promise, resolve };
}

describe('Semaphore', () => {
	it('rejects invalid construction', () => {
		assert.throws(() => sem(0));
		assert.throws(() => sem(-1));
		assert.throws(() => sem(1.5));
		assert.throws(() => new Semaphore({ permits: 1, maxQueued: -1 }));
		assert.throws(() => new Semaphore({ permits: 1, maxQueued: 1.5 }));
	});

	it('grants up to the permit count without waiting', async () => {
		const s = sem(3);
		await s.acquire(NEVER);
		await s.acquire(NEVER);
		await s.acquire(NEVER);
		assert.equal(s.inFlight, 3);
		assert.equal(s.queued, 0);
	});

	it('queues the caller past the limit until a permit is released', async () => {
		const s = sem(1);
		await s.acquire(NEVER);

		let granted = false;
		const waiting = s.acquire(NEVER).then(() => {
			granted = true;
		});

		await new Promise((r) => setTimeout(r, 10));
		assert.equal(granted, false, 'should still be queued');
		assert.equal(s.queued, 1);

		s.release();
		await waiting;
		assert.equal(granted, true);
		assert.equal(s.inFlight, 1, 'permit passed to the waiter, not returned');
	});

	it('hands permits to waiters in FIFO order', async () => {
		const s = sem(1);
		await s.acquire(NEVER);

		const order: number[] = [];
		const waiters = [1, 2, 3].map((n) =>
			s.acquire(NEVER).then(() => {
				order.push(n);
			}),
		);

		await new Promise((r) => setTimeout(r, 10));
		s.release();
		s.release();
		s.release();
		await Promise.all(waiters);

		assert.deepEqual(order, [1, 2, 3]);
	});

	it('times out a caller that waits too long, and stops queueing it', async () => {
		const s = sem(1);
		await s.acquire(NEVER);

		await assert.rejects(() => s.acquire(20), SemaphoreTimeout);
		assert.equal(s.queued, 0, 'timed-out waiter must leave the queue');

		s.release();
		await s.acquire(NEVER);
		assert.equal(s.inFlight, 1);
	});

	it('does not grant a permit to a waiter that already timed out', async () => {
		const s = sem(1);
		await s.acquire(NEVER);

		const timedOut = assert.rejects(() => s.acquire(20), SemaphoreTimeout);
		await new Promise((r) => setTimeout(r, 40));
		await timedOut;

		s.release();
		assert.equal(s.inFlight, 0, 'released permit must not be held by a ghost');
	});

	it('never exceeds the permit count under concurrent load', async () => {
		const permits = 4;
		const s = sem(permits);
		let active = 0;
		let peak = 0;

		await Promise.all(
			Array.from({ length: 50 }, () =>
				s.withPermit(NEVER, async () => {
					active += 1;
					peak = Math.max(peak, active);
					await new Promise((r) => setTimeout(r, 1));
					active -= 1;
				}),
			),
		);

		assert.equal(peak, permits, 'concurrency must saturate but not exceed');
		assert.equal(s.inFlight, 0);
		assert.equal(s.queued, 0);
	});

	it('releases the permit when the guarded task throws', async () => {
		const s = sem(1);
		await assert.rejects(
			() =>
				s.withPermit(NEVER, async () => {
					throw new Error('boom');
				}),
			/boom/,
		);
		assert.equal(s.inFlight, 0, 'a failed task must not leak its permit');
	});

	it('ignores a release with nothing outstanding', async () => {
		const s = sem(2);
		s.release();
		s.release();
		s.release();

		// A leaked permit would let a third caller through here.
		await s.acquire(NEVER);
		await s.acquire(NEVER);
		await assert.rejects(() => s.acquire(20), SemaphoreTimeout);
	});

	it('keeps a slow task from starving a queued one indefinitely', async () => {
		const s = sem(1);
		const slow = deferred();

		const running = s.withPermit(NEVER, () => slow.promise);
		const queued = s.acquire(NEVER);

		slow.resolve();
		await running;
		await queued;
		assert.equal(s.inFlight, 1);
	});

	describe('queue depth', () => {
		it('refuses callers once the queue is full', async () => {
			const s = new Semaphore({ permits: 1, maxQueued: 2 });
			await s.acquire(NEVER);

			const queued = [s.acquire(NEVER), s.acquire(NEVER)];
			assert.equal(s.queued, 2);

			await assert.rejects(() => s.acquire(NEVER), SemaphoreQueueFull);
			assert.equal(s.queued, 2, 'a refused caller must not be queued');

			s.release();
			s.release();
			await Promise.all(queued);
		});

		it('accepts again once the queue drains', async () => {
			const s = new Semaphore({ permits: 1, maxQueued: 1 });
			await s.acquire(NEVER);
			const first = s.acquire(NEVER);
			await assert.rejects(() => s.acquire(NEVER), SemaphoreQueueFull);

			s.release();
			await first;

			// Slot freed: a new caller queues rather than being refused.
			const second = s.acquire(NEVER);
			assert.equal(s.queued, 1);
			s.release();
			await second;
		});

		it('refuses immediately rather than waiting out the timeout', async () => {
			const s = new Semaphore({ permits: 1, maxQueued: 0 });
			await s.acquire(NEVER);

			const started = Date.now();
			await assert.rejects(() => s.acquire(NEVER), SemaphoreQueueFull);
			assert.ok(
				Date.now() - started < 50,
				'a full queue must fail fast, not after the wait',
			);
		});
	});

	describe('abort', () => {
		it('drops a waiter whose caller went away', async () => {
			const s = sem(1);
			await s.acquire(NEVER);

			const controller = new AbortController();
			const waiting = assert.rejects(
				() => s.acquire(NEVER, controller.signal),
				SemaphoreAborted,
			);
			assert.equal(s.queued, 1);

			controller.abort();
			await waiting;
			assert.equal(s.queued, 0, 'aborted waiter must leave the queue');

			// The permit must go to a live caller, not the abandoned one.
			s.release();
			assert.equal(s.inFlight, 0);
		});

		it('rejects at once when the signal is already aborted', async () => {
			const s = sem(1);
			await s.acquire(NEVER);
			await assert.rejects(
				() => s.acquire(NEVER, AbortSignal.abort()),
				SemaphoreAborted,
			);
			assert.equal(s.queued, 0);
		});

		it('does not reject a caller that already holds a permit', async () => {
			const s = sem(1);
			const controller = new AbortController();
			await s.acquire(NEVER, controller.signal);
			controller.abort();
			// Aborting after the permit was granted is a no-op; release still works.
			s.release();
			assert.equal(s.inFlight, 0);
		});

		it('frees the queue slot so a later caller is not refused', async () => {
			const s = new Semaphore({ permits: 1, maxQueued: 1 });
			await s.acquire(NEVER);

			const controller = new AbortController();
			const abandoned = assert.rejects(
				() => s.acquire(NEVER, controller.signal),
				SemaphoreAborted,
			);
			controller.abort();
			await abandoned;

			const queued = s.acquire(NEVER);
			assert.equal(s.queued, 1, 'slot must have been reclaimed');
			s.release();
			await queued;
		});
	});
});
