-- Database privileges.
--
-- The point of this file is one property: the application cannot rewrite its
-- own history. It may INSERT into and SELECT from audit_logs and system_events,
-- but it has no UPDATE or DELETE on them. A full compromise at the application
-- layer therefore still cannot erase what happened.
--
-- Run once as a superuser, after `alembic upgrade head`:
--   psql -d beroapp -f ops/grants.sql

-- The application role. Create it before running this file:
--   CREATE ROLE beroapp LOGIN PASSWORD '<generated>';
-- Never a superuser, never the database owner.

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM beroapp;

-- Full access to the working tables.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    events, markets, market_tokens, market_snapshots, order_book_snapshots,
    trades, external_sources, external_events, predictions, model_predictions,
    signals, risk_decisions, paper_orders, paper_fills, positions,
    portfolio_snapshots, resolutions, model_versions, performance_metrics,
    system_config
TO beroapp;

-- Append-only tables. INSERT and SELECT only, by design.
GRANT SELECT, INSERT ON audit_logs, system_events TO beroapp;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs, system_events FROM beroapp;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO beroapp;
GRANT USAGE ON SCHEMA public TO beroapp;

-- Migrations run as a separate, more privileged role. The application role
-- must not be able to alter the schema it runs against.
REVOKE CREATE ON SCHEMA public FROM beroapp;
