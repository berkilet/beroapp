import { apiFetch, ago, num, usd } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Stat, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface System {
  phase: string;
  live_trading_enabled: boolean;
  overall_health: string;
  components: { component: string; health: string; last_event_at: string | null; message: string }[];
  data_freshness: { last_update_at: string | null; age_seconds: number | null; is_stale: boolean };
  configuration: Record<string, any>;
  recent_events: {
    component: string;
    event: string;
    severity: string;
    health: string | null;
    error_code: string | null;
    duration_ms: number | null;
    occurred_at: string;
    detail: Record<string, any> | null;
  }[];
}

export default async function SystemPage() {
  const { data, error } = await apiFetch<System>('/api/system');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  const cfg = data.configuration;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">System</h1>
        <p className="mt-1 text-xs text-muted">Operational state, configuration and recent events.</p>
      </header>

      <Notice tone={data.live_trading_enabled ? 'warn' : 'info'}>
        Phase <strong className="font-semibold">{data.phase}</strong>. Live trading is{' '}
        <strong className="font-semibold">{data.live_trading_enabled ? 'ENABLED' : 'DISABLED'}</strong>.
      </Notice>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Overall health" value={data.overall_health} tone={data.overall_health === 'HEALTHY' ? 'ok' : 'warn'} />
        <Stat label="Last market data" value={ago(data.data_freshness.last_update_at)} tone={data.data_freshness.is_stale ? 'warn' : 'ok'} />
        <Stat label="Snapshot interval" value={`${cfg.snapshot_interval_s}s`} />
        <Stat label="Staleness limit" value={`${cfg.data_staleness_s}s`} />
      </div>

      <Panel title="Component status">
        <Table headers={['Component', 'Health', 'Last event', 'Detail']}>
          {data.components.map((c) => (
            <tr key={c.component}>
              <Td mono>{c.component}</Td>
              <Td><Badge value={c.health} /></Td>
              <Td mono><span className="text-muted">{ago(c.last_event_at)}</span></Td>
              <Td><span className="text-muted">{c.message}</span></Td>
            </tr>
          ))}
        </Table>
      </Panel>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Signal thresholds">
          <Table headers={['Setting', 'Value']}>
            <tr><Td>Minimum executable edge</Td><Td mono>{num(cfg.min_executable_edge, 3)}</Td></tr>
            <tr><Td>Minimum confidence</Td><Td mono>{num(cfg.min_confidence, 2)}</Td></tr>
            <tr><Td>Minimum liquidity</Td><Td mono>{usd(cfg.min_liquidity)}</Td></tr>
            <tr><Td>Maximum spread</Td><Td mono>{num(cfg.max_spread, 3)}</Td></tr>
            <tr><Td>Maximum allowed slippage</Td><Td mono>{num(cfg.max_allowed_slippage, 3)}</Td></tr>
            <tr><Td>Virtual capital</Td><Td mono>{usd(cfg.virtual_initial_capital)}</Td></tr>
          </Table>
        </Panel>

        <Panel title="Hard risk limits" subtitle="enforced by deterministic code; no model can override them">
          <Table headers={['Limit', 'Value']}>
            {Object.entries(cfg.risk_limits ?? {}).map(([k, v]) => (
              <tr key={k}>
                <Td mono>{k}</Td>
                <Td mono>{String(v)}%</Td>
              </tr>
            ))}
          </Table>
        </Panel>
      </div>

      <Panel title="Recent system events" subtitle="append-only operational log">
        {data.recent_events.length === 0 ? (
          <Empty message="No system events recorded." />
        ) : (
          <Table headers={['When', 'Component', 'Event', 'Severity', 'Health', 'Error', 'Duration']}>
            {data.recent_events.slice(0, 60).map((e, i) => (
              <tr key={i}>
                <Td mono><span className="text-muted">{ago(e.occurred_at)}</span></Td>
                <Td mono>{e.component}</Td>
                <Td mono>{e.event}</Td>
                <Td><Badge value={e.severity} muted /></Td>
                <Td><Badge value={e.health ?? undefined} /></Td>
                <Td mono><span className="text-bad">{e.error_code ?? ''}</span></Td>
                <Td mono>{e.duration_ms === null ? '—' : `${e.duration_ms}ms`}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>
    </div>
  );
}
