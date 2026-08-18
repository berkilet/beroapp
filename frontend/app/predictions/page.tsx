import { apiFetch, ago, num, pct } from '@/lib/api';
import { Badge, EdgeCell, Empty, ErrorBanner, MarketLink, Notice, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface Prediction {
  id: number;
  market_id: number;
  question: string | null;
  category: string;
  model_version: string;
  market_probability: number;
  executable_market_probability: number | null;
  model_probability: number;
  model_uncertainty: number | null;
  confidence: number;
  resolution_risk: string;
  predicted_at: string;
  data_latency_ms: number | null;
  model_latency_ms: number | null;
  rationale: { risk_factors?: string[]; adjustments?: Record<string, number> } | null;
  independent_estimate: {
    available: boolean;
    reason?: string;
    model_id?: string;
    model_version?: string;
    probability?: number;
    uncertainty?: number;
  } | null;
}

export default async function PredictionsPage() {
  const { data, error } = await apiFetch<{ count: number; items: Prediction[] }>(
    '/api/predictions?limit=200',
  );

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Predictions</h1>
        <p className="mt-1 text-xs text-muted">
          Every probability the engine has produced, including the ones that led nowhere. Storing
          the negatives is what makes later performance analysis honest.
        </p>
      </header>

      <Notice>
        The <span className="font-mono">Independent</span> column is the estimate a category model
        produced from outside evidence, before it was blended with the market-anchored baseline. A
        dash there means no category model applied and the row&apos;s model probability is a
        restatement of the market, not a forecast that disagrees with it.
      </Notice>

      {error || !data ? (
        <ErrorBanner error={error ?? 'no data'} />
      ) : data.items.length === 0 ? (
        <Panel title="No predictions"><Empty message="The prediction worker has not produced any output yet." /></Panel>
      ) : (
        <Panel title={`${data.count} most recent predictions`}>
          <Table
            headers={['When', 'Market', 'Category', 'Market P', 'Executable P', 'Model P', 'Independent', 'Edge', 'Uncertainty', 'Confidence', 'Res. risk', 'Data lag', 'Model lag', 'Model', 'Risk factors']}
          >
            {data.items.map((p) => (
              <tr key={p.id} className="hover:bg-edge/20">
                <Td mono><span className="text-muted">{ago(p.predicted_at)}</span></Td>
                <Td><div className="max-w-xs truncate"><MarketLink id={p.market_id} question={p.question} /></div></Td>
                <Td><Badge value={p.category} muted /></Td>
                <Td mono>{pct(p.market_probability)}</Td>
                <Td mono>{pct(p.executable_market_probability)}</Td>
                <Td mono>{pct(p.model_probability)}</Td>
                <Td mono>
                  {p.independent_estimate?.available ? (
                    <span title={p.independent_estimate.model_version}>
                      {pct(p.independent_estimate.probability)}
                    </span>
                  ) : (
                    <span className="text-muted" title={p.independent_estimate?.reason ?? undefined}>
                      —
                    </span>
                  )}
                </Td>
                <Td><EdgeCell value={p.model_probability - p.market_probability} /></Td>
                <Td mono>{num(p.model_uncertainty, 3)}</Td>
                <Td mono>{pct(p.confidence, 0)}</Td>
                <Td><Badge value={p.resolution_risk} /></Td>
                <Td mono>{p.data_latency_ms === null ? '—' : `${p.data_latency_ms}ms`}</Td>
                <Td mono>{p.model_latency_ms === null ? '—' : `${p.model_latency_ms}ms`}</Td>
                <Td mono><span className="text-muted">{p.model_version}</span></Td>
                <Td><span className="text-[11px] text-muted">{(p.rationale?.risk_factors ?? []).join('; ') || '—'}</span></Td>
              </tr>
            ))}
          </Table>
        </Panel>
      )}
    </div>
  );
}
