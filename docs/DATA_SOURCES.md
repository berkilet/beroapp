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
*evidence* (everything else). Phase 1.5 implemented the evidence layer: seven
connectors against Tier-1 sources, all keyless, all verified against live
endpoints on 2026-08-18 before any code was written against them.

`app/evidence/registry.py` is the single declaration of every source the
platform may use. `scripts/seed.py` pushes that declaration into the database;
it does not carry a list of its own. The registry also derives the SSRF
allow-list, so a source without a connector is not merely unused — its host is
not reachable from this application at all.

### 2.1 Implementation status

| Component | Status |
|---|---|
| `external_sources` / `external_events` with full provenance columns | IMPLEMENTED |
| Source registry: tier, reliability, budget, terms, parser version | IMPLEMENTED |
| Per-source health, circuit breaking, daily budget enforcement | IMPLEMENTED |
| Seven Tier-1 connectors (below) | IMPLEMENTED |
| Evidence → market matching with a recorded reason per link | IMPLEMENTED |
| Conflict detection and precedence-based resolution | IMPLEMENTED |
| Tier 2–4 connectors (news, polling, social) | NOT IMPLEMENTED |
| FRED, BEA (require a key this deployment does not hold) | NOT IMPLEMENTED |

### 2.2 Tier 1 — implemented connectors

All verified 2026-08-18. "Frequency" is how often the worker polls; where a
source publishes a daily cap, the platform tracks consumption in the database
and refuses the call rather than exceeding it.

| Source | Endpoint | Access | Frequency | Documented limit | Terms |
|---|---|---|---|---|---|
| U.S. Treasury daily yield curve | `home.treasury.gov/.../pages/xml` | XML feed, no key | 6 h | none published | [terms](https://home.treasury.gov/utility/terms-of-use) |
| U.S. Treasury Fiscal Data | `api.fiscaldata.treasury.gov` | REST JSON, no key | 12 h | none published | [docs](https://fiscaldata.treasury.gov/api-documentation/) |
| U.S. Bureau of Labor Statistics | `api.bls.gov/publicAPI/v2` | REST JSON, no key | 6 h | **25 queries/day** unregistered | [FAQ](https://www.bls.gov/developers/api_faqs.htm) |
| Federal Reserve FOMC calendar | `federalreserve.gov/monetarypolicy/fomccalendars.htm` | HTML, structured parse | 24 h | none published | [disclaimer](https://www.federalreserve.gov/aboutthefed/legal-disclaimer.htm) |
| SEC EDGAR | `data.sec.gov` | REST JSON, no key | 6 h | 10 req/s, declared User-Agent required | [webmaster FAQ](https://www.sec.gov/os/webmaster-faq#developers) |
| Coinbase Exchange | `api.exchange.coinbase.com` | REST JSON, no key | 5 min | ~10 req/s per IP | [docs](https://docs.cdp.coinbase.com/exchange/docs/welcome) |
| Kraken | `api.kraken.com` | REST JSON, no key | 5 min | ~1 req/s sustained | [docs](https://docs.kraken.com/api/) |

Notes that shaped the implementation rather than decorating it:

* **BLS's 25/day cap drove the whole connector design.** Every series the
  platform wants is batched into a single POST — the v2 API accepts 25 series
  per query — and the worker runs four times a day, consuming four of the
  twenty-five. The remaining budget is headroom for retries. Consumption is
  recorded in `external_sources.requests_today` and reset daily; when it is
  exhausted the connector refuses rather than trying anyway.
* **SEC requires a declared, contactable User-Agent.** Without `SEC_USER_AGENT`
  set, the connector refuses to run and the source reports `DISABLED`. It does
  not fall back to an anonymous request.
* **FOMC is an HTML page, not an API.** The parser reads meeting dates from the
  calendar's structure and stores dates only — never prose, never a rate
  expectation inferred from commentary.
* **Two crypto venues, cross-checked.** Coinbase is primary; Kraken's spot is
  ingested independently so a disagreement between them is detectable rather
  than invisible. Binance was probed and returns HTTP 451 from this
  deployment's egress, so it is not used.
* **FEC is implemented but disabled.** It requires a key. The connector
  deliberately does *not* fall back to the shared public `DEMO_KEY`: 30
  requests/hour is too tight for continuous polling, and pointing a 24/7 worker
  at a shared demo credential is poor citizenship. Without `FEC_API_KEY` it
  raises `missing_api_key` and the source reports `DISABLED`.

### 2.3 What is actually collected

| Source | Series | Example keys |
|---|---|---|
| Treasury yield curve | 8 | `UST_YIELD_3M`, `UST_YIELD_2Y`, `UST_YIELD_10Y` |
| Treasury Fiscal Data | 16 | debt-to-the-penny and related fiscal series |
| BLS | 4 | `CPI_URBAN_ALL`, `CPI_CORE`, `UNEMPLOYMENT_RATE`, `NONFARM_PAYROLLS` |
| FOMC calendar | 1 | `FOMC_MEETING` (dates only) |
| SEC EDGAR | 24 | `SEC_FILING_<TICKER>_<FORM>` metadata |
| Coinbase | 15 | `CRYPTO_SPOT_*`, `CRYPTO_VOL30_*`, `CRYPTO_VOL_*` |
| Kraken | 5 | `CRYPTO_SPOT_*` (cross-check) |

Two realised-volatility windows are stored per asset, not one. A 90-day window
priced a five-day question badly; the feature builder now picks the 30-day
series for horizons up to three weeks and the 90-day series beyond that.

### 2.4 Tier 1 — declared, no connector

| Source | Why not implemented |
|---|---|
| FRED | Requires a free key; verified to return HTTP 400 without one. The series this platform needs are available keyless from BLS and Treasury. Worth adding if a key is configured — FRED's coverage is far broader. |
| BEA | Requires a free key. GDP markets are rare on the venue. |

### 2.5 Tier 2 — high-quality secondary

Reuters and AP are declared and disabled. Both require licensing for automated
redistribution, which the zero-cost constraint excludes. If either were added,
items would be stored as `REPORTED_INFORMATION` and could never be promoted to
`CONFIRMED_FACT`.

### 2.6 Tier 3 — polling aggregators

Declared and disabled. Terms differ per publisher and must be assessed one at a
time; there is no single aggregator whose licence covers the others. The
registry entry deliberately names an unroutable host so that nothing can reach
a real publisher through it before a specific one is chosen and documented.

### 2.7 Tier 4 — social / unverified

Declared, disabled, and not planned. If added, every item would be stored
`UNVERIFIED`, could never be promoted to `CONFIRMED_FACT`, and — per the
Phase 1.5 requirement — could never independently raise a signal. It would be
an early warning to go and check a Tier-1 source, nothing more.

### 2.8 Matching evidence to markets

Evidence is linked to markets through `market_evidence_links`, a many-to-many
table rather than a foreign key on the evidence row, because the relationship
genuinely is many-to-many: one CPI release bears on every inflation market at
once.

Routing is by subcategory (`app/evidence/matching.py`), and every link records
*why* it was made — `primary_series:FED_RATES`, `supporting_series:INFLATION` —
along with a relevance score. A market with no route gets no link; the matcher
returns nothing rather than a low score, so "we found nothing relevant" and "we
found something barely relevant" stay distinguishable.

### 2.9 Conflicts

Where two sources report the same fact for the same period and disagree by more
than 0.5% relatively, the disagreement is recorded in `evidence_conflicts` and
resolved by a fixed precedence ladder: higher tier, then better verification
status, then more recent, then more reliable source. If none of those separates
the candidates the conflict is stored `UNRESOLVED` and neither value is used.

Values are **never averaged**. Two sources disagreeing about a published
statistic means one of them is wrong, and the mean of a right answer and a wrong
answer is a third wrong answer that looks more trustworthy than either.

---

## 3. Provenance recorded for every external datum

Every row in `external_events` carries:

`source_id`, `source_type`, `source_tier`, `reference_url`, `published_at`,
`observation_date`, `ingested_at`, `known_at`, `verification_status`,
`reliability_score`, `parser_version`, `content_hash`, `superseded_by_id`.

### Three timestamps, kept separate

| Column | Means |
|---|---|
| `observation_date` | the period the figure describes — July's CPI |
| `published_at` | when the issuing body released it — mid-August |
| `known_at` | when this platform could first legitimately use it |

Conflating the first two lets a backtest "know" July's inflation during July.
Conflating the last two lets it know a figure before it was published. Every
read path filters on `known_at <= as_of`, and `EvidenceItem` refuses at
construction time to accept a `published_at` later than its `known_at`.

`known_at` is set by the code that learns the fact, never by a database
default — a `now()` default would silently stamp a backfilled row with the time
of the backfill, which is exactly the look-ahead these three columns exist to
prevent. There is a test asserting the column has no server default.

### Revisions

Rows are append-only. A revision is a new row plus a `superseded_by_id` pointer
on the old one, never an update in place, so what we believed at the time stays
answerable. The dashboard dims superseded rows rather than hiding them.

`content_hash` covers the *fact* — series, period, value — and deliberately
excludes `known_at`, so re-fetching the same figure deduplicates while a genuine
revision does not.

---

## 4. What this platform deliberately does not do

* No paid data vendor.
* No aggressive scraping; no bypassing of robots directives, rate limits or
  authentication.
* No source added merely because it is popular.
* No use of an LLM as a data source. An LLM may only restructure text that was
  already ingested from a recorded source, and its output is stored as
  `MODEL_OUTPUT`, never as evidence.
* No connector written against a remembered API specification. Every endpoint
  in section 2.2 was probed against the live service before any code was
  written against it, and the registry records the date it was verified.
* No source reached that is not declared in the registry: the SSRF allow-list
  is derived from the same tuple, so an undeclared host is unreachable rather
  than merely unused.
