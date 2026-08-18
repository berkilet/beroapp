import { apiFetch, ago, num, usd } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface PaperTrading {
  phase: string;
  active: boolean;
  capital_label: string;
  notice: string;
  orders: {
    id: number;
    market_id: number;
    question: string | null;
    venue: string;
    side: string;
    state: string;
    requested_price: number;
    estimated_executable_price: number | null;
    requested_size_usd: number;
    signal_at: string;
    execution_latency_ms: number | null;
    reject_reason: string | null;
    fill: {
      simulated_fill_price: number;
      filled_size_usd: number;
      filled_shares: number;
      slippage: number;
      fees: number;
      is_partial: boolean;
      filled_at: string;
    } | null;
  }[];
}

export default async function PaperTradingPage() {
  const { data, error } = await apiFetch<PaperTrading>('/api/paper-trading');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Paper Trading</h1>
        <p className="mt-1 text-xs text-muted">
          Fills are simulated by walking the recorded order book after modelled latency — never
          booked at the signal price.
        </p>
      </header>

      <Notice tone={data.active ? 'warn' : 'info'}>
        <strong className="font-semibold">{data.capital_label}.</strong> {data.notice}
      </Notice>

      <Panel title="Simulated orders" subtitle={`phase ${data.phase}`}>
        {data.orders.length === 0 ? (
          <Empty
            message={
              data.active
                ? 'No simulated orders yet.'
                : 'Paper trading is inactive in this phase, so no orders exist. This is the expected state in Phase 1.'
            }
          />
        ) : (
          <Table headers={['When', 'Market', 'Side', 'State', 'Signal price', 'Est. exec', 'Fill price', 'Slippage', 'Size', 'Filled', 'Partial', 'Latency', 'Reject reason']}>
            {data.orders.map((o) => (
              <tr key={o.id}>
                <Td mono><span className="text-muted">{ago(o.signal_at)}</span></Td>
                <Td><div className="max-w-xs truncate">{o.question}</div></Td>
                <Td><Badge value={o.side} muted /></Td>
                <Td><Badge value={o.state} /></Td>
                <Td mono>{num(o.requested_price, 4)}</Td>
                <Td mono>{num(o.estimated_executable_price, 4)}</Td>
                <Td mono>{num(o.fill?.simulated_fill_price, 4)}</Td>
                <Td mono>{num(o.fill?.slippage, 4)}</Td>
                <Td mono>{usd(o.requested_size_usd)}</Td>
                <Td mono>{usd(o.fill?.filled_size_usd)}</Td>
                <Td mono>{o.fill?.is_partial ? 'yes' : o.fill ? 'no' : '—'}</Td>
                <Td mono>{o.execution_latency_ms === null ? '—' : `${o.execution_latency_ms}ms`}</Td>
                <Td><span className="text-[11px] text-bad">{o.reject_reason ?? ''}</span></Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>
    </div>
  );
}
