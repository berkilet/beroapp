import { apiFetch, ago, count } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Stat, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface ModelHealth {
  active_versions: string[];
  registered_versions: {
    model_id: string;
    version: string;
    algorithm: string;
    category: string | null;
    is_active: boolean;
    feature_set: string[];
    hyperparameters: Record<string, unknown>;
    training_period: { start: string | null; end: string | null };
    performance_summary: Record<string, unknown> | null;
    created_at: string;
  }[];
  training_readiness: {
    resolved_markets: number;
    required: number;
    trained_models_active: boolean;
    note: string;
  };
}

export default async function ModelHealthPage() {
  const { data, error } = await apiFetch<ModelHealth>('/api/model-health');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  const r = data.training_readiness;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Model Health</h1>
        <p className="mt-1 text-xs text-muted">Active estimators, versioning and training readiness.</p>
      </header>

      <Notice>{r.note}</Notice>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Active version" value={data.active_versions[0] ?? '—'} />
        <Stat label="Resolved markets" value={count(r.resolved_markets)} hint={`${count(r.required)} required to train`} />
        <Stat
          label="Learned models"
          value={r.trained_models_active ? 'ACTIVE' : 'INACTIVE'}
          tone={r.trained_models_active ? 'ok' : 'warn'}
        />
        <Stat label="Registered versions" value={count(data.registered_versions.length)} />
      </div>

      <Panel title="Registered model versions" subtitle="a production model is never silently replaced">
        {data.registered_versions.length === 0 ? (
          <Empty message="No trained model version is registered. The interpretable baseline is the only active estimator, which is the intended state until enough markets have resolved to train on." />
        ) : (
          <Table headers={['Model', 'Version', 'Algorithm', 'Category', 'Active', 'Training period', 'Created']}>
            {data.registered_versions.map((v, i) => (
              <tr key={i}>
                <Td mono>{v.model_id}</Td>
                <Td mono>{v.version}</Td>
                <Td mono>{v.algorithm}</Td>
                <Td><Badge value={v.category ?? 'all'} muted /></Td>
                <Td><Badge value={v.is_active ? 'ACTIVE' : 'RETIRED'} /></Td>
                <Td mono>{v.training_period.start ?? '—'} → {v.training_period.end ?? '—'}</Td>
                <Td mono><span className="text-muted">{ago(v.created_at)}</span></Td>
              </tr>
            ))}
          </Table>
        )}
      </Panel>

      <Panel title="Baseline model" subtitle="v0.1.0-baseline — interpretable, deterministic, no training data required">
        <div className="space-y-2 text-xs text-gray-300">
          <p>
            Works in log-odds space against the market price as prior, and departs from it only for
            a named reason. Each reason is recorded per prediction so any divergence can be
            explained after the fact.
          </p>
          <ul className="list-inside list-disc space-y-1 text-muted">
            <li>
              <span className="text-gray-300">Neg-risk coherence</span> — mutually exclusive sibling
              outcomes must sum to 1. Used only when at least 98% of a group&apos;s legs have been
              priced, because a partially sampled group would manufacture a false edge.
            </li>
            <li>
              <span className="text-gray-300">Book imbalance</span> — weak short-horizon drift term,
              decayed to nothing beyond about a week.
            </li>
            <li>
              <span className="text-gray-300">Favourite–longshot</span> — symmetric tail correction
              in multi-outcome groups, so it can never become a directional bet.
            </li>
            <li>
              <span className="text-gray-300">Shrinkage</span> — the output is pulled back toward the
              market in proportion to model uncertainty. At maximum uncertainty the model is the
              market, which is the correct answer for a model that knows nothing.
            </li>
          </ul>
        </div>
      </Panel>
    </div>
  );
}
