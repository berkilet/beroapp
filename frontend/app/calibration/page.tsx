import { apiFetch, num, pct } from '@/lib/api';
import { Empty, ErrorBanner, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

type Scope = { scope_value: string | null; sample_size: number; metrics: Record<string, any> }[];
type Response = Record<string, Scope>;

const SCOPE_LABELS: Record<string, string> = {
  overall: 'Overall',
  category: 'By market category',
  model_version: 'By model version',
  confidence_bucket: 'By stated confidence',
  liquidity_bucket: 'By liquidity',
  horizon_bucket: 'By time to resolution',
};

export default async function CalibrationPage() {
  const { data, error } = await apiFetch<Response>('/api/calibration');
  if (error || !data) return <ErrorBanner error={error ?? 'no data'} />;

  const hasAny = Object.values(data).some((rows) => rows.length > 0);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Calibration</h1>
        <p className="mt-1 text-xs text-muted">
          If the system says 70% for a thousand independent events, roughly 700 should happen. That
          is the property measured here, sliced by the dimensions that could hide a problem in an
          aggregate.
        </p>
      </header>

      {!hasAny && (
        <Panel title="No calibration data">
          <Empty message="Calibration requires resolved markets. None of the markets this system has predicted have resolved yet, so no reliability figure is reported." />
        </Panel>
      )}

      {Object.entries(data).map(([scope, rows]) =>
        rows.length === 0 ? null : (
          <Panel key={scope} title={SCOPE_LABELS[scope] ?? scope}>
            <Table headers={['Slice', 'Sample', 'Brier', 'Log loss', 'ECE', 'Max CE', 'Base rate', 'Mean prediction', 'Note']}>
              {rows.map((r, i) => {
                const m = r.metrics.model ?? {};
                return (
                  <tr key={i}>
                    <Td mono>{r.scope_value ?? 'all'}</Td>
                    <Td mono>{r.sample_size}</Td>
                    <Td mono>{num(m.brier_score, 4)}</Td>
                    <Td mono>{num(m.log_loss, 4)}</Td>
                    <Td mono>{num(m.expected_calibration_error, 4)}</Td>
                    <Td mono>{num(m.max_calibration_error, 4)}</Td>
                    <Td mono>{num(m.base_rate, 3)}</Td>
                    <Td mono>{num(m.mean_prediction, 3)}</Td>
                    <Td><span className="text-[11px] text-muted">{m.note ?? ''}</span></Td>
                  </tr>
                );
              })}
            </Table>

            {scope === 'overall' && rows[0]?.metrics?.model?.bins?.length > 0 && (
              <div className="mt-4">
                <div className="mb-2 text-[11px] uppercase tracking-wider text-muted">Reliability diagram</div>
                <Table headers={['Bucket', 'Count', 'Mean predicted', 'Observed frequency', 'Gap']}>
                  {rows[0].metrics.model.bins.map((b: any, i: number) => (
                    <tr key={i}>
                      <Td mono>{`${b.lower.toFixed(1)}–${b.upper.toFixed(1)}`}</Td>
                      <Td mono>{b.count}</Td>
                      <Td mono>{num(b.mean_predicted, 3)}</Td>
                      <Td mono>{num(b.observed_frequency, 3)}</Td>
                      <Td mono>{num(b.gap, 3)}</Td>
                    </tr>
                  ))}
                </Table>
              </div>
            )}
          </Panel>
        ),
      )}
    </div>
  );
}
