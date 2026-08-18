import { apiFetch, ago, num, pct, pctPoints, until, usd } from '@/lib/api';
import { Badge, EdgeCell, Empty, ErrorBanner, MarketLink, Notice, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface Opportunity {
  id: number;
  market_id: number;
  side: string | null;
  recommendation: string;
  market_probability: number;
  model_probability: number;
  raw_edge: number;
  executable_edge: number | null;
  liquidity_adjusted_edge: number | null;
  risk_adjusted_edge: number | null;
  confidence: number;
  executable_price: number | null;
  liquidity: number | null;
  spread: number | null;
  estimated_slippage: number | null;
  resolution_risk: string;
  model_version: string;
  rank_score: number | null;
  rank_explanation: { contributions?: Record<string, number>; reasons?: string[] } | null;
  signal_at: string;
  hours_to_resolution: number | null;
  risk_status: string;
  risk_reasons: string[];
  approved_size_usd: number | null;
  market: { question: string | null; category: string };
}

interface Response {
  window_hours: number;
  count: number;
  items: Opportunity[];
}

function topDrivers(explanation: Opportunity['rank_explanation']): string {
  const contributions = explanation?.contributions;
  if (!contributions) return '—';
  return Object.entries(contributions)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 3)
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${v.toFixed(3)}`)
    .join(' · ');
}

export default async function OpportunitiesPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const params = await searchParams;
  const query = new URLSearchParams({ limit: '100' });
  for (const key of ['min_edge', 'min_confidence', 'min_liquidity', 'category', 'recommendation', 'max_hours_to_resolution']) {
    const value = params[key];
    if (value) query.set(key, value);
  }

  const { data, error } = await apiFetch<Response>(`/api/opportunities?${query}`);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Opportunities</h1>
        <p className="mt-1 text-xs text-muted">
          Signals from the last 24 hours, ranked by a transparent score whose components are shown
          per row.
        </p>
      </header>

      <Notice>
        A recommendation is <strong className="font-semibold">not an order</strong>. Nothing here has
        been executed, simulated or otherwise, and no route in this application can execute it.
      </Notice>

      <Panel
        title="Filters"
        subtitle="edit the URL query to filter: min_edge, min_confidence, min_liquidity, category, recommendation, max_hours_to_resolution"
      >
        <div className="flex flex-wrap gap-2 text-[11px] text-muted">
          {[
            ['min_edge', '0.02'],
            ['min_confidence', '0.6'],
            ['min_liquidity', '10000'],
            ['category', 'ELECTIONS'],
            ['recommendation', 'BUY'],
            ['max_hours_to_resolution', '720'],
          ].map(([k, v]) => (
            <a
              key={k}
              href={`/opportunities?${k}=${v}`}
              className="rounded border border-edge px-2 py-1 font-mono hover:border-info hover:text-info"
            >
              {k}={v}
            </a>
          ))}
          <a href="/opportunities" className="rounded border border-edge px-2 py-1 font-mono hover:border-info hover:text-info">
            clear
          </a>
        </div>
      </Panel>

      {error || !data ? (
        <ErrorBanner error={error ?? 'no data'} />
      ) : data.items.length === 0 ? (
        <Panel title="No opportunities">
          <Empty
            message={
              'No signal in the last 24 hours passed the executable-edge and confidence thresholds. ' +
              'On a new deployment this is the expected state: the baseline model agrees with the ' +
              'market until it has a specific reason not to.'
            }
          />
        </Panel>
      ) : (
        <Panel title={`${data.count} opportunities`} subtitle="ranked by risk-adjusted score">
          <Table
            headers={[
              'Market',
              'Category',
              'Market P',
              'Model P',
              'Raw edge',
              'Exec edge',
              'Risk-adj',
              'Conf',
              'Liquidity',
              'Spread',
              'Slippage',
              'To resolve',
              'Rec',
              'Risk',
              'Model',
              'Rank drivers',
              'When',
            ]}
          >
            {data.items.map((o) => (
              <tr key={o.id} className="hover:bg-edge/20">
                <Td>
                  <div className="max-w-xs truncate">
                    <MarketLink id={o.market_id} question={o.market.question} />
                  </div>
                </Td>
                <Td>
                  <Badge value={o.market.category} muted />
                </Td>
                <Td mono>{pct(o.market_probability)}</Td>
                <Td mono>{pct(o.model_probability)}</Td>
                <Td>
                  <EdgeCell value={o.raw_edge} />
                </Td>
                <Td>
                  <EdgeCell value={o.executable_edge} />
                </Td>
                <Td>
                  <EdgeCell value={o.risk_adjusted_edge} />
                </Td>
                <Td mono>{pct(o.confidence, 0)}</Td>
                <Td mono>{usd(o.liquidity)}</Td>
                <Td mono>{num(o.spread, 4)}</Td>
                <Td mono>{num(o.estimated_slippage, 4)}</Td>
                <Td mono>
                  {o.hours_to_resolution === null
                    ? '—'
                    : o.hours_to_resolution < 24
                      ? `${Math.round(o.hours_to_resolution)}h`
                      : `${Math.round(o.hours_to_resolution / 24)}d`}
                </Td>
                <Td>
                  <Badge value={o.recommendation} />
                </Td>
                <Td>
                  <Badge value={o.risk_status} />
                </Td>
                <Td mono>
                  <span className="text-muted">{o.model_version}</span>
                </Td>
                <Td>
                  <span className="text-[11px] text-muted">{topDrivers(o.rank_explanation)}</span>
                </Td>
                <Td mono>
                  <span className="text-muted">{ago(o.signal_at)}</span>
                </Td>
              </tr>
            ))}
          </Table>
        </Panel>
      )}
    </div>
  );
}
