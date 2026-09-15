import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { parseSize } from './duration.js';

describe('parseSize', () => {
	it('treats a bare number as octets', () => {
		assert.equal(parseSize('512', undefined), 512);
	});

	it('reads decimal suffixes as powers of 1000', () => {
		assert.equal(parseSize('1B', undefined), 1);
		assert.equal(parseSize('2KB', undefined), 2000);
		assert.equal(parseSize('3MB', undefined), 3_000_000);
		assert.equal(parseSize('4GB', undefined), 4_000_000_000);
	});

	it('reads binary suffixes as powers of 1024', () => {
		assert.equal(parseSize('2KiB', undefined), 2048);
		assert.equal(parseSize('5MiB', undefined), 5 * 1024 * 1024);
		assert.equal(parseSize('1GiB', undefined), 1024 ** 3);
	});

	it('accepts fractions, spacing and any casing', () => {
		assert.equal(parseSize('1.5 mib', undefined), 1024 * 1024 * 1.5);
		assert.equal(parseSize(' 2gIb ', undefined), 2 * 1024 ** 3);
	});

	it('falls back to the default only when nothing was given', () => {
		assert.equal(parseSize(undefined, 7), 7);
		assert.equal(parseSize('', 7), 7);
		assert.equal(parseSize('0', 7), 0);
	});

	it('rejects garbage rather than guessing', () => {
		for (const bad of ['MiB', '1 potato', '1KB2', '-1MiB', '1e3']) {
			assert.throws(() => parseSize(bad, undefined), /invalid size/, bad);
		}
	});
});
