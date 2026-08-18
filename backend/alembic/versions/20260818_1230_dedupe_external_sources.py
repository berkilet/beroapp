"""Remove legacy external_sources rows superseded by the evidence registry.

Phase 1 seeded a list of declared sources keyed by display name. Phase 1.5
introduced `app.evidence.registry` keyed by `source_key`, and where the two
lists spelled an institution differently ("Bureau of Labor Statistics" against
"U.S. Bureau of Labor Statistics") the sync created a second row instead of
updating the first. The dashboard then showed the same body twice: once
enabled and collecting, once disabled and empty.

This deletes only rows that have no `source_key` — that is, rows no registry
definition claims — and only when nothing references them. The guard matters:
`external_events.source_id` is a real foreign key, and a legacy row that did
collect evidence must be kept and reconciled by hand rather than dropped.

The Polymarket API rows go too. They are market-data feeds, not evidence
sources; the API reports them from configuration in their own block, so having
them in this table double-counted them in the enabled-source total.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f1c6b28e40"
down_revision = "d765ad5c0d73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    orphans = conn.execute(
        sa.text(
            """
            SELECT id, name FROM external_sources
            WHERE source_key IS NULL OR source_key = ''
            """
        )
    ).all()

    for source_id, name in orphans:
        referenced = conn.execute(
            sa.text("SELECT 1 FROM external_events WHERE source_id = :sid LIMIT 1"),
            {"sid": source_id},
        ).scalar()
        if referenced:
            # Evidence exists against this row, so it is not a duplicate
            # placeholder. Leave it; a human should decide what it maps to.
            print(f"keeping external_sources id={source_id} ({name}): has evidence")
            continue
        conn.execute(
            sa.text("DELETE FROM external_sources WHERE id = :sid"), {"sid": source_id}
        )


def downgrade() -> None:
    # These rows carried no evidence and no operational history worth restoring;
    # re-running scripts/seed.py repopulates whatever the registry declares.
    pass
