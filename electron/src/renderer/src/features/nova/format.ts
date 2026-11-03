/** Clock text for a duration in seconds: 0:07, 12:03, 1:02:09 (Latin digits). */
export function formatClock(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || !Number.isFinite(totalSeconds)) return '--:--';
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

export function countWords(text: string): number {
  const t = text.trim();
  return t ? t.split(/\s+/).length : 0;
}

/** "about N seconds / minutes / hours" in Arabic, for time estimates. */
export function formatApprox(totalSeconds: number): string {
  const s = Math.max(1, Math.round(totalSeconds));
  if (s < 90) return `${s} ثانية`;
  if (s < 5400) return `${Math.round(s / 60)} دقيقة`;
  const h = Math.round((s / 3600) * 10) / 10;
  return `${h} ساعة`;
}
