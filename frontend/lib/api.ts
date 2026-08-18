/**
 * Server-side API client.
 *
 * This module runs ONLY on the Next.js server. The API key is read from a
 * non-`NEXT_PUBLIC_` environment variable and is therefore never serialised
 * into client props or shipped in the browser bundle — the browser talks to
 * this app, this app talks to the backend, and the credential stays on the
 * server side of that boundary.
 */

const API_BASE = process.env.BACKEND_API_URL ?? 'http://127.0.0.1:8000';
const API_KEY = process.env.BACKEND_API_KEY ?? '';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export interface FetchResult<T> {
  data: T | null;
  error: string | null;
}

/**
 * Fetch JSON from the backend.
 *
 * Never throws to the page. A dashboard whose backend is down should say the
 * backend is down, not render a stack trace or — worse — an empty state that
 * looks like "no opportunities found".
 */
export async function apiFetch<T>(path: string, revalidate = 15): Promise<FetchResult<T>> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (API_KEY) headers['X-API-Key'] = API_KEY;

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers,
      next: { revalidate },
      signal: AbortSignal.timeout(20_000),
    });

    if (!response.ok) {
      return {
        data: null,
        error: `Backend returned HTTP ${response.status} for ${path}`,
      };
    }
    return { data: (await response.json()) as T, error: null };
  } catch (err) {
    const reason = err instanceof Error ? err.message : 'unknown error';
    return {
      data: null,
      error: `Could not reach the backend at ${API_BASE}: ${reason}`,
    };
  }
}

// ---------------------------------------------------------------------------
// Formatting helpers.
//
// All of these return an explicit dash for null/undefined rather than 0 or an
// empty string. "Unknown" and "zero" are different facts and the dashboard must
// not conflate them.
// ---------------------------------------------------------------------------

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

export function pctPoints(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const sign = value > 0 ? '+' : '';
  return `${sign}${(value * 100).toFixed(digits)}pp`;
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `$${value.toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return value.toLocaleString('en-US');
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 0) return 'in the future';
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

export function until(iso: string | null | undefined): string {
  if (!iso) return '—';
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  if (seconds <= 0) return 'past';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}
