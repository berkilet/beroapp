import { apiFetch, num, usd } from '@/lib/api';
import { Badge, ErrorBanner, Notice, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface System {
  phase: string;
  live_trading_enabled: boolean;
  configuration: Record<string, any>;
}

/**
 * Settings is read-only, deliberately.
 *
 * Every value here is loaded from environment configuration into a frozen
 * settings object at process start. There is no setter, and no API route that
 * mutates one. Changing a risk limit or a phase means editing the environment
 * and restarting — which leaves a trace — rather than clicking a button in a
 * browser session, which does not.
 */
export default async function SettingsPage() {
  const { data, error } = await apiFetch<System>('/api/system');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  const cfg = data.configuration;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Settings</h1>
        <p className="mt-1 text-xs text-muted">
          Read-only. Configuration is loaded once at startup into a frozen object.
        </p>
      </header>

      <Notice tone="warn">
        This page cannot change anything. Risk limits, kill switches and the operating phase are not
        editable from a browser by design — an interface that can arm live trading is an interface
        that can arm it by accident. Changes are made in the environment file and require a restart.
      </Notice>

      <Panel title="Safety">
        <Table headers={['Setting', 'Value', 'Meaning']}>
          <tr>
            <Td mono>LIVE_TRADING_ENABLED</Td>
            <Td><Badge value={data.live_trading_enabled ? 'TRUE' : 'FALSE'} /></Td>
            <Td><span className="text-muted">No real-money order can be placed while this is false. It is also refused outside PHASE_3.</span></Td>
          </tr>
          <tr>
            <Td mono>CURRENT_PHASE</Td>
            <Td><Badge value={data.phase} /></Td>
            <Td><span className="text-muted">Phase 1 predicts only. Phase 2 adds paper trading. Phase 3 is not an implemented path.</span></Td>
          </tr>
          <tr>
            <Td mono>Withdrawal functionality</Td>
            <Td><Badge value="ABSENT" /></Td>
            <Td><span className="text-muted">No withdrawal, transfer, wallet export or key display exists anywhere in this codebase, and a test fails the build if one appears.</span></Td>
          </tr>
        </Table>
      </Panel>

      <Panel title="Worker cadence">
        <Table headers={['Setting', 'Value']}>
          <tr><Td mono>DISCOVERY_INTERVAL_S</Td><Td mono>{cfg.discovery_interval_s}</Td></tr>
          <tr><Td mono>SNAPSHOT_INTERVAL_S</Td><Td mono>{cfg.snapshot_interval_s}</Td></tr>
          <tr><Td mono>PREDICTION_INTERVAL_S</Td><Td mono>{cfg.prediction_interval_s}</Td></tr>
          <tr><Td mono>DATA_STALENESS_S</Td><Td mono>{cfg.data_staleness_s}</Td></tr>
        </Table>
      </Panel>

      <Panel title="Signal thresholds">
        <Table headers={['Setting', 'Value']}>
          <tr><Td mono>MIN_EXECUTABLE_EDGE</Td><Td mono>{num(cfg.min_executable_edge, 3)}</Td></tr>
          <tr><Td mono>MIN_CONFIDENCE</Td><Td mono>{num(cfg.min_confidence, 2)}</Td></tr>
          <tr><Td mono>MIN_LIQUIDITY</Td><Td mono>{usd(cfg.min_liquidity)}</Td></tr>
          <tr><Td mono>MAX_SPREAD</Td><Td mono>{num(cfg.max_spread, 3)}</Td></tr>
          <tr><Td mono>MAX_ALLOWED_SLIPPAGE</Td><Td mono>{num(cfg.max_allowed_slippage, 3)}</Td></tr>
        </Table>
      </Panel>

      <Panel title="Hard risk limits" subtitle="percent of virtual equity">
        <Table headers={['Limit', 'Value']}>
          {Object.entries(cfg.risk_limits ?? {}).map(([k, v]) => (
            <tr key={k}>
              <Td mono>{k}</Td>
              <Td mono>{String(v)}%</Td>
            </tr>
          ))}
          <tr><Td mono>VIRTUAL_INITIAL_CAPITAL</Td><Td mono>{usd(cfg.virtual_initial_capital)}</Td></tr>
        </Table>
      </Panel>
    </div>
  );
}
