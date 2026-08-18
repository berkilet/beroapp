import { apiFetch, ago, count, num, pct } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Stat, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface DataSources {
  polymarket: {
    name: string;
    tier: number;
    source_type: string;
    base_url: string;
    purpose: string;
    status: string;
    documented_rate_limit: string;
    configured_rps: number;
  }[];
  market_data_feed_health: { health: string; message: string; last_event_at: string | null } | null;
  external_sources: {
    name: string;
    tier: number;
    source_type: string;
    base_url: string | null;
    enabled: boolean;
    requires_api_key: boolean;
    health: string;
    reliability_score: number;
    last_success_at: string | null;
    last_error_at: string | null;
    last_error_code: string | null;
    last_latency_ms: number | null;
    error_rate: number | null;
    usage_notes: string | null;
    terms_url: string | null;
    access_method: string | null;
    update_frequency_s: number | null;
    daily_request_budget: number | null;
    requests_today: number;
    evidence_items: number;
    newest_evidence_at: string | null;
    categories: { categories?: string[]; subcategories?: string[] } | null;
  }[];
  totals: {
    evidence_items: number;
    enabled_sources: number;
    declared_sources: number;
  };
}

export default async function DataSourcesPage() {
  const { data, error } = await apiFetch<DataSources>('/api/data-sources');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Data Sources</h1>
        <p className="mt-1 text-xs text-muted">
          Which feeds are actually working, and which are registered but not implemented. A source
          listed here is not a source that is running unless its status says so.
        </p>
      </header>

      <Notice>
        Request budgets are configured far below the documented ceilings. The point is not to
        maximise throughput; it is to stay comfortably inside a published limit.
      </Notice>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="Evidence items"
          value={count(data.totals.evidence_items)}
          hint="external observations stored"
        />
        <Stat
          label="Enabled sources"
          value={`${data.totals.enabled_sources} / ${data.totals.declared_sources}`}
          hint="declared sources that are actually running"
        />
        <Stat
          label="Polymarket feeds"
          value={count(data.polymarket.length)}
          hint="primary market data"
        />
        <Stat
          label="Market feed"
          value={data.market_data_feed_health?.health ?? 'UNKNOWN'}
          tone={data.market_data_feed_health?.health === 'HEALTHY' ? 'ok' : 'warn'}
        />
      </div>

      <Panel
        title="Polymarket (primary market data)"
        subtitle={data.market_data_feed_health ? `feed ${data.market_data_feed_health.health} — ${data.market_data_feed_health.message}` : undefined}
      >
        <Table headers={['Source', 'Tier', 'Purpose', 'Endpoint', 'Status', 'Documented limit', 'Our rate']}>
          {data.polymarket.map((s) => (
            <tr key={s.name}>
              <Td>{s.name}</Td>
              <Td mono>{s.tier}</Td>
              <Td><span className="text-muted">{s.purpose}</span></Td>
              <Td mono><span className="text-muted">{s.base_url}</span></Td>
              <Td><Badge value={s.status} /></Td>
              <Td mono><span className="text-muted">{s.documented_rate_limit}</span></Td>
              <Td mono>{num(s.configured_rps, 1)}/s</Td>
            </tr>
          ))}
        </Table>
      </Panel>

      <Panel
        title="External evidence sources"
        subtitle="tiered by authority; a disabled source contributes nothing and says so"
      >
        {data.external_sources.length === 0 ? (
          <Empty message="No external evidence source is registered yet." />
        ) : (
          <Table
            headers={[
              'Source',
              'Tier',
              'Type',
              'Enabled',
              'Health',
              'Evidence',
              'Newest',
              'Budget today',
              'Access',
              'Reliability',
              'Last success',
              'Last error',
              'Error rate',
              'Latency',
              'Categories',
              'Notes',
            ]}
          >
            {data.external_sources.map((s) => (
              <tr key={s.name}>
                <Td>
                  {s.terms_url ? (
                    <a href={s.terms_url} className="text-info hover:underline" rel="noreferrer noopener" target="_blank">
                      {s.name}
                    </a>
                  ) : (
                    s.name
                  )}
                </Td>
                <Td mono>{s.tier}</Td>
                <Td mono><span className="text-muted">{s.source_type}</span></Td>
                <Td><Badge value={s.enabled ? 'ENABLED' : 'DISABLED'} /></Td>
                <Td><Badge value={s.health} /></Td>
                <Td mono>{count(s.evidence_items)}</Td>
                <Td mono><span className="text-muted">{ago(s.newest_evidence_at)}</span></Td>
                {/* A budget is only meaningful where the source publishes one.
                    Where it does, this is the number to watch: exhausting it
                    means the connector stops rather than exceeding the limit. */}
                <Td mono>
                  {s.daily_request_budget === null ? (
                    <span className="text-muted">unmetered</span>
                  ) : (
                    <span className={s.requests_today >= s.daily_request_budget ? 'text-warn' : undefined}>
                      {s.requests_today} / {s.daily_request_budget}
                    </span>
                  )}
                </Td>
                <Td mono><span className="text-muted">{s.access_method ?? '—'}</span></Td>
                <Td mono>{num(s.reliability_score, 2)}</Td>
                <Td mono><span className="text-muted">{ago(s.last_success_at)}</span></Td>
                <Td mono><span className="text-muted">{s.last_error_code ?? '—'}</span></Td>
                <Td mono>{pct(s.error_rate, 1)}</Td>
                <Td mono>{s.last_latency_ms === null ? '—' : `${s.last_latency_ms}ms`}</Td>
                <Td>
                  {/* Stored as an object with both levels; the subcategory list
                      is the one that decides routing, so it is shown first. */}
                  <span className="text-[11px] text-muted">
                    {[
                      ...(s.categories?.subcategories ?? []),
                      ...(s.categories?.categories ?? []),
                    ].join(', ') || '—'}
                  </span>
                </Td>
                <Td><span className="text-[11px] text-muted">{s.usage_notes ?? ''}</span></Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>

      <Panel title="What a registered source is not">
        <div className="space-y-1.5 text-xs text-muted">
          <p>
            A row above is a <span className="text-gray-300">declaration</span>, not a promise. A
            source with <span className="font-mono">DISABLED</span> and zero evidence items is
            registered so that the allow-list, the terms link and the reason it is off are all
            visible — usually a missing API key we have deliberately not shipped a shared fallback
            for.
          </p>
          <p>
            Evidence counts are cumulative, not a health signal. Check the newest-evidence column
            for whether a connector is still working.
          </p>
        </div>
      </Panel>
    </div>
  );
}
