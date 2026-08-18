import { apiFetch, count, num, until, usd } from '@/lib/api';
import { Badge, Empty, ErrorBanner, MarketLink, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface Market {
  id: number;
  question: string | null;
  category: string;
  category_confidence: number | null;
  status: string;
  modelability_status: string;
  modelability_score: number | null;
  liquidity_num: number | null;
  volume_num: number | null;
  volume_24hr: number | null;
  end_date: string | null;
  neg_risk: boolean | null;
}

interface Response {
  total: number;
  limit: number;
  offset: number;
  items: Market[];
}

const CATEGORIES = [
  'ELECTIONS', 'POLITICS', 'MACROECONOMICS', 'FEDERAL_RESERVE', 'CRYPTO',
  'SPORTS', 'TECHNOLOGY', 'BUSINESS', 'GEOPOLITICS', 'ENTERTAINMENT', 'OTHER',
];

const MODELABILITY = ['TRADEABLE', 'WATCHLIST', 'INSUFFICIENT_DATA', 'UNMODELABLE', 'RESOLUTION_RISK'];

export default async function MarketsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const params = await searchParams;
  const offset = Number(params.offset ?? 0);
  const query = new URLSearchParams({ limit: '50', offset: String(offset) });
  for (const key of ['category', 'modelability', 'status', 'search']) {
    const value = params[key];
    if (value) query.set(key, value);
  }

  const { data, error } = await apiFetch<Response>(`/api/markets?${query}`);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Markets</h1>
        <p className="mt-1 text-xs text-muted">
          The full discovered universe, including markets that will never be traded. Closed and
          resolved markets are retained deliberately — dropping them would make later performance
          figures survivorship-biased.
        </p>
      </header>

      <Panel title="Filters">
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {CATEGORIES.map((c) => (
              <a
                key={c}
                href={`/markets?category=${c}`}
                className="rounded border border-edge px-2 py-1 font-mono text-[11px] text-muted hover:border-info hover:text-info"
              >
                {c}
              </a>
            ))}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {MODELABILITY.map((m) => (
              <a
                key={m}
                href={`/markets?modelability=${m}`}
                className="rounded border border-edge px-2 py-1 font-mono text-[11px] text-muted hover:border-info hover:text-info"
              >
                {m}
              </a>
            ))}
            <a href="/markets" className="rounded border border-edge px-2 py-1 font-mono text-[11px] text-muted hover:border-info hover:text-info">
              clear
            </a>
          </div>
        </div>
      </Panel>

      {error || !data ? (
        <ErrorBanner error={error ?? 'no data'} />
      ) : data.items.length === 0 ? (
        <Panel title="No markets"><Empty message="No market matched this filter." /></Panel>
      ) : (
        <Panel
          title={`${count(data.total)} markets`}
          subtitle={`showing ${data.offset + 1}–${data.offset + data.items.length}, ordered by liquidity`}
        >
          <Table
            headers={['Market', 'Category', 'Status', 'Modelability', 'Score', 'Liquidity', 'Volume', '24h vol', 'Resolves', 'Neg-risk']}
          >
            {data.items.map((m) => (
              <tr key={m.id} className="hover:bg-edge/20">
                <Td>
                  <div className="max-w-md truncate">
                    <MarketLink id={m.id} question={m.question} />
                  </div>
                </Td>
                <Td>
                  <Badge value={m.category} muted />
                  {m.category_confidence !== null && m.category_confidence < 0.5 && (
                    <span className="ml-1 text-[10px] text-warn">low conf</span>
                  )}
                </Td>
                <Td><Badge value={m.status} muted /></Td>
                <Td><Badge value={m.modelability_status} /></Td>
                <Td mono>{num(m.modelability_score, 3)}</Td>
                <Td mono>{usd(m.liquidity_num)}</Td>
                <Td mono>{usd(m.volume_num)}</Td>
                <Td mono>{usd(m.volume_24hr)}</Td>
                <Td mono>{until(m.end_date)}</Td>
                <Td mono>{m.neg_risk ? 'yes' : 'no'}</Td>
              </tr>
            ))}
          </Table>

          <div className="mt-4 flex gap-2 text-xs">
            {offset > 0 && (
              <a href={`/markets?offset=${Math.max(0, offset - 50)}`} className="text-info hover:underline">
                ← previous
              </a>
            )}
            {data.offset + data.items.length < data.total && (
              <a href={`/markets?offset=${offset + 50}`} className="text-info hover:underline">
                next →
              </a>
            )}
          </div>
        </Panel>
      )}
    </div>
  );
}
