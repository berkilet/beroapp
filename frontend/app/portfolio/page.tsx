import { apiFetch, ago, num, usd } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Stat, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface Portfolio {
  summary: {
    capital_label: string;
    initial_capital_usd: number;
    equity_usd: number | null;
    cash_usd?: number;
    positions_value_usd?: number;
    unrealised_pnl_usd?: number;
    realised_pnl_usd?: number;
    roi_pct?: number;
    drawdown_pct?: number;
    peak_equity_usd?: number;
    open_positions?: number;
    note?: string;
  };
  phase: string;
  positions: {
    market_id: number;
    question: string | null;
    shares: number;
    average_entry_price: number;
    cost_basis_usd: number;
    realised_pnl_usd: number;
    fees_paid_usd: number;
    slippage_paid_usd: number;
    opened_at: string;
  }[];
  equity_curve: { taken_at: string; equity_usd: number; drawdown_pct: number }[];
}

export default async function PortfolioPage() {
  const { data, error } = await apiFetch<Portfolio>('/api/portfolio');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  const s = data.summary;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Portfolio</h1>
        <p className="mt-1 text-xs text-muted">Simulated positions and equity.</p>
      </header>

      <Notice tone="warn">
        <strong className="font-semibold">{s.capital_label}.</strong> Every figure on this page is
        virtual. No real funds exist, no account is connected, and no order has been sent to any
        venue.
      </Notice>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Virtual capital" value={usd(s.initial_capital_usd)} hint="configured starting balance" />
        <Stat label="Equity" value={usd(s.equity_usd)} />
        <Stat label="Cash" value={usd(s.cash_usd)} />
        <Stat label="Positions value" value={usd(s.positions_value_usd)} />
        <Stat label="Realised P&L" value={usd(s.realised_pnl_usd)} tone={(s.realised_pnl_usd ?? 0) >= 0 ? 'ok' : 'bad'} />
        <Stat label="Unrealised P&L" value={usd(s.unrealised_pnl_usd)} tone={(s.unrealised_pnl_usd ?? 0) >= 0 ? 'ok' : 'bad'} />
        <Stat label="ROI" value={s.roi_pct === undefined ? '—' : `${num(s.roi_pct)}%`} />
        <Stat label="Drawdown" value={s.drawdown_pct === undefined ? '—' : `${num(s.drawdown_pct)}%`} tone="warn" />
      </div>

      {s.note && <Panel title="Status"><p className="text-xs text-muted">{s.note}</p></Panel>}

      <Panel title="Open positions">
        {data.positions.length === 0 ? (
          <Empty message="No open positions." />
        ) : (
          <Table headers={['Market', 'Shares', 'Avg entry', 'Cost basis', 'Realised P&L', 'Fees', 'Slippage', 'Opened']}>
            {data.positions.map((p, i) => (
              <tr key={i}>
                <Td><div className="max-w-md truncate">{p.question}</div></Td>
                <Td mono>{num(p.shares, 2)}</Td>
                <Td mono>{num(p.average_entry_price, 4)}</Td>
                <Td mono>{usd(p.cost_basis_usd)}</Td>
                <Td mono>{usd(p.realised_pnl_usd)}</Td>
                <Td mono>{usd(p.fees_paid_usd)}</Td>
                <Td mono>{usd(p.slippage_paid_usd)}</Td>
                <Td mono><span className="text-muted">{ago(p.opened_at)}</span></Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>

      <Panel title="Equity curve" subtitle={`${data.equity_curve.length} snapshots`}>
        {data.equity_curve.length < 2 ? (
          <Empty message="Not enough portfolio snapshots to draw a curve." />
        ) : (
          <Table headers={['When', 'Equity', 'Drawdown']}>
            {data.equity_curve.slice(-30).reverse().map((e, i) => (
              <tr key={i}>
                <Td mono><span className="text-muted">{ago(e.taken_at)}</span></Td>
                <Td mono>{usd(e.equity_usd)}</Td>
                <Td mono>{num(e.drawdown_pct)}%</Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>
    </div>
  );
}
