import { apiFetch, ago, count, num } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface Audit {
  total: number;
  items: {
    id: number;
    actor: string;
    action: string;
    component: string;
    market_id: number | null;
    model_version: string | null;
    confidence: number | null;
    edge: number | null;
    risk_status: string | null;
    execution_status: string | null;
    correlation_id: string | null;
    occurred_at: string;
    output: Record<string, any> | null;
  }[];
}

export default async function AuditPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const params = await searchParams;
  const offset = Number(params.offset ?? 0);
  const { data, error } = await apiFetch<Audit>(`/api/audit?limit=100&offset=${offset}`);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Audit Log</h1>
        <p className="mt-1 text-xs text-muted">
          Append-only record of every material decision. The application database role has no
          UPDATE or DELETE grant on this table, so a compromise at the application layer still
          cannot rewrite what happened.
        </p>
      </header>

      {error || !data ? (
        <ErrorBanner error={error ?? 'no data'} />
      ) : data.items.length === 0 ? (
        <Panel title="No audit entries"><Empty message="Nothing has been recorded yet." /></Panel>
      ) : (
        <Panel title={`${count(data.total)} entries`} subtitle={`showing ${offset + 1}–${offset + data.items.length}`}>
          <Table headers={['When', 'Actor', 'Action', 'Component', 'Market', 'Model', 'Confidence', 'Edge', 'Risk', 'Execution', 'Correlation']}>
            {data.items.map((a) => (
              <tr key={a.id}>
                <Td mono><span className="text-muted">{ago(a.occurred_at)}</span></Td>
                <Td mono>{a.actor}</Td>
                <Td mono>{a.action}</Td>
                <Td mono><span className="text-muted">{a.component}</span></Td>
                <Td mono>{a.market_id ?? '—'}</Td>
                <Td mono><span className="text-muted">{a.model_version ?? '—'}</span></Td>
                <Td mono>{num(a.confidence, 3)}</Td>
                <Td mono>{num(a.edge, 4)}</Td>
                <Td><Badge value={a.risk_status ?? undefined} /></Td>
                <Td mono><span className="text-muted">{a.execution_status ?? '—'}</span></Td>
                <Td mono><span className="text-muted">{a.correlation_id?.slice(0, 10) ?? '—'}</span></Td>
              </tr>
            ))}
          </Table>
          <div className="mt-4 flex gap-2 text-xs">
            {offset > 0 && <a href={`/audit?offset=${Math.max(0, offset - 100)}`} className="text-info hover:underline">← previous</a>}
            {offset + data.items.length < data.total && <a href={`/audit?offset=${offset + 100}`} className="text-info hover:underline">next →</a>}
          </div>
        </Panel>
      )}
    </div>
  );
}
