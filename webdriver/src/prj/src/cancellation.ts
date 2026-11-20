import type http from 'node:http';

export function disconnectSignal(res: http.ServerResponse): AbortSignal {
	const controller = new AbortController();
	res.once('close', () => {
		if (!res.writableFinished) controller.abort();
	});
	if (res.destroyed && !res.writableFinished) controller.abort();
	return controller.signal;
}

// Cancels the wait; callers must dispose resources owned by the operation.
export async function abortable<T>(
	signal: AbortSignal | undefined,
	fn: () => Promise<T>,
): Promise<T> {
	signal?.throwIfAborted();
	if (!signal) return await fn();
	let onAbort!: () => void;
	const aborted = new Promise<never>((_, reject) => {
		onAbort = () => reject(signal.reason);
		signal.addEventListener('abort', onAbort, { once: true });
	});
	try {
		return await Promise.race([aborted, fn()]);
	} finally {
		signal.removeEventListener('abort', onAbort);
	}
}
