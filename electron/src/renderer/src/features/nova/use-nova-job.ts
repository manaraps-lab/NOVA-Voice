import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, apiJson } from '@/lib/api/client';

export type JobState =
  | 'idle'
  | 'queued'
  | 'running'
  | 'pausing'
  | 'paused'
  | 'cancelling'
  | 'done'
  | 'cancelled'
  | 'error';

export interface JobProgress {
  id: string;
  state: JobState;
  percent: number;
  elapsed_s: number;
  eta_s: number | null;
  total_estimate_s: number | null;
  calibrating: boolean;
  warming: boolean;
  chunk_elapsed_s: number;
  chunk: number;
  chunks: number;
  chars_done: number;
  chars_total: number;
  engine: string;
  error: string | null;
  result: { id: string; filename: string; duration: number } | null;
  partial: boolean;
}

export interface JobRequest {
  text: string;
  engine: 'fast' | 'quality' | 'expressive';
  quality: string;
  wav_bits: 16 | 24 | 32;
  style: string;
  gender: 'male' | 'female';
  age: string;
  voice: string | null;
  speed: number;
  pitch: number;
  depth: number;
  language: string;
}

export interface DeviceStats {
  cpu_percent: number;
  cpu_cores: number;
  ram_percent: number;
  ram_used_gb: number;
  ram_total_gb: number;
  app_ram_gb: number;
  disk_free_gb: number;
  battery: { percent: number; plugged: boolean } | null;
  temperature_c: number | null;
}

const ACTIVE: JobState[] = ['queued', 'running', 'pausing', 'paused', 'cancelling'];
export const isActive = (s: JobState | undefined) => !!s && ACTIVE.includes(s);
const POLL_MS = 600;

function message(err: unknown): string {
  if (err instanceof ApiError) return err.detail || err.message;
  return err instanceof Error ? err.message : String(err);
}

/** Drives one background job: start, poll, pause, resume, cancel. */
export function useNovaJob() {
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [receivedAt, setReceivedAt] = useState(() => Date.now());
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const alive = useRef(true);

  const accept = useCallback((p: JobProgress) => {
    setProgress(p);
    setReceivedAt(Date.now());
  }, []);

  // A job started earlier keeps running in the backend: pick it up again.
  useEffect(() => {
    alive.current = true;
    apiJson<JobProgress | { state: 'idle' }>('/nova/jobs/active')
      .then((p) => {
        if (alive.current && p.state !== 'idle') accept(p as JobProgress);
      })
      .catch(() => undefined);
    return () => {
      alive.current = false;
    };
  }, [accept]);

  const id = progress?.id;
  const live = isActive(progress?.state);
  useEffect(() => {
    if (!id || !live) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const p = await apiJson<JobProgress>(`/nova/jobs/${id}`);
        if (!stop) accept(p);
      } catch {
        // Transient: keep polling, the job lives in the backend.
      }
      if (!stop) timer = setTimeout(tick, POLL_MS);
    };
    timer = setTimeout(tick, POLL_MS);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [id, live, accept]);

  const start = useCallback(
    async (req: JobRequest) => {
      setError(null);
      setStarting(true);
      try {
        accept(await apiJson<JobProgress>('/nova/jobs', { method: 'POST', body: JSON.stringify(req), headers: { 'Content-Type': 'application/json' } }));
      } catch (err) {
        // 409: a job is already running in the backend; show its progress.
        if (err instanceof ApiError && err.status === 409) {
          try {
            const p = await apiJson<JobProgress | { state: 'idle' }>('/nova/jobs/active');
            if (p.state !== 'idle') {
              accept(p as JobProgress);
              return;
            }
          } catch {
            /* fall through to the message */
          }
        }
        setError(message(err));
      } finally {
        setStarting(false);
      }
    },
    [accept],
  );

  const control = useCallback(
    async (action: 'pause' | 'resume' | 'cancel') => {
      if (!id) return;
      try {
        accept(await apiJson<JobProgress>(`/nova/jobs/${id}/${action}`, { method: 'POST' }));
      } catch (err) {
        setError(message(err));
      }
    },
    [id, accept],
  );

  const reset = useCallback(() => {
    setProgress(null);
    setError(null);
  }, []);

  return {
    progress,
    receivedAt,
    error,
    starting,
    start,
    pause: () => control('pause'),
    resume: () => control('resume'),
    cancel: () => control('cancel'),
    reset,
  };
}

export interface EngineRow {
  key: 'fast' | 'quality' | 'expressive';
  engine_id: string;
  label: string;
  model: string;
  note: string;
  available: boolean;
  reason: string;
  loaded: boolean;
  installable: boolean;
  installed: boolean | null;
  install: {
    state: string | null;
    error: string | null;
    weights_progress: unknown;
    steps: { id: string; state: string }[];
  } | null;
  weights_cached: boolean | null;
  badge: 'fastest' | 'best_arabic' | 'studio' | null;
  arabic_note: string;
  min_ram_gb: number | null;
  system_ram_gb: number | null;
  fit: 'ok' | 'tight' | 'heavy' | null;
}

/** Which engines can render now. Polls while `enabled`. */
export function useEngines(enabled: boolean, intervalMs = 5000) {
  const [rows, setRows] = useState<EngineRow[] | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const run = async () => {
      try {
        const r = await apiJson<{ engines: EngineRow[] }>('/nova/engines');
        if (!stop) setRows(r.engines);
      } catch {
        // Keep the last reading; the dots only change on a real answer.
      }
      if (!stop) timer = setTimeout(run, intervalMs);
    };
    void run();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [enabled, intervalMs, tick]);
  const refresh = useCallback(() => setTick((n) => n + 1), []);
  return { rows, refresh };
}

export async function installEngine(engineId: string): Promise<string | null> {
  try {
    await apiJson(`/engines/sidecar/${engineId}/install`, { method: 'POST' });
    return null;
  } catch (err) {
    return message(err);
  }
}

export function useDeviceStats(enabled: boolean, intervalMs = 1000) {
  const [stats, setStats] = useState<DeviceStats | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const s = await apiJson<DeviceStats>('/nova/system');
        if (!stop) setStats(s);
      } catch {
        // The panel just keeps its last reading.
      }
      if (!stop) timer = setTimeout(tick, intervalMs);
    };
    void tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [enabled, intervalMs]);
  return stats;
}

/** Re-render every `ms` while `on`, so clocks tick between polls. */
export function useNow(on: boolean, ms = 250): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [on, ms]);
  return now;
}
