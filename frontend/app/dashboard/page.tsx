import { apiFetch, ago, count, num, pct, usd } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Stat, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface ComponentStatus {
  component: string;
  health: string;
  last_event_at: string | null;
  age_seconds: number | null;
  message: string;
}

interface Dashboard {
  phase: {
    current: string;
    live_trading_enabled: boolean;
    paper_trading_active: boolean;
    notice: string;
  };
  system_health: string;
  components: ComponentStatus[];
  data_freshness: { last_update_at: string | null; age_seconds: number | null; is_stale: boolean };
  counters: Record<string, number>;
  kill_switches: Record<string, { tripped: boolean; reason: string }>;
  portfolio: {
    is_virtual: boolean;
    capital_label: string;
    initial_capital_usd: number;
    equity_usd: number | null;
    roi_pct?: number;
    realised_pnl_usd?: number;
    unrealised_pnl_usd?: number;
    drawdown_pct?: number;
    note?: string;
  };
  calibration: Record<string, any> | null;
  calibration_sample_size: number;
}

export default async function DashboardPage() {
  const { data, error } = await apiFetch<Dashboard>('/api/dashboard');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  const c = data.counters;
  const brier = data.calibration?.model?.brier_score ?? null;
  const insufficient = data.calibration?.model?.insufficient_data ?? true;

  const healthTone = (h: string) =>
    h === 'HEALTHY' ? 'ok' : h === 'FAILED' ? 'bad' : ('warn' as const);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Dashboard</h1>
        <p className="mt-1 text-xs text-muted">
          Every figure below is computed from stored observations. Where a value is unknown it
          shows as a dash rather than a zero.
        </p>
      </header>

      <Notice tone={data.phase.live_trading_enabled ? 'warn' : 'info'}>
        <strong className="font-semibold">{data.phase.current}</strong> — {data.phase.notice}
      </Notice>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="System health" value={data.system_health} tone={healthTone(data.system_health)} />
        <Stat
          label="Last market update"
          value={ago(data.data_freshness.last_update_at)}
          tone={data.data_freshness.is_stale ? 'warn' : 'ok'}
          hint={data.data_freshness.is_stale ? 'stale — signals suppressed' : 'within freshness limit'}
        />
        <Stat label="Markets discovered" value={count(c.markets_discovered)} />
        <Stat
          label="Markets with book data"
          value={count(c.markets_with_market_data)}
          hint="order book observed at least once"
        />
        <Stat label="Modelled: tradeable" value={count(c.markets_tradeable)} />
        <Stat label="Modelled: watchlist" value={count(c.markets_watchlist)} />
        <Stat label="Predictions stored" value={count(c.predictions_total)} hint={`${count(c.predictions_24h)} in 24h`} />
        <Stat
          label="Opportunities (24h)"
          value={count(c.opportunities_24h)}
          hint={`${count(c.high_confidence_opportunities_24h)} high confidence`}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel
          title="Virtual portfolio"
          subtitle={data.portfolio.capital_label}
          right={<Badge value="VIRTUAL" />}
        >
          {data.portfolio.equity_usd === null ? (
            <div className="space-y-3">
              <Stat
                label="Configured virtual capital"
                value={usd(data.portfolio.initial_capital_usd)}
                hint="not yet deployed"
              />
              <p className="text-xs text-muted">{data.portfolio.note}</p>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Equity (virtual)" value={usd(data.portfolio.equity_usd)} />
              <Stat
                label="ROI"
                value={data.portfolio.roi_pct === undefined ? '—' : `${num(data.portfolio.roi_pct)}%`}
                tone={(data.portfolio.roi_pct ?? 0) >= 0 ? 'ok' : 'bad'}
              />
              <Stat label="Realised P&L" value={usd(data.portfolio.realised_pnl_usd)} />
              <Stat
                label="Max drawdown"
                value={data.portfolio.drawdown_pct === undefined ? '—' : `${num(data.portfolio.drawdown_pct)}%`}
                tone="warn"
              />
            </div>
          )}
        </Panel>

        <Panel title="Calibration" subtitle="measured against resolved markets only">
          {insufficient ? (
            <div className="space-y-2">
              <Stat label="Resolved observations" value={count(data.calibration_sample_size)} />
              <p className="text-xs text-muted">
                {data.calibration?.model?.note ??
                  'No resolved markets yet. No Brier score, log loss or calibration curve is reported until the sample is large enough to support one.'}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Brier score" value={num(brier, 4)} hint="lower is better; 0.25 = always 0.5" />
              <Stat label="Log loss" value={num(data.calibration?.model?.log_loss, 4)} />
              <Stat label="ECE" value={num(data.calibration?.model?.expected_calibration_error, 4)} />
              <Stat
                label="Beats market?"
                value={String(data.calibration?.skill_vs_market?.beats_baseline ?? '—')}
                tone={data.calibration?.skill_vs_market?.beats_baseline ? 'ok' : 'warn'}
                hint="model Brier vs market-implied Brier"
              />
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Kill switches" subtitle="all fail closed; a tripped switch suppresses signals">
        <Table headers={['Switch', 'State', 'Reason']}>
          {Object.entries(data.kill_switches).map(([name, state]) => (
            <tr key={name}>
              <Td mono>{name}</Td>
              <Td>
                <Badge value={state.tripped ? 'TRIPPED' : 'CLEAR'} />
              </Td>
              <Td>
                <span className="text-muted">{state.reason}</span>
              </Td>
            </tr>
          ))}
        </Table>
      </Panel>

      <Panel title="Component health" subtitle="derived from recorded system events, not in-process state">
        {data.components.length === 0 ? (
          <Empty message="No components have reported." />
        ) : (
          <Table headers={['Component', 'Health', 'Last event', 'Detail']}>
            {data.components.map((s) => (
              <tr key={s.component}>
                <Td mono>{s.component}</Td>
                <Td>
                  <Badge value={s.health} />
                </Td>
                <Td mono>{ago(s.last_event_at)}</Td>
                <Td>
                  <span className="text-muted">{s.message}</span>
                </Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>
    </div>
  );
}
