import { apiFetch, num } from '@/lib/api';
import { Empty, ErrorBanner, Notice, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface Metrics {
  scope: string;
  kind: string;
  count: number;
  note: string;
  items: {
    scope_value: string | null;
    model_version: string | null;
    sample_size: number;
    window_start: string | null;
    window_end: string | null;
    metrics: Record<string, any>;
    computed_at: string;
  }[];
}

export default async function PerformancePage() {
  const { data, error } = await apiFetch<Metrics>('/api/performance?scope=overall&kind=calibration');

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Performance</h1>
        <p className="mt-1 text-xs text-muted">
          Computed from resolved markets only. Annualised returns are deliberately not reported —
          with a short history they would be arithmetic dressed up as a claim.
        </p>
      </header>

      {error || !data ? (
        <ErrorBanner error={error ?? 'no data'} />
      ) : data.items.length === 0 ? (
        <Panel title="No performance data">
          <Empty message="No market that this system predicted has resolved yet, so there is nothing to score. This is the expected state on a new deployment." />
        </Panel>
      ) : (
        <>
          <Notice>{data.note}</Notice>
          {data.items.slice(0, 5).map((item, i) => {
            const model = item.metrics.model ?? {};
            const skill = item.metrics.skill_vs_market ?? {};
            return (
              <Panel key={i} title={`Computed ${new Date(item.computed_at).toISOString()}`} subtitle={`sample size ${item.sample_size}`}>
                {model.insufficient_data ? (
                  <p className="text-xs text-muted">{model.note}</p>
                ) : (
                  <Table headers={['Metric', 'Model', 'Market baseline']}>
                    <tr><Td>Brier score</Td><Td mono>{num(model.brier_score, 4)}</Td><Td mono>{num(skill.baseline_brier, 4)}</Td></tr>
                    <tr><Td>Log loss</Td><Td mono>{num(model.log_loss, 4)}</Td><Td mono>—</Td></tr>
                    <tr><Td>Expected calibration error</Td><Td mono>{num(model.expected_calibration_error, 4)}</Td><Td mono>—</Td></tr>
                    <tr><Td>Max calibration error</Td><Td mono>{num(model.max_calibration_error, 4)}</Td><Td mono>—</Td></tr>
                    <tr><Td>Base rate</Td><Td mono>{num(model.base_rate, 4)}</Td><Td mono>—</Td></tr>
                    <tr><Td>Brier skill score</Td><Td mono>{num(skill.brier_skill_score, 4)}</Td><Td mono>—</Td></tr>
                    <tr><Td>Beats the market?</Td><Td mono>{String(skill.beats_baseline ?? '—')}</Td><Td mono>—</Td></tr>
                  </Table>
                )}
              </Panel>
            );
          })}
        </>
      )}
    </div>
  );
}
