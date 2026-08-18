import { apiFetch, ago, num } from '@/lib/api';
import { Badge, Empty, ErrorBanner, Notice, Panel, Table, Td } from '@/components/ui';

export const dynamic = 'force-dynamic';

interface EvidenceItem {
  id: number;
  source: string;
  source_key: string;
  source_tier: number;
  evidence_type: string | null;
  series_key: string | null;
  title: string | null;
  numeric_value: number | null;
  unit: string | null;
  observation_date: string | null;
  published_at: string | null;
  known_at: string;
  verification_status: string;
  reliability_score: number | null;
  reference_url: string | null;
  superseded: boolean;
  parser_version: string;
}

interface Conflict {
  series_key: string;
  observation_date: string | null;
  resolution: string;
  spread: number | null;
  candidates: { evidence_id: number; source_tier: number; value: number | null }[];
  detail: Record<string, unknown>;
  detected_at: string;
}

interface Response {
  count: number;
  items: EvidenceItem[];
  conflicts: Conflict[];
  notice: string;
}

export default async function EvidencePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const params = await searchParams;
  const query = new URLSearchParams({ limit: '120' });
  if (params.series_key) query.set('series_key', params.series_key);
  if (params.market_id) query.set('market_id', params.market_id);

  const { data, error } = await apiFetch<Response>(`/api/evidence?${query}`);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold text-gray-100">Evidence</h1>
        <p className="mt-1 text-xs text-muted">
          External observations the probability models are allowed to use, with full provenance.
        </p>
      </header>

      {error || !data ? (
        <ErrorBanner error={error ?? 'no data'} />
      ) : (
        <>
          <Notice>{data.notice}</Notice>

          <Panel
            title="Conflicts"
            subtitle="disagreements between sources about the same fact — resolved by precedence, never averaged"
          >
            {data.conflicts.length === 0 ? (
              <Empty message="No material conflicts recorded. Sources reporting the same fact currently agree within the materiality threshold." />
            ) : (
              <Table headers={['Series', 'Period', 'Resolution', 'Spread', 'Candidates', 'When']}>
                {data.conflicts.map((c, i) => (
                  <tr key={i}>
                    <Td mono>{c.series_key}</Td>
                    <Td mono>{c.observation_date?.slice(0, 10) ?? '—'}</Td>
                    <Td>
                      <Badge value={c.resolution} />
                    </Td>
                    <Td mono>{c.spread === null ? '—' : `${(c.spread * 100).toFixed(3)}%`}</Td>
                    <Td mono>
                      <span className="text-muted">
                        {c.candidates.map((x) => `T${x.source_tier}:${num(x.value, 4)}`).join(' vs ')}
                      </span>
                    </Td>
                    <Td mono>
                      <span className="text-muted">{ago(c.detected_at)}</span>
                    </Td>
                  </tr>
                ))}
              </Table>
            )}
          </Panel>

          <Panel title={`${data.count} evidence items`} subtitle="newest first">
            {data.items.length === 0 ? (
              <Empty message="No evidence has been collected yet. Run the evidence worker, or check the data-sources page for connector health." />
            ) : (
              <Table
                headers={[
                  'Series',
                  'Value',
                  'Observation',
                  'Published',
                  'Known at',
                  'Source',
                  'Tier',
                  'Status',
                  'Parser',
                  'Title',
                ]}
              >
                {data.items.map((e) => (
                  <tr key={e.id} className={e.superseded ? 'opacity-50' : undefined}>
                    <Td mono>{e.series_key ?? '—'}</Td>
                    <Td mono>
                      {e.numeric_value === null ? '—' : num(e.numeric_value, 4)}
                      {e.unit && <span className="ml-1 text-muted">{e.unit}</span>}
                    </Td>
                    <Td mono>
                      <span className="text-muted">{e.observation_date?.slice(0, 10) ?? '—'}</span>
                    </Td>
                    <Td mono>
                      <span className="text-muted">{e.published_at?.slice(0, 10) ?? '—'}</span>
                    </Td>
                    <Td mono>
                      <span className="text-muted">{ago(e.known_at)}</span>
                    </Td>
                    <Td>{e.source}</Td>
                    <Td mono>{e.source_tier}</Td>
                    <Td>
                      <Badge value={e.superseded ? 'SUPERSEDED' : e.verification_status} />
                    </Td>
                    <Td mono>
                      <span className="text-muted">{e.parser_version}</span>
                    </Td>
                    <Td>
                      <span className="max-w-md truncate text-muted">{e.title}</span>
                    </Td>
                  </tr>
                ))}
              </Table>
            )}
          </Panel>

          <Panel title="Why three timestamps">
            <div className="space-y-1.5 text-xs text-muted">
              <p>
                <span className="text-gray-300">Observation</span> — the period the figure
                describes (July&apos;s CPI).
              </p>
              <p>
                <span className="text-gray-300">Published</span> — when the issuing body released
                it (mid-August).
              </p>
              <p>
                <span className="text-gray-300">Known at</span> — when this platform could first
                use it.
              </p>
              <p className="pt-1">
                Conflating the first two lets a backtest &ldquo;know&rdquo; July&apos;s inflation
                during July. Conflating the last two lets it know a figure before it was published.
                A superseded row is a revision, kept rather than overwritten so that what we
                believed at the time stays answerable.
              </p>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}
