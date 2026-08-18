# DATA_SOURCES.md

Authoritative record of every external data source the platform is permitted to
contact, verified against current official documentation and against live
responses.

**Verification date:** 2026-08-18
**Verification method:** official OpenAPI specifications downloaded from
`https://docs.polymarket.com/api-spec/`, official documentation pages under
`https://docs.polymarket.com/`, and live probe requests against the production
hosts to confirm response shapes.

Anything in this document marked **VERIFIED** was observed in a live response or
read directly from the official OpenAPI specification. Anything marked
**DOCUMENTED (not exercised)** was read from official documentation but is not
used by the current implementation.

---

## 1. Polymarket

Polymarket exposes three separate public HTTP services plus a public WebSocket.
They are not interchangeable. The platform uses each one only for its documented
purpose.

### 1.1 Gamma API — market and event discovery, metadata

| Field | Value |
|---|---|
| Base URL | `https://gamma-api.polymarket.com` |
| Official docs | https://docs.polymarket.com/api-reference/markets/list-markets |
| OpenAPI | `https://docs.polymarket.com/api-spec/gamma-openapi.json` (**returned an empty document on 2026-08-18** — parameters below were taken from the documentation pages and confirmed live) |
| Purpose in this platform | Discover the market universe; store market/event metadata, resolution source text, tags, status flags, token identifiers |
| Authentication | None for the read endpoints used |
| Used for order books | **No.** Gamma's `bestBid`/`bestAsk`/`spread` fields are metadata and are stored as *reference* values only. Executable prices come from the CLOB order book. |

#### Endpoints used

**`GET /markets`** — VERIFIED

Query parameters used: `limit`, `offset`, `order`, `ascending`, `closed`,
`condition_ids`, `end_date_min`, `id`. Full documented parameter list is at the
docs URL above.

Fields consumed (all VERIFIED present in a live response):

`id`, `conditionId`, `questionID`, `slug`, `question`, `description`,
`outcomes`, `outcomePrices`, `clobTokenIds`, `liquidityNum`, `volumeNum`,
`volume24hr`, `active`, `closed`, `archived`, `acceptingOrders`,
`enableOrderBook`, `negRisk`, `startDate`, `endDate`, `createdAt`, `updatedAt`,
`resolutionSource`, `resolvedBy`, `umaResolutionStatuses`, `bestBid`, `bestAsk`,
`spread`, `lastTradePrice`, `orderPriceMinTickSize`, `orderMinSize`,
`groupItemTitle`, `events`.

**Encoding hazard (VERIFIED):** `outcomes`, `outcomePrices` and `clobTokenIds`
are returned as *JSON-encoded strings*, not arrays. Example observed value:

```
"clobTokenIds": "[\"27146956652877944551877724690365745048289675287536243265951843487691050802191\", \"332166952178617421959413696638735739496796344324521420925454868498019152833
92\"]"
```

The ingestion layer decodes these defensively and rejects the market if decoding
fails or the arity does not match `outcomes`.

**`GET /events`** — VERIFIED

Used to obtain event grouping and, critically, **tags**. Fields consumed:
`id`, `ticker`, `slug`, `title`, `description`, `startDate`, `endDate`,
`liquidity`, `volume`, `openInterest`, `active`, `closed`, `archived`,
`negRisk`, `tags[]` (each with `id`, `label`, `slug`), `markets[]`.

Tags are the primary input to market classification.

#### Rate limits — from official documentation (`/api-reference/rate-limits`)

| Endpoint | Documented limit |
|---|---|
| Gamma general | 4,000 req / 10 s |
| `/events` | 500 req / 10 s |
| `/markets` | 300 req / 10 s |
| `/markets` + `/events` listing combined | 900 req / 10 s |
| `/comments` | 200 req / 10 s |
| `/tags` | 200 req / 10 s |
| `/public-search` | 350 req / 10 s |

The platform configures its limiter **far below** these ceilings (see
`POLYMARKET_GAMMA_RPS`, default 3 req/s) because it has no need for burst
throughput and because staying well inside a published limit is the courteous
and robust choice.

---

### 1.2 CLOB API — market microstructure, executable prices

| Field | Value |
|---|---|
| Base URL | `https://clob.polymarket.com` (VERIFIED from `servers:` block of the official CLOB OpenAPI spec) |
| OpenAPI | `https://docs.polymarket.com/api-spec/clob-openapi.yaml` (215,862 bytes, downloaded and parsed 2026-08-18) |
| Purpose in this platform | Order books, best bid/ask, midpoint, spread, price history, server time |
| Authentication | **None** for every endpoint the platform uses. All read endpoints listed below are public. L1/L2 signed authentication is required only for order placement and account endpoints, which this platform does not call. |

#### Endpoints used

| Method & path | Status | Use |
|---|---|---|
| `GET /book?token_id=` | VERIFIED | Single order book |
| `POST /books` | VERIFIED | **Batch** order books — the primary microstructure ingest path |
| `POST /midpoints` | VERIFIED | Batch midpoints (cross-check) |
| `GET /prices-history?market=<token_id>&interval=&fidelity=` | VERIFIED | Historical price series for backfill |
| `GET /time` | VERIFIED | Server clock skew measurement |
| `GET /price?token_id=&side=` | VERIFIED | Single best bid/ask (diagnostics only) |
| `GET /spread?token_id=` | VERIFIED | Single spread (diagnostics only) |

**`POST /books` response shape (VERIFIED):** a JSON array, one entry per
requested token, each with keys
`market`, `asset_id`, `timestamp`, `hash`, `bids`, `asks`, `min_order_size`,
`tick_size`, `neg_risk`, `last_trade_price`.
`bids` and `asks` are arrays of `{"price": "0.001", "size": "2757398"}` — both
values are **strings** and are parsed with explicit float conversion and
validation.

**Book ordering hazard (VERIFIED):** in observed responses `bids` were ordered
ascending by price and `asks` descending by price, i.e. the best level of each
side was the *last* element. The platform never relies on element order: best
bid is computed as `max(price)` over bids and best ask as `min(price)` over
asks.

**Side semantics (VERIFIED empirically):** for a token whose book gave
`max(bid)=0.004` and `min(ask)=0.010`, `GET /price?side=buy` returned `0.004`
and `GET /price?side=sell` returned `0.010`. Therefore `side=buy` is the **best
bid** and `side=sell` is the **best ask**. Because this naming is easy to invert,
the platform derives bid/ask from the raw book instead of from `/price`, and the
`/price` endpoint is used only for diagnostics.

**`GET /prices-history` response (VERIFIED):** `{"history": [{"t": <unix_s>,
"p": <float>}, ...]}`.

#### Rate limits — from official documentation

| Endpoint | Documented limit |
|---|---|
| CLOB general | 9,000 req / 10 s |
| `/book` | 1,500 req / 10 s |
| `/books` | 500 req / 10 s |
| `/price` | 1,500 req / 10 s |
| `/prices` | 500 req / 10 s |
| `/midpoint` | 1,500 req / 10 s |
| `/midpoints` | 500 req / 10 s |
| `/prices-history` | 1,000 req / 10 s |
| Tick size | 200 req / 10 s |

Configured default in this platform: `POLYMARKET_CLOB_RPS=5`.

#### Batching policy

The specification requires that we not issue one request per market where a bulk
endpoint exists. `POST /books` accepts a list of `{"token_id": ...}` objects and
returns one book per token. The ingestion worker chunks tokens into batches
(`POLYMARKET_BOOK_BATCH_SIZE`, default 50) so a universe of N tokens costs
`ceil(N/50)` requests per sampling cycle rather than N.

---

### 1.3 Data API — activity and analytics

| Field | Value |
|---|---|
| Base URL | `https://data-api.polymarket.com` (VERIFIED live; the official Data OpenAPI spec has an empty `servers:` block) |
| OpenAPI | `https://docs.polymarket.com/api-spec/data-openapi.yaml` (63,169 bytes, downloaded and parsed 2026-08-18) |
| Purpose in this platform | Open interest and public trade prints, as corroborating liquidity/activity evidence |
| Authentication | None for the endpoints used |

| Method & path | Status | Use |
|---|---|---|
| `GET /oi?market=<conditionId>` | VERIFIED — returns `[{"market": "0x...", "value": 1543.73}]` | Open interest |
| `GET /trades?market=<conditionId>&limit=` | VERIFIED — array of trade prints with `proxyWallet`, `side`, `asset`, `conditionId`, `size`, price, timestamp | Recent public trades |
| `GET /holders?market=<conditionId>` | VERIFIED | Holder concentration (not yet consumed) |
| `GET /positions`, `/closed-positions`, `/value`, `/activity` | DOCUMENTED (not exercised) | User-scoped; not used — the platform holds no user account |

`GET /live-volume?market=<conditionId>` returned **HTTP 400** on probe and is
therefore **not implemented**. It is recorded here so that a future maintainer
does not assume it works.

#### Rate limits — from official documentation

| Endpoint | Documented limit |
|---|---|
| Data API general | 1,000 req / 10 s |
| `/trades` | 200 req / 10 s |
| `/positions` | 150 req / 10 s |
| `/closed-positions` | 150 req / 10 s |

Configured default: `POLYMARKET_DATA_RPS=2`.

---

### 1.4 CLOB market WebSocket — DOCUMENTED, not yet implemented

| Field | Value |
|---|---|
| URL | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| Official docs | https://docs.polymarket.com/api-reference/wss/market |
| Authentication | None (public channel) |
| Subscribe payload | `{"assets_ids": [...], "type": "market", "initial_dump": true, "level": 2, "custom_feature_enabled": false}` |
| Message types | `book`, `price_change`, `last_trade_price`, `tick_size_change`, `best_bid_ask`*, `new_market`*, `market_resolved`* (*require `custom_feature_enabled: true`) |
| Heartbeat | Send `PING` every 10 s; server replies `PONG` |

**Current status: not implemented.** Phase 1 uses REST polling of `POST /books`
because it is simpler to make crash-safe and its cost is bounded and predictable.
The WebSocket is the correct next step for reducing data latency and is recorded
here with its verified contract so it can be added without re-research. It is
listed as DEGRADED-capability, not as a delivered feature.

---

### 1.5 Polymarket usage constraints

* The platform reads only public, documented, unauthenticated endpoints.
* It performs **no scraping** of `polymarket.com` HTML.
* It holds no Polymarket account, submits no orders, and stores no credentials
  for Polymarket. Phase 1 and Phase 2 have no code path that can place an order
  (see `SECURITY.md`).
* Requests carry an identifying `User-Agent` (`POLYMARKET_USER_AGENT`).
  A default `python-urllib` user agent received **HTTP 403** during probing; a
  descriptive UA succeeded. The platform therefore always sends a descriptive UA
  and never impersonates a browser.
* Geographic restrictions are documented at
  https://docs.polymarket.com/api-reference/geoblock. The platform does not
  attempt to detect or circumvent them.

---

## 2. External evidence sources

The probability engine's design separates *market data* (Polymarket) from
*evidence* (everything else). The evidence-source framework — tiering,
reliability scoring, `known_at` provenance — is implemented in the database
schema and the source-registry service.

### Implementation status, stated honestly

| Component | Status |
|---|---|
| `external_sources` / `external_events` tables with full provenance columns | IMPLEMENTED |
| Source registry with tier + reliability score, seeded by `scripts/seed.py` | IMPLEMENTED |
| Data-sources page reporting per-source health and ENABLED/DISABLED | IMPLEMENTED |
| **Any external-evidence ingestion connector** | **NOT IMPLEMENTED** |

To be unambiguous: **no external evidence source is currently ingested.** The
registry rows below exist so the framework is visible and so a connector can be
added without redesigning the schema, but every one of them reports `DISABLED`
on the data-sources page, and `external_events` is empty on a running system.

The consequence is stated plainly rather than glossed: the probability engine
presently has no external evidence, so its only genuinely independent signal is
the neg-risk coherence constraint, which is derived from Polymarket's own
prices. Markets outside the evidence-supported categories are capped at
`WATCHLIST` and never reach `TRADEABLE`, because a probability formed without
evidence is a repackaging of the market price rather than an independent
estimate.

The platform does not fabricate evidence for markets it has no connector for.

### 2.1 Tier 1 — primary / authoritative

| Source | URL | Category | Access | Frequency | Licensing note | Fallback |
|---|---|---|---|---|---|---|
All rows below are **registered but not implemented**. They record which
sources have been assessed as acceptable to use, on what terms, so that adding
one is a matter of writing a connector rather than re-doing the assessment.

| Source | URL | Category | Access | Licensing note | Status |
|---|---|---|---|---|---|
| U.S. Treasury Fiscal Data | https://fiscaldata.treasury.gov/api-documentation/ | Macro | Public REST JSON, no key | Public domain | registered, no connector |
| FRED (St. Louis Fed) | https://fred.stlouisfed.org/docs/api/fred/ | Macro | REST, free API key | Free registration; series terms vary by originating agency | registered, no connector |
| SEC EDGAR submissions & company facts | https://www.sec.gov/search-filings/edgar-application-programming-interfaces | Companies | Public REST JSON | SEC requires a declared `User-Agent` with contact info and ≤10 req/s | registered, no connector |
| Federal Reserve / FOMC calendar | https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm | Fed | Public | Public domain | registered, no connector |
| BLS | https://www.bls.gov/developers/ | Macro | REST, optional key | Registration raises quota | registered, no connector |
| BEA | https://apps.bea.gov/api/ | Macro | REST, free key | Free registration | registered, no connector |

When a connector is eventually added, a source whose required key is absent must
report `DISABLED` on the data-sources page rather than silently returning
nothing.

### 2.2 Tier 2 — high-quality secondary

Reuters, AP, Bloomberg, FT, WSJ, BBC. **No connector implemented.** Several of
these prohibit automated redistribution or require paid licensing; per the
zero-cost requirement none has been integrated. Where a public RSS feed exists
and its terms permit, a future connector would store headline + link + timestamp
only, never full article text, and would classify every item as
`REPORTED_INFORMATION`, never `CONFIRMED_FACT`.

### 2.3 Tier 3 — specialist / research

Academic and polling sources. **No connector implemented.**

### 2.4 Tier 4 — social / unverified

**No connector implemented, and none is planned for Phase 1.** If added, items
would be stored with `source_type='social_media'` and
`verification_status='unverified'`, and the schema forbids an unverified item
from raising a claim's status to `CONFIRMED_FACT` (enforced in
`engines/evidence.py`).

---

## 3. Provenance recorded for every external datum

Every row in `external_events` carries:

`source_id`, `source_type`, `source_tier`, `reference_url`, `published_at`,
`ingested_at`, `known_at`, `verification_status`, `reliability_score`,
`parser_version`, `content_hash`.

`known_at` is the timestamp the platform may first legitimately use the datum;
it is `max(published_at, ingested_at)` and it is what the backtester filters on.
Rows are append-only: a correction is a new row with a new `content_hash`, never
an update to the old row.

---

## 4. What this platform deliberately does not do

* No paid data vendor.
* No aggressive scraping; no bypassing of robots directives, rate limits or
  authentication.
* No source added merely because it is popular.
* No use of an LLM as a data source. An LLM may only restructure text that was
  already ingested from a recorded source, and its output is stored as
  `MODEL_OUTPUT`, never as evidence.
