# OPERATIONS.md

Running the platform continuously, and what to do when it misbehaves.

---

## 1. Install

### PostgreSQL

```bash
brew install postgresql@16
brew services start postgresql@16

createdb beroapp
psql -d postgres -c "CREATE ROLE beroapp LOGIN PASSWORD '$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')'"
```

Keep PostgreSQL on loopback. In `postgresql.conf`:

```
listen_addresses = 'localhost'
```

### Backend

```bash
cd ~/beroapp/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env: set DATABASE_URL and generate API_KEY / OPERATOR_API_KEY with
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"

.venv/bin/alembic upgrade head
psql -d beroapp -f ../ops/grants.sql
PYTHONPATH=. .venv/bin/python scripts/seed.py
```

`ops/grants.sql` is not optional. It is what makes `audit_logs` and
`system_events` append-only for the application role.

### Dashboard

```bash
cd ~/beroapp/frontend
npm ci
cp .env.local.example .env.local     # BACKEND_API_KEY must match backend API_KEY
npm run build
```

### Log directory

```bash
mkdir -p ~/beroapp/logs
```

---

## 2. 24/7 operation with launchd

Four jobs. The worker is the one that matters; the API and dashboard only serve
what it has already written.

```bash
cd ~/beroapp
for job in worker api dashboard backup; do
  sed "s|__HOME__|$HOME|g" "ops/launchd/com.beroapp.$job.plist" \
    > ~/Library/LaunchAgents/"com.beroapp.$job.plist"
  launchctl load -w ~/Library/LaunchAgents/"com.beroapp.$job.plist"
done
```

`RunAtLoad` starts each job after a reboot; `KeepAlive` restarts it on crash.
You do not need to start anything by hand in the morning.

### Ordering after a reboot

There is deliberately no dependency declaration between PostgreSQL and the
worker. The worker waits for the database itself — `_wait_for_database` retries
with backoff for up to about ten minutes — because waiting is correct and
crash-looping until launchd gives up is not.

### Control

```bash
launchctl list | grep beroapp                       # status
launchctl stop  com.beroapp.worker                  # stop (KeepAlive restarts it)
launchctl unload ~/Library/LaunchAgents/com.beroapp.worker.plist   # stop for real
launchctl load -w ~/Library/LaunchAgents/com.beroapp.worker.plist  # start
```

To restart after a configuration change: `unload` then `load`. Settings are read
once at startup into a frozen object, so a change to `.env` has no effect until
the process restarts. This is intentional — a limit that can change under a
running process is not a limit.

### Graceful shutdown

The worker installs SIGTERM/SIGINT handlers. In-flight jobs finish their current
iteration and the process exits cleanly; `ExitTimeOut` is 30s. Jobs are
idempotent, so a mid-cycle death loses at most one cycle of work and never
corrupts state.

---

## 3. Health

### Endpoints

| Endpoint | Auth | Meaning |
|---|---|---|
| `GET /health` | none | Liveness. The process is up. Reveals nothing else. |
| `GET /readiness` | none | Dependencies usable. 503 when the database is unreachable or a component has FAILED. |
| `GET /metrics` | none | Prometheus text: row counts, per-component health, data age, live-trading flag. |

```bash
curl -s localhost:8000/readiness | python3 -m json.tool
curl -s localhost:8000/metrics
```

### Component states

Health is derived from `system_events` rows written by the worker, not from an
in-memory flag, so it survives a restart and reflects reality rather than
optimism.

| State | Meaning |
|---|---|
| `HEALTHY` | Reported recently and within tolerance |
| `DEGRADED` | Working, but with errors or partial results |
| `STALE` | Has not reported within 3× its expected interval |
| `FAILED` | Last cycle failed outright |
| `UNKNOWN` | Never reported — this component has not run |
| `DISABLED` | Deliberately off — no connector, or a required credential is absent |

`UNKNOWN` is never silently upgraded to `HEALTHY`.

### What healthy looks like

Roughly, on the default cadence:

* `MARKET_DISCOVERY` reports every 15 minutes; markets discovered in the tens of
  thousands.
* `DATA_FEED` reports every 60 seconds, `batches_failed` at 0 and
  `snapshots_skipped_unchanged` substantial — most markets are quiet, and
  skipping them is the intended behaviour, not a fault.
* `PROBABILITY_ENGINE` reports every 5 minutes.
* Data age below `DATA_STALENESS_S` (300s).

### Evidence sources

The data-sources page is the place to check the evidence layer. Three things
there are worth reading in order:

1. **Enabled sources**, e.g. `6 / 14`. A source is disabled when it has no
   connector or when a required credential is absent — both are normal, and the
   `usage_notes` column says which.
2. **Newest evidence**, per source. This, not the cumulative item count, is the
   health signal: a source with thousands of items and nothing new in two days
   is broken.
3. **Budget today**, e.g. `2 / 25` for BLS. Approaching the cap means the
   connector will start refusing calls — which is the intended behaviour, but it
   is worth knowing before the evidence goes stale.

Two credentials change what runs, and neither ships with a fallback:

| Variable | Effect when absent |
|---|---|
| `SEC_USER_AGENT` | SEC EDGAR reports `DISABLED`. SEC policy requires a declared, contactable User-Agent, so the connector refuses rather than sending an anonymous request. |
| `FEC_API_KEY` | FEC reports `DISABLED`. It does *not* fall back to the shared public `DEMO_KEY` — 30 requests/hour is too tight for continuous polling, and pointing a 24/7 worker at a shared demo credential is poor citizenship. |

On a fresh install the `GLOBAL_KILL_SWITCH` is tripped, which is correct: it
fails closed until an operator clears it. Phase 1 emits recommendations
regardless; the switch only gates execution, which Phase 1 does not do.

---

## 4. Backups

Daily at 03:15 via `com.beroapp.backup`, or on demand:

```bash
PGPASSWORD=... scripts/backup.sh
```

Every backup is verified by restoring it into a scratch database and counting
tables and rows. A dump that does not restore is not a backup, and the script
exits non-zero rather than reporting success.

The backup role needs `CREATEDB` for self-verification:

```bash
psql -d postgres -c "ALTER ROLE beroapp CREATEDB"
```

Retention defaults to 14 days (`BEROAPP_BACKUP_RETENTION_DAYS`). Archives are
written `0600` inside a `0700` directory. No table in this schema stores a
credential, so no dump contains one.

### Restore

```bash
scripts/restore.sh backups/beroapp_20260818T031500Z.dump beroapp_restored
psql -d beroapp_restored -f ops/grants.sql
```

`restore.sh` refuses to overwrite a database that already has tables. To
actually replace the live database you must drop it explicitly first — a
destructive step should be a deliberate one.

---

## 5. Logs

Structured JSON, one object per line:

```bash
tail -f ~/beroapp/logs/worker.log | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        e = json.load(open('/dev/null')) if False else json.loads(line)
        print(f\"{e['timestamp']} {e['level']:8} {e['component']:22} {e['event']}\")
    except Exception:
        print(line, end='')
"
```

Every record carries `timestamp`, `level`, `component`, `event` and
`correlation_id`, plus `market_id` and `error_code` where applicable.

**Nothing that looks like a credential survives the formatter.** Redaction runs
over the fully-rendered record — including exception tracebacks — because the
ways a secret reaches a log line outnumber the ways one intends it to.

Find everything from one request or cycle:

```bash
grep '"correlation_id": "abc123' ~/beroapp/logs/*.log
```

---

## 6. Troubleshooting

### Dashboard shows "Data unavailable"

The backend is unreachable. In order:

```bash
curl -s localhost:8000/health          # is the API up?
launchctl list | grep beroapp.api
tail -50 ~/beroapp/logs/api.error.log
```

If `/health` answers but pages still fail, the dashboard's `BACKEND_API_KEY`
does not match the backend's `API_KEY`.

### Everything reports STALE

The worker is not running or cannot reach the database.

```bash
launchctl list | grep beroapp.worker
tail -50 ~/beroapp/logs/worker.error.log
psql -d beroapp -c "SELECT component, event, occurred_at FROM system_events ORDER BY occurred_at DESC LIMIT 10"
```

### No opportunities

Usually correct, not broken. In order of likelihood:

1. **The baseline agrees with the market.** Without evidence connectors it is
   built to, and a model that manufactures disagreement from nothing is worse
   than one that does not.
2. **Markets are `INSUFFICIENT_DATA`.** Modelability requires at least 3
   snapshots and 24 hours of market age. Wait a cycle or two.
3. **A kill switch is tripped.** Check `/api/dashboard` → `kill_switches`.

Check what the filter actually decided:

```sql
SELECT modelability_status, count(*) FROM markets GROUP BY 1 ORDER BY 2 DESC;
SELECT recommendation, count(*) FROM signals GROUP BY 1 ORDER BY 2 DESC;
```

### Rate limiting from Polymarket

Should not happen — the configured budgets are far below the documented
ceilings. If it does, lower `GAMMA_RPS` / `CLOB_RPS` / `DATA_RPS` and restart.
The client already honours `Retry-After`.

### Database growing

Snapshots are the bulk. They are only written on material change, but a large
universe still accumulates.

```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
```

To slow growth: raise `SNAPSHOT_INTERVAL_S`, raise `SNAPSHOT_MIN_PRICE_CHANGE`,
or raise `MIN_LIQUIDITY` so fewer markets are polled. **Do not delete historical
rows** — that is what creates survivorship bias.

### Worker crash-looping

`ThrottleInterval` is 30s, so launchd will not spin. Read the error log first;
the most common causes are an unreachable database and an invalid `.env` (the
settings validator refuses unsafe combinations at startup, by design).

---

## 7. Routine checks

**Daily** — glance at `/dashboard`: system health, data age, kill switches.

**Weekly**

```bash
cd ~/beroapp && bash scripts/security_scan.sh
ls -lh backups/ | tail -5
```

**Monthly**

```bash
cd backend
PYTHONPATH=. .venv/bin/python scripts/phase_gate.py --target 2
.venv/bin/pip-audit -r requirements.txt
scripts/restore.sh backups/<latest>.dump beroapp_drill && dropdb beroapp_drill
```

A restore you have never performed is a restore you do not have.

---

## 8. Changing configuration

1. Edit `backend/.env`.
2. `launchctl unload` then `load` the affected job.
3. Confirm at `/api/system` that the new value is live.

Risk limits, the operating phase and `LIVE_TRADING_ENABLED` are **not** editable
through the API or the dashboard. That is deliberate: an interface that can arm
live trading is an interface that can arm it by accident.

The one exception is the global kill switch, which an operator key may set
through `POST /api/system/kill-switch/global`. It is audited, and it only ever
moves the system toward safer.
