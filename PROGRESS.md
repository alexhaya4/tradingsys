# Progress Tracker

Companion to `SPEC.md`. This file is updated as work completes. `SPEC.md` is
not modified except by explicit direction from the director.

**Current phase:** 1, Foundation
**Status:** Complete, awaiting director review before phase 2

---

## Phase 1: Foundation

| Task | Status | Notes |
|---|---|---|
| Project structure, src layout, pyproject with uv | Complete | `src/tradingsys`, uv lockfile committed |
| Tooling: ruff, mypy strict, pytest with asyncio | Complete | Plus hypothesis for money and sizing property tests |
| Configuration system, pydantic-settings, TOML plus env | Complete | Four layers, secrets refused from files |
| Core domain types: Money, Instrument, precision handling | Complete | Decimal throughout, float rejected at construction |
| MarketDataSource abstract interface | Complete | Signatures only, no adapter |
| ExecutionVenue abstract interface | Complete | Signatures only, no adapter |
| Database: asyncpg, TimescaleDB, Alembic migrations | Complete | Migration 0001, upgrade and downgrade both verified |
| Schema: instrument, ohlcv, tick, audit_log | Complete | Bars and ticks are hypertables; audit log is hash chained |
| Logging: structlog JSON with correlation IDs | Complete | Stdlib bridged, Decimals rendered exactly |
| Metrics endpoint, health and ready endpoints | Complete | Own registry, liveness independent of dependencies |
| Docker Compose: app, timescale, redis, prometheus, grafana | Complete | Scrape and dashboard provisioning verified live |

### Phase 1 exit criteria

- [x] `mypy --strict` clean (63 source files)
- [x] `ruff` clean (check and format)
- [x] Test suite green (659 unit, 61 integration, 720 total)
- [x] Compose stack starts, all health checks pass
- [x] Configuration proven to fail loudly on missing required values
- [x] No venue adapter implementations present

### How the exit criteria were verified

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run pytest                                  # 659 passed
TRADINGSYS_APP__ENVIRONMENT=test uv run pytest -m integration   # 61 passed
docker compose up -d --build                   # all containers healthy
```

Compose was brought up in full and observed: the app reports `{"status":"pass"}`
on `/ready` with live database and Redis checks, Prometheus scrapes
`app:8000/metrics` with target health `up`, and Grafana has the Prometheus
datasource and the service health dashboard provisioned. Migration `0001` was
applied, rolled back to base, and re-applied cleanly.

Configuration failure is covered by test rather than by assertion in prose:
missing required values, unknown keys, malformed values, non-https venue URLs,
mismatched environments, and a secret placed in a TOML file each abort startup
with a message naming the field and the environment variable that would set it.

### Decisions taken during phase 1

| Decision | Rationale |
|---|---|
| Package named `tradingsys` | Directory name is incidental; the package name appears in every import |
| Interfaces shaped against OANDA v20 semantics | Reference target per SPEC 3.3, with no OANDA vocabulary in the base classes |
| TOML for the file layers | Unambiguous typing, consistent with pyproject.toml |
| `venue_symbol` stored beside the canonical symbol | SPEC 4 requires venue symbol mapping; the transformation is not derivable |
| Audit appends serialised by a transaction level advisory lock | Two concurrent writers would otherwise fork the hash chain |
| `previous_hash` is UNIQUE | Makes a chain fork impossible at the storage layer, not only by convention |
| Live venues require `app.allow_live_trading` and production | Copying a production config to a developer machine cannot arm real orders |
| Integration tests fail rather than skip | A suite that skips its only storage coverage reports green while testing nothing |

### Defects found and fixed by running the stack, not by unit tests

| Defect | Consequence had it shipped |
|---|---|
| Metrics served the classic text format under the OpenMetrics content type | Prometheus rejected every scrape with "data does not end with # EOF"; no metrics at all |
| Prometheus scrape config added a `service` target label | Collided with the metric's own label, renaming it to `exported_service` and breaking joins |
| Startup surfaced a raw `UndefinedTableError` on an unmigrated database | Operator sees a driver error instead of "run alembic upgrade head" |

Each now has a regression test.

---

## Phases 2 through 9

Not started. See `SPEC.md` section 8 for scope and exit criteria. Do not begin
a phase before the previous phase's exit criteria are all met.

---

## Open questions for the director

| Question | Raised | Resolved |
|---|---|---|
| OANDA live account eligibility for Kenya, needed before phase 8 | Phase 1 | Open |
| Primary crypto exchange selection, needed before phase 2 | Phase 1 | Open |
| Instrument universe: which pairs and markets to cover initially | Phase 1 | Open |
| Base currency for accounting | Phase 1 | Open |
| Starting capital, needed to set risk limits in absolute terms | Phase 5 | Open |

### Raised during phase 1, needed before phase 2

| Question | Why it matters now |
|---|---|
| Tick retention window, and bar retention before compression | The schema sets 7 day tick chunks compressed after 7 days and 7 day bar chunks compressed after 30. Both are placeholders for a real retention policy, which depends on how far back backtests must reach and what storage is available. |
| Which candle price component is authoritative for forex | Bid, ask, and mid are stored separately and deliberately not interchangeable. Backtesting on mid and executing on ask is a systematic bias, so the choice has to be explicit before ingestion begins. |
| Whether the crypto venue is netting or hedging | `VenueCapabilities.position_mode` expresses both, but the risk engine's view of "the position" differs between them. |
