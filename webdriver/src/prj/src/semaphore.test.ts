import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { Semaphore, SemaphoreTimeout } from './semaphore.js';

const NEVER = 60_000;

function deferred<T = void>() {
	let resolve!: (v: T) => void;
	const promise = new Promise<T>((r) => {
		resolve = r;
	});
	return { promise, resolve };
}

describe('Semaphore', () => {
	it('rejects a non-positive permit count', () => {
		assert.throws(() => new Semaphore(0));
		assert.throws(() => new Semaphore(-1));
		assert.throws(() => new Semaphore(1.5));
	});

	it('grants up to the permit count without waiting', async () => {
		const sem = new Semaphore(3);
		await sem.acquire(NEVER);
		await sem.acquire(NEVER);
		await sem.acquire(NEVER);
		assert.equal(sem.inFlight, 3);
		assert.equal(sem.queued, 0);
	});

	it('queues the caller past the limit until a permit is released', async () => {
		const sem = new Semaphore(1);
		await sem.acquire(NEVER);

		let granted = false;
		const waiting = sem.acquire(NEVER).then(() => {
			granted = true;
		});

		await new Promise((r) => setTimeout(r, 10));
		assert.equal(granted, false, 'should still be queued');
		assert.equal(sem.queued, 1);

		sem.release();
		await waiting;
		assert.equal(granted, true);
		assert.equal(sem.inFlight, 1, 'permit passed to the waiter, not returned');
	});

	it('hands permits to waiters in FIFO order', async () => {
		const sem = new Semaphore(1);
		await sem.acquire(NEVER);

		const order: number[] = [];
		const waiters = [1, 2, 3].map((n) =>
			sem.acquire(NEVER).then(() => {
				order.push(n);
			}),
		);

		await new Promise((r) => setTimeout(r, 10));
		sem.release();
		sem.release();
		sem.release();
		await Promise.all(waiters);

		assert.deepEqual(order, [1, 2, 3]);
	});

	it('times out a caller that waits too long, and stops queueing it', async () => {
		const sem = new Semaphore(1);
		await sem.acquire(NEVER);

		await assert.rejects(() => sem.acquire(20), SemaphoreTimeout);
		assert.equal(sem.queued, 0, 'timed-out waiter must leave the queue');

		// The permit must still be grantable afterwards.
		sem.release();
		await sem.acquire(NEVER);
		assert.equal(sem.inFlight, 1);
	});

	it('does not grant a permit to a waiter that already timed out', async () => {
		const sem = new Semaphore(1);
		await sem.acquire(NEVER);

		const timedOut = assert.rejects(() => sem.acquire(20), SemaphoreTimeout);
		await new Promise((r) => setTimeout(r, 40));
		await timedOut;

		sem.release();
		assert.equal(sem.inFlight, 0, 'released permit must not be held by a ghost');
	});

	it('never exceeds the permit count under concurrent load', async () => {
		const permits = 4;
		const sem = new Semaphore(permits);
		let active = 0;
		let peak = 0;

		await Promise.all(
			Array.from({ length: 50 }, () =>
				sem.withPermit(NEVER, async () => {
					active += 1;
					peak = Math.max(peak, active);
					await new Promise((r) => setTimeout(r, 1));
					active -= 1;
				}),
			),
		);

		assert.equal(peak, permits, 'concurrency must saturate but not exceed');
		assert.equal(sem.inFlight, 0);
		assert.equal(sem.queued, 0);
	});

	it('releases the permit when the guarded task throws', async () => {
		const sem = new Semaphore(1);
		await assert.rejects(
			() =>
				sem.withPermit(NEVER, async () => {
					throw new Error('boom');
				}),
			/boom/,
		);
		assert.equal(sem.inFlight, 0, 'a failed task must not leak its permit');
	});

	it('ignores a release with nothing outstanding', async () => {
		const sem = new Semaphore(2);
		sem.release();
		sem.release();
		sem.release();

		// A leaked permit would let a third caller through here.
		await sem.acquire(NEVER);
		await sem.acquire(NEVER);
		await assert.rejects(() => sem.acquire(20), SemaphoreTimeout);
	});

	it('keeps a slow task from starving a queued one indefinitely', async () => {
		const sem = new Semaphore(1);
		const slow = deferred();

		const running = sem.withPermit(NEVER, () => slow.promise);
		const queued = sem.acquire(NEVER);

		slow.resolve();
		await running;
		await queued;
		assert.equal(sem.inFlight, 1);
	});
});
