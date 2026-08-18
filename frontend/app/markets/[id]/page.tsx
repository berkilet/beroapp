import { apiFetch, ago, num, pct, pctPoints, until, usd } from '@/lib/api';
import { Badge, EdgeCell, Empty, ErrorBanner, Notice, Panel, Stat, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface Detail {
  market: {
    id: number;
    question: string | null;
    description: string | null;
    resolution_source: string | null;
    resolved_by: string | null;
    category: string;
    status: string;
    modelability_status: string;
    modelability_score: number | null;
    modelability_detail: {
      components?: Record<string, number>;
      weights?: Record<string, number>;
      disqualifiers?: string[];
      notes?: string[];
    } | null;
    liquidity_num: number | null;
    volume_num: number | null;
    end_date: string | null;
    outcomes: string[] | null;
    tick_size: number | null;
    first_seen_at: string;
    untrusted_text_notice: string;
    subcategory: string | null;
    event_type: string | null;
    resolution_mechanism: string | null;
    modelability_tier: string | null;
    evidence_available: boolean | null;
    classification_detail: Record<string, unknown> | null;
  };
  evidence: {
    relevance: number;
    match_reason: string;
    source: string;
    source_tier: number;
    series_key: string | null;
    title: string | null;
    numeric_value: number | null;
    unit: string | null;
    observation_date: string | null;
    known_at: string;
    reference_url: string | null;
    verification_status: string;
  }[];
  current: {
    best_bid: number | null;
    best_ask: number | null;
    midpoint: number | null;
    spread: number | null;
    bid_depth_usd: number | null;
    ask_depth_usd: number | null;
    book_imbalance: number | null;
    last_trade_price: number | null;
    is_stale: boolean;
    data_latency_ms: number | null;
    known_at: string;
  } | null;
  order_book: {
    observed_at: string;
    bids: { price: number; size: number }[];
    asks: { price: number; size: number }[];
  } | null;
  price_history: { known_at: string; midpoint: number | null; spread: number | null }[];
  prediction_history: {
    predicted_at: string;
    market_probability: number;
    model_probability: number;
    confidence: number;
    model_uncertainty: number | null;
    model_version: string;
    rationale: Record<string, any> | null;
  }[];
  signals: {
    id: number;
    recommendation: string;
    raw_edge: number;
    executable_edge: number | null;
    confidence: number;
    signal_at: string;
    rank_explanation: { reasons?: string[] } | null;
  }[];
  resolution: {
    outcome: string;
    is_ambiguous: boolean;
    known_at: string;
    evidence: Record<string, any>;
  } | null;
}

export default async function MarketDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const { data, error } = await apiFetch<Detail>(`/api/markets/${encodeURIComponent(id)}`);

  if (error || !data) return <ErrorBanner error={error ?? 'market not found'} />;

  const m = data.market;
  const latestPrediction = data.prediction_history.at(-1);
  const mod = m.modelability_detail;

  return (
    <div className="space-y-5">
      <header>
        {/* Rendered as a text node. No dangerouslySetInnerHTML anywhere. */}
        <h1 className="text-lg font-semibold text-gray-100">{m.question ?? `Market ${m.id}`}</h1>
        <div className="mt-2 flex flex-wrap gap-2">
          <Badge value={m.category} muted />
          {m.subcategory && <Badge value={m.subcategory} muted />}
          {m.event_type && <Badge value={m.event_type} muted />}
          <Badge value={m.status} muted />
          <Badge value={m.modelability_status} />
          {m.resolution_mechanism && <Badge value={m.resolution_mechanism} muted />}
        </div>
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Best bid" value={num(data.current?.best_bid, 4)} />
        <Stat label="Best ask" value={num(data.current?.best_ask, 4)} />
        <Stat label="Midpoint" value={num(data.current?.midpoint, 4)} hint="market-implied probability" />
        <Stat label="Spread" value={num(data.current?.spread, 4)} />
        <Stat label="Bid depth" value={usd(data.current?.bid_depth_usd)} />
        <Stat label="Ask depth" value={usd(data.current?.ask_depth_usd)} />
        <Stat label="Liquidity (venue)" value={usd(m.liquidity_num)} />
        <Stat label="Volume" value={usd(m.volume_num)} />
        <Stat label="Resolves in" value={until(m.end_date)} />
        <Stat
          label="Data age"
          value={data.current ? ago(data.current.known_at) : '—'}
          tone={data.current?.is_stale ? 'warn' : 'ok'}
        />
        <Stat
          label="Model probability"
          value={latestPrediction ? pct(latestPrediction.model_probability) : '—'}
        />
        <Stat
          label="Model confidence"
          value={latestPrediction ? pct(latestPrediction.confidence, 0) : '—'}
        />
      </div>

      <Panel title="Resolution criteria" subtitle="verbatim text from the venue">
        <Notice tone="warn">{m.untrusted_text_notice}</Notice>
        <div className="mt-3 space-y-3 text-xs">
          <div>
            <div className="text-[11px] uppercase tracking-wider text-muted">Resolution source</div>
            <p className="mt-1 whitespace-pre-wrap text-gray-300">
              {m.resolution_source || <span className="text-muted">not published by the venue</span>}
            </p>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-wider text-muted">Description</div>
            <p className="mt-1 whitespace-pre-wrap text-gray-300">
              {m.description || <span className="text-muted">none</span>}
            </p>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-wider text-muted">Resolver address</div>
            <p className="mt-1 font-mono text-gray-300">{m.resolved_by || '—'}</p>
          </div>
        </div>
      </Panel>

      <Panel
        title="Linked evidence"
        subtitle={
          data.evidence.length > 0
            ? `${data.evidence.length} external observations judged relevant to this question`
            : undefined
        }
        right={
          /* Three states, not two: null means the evidence worker has not
             reached this market yet, which is not the same as having looked
             and found nothing. */
          <Badge
            value={
              m.evidence_available === null
                ? 'NOT_ASSESSED'
                : m.evidence_available
                  ? 'EVIDENCE'
                  : 'NO_EVIDENCE'
            }
          />
        }
      >
        {data.evidence.length === 0 ? (
          <Empty
            message={
              m.evidence_available === null
                ? 'The evidence worker has not assessed this market yet — it works through the ' +
                  'most liquid markets first. Nothing has been ruled out; nothing has been found.'
                : 'No external evidence is linked to this market. Without it the model has ' +
                  'nothing to go on but the market price, so any probability shown is anchored ' +
                  'to that price rather than independent of it.'
            }
          />
        ) : (
          <Table
            headers={['Series', 'Value', 'Observation', 'Known at', 'Source', 'Tier', 'Status', 'Relevance', 'Why linked']}
          >
            {data.evidence.map((e, i) => (
              <tr key={i}>
                <Td mono>
                  {e.reference_url ? (
                    <a href={e.reference_url} className="text-info hover:underline" rel="noreferrer noopener" target="_blank">
                      {e.series_key ?? e.title ?? 'item'}
                    </a>
                  ) : (
                    (e.series_key ?? e.title ?? 'item')
                  )}
                </Td>
                <Td mono>
                  {e.numeric_value === null ? '—' : num(e.numeric_value, 4)}
                  {e.unit && <span className="ml-1 text-muted">{e.unit}</span>}
                </Td>
                <Td mono><span className="text-muted">{e.observation_date?.slice(0, 10) ?? '—'}</span></Td>
                <Td mono><span className="text-muted">{ago(e.known_at)}</span></Td>
                <Td>{e.source}</Td>
                <Td mono>{e.source_tier}</Td>
                <Td><Badge value={e.verification_status} /></Td>
                <Td mono>{num(e.relevance, 2)}</Td>
                <Td><span className="text-[11px] text-muted">{e.match_reason}</span></Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>

      {mod && (
        <Panel
          title="Modelability assessment"
          subtitle={`score ${num(m.modelability_score, 3)} — weighted sum of the components below`}
        >
          <Table headers={['Component', 'Score', 'Weight', 'Contribution']}>
            {Object.entries(mod.components ?? {}).map(([name, value]) => (
              <tr key={name}>
                <Td>{name.replace(/_/g, ' ')}</Td>
                <Td mono>{num(value, 3)}</Td>
                <Td mono>{num(mod.weights?.[name], 2)}</Td>
                <Td mono>{num(value * (mod.weights?.[name] ?? 0), 4)}</Td>
              </tr>
            ))}
          </Table>
          {(mod.disqualifiers?.length ?? 0) > 0 && (
            <div className="mt-3 text-xs text-bad">
              <strong>Disqualifiers:</strong> {mod.disqualifiers!.join('; ')}
            </div>
          )}
          {(mod.notes?.length ?? 0) > 0 && (
            <div className="mt-2 text-xs text-muted">
              <strong>Notes:</strong> {mod.notes!.join('; ')}
            </div>
          )}
        </Panel>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Order book" subtitle={data.order_book ? `observed ${ago(data.order_book.observed_at)}` : undefined}>
          {!data.order_book ? (
            <Empty message="No order book has been recorded for this market." />
          ) : (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <div className="mb-1 text-[11px] uppercase tracking-wider text-ok">Bids</div>
                <Table headers={['Price', 'Size']}>
                  {data.order_book.bids.slice(0, 12).map((l, i) => (
                    <tr key={i}>
                      <Td mono>{num(l.price, 4)}</Td>
                      <Td mono>{num(l.size, 0)}</Td>
                    </tr>
                  ))}
                </Table>
              </div>
              <div>
                <div className="mb-1 text-[11px] uppercase tracking-wider text-bad">Asks</div>
                <Table headers={['Price', 'Size']}>
                  {data.order_book.asks.slice(0, 12).map((l, i) => (
                    <tr key={i}>
                      <Td mono>{num(l.price, 4)}</Td>
                      <Td mono>{num(l.size, 0)}</Td>
                    </tr>
                  ))}
                </Table>
              </div>
            </div>
          )}
        </Panel>

        <Panel title="Price history" subtitle={`${data.price_history.length} recorded snapshots`}>
          {data.price_history.length === 0 ? (
            <Empty message="No price history recorded yet." />
          ) : (
            <Sparkline points={data.price_history.map((p) => p.midpoint)} />
          )}
        </Panel>
      </div>

      <Panel title="Prediction history" subtitle="model probability against the market at each evaluation">
        {data.prediction_history.length === 0 ? (
          <Empty message="No predictions recorded for this market." />
        ) : (
          <Table headers={['When', 'Market P', 'Model P', 'Independent', 'Edge', 'Confidence', 'Uncertainty', 'Model', 'Adjustments']}>
            {[...data.prediction_history].reverse().slice(0, 40).map((p, i) => (
              <tr key={i}>
                <Td mono><span className="text-muted">{ago(p.predicted_at)}</span></Td>
                <Td mono>{pct(p.market_probability)}</Td>
                <Td mono>{pct(p.model_probability)}</Td>
                {/* The category model's own view, before blending. A dash means
                    the row is the market restated, not a competing forecast. */}
                <Td mono>
                  {p.rationale?.independent_estimate?.available ? (
                    pct(p.rationale.independent_estimate.probability)
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </Td>
                <Td><EdgeCell value={p.model_probability - p.market_probability} /></Td>
                <Td mono>{pct(p.confidence, 0)}</Td>
                <Td mono>{num(p.model_uncertainty, 3)}</Td>
                <Td mono><span className="text-muted">{p.model_version}</span></Td>
                <Td>
                  <span className="text-[11px] text-muted">
                    {Object.entries(p.rationale?.adjustments ?? {})
                      .filter(([, v]) => Math.abs(Number(v)) > 1e-9)
                      .map(([k, v]) => `${k} ${Number(v).toFixed(3)}`)
                      .join(' · ') || 'none — deferred to market'}
                  </span>
                </Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>

      <Panel title="Signals">
        {data.signals.length === 0 ? (
          <Empty message="No signals recorded." />
        ) : (
          <Table headers={['When', 'Recommendation', 'Raw edge', 'Exec edge', 'Confidence', 'Reasoning']}>
            {data.signals.map((s) => (
              <tr key={s.id}>
                <Td mono><span className="text-muted">{ago(s.signal_at)}</span></Td>
                <Td><Badge value={s.recommendation} /></Td>
                <Td><EdgeCell value={s.raw_edge} /></Td>
                <Td><EdgeCell value={s.executable_edge} /></Td>
                <Td mono>{pct(s.confidence, 0)}</Td>
                <Td>
                  <span className="text-[11px] text-muted">
                    {(s.rank_explanation?.reasons ?? []).join(' · ') || '—'}
                  </span>
                </Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>

      <Panel title="Resolution">
        {!data.resolution ? (
          <Empty message="This market has not resolved. Resolution is taken from the venue's own status, never inferred from price." />
        ) : (
          <div className="space-y-2 text-xs">
            <div className="flex gap-2">
              <Badge value={data.resolution.outcome} />
              {data.resolution.is_ambiguous && <Badge value="AMBIGUOUS" />}
            </div>
            <p className="text-muted">Recorded {ago(data.resolution.known_at)}</p>
            <pre className="overflow-x-auto rounded border border-edge bg-surface p-3 font-mono text-[11px] text-gray-400">
              {JSON.stringify(data.resolution.evidence, null, 2)}
            </pre>
          </div>
        )}
      </Panel>
    </div>
  );
}

/** Inline SVG sparkline. No charting dependency, no external request. */
function Sparkline({ points }: { points: (number | null)[] }) {
  const values = points.filter((p): p is number => p !== null);
  if (values.length < 2) return <Empty message="Not enough points to draw a series." />;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const width = 600;
  const height = 140;

  const path = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * width;
      const y = height - ((v - min) / range) * height;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" preserveAspectRatio="none">
        <path d={path} fill="none" stroke="#58a6ff" strokeWidth="1.5" />
      </svg>
      <div className="mt-1 flex justify-between font-mono text-[11px] text-muted">
        <span>min {min.toFixed(4)}</span>
        <span>{values.length} points</span>
        <span>max {max.toFixed(4)}</span>
      </div>
    </div>
  );
}
