import { describe, expect, it } from 'vitest';
import { countWords, formatClock } from './format';

describe('formatClock', () => {
  it('formats minutes and hours', () => {
    expect(formatClock(7)).toBe('0:07');
    expect(formatClock(723)).toBe('12:03');
    expect(formatClock(3729)).toBe('1:02:09');
  });
  it('shows a placeholder when unknown', () => {
    expect(formatClock(null)).toBe('--:--');
    expect(formatClock(Number.NaN)).toBe('--:--');
  });
});

describe('countWords', () => {
  it('counts whitespace separated words', () => {
    expect(countWords('  مرحبا   بكم ')).toBe(2);
    expect(countWords('')).toBe(0);
  });
});
