import { apiFetch, ago, num, pct } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Table, Td } from '@/components/ui';

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
  }[];
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

      <Panel title="External evidence sources" subtitle="tiered by authority; a disabled source contributes nothing and says so">
        {data.external_sources.length === 0 ? (
          <Empty message="No external evidence source is registered yet." />
        ) : (
          <Table headers={['Source', 'Tier', 'Type', 'Enabled', 'Health', 'Reliability', 'Last success', 'Last error', 'Error rate', 'Latency', 'Notes']}>
            {data.external_sources.map((s) => (
              <tr key={s.name}>
                <Td>{s.name}</Td>
                <Td mono>{s.tier}</Td>
                <Td mono><span className="text-muted">{s.source_type}</span></Td>
                <Td><Badge value={s.enabled ? 'ENABLED' : 'DISABLED'} /></Td>
                <Td><Badge value={s.health} /></Td>
                <Td mono>{num(s.reliability_score, 2)}</Td>
                <Td mono><span className="text-muted">{ago(s.last_success_at)}</span></Td>
                <Td mono><span className="text-muted">{s.last_error_code ?? '—'}</span></Td>
                <Td mono>{pct(s.error_rate, 1)}</Td>
                <Td mono>{s.last_latency_ms === null ? '—' : `${s.last_latency_ms}ms`}</Td>
                <Td><span className="text-[11px] text-muted">{s.usage_notes ?? ''}</span></Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>
    </div>
  );
}
