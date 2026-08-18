/**
 * Shared presentation components.
 *
 * Functional rather than decorative, per spec. Two rules run through all of
 * them:
 *
 * 1. Every value that could be unknown renders as an em dash, never as zero.
 * 2. All text is rendered as a React text node. `dangerouslySetInnerHTML` does
 *    not appear anywhere in this codebase — market descriptions are attacker-
 *    controlled strings and are treated as data on the way out as well as in.
 */

import Link from 'next/link';
import type { ReactNode } from 'react';

export function Panel({
  title,
  subtitle,
  children,
  right,
}: {
  title?: string;
  subtitle?: string;
  children: ReactNode;
  right?: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-edge bg-panel">
      {(title || right) && (
        <header className="flex items-start justify-between gap-4 border-b border-edge px-4 py-3">
          <div>
            {title && <h2 className="text-sm font-semibold tracking-wide text-gray-200">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'neutral' | 'ok' | 'warn' | 'bad';
}) {
  const toneClass = {
    neutral: 'text-gray-100',
    ok: 'text-ok',
    warn: 'text-warn',
    bad: 'text-bad',
  }[tone];
  return (
    <div className="rounded-md border border-edge bg-surface px-3 py-2.5">
      <div className="text-[11px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-1 font-mono text-lg ${toneClass}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-muted">{hint}</div>}
    </div>
  );
}

const HEALTH_TONE: Record<string, string> = {
  HEALTHY: 'bg-ok/15 text-ok border-ok/30',
  DEGRADED: 'bg-warn/15 text-warn border-warn/30',
  STALE: 'bg-warn/15 text-warn border-warn/30',
  FAILED: 'bg-bad/15 text-bad border-bad/30',
  DISABLED: 'bg-muted/10 text-muted border-muted/30',
  UNKNOWN: 'bg-muted/10 text-muted border-muted/30',
};

const RECOMMENDATION_TONE: Record<string, string> = {
  BUY: 'bg-ok/15 text-ok border-ok/30',
  SELL: 'bg-info/15 text-info border-info/30',
  HOLD: 'bg-muted/10 text-muted border-muted/30',
  WATCH: 'bg-warn/10 text-warn border-warn/30',
  NO_TRADE: 'bg-bad/10 text-bad border-bad/30',
  INSUFFICIENT_DATA: 'bg-muted/10 text-muted border-muted/30',
  TRADEABLE: 'bg-ok/15 text-ok border-ok/30',
  WATCHLIST: 'bg-warn/10 text-warn border-warn/30',
  UNMODELABLE: 'bg-bad/10 text-bad border-bad/30',
  RESOLUTION_RISK: 'bg-bad/10 text-bad border-bad/30',
  APPROVED: 'bg-ok/15 text-ok border-ok/30',
  REJECTED: 'bg-bad/10 text-bad border-bad/30',
  BLOCKED_BY_KILL_SWITCH: 'bg-bad/15 text-bad border-bad/30',
  NOT_EVALUATED: 'bg-muted/10 text-muted border-muted/30',
  LOW: 'bg-ok/15 text-ok border-ok/30',
  MEDIUM: 'bg-warn/15 text-warn border-warn/30',
  HIGH: 'bg-bad/15 text-bad border-bad/30',
  // Signal strength. SIGNAL is the only state that clears every evidence gate,
  // so it is the only one coloured as an affirmative result.
  SIGNAL: 'bg-ok/15 text-ok border-ok/30',
  CANDIDATE: 'bg-warn/15 text-warn border-warn/30',
  NONE: 'bg-muted/10 text-muted border-muted/30',
  READY: 'bg-ok/15 text-ok border-ok/30',
  EVIDENCE: 'bg-ok/15 text-ok border-ok/30',
  NO_EVIDENCE: 'bg-warn/10 text-warn border-warn/30',
  NOT_ASSESSED: 'bg-muted/10 text-muted border-muted/30',
  // Evidence verification and conflict resolution.
  CONFIRMED_FACT: 'bg-ok/15 text-ok border-ok/30',
  REPORTED_INFORMATION: 'bg-info/15 text-info border-info/30',
  UNCONFIRMED_CLAIM: 'bg-warn/10 text-warn border-warn/30',
  ANALYST_OPINION: 'bg-warn/10 text-warn border-warn/30',
  UNVERIFIED: 'bg-muted/10 text-muted border-muted/30',
  SUPERSEDED: 'bg-muted/10 text-muted border-muted/30',
  UNRESOLVED: 'bg-bad/15 text-bad border-bad/30',
};

export function Badge({ value, muted = false }: { value: string | null | undefined; muted?: boolean }) {
  if (!value) return <span className="text-muted">—</span>;
  const tone =
    HEALTH_TONE[value] ??
    RECOMMENDATION_TONE[value] ??
    (muted ? 'bg-muted/10 text-muted border-muted/30' : 'bg-edge text-gray-300 border-edge');
  return (
    <span className={`inline-block rounded border px-1.5 py-0.5 font-mono text-[11px] ${tone}`}>
      {value}
    </span>
  );
}

export function Table({ headers, children }: { headers: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-max text-left text-xs">
        <thead>
          <tr className="border-b border-edge text-[11px] uppercase tracking-wider text-muted">
            {headers.map((h) => (
              <th key={h} className="whitespace-nowrap px-2 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-edge/60">{children}</tbody>
      </table>
    </div>
  );
}

export function Td({ children, mono = false }: { children: ReactNode; mono?: boolean }) {
  return (
    <td className={`whitespace-nowrap px-2 py-2 align-top ${mono ? 'font-mono' : ''}`}>{children}</td>
  );
}

export function Empty({ message }: { message: string }) {
  return <p className="py-6 text-center text-xs text-muted">{message}</p>;
}

export function ErrorBanner({ error }: { error: string }) {
  return (
    <div className="rounded-md border border-bad/40 bg-bad/10 px-4 py-3 text-xs text-bad">
      <strong className="font-semibold">Data unavailable.</strong> {error}
      <div className="mt-1 text-muted">
        Nothing is shown rather than showing a value that might be wrong.
      </div>
    </div>
  );
}

export function Notice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warn' }) {
  const cls =
    tone === 'warn'
      ? 'border-warn/40 bg-warn/10 text-warn'
      : 'border-info/30 bg-info/10 text-info';
  return <div className={`rounded-md border px-4 py-2.5 text-xs ${cls}`}>{children}</div>;
}

export function MarketLink({ id, question }: { id: number; question: string | null }) {
  return (
    <Link href={`/markets/${id}`} className="text-info hover:underline">
      {question ?? `market ${id}`}
    </Link>
  );
}

/** Renders a percentage-point edge with a sign-appropriate tone. */
export function EdgeCell({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="text-muted">—</span>;
  const tone = value > 0 ? 'text-ok' : value < 0 ? 'text-bad' : 'text-muted';
  return (
    <span className={`font-mono ${tone}`}>
      {value > 0 ? '+' : ''}
      {(value * 100).toFixed(2)}pp
    </span>
  );
}
