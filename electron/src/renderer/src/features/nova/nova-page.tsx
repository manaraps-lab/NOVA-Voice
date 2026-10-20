import './nova.css';
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { Link } from '@tanstack/react-router';
import {
  AudioLines,
  Bell,
  BellOff,
  Check,
  ChevronDown,
  Cpu,
  Download,
  Moon,
  Pause,
  Play,
  Settings as SettingsIcon,
  Square,
  Sun,
  Volume2,
  VolumeX,
} from 'lucide-react';
import { audioUrl } from '@/lib/api/client';
import { saveBackendFile } from '@/lib/native-save';
import { sfx } from './click-sound';
import { countWords, formatClock } from './format';
import {
  AGE_LABELS,
  AGE_ORDER,
  QUALITY_LABELS,
  QUALITY_ORDER,
  S,
  STYLE_LABELS,
  STYLE_ORDER,
  VOICES,
} from './strings';
import {
  type DeviceStats,
  type EngineRow,
  type JobRequest,
  type JobProgress,
  installEngine,
  isActive,
  useDeviceStats,
  useEngines,
  useNovaJob,
  useNow,
} from './use-nova-job';

const STORAGE_KEY = 'nova.settings.v1';

interface Prefs {
  text: string;
  engine: 'fast' | 'quality' | 'expressive';
  quality: string;
  wavBits: 16 | 24 | 32;
  theme: 'aurora' | 'night';
  style: string;
  gender: 'male' | 'female';
  age: string;
  voice: string | null;
  speed: number;
  pitch: number;
  depth: number;
}

const DEFAULTS: Prefs = {
  text: '',
  engine: 'fast',
  quality: 'balanced',
  wavBits: 16,
  theme: 'aurora',
  style: 'natural',
  gender: 'female',
  age: 'young adult',
  voice: null,
  speed: 1,
  pitch: 0,
  depth: 0,
};

function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Prefs>) };
  } catch {
    // Fresh defaults when storage is unavailable or corrupt.
  }
  return DEFAULTS;
}

function Logo() {
  return (
    <svg viewBox="0 0 40 40" className="nova-logo" aria-hidden="true">
      <circle cx="20" cy="20" r="20" fill="var(--nv-emerald)" />
      <g stroke="var(--nv-gold-soft)" strokeWidth="3" strokeLinecap="round">
        <line x1="10" y1="17" x2="10" y2="23" />
        <line x1="15" y1="12" x2="15" y2="28" />
        <line x1="20" y1="8" x2="20" y2="32" />
        <line x1="25" y1="13" x2="25" y2="27" />
        <line x1="30" y1="17" x2="30" y2="23" />
      </g>
    </svg>
  );
}

function Segmented<T extends string>(props: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <div className="nova-seg" role="radiogroup" aria-label={props.label}>
      {props.options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={props.value === o.value}
          disabled={props.disabled}
          className={props.value === o.value ? 'is-on' : ''}
          onClick={() => props.onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Slider(props: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  low: string;
  high: string;
  format: (v: number) => string;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <label className="nova-slider">
      <span className="nova-slider-head">
        <span>{props.label}</span>
        <bdi className="nova-num">{props.format(props.value)}</bdi>
      </span>
      <input
        type="range"
        min={props.min}
        max={props.max}
        step={props.step}
        value={props.value}
        disabled={props.disabled}
        onChange={(e) => props.onChange(Number(e.target.value))}
      />
      <span className="nova-slider-ends">
        <span>{props.low}</span>
        <span>{props.high}</span>
      </span>
    </label>
  );
}

function Ring({ percent, paused }: { percent: number; paused: boolean }) {
  const r = 54;
  const c = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 128 128" className={`nova-ring${paused ? ' is-paused' : ''}`} aria-hidden="true">
      <circle cx="64" cy="64" r={r} className="nova-ring-bg" />
      <circle
        cx="64"
        cy="64"
        r={r}
        className="nova-ring-fg"
        strokeDasharray={c}
        strokeDashoffset={c * (1 - Math.min(100, Math.max(0, percent)) / 100)}
        transform="rotate(-90 64 64)"
      />
    </svg>
  );
}

function Meter(props: { label: string; value: number; detail: string; warn?: boolean }) {
  return (
    <div className="nova-meter">
      <div className="nova-meter-head">
        <span>{props.label}</span>
        <bdi className="nova-num">{props.detail}</bdi>
      </div>
      <div className="nova-meter-track" role="progressbar" aria-valuenow={Math.round(props.value)} aria-valuemin={0} aria-valuemax={100} aria-label={props.label}>
        <div
          className={`nova-meter-fill${props.warn ? ' is-warn' : ''}`}
          style={{ width: `${Math.min(100, Math.max(2, props.value))}%` }}
        />
      </div>
    </div>
  );
}

function DevicePanel({ stats }: { stats: DeviceStats | null }) {
  if (!stats) return <div className="nova-card nova-device"><h2>{S.device}</h2><p className="nova-muted">…</p></div>;
  return (
    <section className="nova-card nova-device" aria-label={S.device}>
      <h2>{S.device}</h2>
      <Meter label={S.cpu} value={stats.cpu_percent} detail={`${Math.round(stats.cpu_percent)}%`} warn={stats.cpu_percent > 95} />
      <Meter
        label={S.ram}
        value={stats.ram_percent}
        detail={`${stats.ram_used_gb.toFixed(1)} / ${stats.ram_total_gb.toFixed(1)} ${S.gb}`}
        warn={stats.ram_percent > 90}
      />
      <Meter
        label={S.appRam}
        value={(stats.app_ram_gb / stats.ram_total_gb) * 100}
        detail={`${stats.app_ram_gb.toFixed(1)} ${S.gb}`}
      />
      <div className="nova-facts">
        <span>{S.disk}</span>
        <bdi className="nova-num">{stats.disk_free_gb.toFixed(0)} {S.gb}</bdi>
        {stats.battery && (
          <>
            <span>{S.battery}</span>
            <bdi className="nova-num">
              {stats.battery.percent}%{stats.battery.plugged ? ` · ${S.plugged}` : ''}
            </bdi>
          </>
        )}
        {stats.temperature_c != null && (
          <>
            <span>{S.temperature}</span>
            <bdi className="nova-num">{stats.temperature_c}°C</bdi>
          </>
        )}
      </div>
      {stats.ram_percent > 90 && <p className="nova-warn">{S.lowMemory}</p>}
    </section>
  );
}

const STATE_TEXT: Record<string, string> = {
  queued: S.stateQueued,
  running: S.stateRunning,
  pausing: S.statePausing,
  paused: S.statePaused,
  cancelling: S.stateCancelling,
  done: S.stateDone,
  cancelled: S.stateCancelled,
  error: S.stateError,
};

function ProgressCard(props: {
  p: JobProgress;
  receivedAt: number;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
}) {
  const { p } = props;
  const live = isActive(p.state);
  const ticking = p.state === 'running' || p.state === 'pausing' || p.state === 'cancelling';
  const now = useNow(ticking);
  const drift = ticking ? Math.max(0, (now - props.receivedAt) / 1000) : 0;
  const elapsed = p.elapsed_s + drift;
  const eta = p.eta_s == null ? null : Math.max(0, p.eta_s - drift);
  const canPause = p.state === 'running';
  const canResume = p.state === 'paused' || p.state === 'pausing';
  const canCancel = live && p.state !== 'cancelling';
  return (
    <section className="nova-card nova-progress" aria-live="polite">
      <div className="nova-progress-top">
        <div className="nova-ring-wrap">
          <Ring percent={p.percent} paused={p.state === 'paused'} />
          <bdi className="nova-ring-text nova-num">{Math.floor(p.percent)}%</bdi>
        </div>
        <div className="nova-progress-info">
          <h2>{STATE_TEXT[p.state] ?? p.state}</h2>
          <div className="nova-times">
            <div>
              <span>{S.elapsed}</span>
              <bdi className="nova-num">{formatClock(elapsed)}</bdi>
            </div>
            <div>
              <span>{S.remaining}</span>
              <bdi className="nova-num">{live ? formatClock(eta) : '0:00'}</bdi>
            </div>
            <div>
              <span>{S.total}</span>
              <bdi className="nova-num">{live ? (eta == null ? '--:--' : formatClock(elapsed + eta)) : formatClock(elapsed)}</bdi>
            </div>
          </div>
          <p className="nova-muted">
            {S.part} <bdi className="nova-num">{p.chunk}</bdi> {S.of} <bdi className="nova-num">{p.chunks}</bdi>
            {p.calibrating && live && !p.warming ? ` · ${S.calibrating}` : ''}
          </p>
          {p.warming && live && <p className="nova-note">{S.warming}</p>}
        </div>
      </div>
      <div className="nova-bar" role="progressbar" aria-valuenow={Math.floor(p.percent)} aria-valuemin={0} aria-valuemax={100}>
        <div className={`nova-bar-fill${p.warming ? ' is-indeterminate' : ''}`} style={{ width: p.warming ? '35%' : `${p.percent}%` }} />
      </div>
      {live && (
        <>
          <div className="nova-actions">
            {canPause && (
              <button type="button" className="nova-btn nova-btn-soft" onClick={props.onPause}>
                <Pause size={18} /> {S.pause}
              </button>
            )}
            {canResume && (
              <button type="button" className="nova-btn nova-btn-gold" onClick={props.onResume}>
                <Play size={18} /> {S.resume}
              </button>
            )}
            {canCancel && (
              <button type="button" className="nova-btn nova-btn-danger" onClick={props.onCancel}>
                <Square size={16} /> {S.cancel}
              </button>
            )}
          </div>
          <p className="nova-muted">{S.cancelHint} {S.runsInBackground}</p>
        </>
      )}
      {p.state === 'error' && p.error && <p className="nova-warn" dir="auto">{p.error}</p>}
    </section>
  );
}

function ResultCard({ p, onNew }: { p: JobProgress; onNew: () => void }) {
  const [saved, setSaved] = useState(false);
  const res = p.result;
  if (!res) return null;
  const url = audioUrl(res.filename);
  const save = async () => {
    try {
      const out = await saveBackendFile({ url, suggestedName: `nova-voice-${res.id}.wav` });
      if (out === null) {
        const a = document.createElement('a');
        a.href = url;
        a.download = `nova-voice-${res.id}.wav`;
        a.click();
        setSaved(true);
      } else if (!out.canceled) setSaved(true);
    } catch {
      // The interface keeps the player; the file stays in the history folder.
    }
  };
  return (
    <section className="nova-card nova-result">
      <h2>{p.partial ? S.partialResult : S.result} <bdi className="nova-num nova-muted">· {formatClock(res.duration)}</bdi></h2>
      <audio controls src={url} className="nova-audio" />
      <div className="nova-actions">
        <button type="button" className="nova-btn nova-btn-primary" onClick={save}>
          {saved ? <Check size={18} /> : <Download size={18} />} {saved ? S.saved : S.save}
        </button>
        <button type="button" className="nova-btn nova-btn-soft" onClick={onNew}>
          {S.newJob}
        </button>
      </div>
    </section>
  );
}

function notify(body: string) {
  try {
    if (!document.hidden || typeof Notification === 'undefined') return;
    const show = () => new Notification(S.appName, { body, silent: true });
    if (Notification.permission === 'granted') show();
    else if (Notification.permission !== 'denied') void Notification.requestPermission().then((r) => r === 'granted' && show());
  } catch {
    // Notifications are optional.
  }
}

function Dot({ on }: { on: boolean | null }) {
  return <span className={`nova-dot ${on === null ? 'is-unknown' : on ? 'is-on' : 'is-off'}`} aria-hidden="true" />;
}

const BADGES: Record<string, string> = { fastest: S.badgeFastest, best_arabic: S.badgeBestArabic, studio: S.badgeStudio };

function EnginePanel(props: {
  rows: EngineRow[] | null;
  selected: string;
  locked: boolean;
  onSelect: (key: Prefs['engine']) => void;
  onRefresh: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const install = async (id: string) => {
    setBusy(id);
    setErr(null);
    const failure = await installEngine(id);
    if (failure) setErr(failure);
    props.onRefresh();
    setBusy(null);
  };
  const order = ['fast', 'quality', 'expressive'];
  const rows = props.rows ? [...props.rows].sort((x, y) => order.indexOf(x.key) - order.indexOf(y.key)) : null;
  return (
    <section className="nova-card nova-enginepanel" id="nova-engine-panel" aria-label={S.modelsTitle}>
      <div className="nova-panel-head">
        <h2>{S.modelsTitle}</h2>
        <button type="button" className="nova-link" onClick={props.onRefresh}>{S.refresh}</button>
      </div>
      <p className="nova-hint">{S.modelsHint}</p>
      <ul className="nova-model-list" role="radiogroup" aria-label={S.chooseEngine}>
        {(rows ?? []).map((r) => {
          const installing = r.install?.state === 'running' || busy === r.engine_id;
          const on = props.selected === r.key;
          return (
            <li
              key={r.key}
              role="radio"
              aria-checked={on}
              tabIndex={props.locked ? -1 : 0}
              className={`nova-model${on ? ' is-on' : ''}${props.locked ? ' is-locked' : ''}`}
              onClick={() => { if (!props.locked) props.onSelect(r.key); }}
              onKeyDown={(e) => { if (!props.locked && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); props.onSelect(r.key); } }}
            >
              <Dot on={r.available} />
              <div className="nova-model-info">
                <strong>
                  {r.label}
                  {r.badge && BADGES[r.badge] && <span className="nova-badge">{BADGES[r.badge]}</span>}
                </strong>
                <span className="nova-muted" dir="auto">{r.model}</span>
                <span className="nova-muted">
                  {r.available ? `${S.connected}${r.loaded ? ` · ${S.loadedInMemory}` : ''}` : S.notConnected}
                  {r.available && r.weights_cached === false ? ` · ${S.weightsMissing}` : ''}
                </span>
                {r.arabic_note && (
                  <button
                    type="button"
                    className="nova-link nova-details-btn"
                    onClick={(e) => { e.stopPropagation(); setOpen(open === r.key ? null : r.key); }}
                  >
                    {open === r.key ? S.lessDetails : S.moreDetails}
                  </button>
                )}
                {open === r.key && r.arabic_note && <span className="nova-note-ar" dir="auto">{r.arabic_note}</span>}
                {r.fit && r.min_ram_gb && (
                  <span className={`nova-fit is-${r.fit}`}>
                    {r.fit === 'ok' ? S.fitOk : r.fit === 'tight' ? S.fitTight : S.fitHeavy}
                    {' · '}
                    {S.needsRam} <bdi className="nova-num">{r.min_ram_gb}</bdi> {S.gb}
                    {r.system_ram_gb ? <> · {S.yourRam} <bdi className="nova-num">{r.system_ram_gb}</bdi> {S.gb}</> : null}
                  </span>
                )}
                {!r.available && r.reason && <span className="nova-reason" dir="auto">{r.reason}</span>}
                {installing && <span className="nova-note">{S.installing}</span>}
                {r.install?.state === 'failed' && r.install.error && (
                  <span className="nova-reason" dir="auto">{S.installFailed}: {r.install.error}</span>
                )}
              </div>
              {!r.available && r.installable && !installing && (
                <button
                  type="button"
                  className="nova-btn nova-btn-soft"
                  onClick={(e) => { e.stopPropagation(); void install(r.engine_id); }}
                >
                  {S.install}
                </button>
              )}
            </li>
          );
        })}
        {!rows && <li className="nova-muted">…</li>}
      </ul>
      {err && <p className="nova-warn" dir="auto">{err}</p>}
    </section>
  );
}

export function NovaPage() {
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [muted, setMuted] = useState(sfx.isMuted());
  const [chimeMuted, setChimeMuted] = useState(sfx.isChimeMuted());
  const job = useNovaJob();
  const { progress } = job;
  const live = isActive(progress?.state);
  const stats = useDeviceStats(live);
  const [showModels, setShowModels] = useState(false);
  const engines = useEngines(true, showModels ? 3000 : 15000);
  const currentEngine = engines.rows?.find((r) => r.key === prefs.engine) ?? null;
  const engineReady = currentEngine ? currentEngine.available : true;
  const anyOnline = engines.rows ? engines.rows.some((r) => r.available) : null;
  const autoPicked = useRef(false);
  useEffect(() => {
    if (autoPicked.current || !engines.rows) return;
    autoPicked.current = true;
    const cur = engines.rows.find((r) => r.key === prefs.engine);
    if (cur && !cur.available) {
      const pick = ['quality', 'fast', 'expressive'].map((k) => engines.rows!.find((r) => r.key === k)).find((r) => r && r.available);
      if (pick) setPrefs((p) => ({ ...p, engine: pick.key }));
    }
  }, [engines.rows, prefs.engine]);
  const set = useCallback(<K extends keyof Prefs>(key: K, value: Prefs[K]) => setPrefs((p) => ({ ...p, [key]: value })), []);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
    } catch {
      // Settings simply are not remembered.
    }
  }, [prefs]);

  // Sound + notification when a job reaches an end state.
  const lastState = useRef<string | undefined>(undefined);
  useEffect(() => {
    const s = progress?.state;
    if (s === lastState.current) return;
    const was = lastState.current;
    lastState.current = s;
    if (!was || !isActive(was as JobProgress['state'])) return;
    if (s === 'done') {
      sfx.done();
      notify(S.notifyDone);
    } else if (s === 'cancelled') {
      sfx.stopped();
      notify(S.notifyCancelled);
    } else if (s === 'error') {
      sfx.failed();
      notify(S.notifyError);
    }
  }, [progress?.state]);

  const voices = VOICES[prefs.gender];
  const voice = prefs.voice && voices.includes(prefs.voice) ? prefs.voice : voices[0];
  const fast = prefs.engine === 'fast';
  const words = useMemo(() => countWords(prefs.text), [prefs.text]);
  const canGenerate = prefs.text.trim().length > 0 && !live && !job.starting && engineReady;

  const generate = () => {
    if (!canGenerate) return;
    sfx.start();
    const { wavBits, theme: _theme, ...rest } = prefs;
    void _theme;
    const req: JobRequest = { ...rest, wav_bits: wavBits, voice, language: 'ar' };
    void job.start(req);
  };

  const onClickCapture = (e: ReactMouseEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-nosound]')) return;
    if (t.closest('.nova-chip, .nova-seg button')) sfx.select();
    else if (t.closest('button, a')) sfx.click();
  };

  const showResult = progress && progress.result && !live;

  return (
    <div className="nova-root" data-theme={prefs.theme} dir="rtl" lang="ar" onClickCapture={onClickCapture}>
      <header className="nova-header">
        <div className="nova-brand">
          <Logo />
          <div>
            <h1>{S.appName}</h1>
            <p>{S.tagline}</p>
          </div>
        </div>
        <div className="nova-header-actions" data-nodrag>
          <button
            type="button"
            className="nova-pill-btn"
            data-nosound
            onClick={() => { sfx.click(); setShowModels((v) => !v); }}
            title={S.modelsTitle}
            aria-label={S.modelsTitle}
          >
            <Dot on={anyOnline} /> <Cpu size={17} /> <span>{S.models}</span>
          </button>
          <button
            type="button"
            className="nova-icon-btn"
            data-nosound
            title={S.theme}
            aria-label={S.theme}
            onClick={() => { sfx.click(); set('theme', prefs.theme === 'aurora' ? 'night' : 'aurora'); }}
          >
            {prefs.theme === 'aurora' ? <Moon size={20} /> : <Sun size={20} />}
          </button>
          <button
            type="button"
            className="nova-icon-btn"
            data-nosound
            title={chimeMuted ? S.chimeOff : S.chimeOn}
            aria-label={chimeMuted ? S.chimeOff : S.chimeOn}
            onClick={() => {
              const next = !chimeMuted;
              sfx.setChimeMuted(next);
              setChimeMuted(next);
              if (!next) sfx.done();
            }}
          >
            {chimeMuted ? <BellOff size={20} /> : <Bell size={20} />}
          </button>
          <button
            type="button"
            className="nova-icon-btn"
            data-nosound
            title={muted ? S.soundOff : S.soundOn}
            aria-label={muted ? S.soundOff : S.soundOn}
            onClick={() => {
              const next = !muted;
              sfx.setMuted(next);
              setMuted(next);
              if (!next) sfx.click();
            }}
          >
            {muted ? <VolumeX size={20} /> : <Volume2 size={20} />}
          </button>
          <Link to="/settings" className="nova-icon-btn" title={S.settings} aria-label={S.settings}>
            <SettingsIcon size={20} />
          </Link>
        </div>
      </header>

      <main className="nova-main">
        <div className="nova-col-main">
          <button
            type="button"
            className={`nova-model-toggle${showModels ? ' is-open' : ''}`}
            data-nosound
            aria-expanded={showModels}
            aria-controls="nova-engine-panel"
            onClick={() => { sfx.click(); setShowModels((v) => !v); }}
          >
            <Dot on={currentEngine ? currentEngine.available : null} />
            <Cpu size={18} />
            <span className="nova-model-toggle-text">
              {S.models}
              {currentEngine ? <span className="nova-muted"> · {currentEngine.label}</span> : null}
            </span>
            <ChevronDown size={18} className="nova-chev" />
          </button>
          {showModels && (
            <EnginePanel rows={engines.rows} selected={prefs.engine} locked={live} onSelect={(k) => set('engine', k)} onRefresh={engines.refresh} />
          )}

          <section className="nova-card nova-text-card">
            <div className="nova-text-head">
              <label htmlFor="nova-text">{S.textLabel}</label>
              <div className="nova-text-tools">
                {!prefs.text && (
                  <button type="button" className="nova-link" onClick={() => set('text', S.sampleText)}>
                    {S.sample}
                  </button>
                )}
                {prefs.text && !live && (
                  <button type="button" className="nova-link" onClick={() => set('text', '')}>
                    {S.clear}
                  </button>
                )}
              </div>
            </div>
            <textarea
              id="nova-text"
              dir="auto"
              value={prefs.text}
              placeholder={S.textPlaceholder}
              onChange={(e) => set('text', e.target.value)}
              disabled={live}
              spellCheck={false}
            />
            <div className="nova-text-foot">
              <span className="nova-muted">
                <bdi className="nova-num">{prefs.text.length.toLocaleString('en-US')}</bdi> {S.chars} · <bdi className="nova-num">{words.toLocaleString('en-US')}</bdi> {S.words}
              </span>
              <button type="button" className="nova-btn nova-btn-primary nova-generate" disabled={!canGenerate} onClick={generate}>
                <AudioLines size={20} /> {S.generate}
              </button>
            </div>
            {!engineReady && currentEngine && (
              <p className="nova-warn" dir="auto">
                {S.engineNotReady} {currentEngine.reason || S.notConnected}{' '}
                <button type="button" className="nova-link" onClick={() => setShowModels(true)}>{S.models}</button>
              </p>
            )}
            {currentEngine?.fit === 'heavy' && engineReady && (
              <p className="nova-warn" dir="auto">{S.heavyWarn}</p>
            )}
            {job.error && <p className="nova-warn" dir="auto">{job.error}</p>}
          </section>

          {progress && <ProgressCard p={progress} receivedAt={job.receivedAt} onPause={() => { sfx.pause(); void job.pause(); }} onResume={() => { sfx.resume(); void job.resume(); }} onCancel={() => void job.cancel()} />}
          {showResult && <ResultCard p={progress} onNew={job.reset} />}
        </div>

        <aside className="nova-col-side">
          <section className="nova-card nova-controls">
            <div className="nova-field">
              <span className="nova-label">{S.style}</span>
              <div className="nova-chips">
                {STYLE_ORDER.map((id) => (
                  <button
                    key={id}
                    type="button"
                    disabled={live}
                    className={`nova-chip${prefs.style === id ? ' is-on' : ''}`}
                    aria-pressed={prefs.style === id}
                    onClick={() => set('style', id)}
                  >
                    {STYLE_LABELS[id]}
                  </button>
                ))}
              </div>
            </div>

            <div className="nova-field">
              <span className="nova-label">{S.gender}</span>
              <Segmented
                label={S.gender}
                value={prefs.gender}
                onChange={(v) => set('gender', v)}
                disabled={live}
                options={[
                  { value: 'female', label: S.female },
                  { value: 'male', label: S.male },
                ]}
              />
            </div>

            {fast ? (
              <div className="nova-field">
                <span className="nova-label">{S.voice}</span>
                <div className="nova-chips">
                  {voices.map((v, i) => (
                    <button
                      key={v}
                      type="button"
                      disabled={live}
                      className={`nova-chip${voice === v ? ' is-on' : ''}`}
                      aria-pressed={voice === v}
                      onClick={() => set('voice', v)}
                    >
                      {S.voice} <bdi className="nova-num">{i + 1}</bdi>
                    </button>
                  ))}
                </div>
                <p className="nova-hint">{S.ageFastNote}</p>
              </div>
            ) : (
              <div className="nova-field">
                <label className="nova-label" htmlFor="nova-age">{S.age}</label>
                <select id="nova-age" className="nova-select" value={prefs.age} disabled={live} onChange={(e) => set('age', e.target.value)}>
                  {AGE_ORDER.map((a) => (
                    <option key={a} value={a}>{AGE_LABELS[a]}</option>
                  ))}
                </select>
              </div>
            )}

            <div className="nova-field">
              <span className="nova-label">{S.audioQuality}</span>
              <Segmented
                label={S.audioQuality}
                value={prefs.quality as (typeof QUALITY_ORDER)[number]}
                onChange={(v) => set('quality', v)}
                disabled={live}
                options={QUALITY_ORDER.map((q) => ({ value: q, label: QUALITY_LABELS[q] }))}
              />
              <p className="nova-hint">{S.qualityHint}</p>
              <label className="nova-label" htmlFor="nova-bits">{S.fileFormat}</label>
              <select id="nova-bits" className="nova-select" value={prefs.wavBits} disabled={live} onChange={(e) => set('wavBits', Number(e.target.value) as 16 | 24 | 32)}>
                <option value={16}>{S.bits16}</option>
                <option value={24}>{S.bits24}</option>
                <option value={32}>{S.bits32}</option>
              </select>
            </div>

            <Slider label={S.speed} value={prefs.speed} min={0.6} max={1.6} step={0.05} low={S.slower} high={S.faster} format={(v) => `×${v.toFixed(2)}`} onChange={(v) => set('speed', v)} disabled={live} />
            <Slider label={S.pitch} value={prefs.pitch} min={-6} max={6} step={0.5} low={S.deeper} high={S.higher} format={(v) => (v > 0 ? `+${v}` : `${v}`)} onChange={(v) => set('pitch', v)} disabled={live} />
            <Slider label={S.depth} value={prefs.depth} min={-2} max={2} step={1} low={S.deeper} high={S.higher} format={(v) => (v > 0 ? `+${v}` : `${v}`)} onChange={(v) => set('depth', v)} disabled={live} />
            <button type="button" className="nova-link" disabled={live} onClick={() => setPrefs((p) => ({ ...p, speed: 1, pitch: 0, depth: 0 }))}>
              {S.reset}
            </button>
          </section>

          {live && <DevicePanel stats={stats} />}
        </aside>
      </main>
    </div>
  );
}
