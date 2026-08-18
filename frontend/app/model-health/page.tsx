import { apiFetch, ago, count } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Stat, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface ModelHealth {
  active_versions: string[];
  category_models: { implemented: string[]; note: string };
  evidence: {
    items_stored: number;
    markets_with_linked_evidence: number;
    min_features_for_model: number;
  };
  training_readiness_by_category: {
    total_resolved_observations: number;
    per_category: Record<string, number>;
    per_category_threshold: number;
    categories_ready: string[];
    global_threshold: number;
    global_ready: boolean;
    note: string;
  };
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
  const byCategory = data.training_readiness_by_category;

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
        <Stat
          label="Category models"
          value={count(data.category_models.implemented.length)}
          hint="subcategories with an independent estimator"
        />
        <Stat
          label="Evidence items"
          value={count(data.evidence.items_stored)}
          hint="external observations stored"
        />
        <Stat
          label="Markets with evidence"
          value={count(data.evidence.markets_with_linked_evidence)}
          hint={`${data.evidence.min_features_for_model} features minimum to model`}
          tone={data.evidence.markets_with_linked_evidence > 0 ? 'ok' : 'warn'}
        />
        <Stat
          label="Categories trainable"
          value={count(byCategory.categories_ready.length)}
          hint={`${count(byCategory.per_category_threshold)} observations each`}
        />
      </div>

      <Panel
        title="Category probability models"
        subtitle="an independent estimate exists only where a real-world quantity can be measured"
      >
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {data.category_models.implemented.length === 0 ? (
              <span className="text-xs text-muted">None implemented.</span>
            ) : (
              data.category_models.implemented.map((s) => <Badge key={s} value={s} muted />)
            )}
          </div>
          <p className="text-xs text-muted">{data.category_models.note}</p>
          <p className="text-xs text-muted">
            Where no category model applies, the prediction is the market-anchored baseline and the
            prediction record says so explicitly (
            <span className="font-mono">independent_estimate.available = false</span>). That is not
            a forecast that beat the market; it is the market, restated.
          </p>
        </div>
      </Panel>

      <Panel
        title="Training readiness by category"
        subtitle={byCategory.note}
        right={
          <Badge value={byCategory.global_ready ? 'READY' : 'NOT_EVALUATED'} />
        }
      >
        {Object.keys(byCategory.per_category).length === 0 ? (
          <Empty message="No market this system predicted has resolved yet, so no category has any training observations. This is the expected state on a new deployment." />
        ) : (
          <Table headers={['Category', 'Resolved observations', 'Required', 'Trainable']}>
            {Object.entries(byCategory.per_category)
              .sort(([, a], [, b]) => b - a)
              .map(([category, n]) => (
                <tr key={category}>
                  <Td><Badge value={category} muted /></Td>
                  <Td mono>{count(n)}</Td>
                  <Td mono><span className="text-muted">{count(byCategory.per_category_threshold)}</span></Td>
                  <Td>
                    <Badge value={n >= byCategory.per_category_threshold ? 'READY' : 'INSUFFICIENT_DATA'} />
                  </Td>
                </tr>
              ))}
          </Table>
        )}
        <p className="mt-3 text-xs text-muted">
          {count(byCategory.total_resolved_observations)} resolved observations in total against a
          global threshold of {count(byCategory.global_threshold)}. Counts are never pooled across
          categories to reach a threshold — a model trained on elections is not evidence that a
          crypto model is ready.
        </p>
      </Panel>

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
