import * as pup from 'puppeteer-core';

export interface Handle {
	close(): Promise<void>;
	get(): pup.Browser;
}

export interface Manager {
	getBrowser(): Handle;
	report(handle: Handle, failed: boolean): void;
}

/**
 * Whether a render error says the browser is broken rather than the page:
 * a lost connection, or a timed-out browser-level CDP command. Page-level
 * commands can time out because of the page, so they do not count.
 */
export function isBrowserFailure(error: unknown): boolean {
	return (
		error instanceof pup.ConnectionClosedError ||
		(error instanceof pup.ProtocolError &&
			/^Target\.\w+ timed out/.test(error.message))
	);
}
