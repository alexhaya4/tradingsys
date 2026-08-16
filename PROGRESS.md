# Progress Tracker

Companion to `SPEC.md`. This file is updated as work completes. `SPEC.md` is
not modified except by explicit direction from the director.

**Current phase:** 2, Market data
**Status:** in progress. Phase 1 is complete and accepted; its record is kept
below unchanged.

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

- [x] `mypy --strict` clean (33 files under src, 67 including tests)
- [x] `ruff` clean (check and format)
- [x] Test suite green (759 unit, 62 integration, 821 total)
- [x] Compose stack starts, all health checks pass
- [x] Configuration proven to fail loudly on missing required values
- [x] No venue adapter implementations present

### Verification evidence

Recorded against phase 1 completion. Every line below was observed, not inferred.

| Evidence | Result |
|---|---|
| `ruff format --check` and `ruff check` | Clean across 70 files |
| `mypy --strict` | Clean. 33 files under `src/`, 67 including tests |
| Unit tests | 759 passed, including 35 hypothesis property tests over money and sizing |
| Integration tests | 62 passed against live PostgreSQL 16 with TimescaleDB 2.17.2 |
| Combined suite with branch coverage | 821 passed, 96 percent statement and branch |
| `alembic current` | `0001 (head)` |
| Hypertables | `ohlcv_bars` and `ticks` confirmed in `timescaledb_information.hypertables`, both with compression enabled and a compression policy |
| Migration reversibility | `downgrade base` then `upgrade head` applied cleanly, all four tables recreated |
| Compose stack | Five services healthy: app, db, redis, prometheus, grafana |
| App connectivity | Connects to PostgreSQL and Redis at startup; `live_venues=[]` in the startup log |
| Config abort | Demonstrated against a real missing secret: exit code 78, reason on stderr, nothing on stdout |
| `scripts/verify.sh` | Full run, all thirteen steps passed, exit 0 |

The `verify.sh` run was performed on the CI path, meaning no `.env` file present
and credentials injected as environment variables, which is exactly what the
GitHub Actions workflow does.

### How to reproduce

```bash
scripts/verify.sh
```

That script is the only verification path. It starts the stack, creates and migrates
both databases, runs the formatter, linter, type checker, unit suite, and integration
suite, brings up the app with Prometheus and Grafana, and probes the endpoints. CI runs
the same script, so the local and pipeline paths cannot diverge, and the README points
at it rather than listing its steps, so the documented path cannot drift from the
working one.

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
| Interfaces shaped against real venue semantics, with no venue vocabulary in them | Proven when the forex venue changed from OANDA to cTrader mid-phase and no interface changed |
| Forex venue is cTrader Open API via Pepperstone | OANDA does not accept registrations from Kenya. See SPEC 3.3 |
| OAuth credentials modelled as four flat secret fields | A validation failure can then name each absent field, which a nested `credentials` section cannot |
| Credential expiry and refresh live on `VenueConnection` | An expiring token is a property of some venues, not a cTrader implementation detail, and a supervisor has to see it |
| `refresh_credentials` returns the new material instead of absorbing it | cTrader rotates the refresh token, and the replacement must be persisted before the next restart or the account locks out |
| The audit digest is pinned to a golden value in a test | Every other hash test compares production output to production output, so a change to the canonical form would pass while invalidating every stored digest |
| TOML for the file layers | Unambiguous typing, consistent with pyproject.toml |
| `venue_symbol` stored beside the canonical symbol | SPEC 4 requires venue symbol mapping; the transformation is not derivable |
| Audit appends serialised by a transaction level advisory lock | Two concurrent writers would otherwise fork the hash chain |
| `previous_hash` is UNIQUE | Makes a chain fork impossible at the storage layer, not only by convention |
| Live venues require `app.allow_live_trading` and production | Copying a production config to a developer machine cannot arm real orders |
| Integration tests fail rather than skip | A suite that skips its only storage coverage reports green while testing nothing |
| The application never reads `.env`, in any environment | See below |
| `scripts/verify.sh` is the only verification path, invoked by both the README and CI | A transcribed command sequence drifts from the one that actually runs, which is how the 61 error run happened |

### Why the application never reads .env

`.env` is read by docker compose and by `scripts/verify.sh`. The application has no
dotenv source at all, in development, test, staging, or production.

The rule is that the application reads secrets only from the environment or the secrets
directory, never from a file it parses, and the rule is valuable precisely because it
has no exceptions. The tempting alternative, permitting `.env` in development and test
and refusing it in staging and production, makes the guarantee conditional on the
environment selector itself being correct, which adds a failure mode on the day it
matters most. The invariant stays absolute and the cost is paid in documentation: one
`set -a` line, which `scripts/verify.sh` runs for you.

### Follow-up round: the 61 error integration run

An integration run against real Postgres produced 61 setup errors. All 61 had one
cause, and no application code was at fault: `TRADINGSYS_DATABASE__PASSWORD` was absent
from the shell. The app container was healthy in the same run because compose injects
`.env` and host side processes do not.

| Defect | Fix |
|---|---|
| The README's integration recipe omitted the password on both the alembic and pytest lines, so following it verbatim reproduced the failure | The README now points at `scripts/verify.sh` instead of transcribing steps, and `tests/test_verification_path.py` fails if a migration or integration command reappears in the README |
| `.env` was documented as the place for local secrets but silently ignored by the application | Stated explicitly in the README, `.env.example`, and the code that drops the dotenv source |
| The harness reported the same message 61 times across 13,000 lines | A collection hook reports it once and stops the session when every selected test needs live settings; mixed selections still fall back to per-test failure so unit results are not lost. 13,000 lines to 25 |
| The guidance told operators to set a variable even when the real error was an unrecognised one | `format_validation_error` now gives advice per error type: "set with" for missing fields, "remove or model" for unrecognised ones |
| `scripts/verify.sh` used `((waited++))`, which returns exit status 1 on the first iteration and killed the run under `set -e` | Replaced with arithmetic assignment. Found by running the script, not by reading it |
| The script's ERR trap never fired because `set -E` was absent, so failures reported no step name | `set -Eeuo pipefail` |

### Defects found and fixed by running the stack, not by unit tests

| Defect | Consequence had it shipped |
|---|---|
| Metrics served the classic text format under the OpenMetrics content type | Prometheus rejected every scrape with "data does not end with # EOF"; no metrics at all |
| Prometheus scrape config added a `service` target label | Collided with the metric's own label, renaming it to `exported_service` and breaking joins |
| Startup surfaced a raw `UndefinedTableError` on an unmigrated database | Operator sees a driver error instead of "run alembic upgrade head" |

Each now has a regression test.

---

### Resolved: the forex venue changed to cTrader

The director's decision: model the cTrader credential set now. This is not phase 2
work leaking into phase 1. `ForexVenueSettings` modelled an OANDA style bearer token,
and the venue changed before phase 1 closed, so the configuration model was incomplete
for its own phase and completing it is phase 1 scope. `extra="forbid"` was left
untouched.

**Configuration.** `venues.forex` now carries `client_id`, `client_secret`,
`access_token`, and `refresh_token`, all typed `SecretStr` and therefore all refused
from TOML files by the existing guard, plus the non-secret `account_id`. The five
variable names already in `.env` were kept exactly as they were, so nothing had to be
renamed. They are flat rather than nested under a `credentials` section for a reason:
a validation failure names each field that is absent, and being told "credentials are
required" five times while supplying them one at a time is a miserable way to
configure a venue.

The endpoints changed shape too. cTrader's trading connection is a persistent TLS
socket carrying protobuf, not an HTTP endpoint, so `rest_url` and `stream_url` were
replaced by a host and port, with `token_url` kept separate because OAuth token
exchange genuinely is an HTTPS call. The host is not a single field: `demo_api_host`
and `live_api_host` are both configured and the code selects between them by
`environment`, so a deployment cannot point a demo account at the live endpoint. The
two are fully separated at the venue, and there is now no field in which to express
the mismatch. The https validation now guards `token_url`,
which is the one that carries the client secret.

**Interface.** Credential expiry and refresh are now part of `VenueConnection`:
`credentials_expire_at`, `refresh_credentials()`, and a `credentials_expire` capability
flag so callers ask rather than know. This is venue neutral, not a cTrader detail: a
static key and secret reports no expiry and raises `UnsupportedVenueOperationError` on
refresh.

Two properties of OAuth drove the design. The access token expires after about thirty
days, so a process that reads credentials only at startup authenticates perfectly for a
month and then stops; expiry therefore has to be visible to a supervisor rather than
buried in an adapter. And refreshing rotates the refresh token, so `refresh_credentials`
returns the new material rather than absorbing it: an adapter that updated itself in
memory would work until the first restart after a rotation and then fail authentication
with no obvious cause. `token_refresh_margin_seconds`, three days by default, exists so
that a failed renewal can be retried while the old credential still works. Renewing at
expiry leaves no room for a retry, and a dead credential needs manual reauthorisation.

**Endpoint values confirmed at the start of phase 2.** The director confirmed
`demo.ctraderapi.com:5035` and `live.ctraderapi.com:5035`, that port 5035 carries
protobuf only (5036 is JSON), and that TLS is mandatory. `https://openapi.ctrader.com/apps/token`
was verified against help.ctrader.com/open-api/account-authentication and is correct as
configured.

**Crypto venue key renamed to `bybit`.** The position model decision names Bybit, and
leaving a `binance` key configured while the specification says Bybit would be
incoherent. This was an inference from that sentence rather than an explicit
instruction; say so if it is wrong. The key is non-secret configuration with
`enabled = false`, so nothing operational depends on it.

Separately: the tokens in `.env` are live credentials for the demo account. `.env` is
gitignored so they cannot be committed, but they should not be pasted into tickets or
logs.

### Test quality report

Requested before phase 1 could be marked complete.

**Coverage, with branch coverage on.** 96 percent statement and branch across
2,836 statements and 658 branches, from 821 tests. Modules at 100 percent
include `money`, `currency`, `numeric`, `clock`, `rounding`, all four
observability modules, and the venue enums and errors. The lowest are
`__main__` at 50 percent and `database` at 86 percent.

`__main__` reads low for a measurement reason rather than a coverage one: its
tests launch the real entry point as a subprocess, so the parent process's
coverage cannot see the lines execute. Every branch in it is exercised, and the
exit codes are asserted against what the operating system reports, which is
what a supervisor actually reads. The `database` gaps are defensive branches on
a connection that is already closing.

**Tests that only asserted a call did not raise: 7 found, 7 fixed.** They were
`test_stop_is_safe_before_start`, `test_stop_is_idempotent`,
`test_serve_stops_on_a_shutdown_signal`, `test_plain_data_is_accepted`,
`test_health_check`, `test_close_is_idempotent`, and
`test_a_valid_request_passes_instrument_validation`. Each now asserts observable
state as well: pool statistics after close, `is_started` after shutdown, that
the server task completed rather than being cancelled, that a quantity and every
price field are on grid. A rerun of the scan finds zero remaining.

**Tests asserting against a value they computed themselves: 21 candidates, 20
legitimate, 1 real defect, fixed.** A scan for assertions calling the same
production method on both sides flagged 21. Twenty are sound: using
`Money.of(...)` to express an expected value while testing `-` or `*` is a
constructor, not a tautology, and the property tests deliberately assert
relationships such as "doubling the size doubles the notional", which is the
entire point of property testing.

The genuine defect was the audit log's hash. Every hash test compared one output
of `compute_entry_hash` against another, so the suite verified that the chain
was self-consistent but never that the algorithm was what it had been. Reordering
the hashed fields, changing the separator, or dropping the timestamp would have
left all of them green while silently invalidating every digest already written
to the database. Fixed by pinning a known entry to a literal digest, so a change
to the canonical form now fails a test and forces the question of whether stored
logs need rehashing.

**Production code exercised only through a mock: none, because there are no
mocks.** `unittest.mock`, `MagicMock`, `AsyncMock`, and `monkeypatch` appear
nowhere in the suite. There are four hand-written doubles, and each production
path they touch is also exercised against the real dependency:

| Double | What it drives | Also covered for real by |
|---|---|---|
| `FakeDatabase`, `FakeRedis` | `DatabaseCheck`, `RedisCheck` | Integration readiness tests against live PostgreSQL and Redis |
| `ScriptedCheck` | `HealthRegistry` concurrency, timeout, and aggregation | The same registry holding real checks in the app integration tests |
| `FakeRecord` | `candle_from_row`, `quote_from_row` | The same functions over real asyncpg rows in `fetch_bars` and `fetch_ticks` |
| `ConformingMarketData`, `ConformingExecution` | The venue ABCs and their context manager | Nothing, by design: phase 1 has no adapter, so there is no real implementation to test against. This is the one gap, and it closes in phase 2 |

The doubles exist to drive branches a real dependency cannot be made to take on
demand, such as a health check that hangs past its deadline. None of them stands
in for logic that is otherwise untested.

### Questions raised during phase 1, now answered

All three have been answered by the director and recorded in SPEC 4.0:

| Question | Answer |
|---|---|
| Tick and bar retention | Ticks compressed after 7 days, retained 24 months. One minute bars never dropped, higher timeframes derived on read through continuous aggregates. Both windows are configuration values |
| Authoritative candle price component | Bid and ask stored as separate series. Mid computed on read for signals only, never stored as authoritative, never used for fill simulation. Fills use the side crossed |
| Netting or hedging | Both modelled at the venue abstraction, netting enforced as system policy by the risk engine regardless of venue capability |

Two items remain open for phase 2:

| Item | Why it matters |
|---|---|
| Confirm the cTrader endpoint values | Resolved at the start of phase 2. Hosts and port confirmed by the director; token URL verified against the vendor documentation and found correct |
| Confirm Bybit as the crypto venue | Resolved: confirmed by the director, the rename was correct |


---

## Phase 2: Market data

**Status:** in progress.

| Task | Status | Notes |
|---|---|---|
| Schedule-aware gap detection | Complete | Compares coverage against `TradingSchedule`, so weekends, holidays, and session boundaries are never reported. Both daylight saving transitions pinned |
| Dukascopy `.bi5` reader | Complete | In house, validated payloads, exact prices and volumes, byte for byte round trip against a recorded hour |
| Bybit instrument metadata | Complete | Mapped from `instruments-info`, tested against recorded responses |
| Bybit public REST client and rate limiter | Complete | Envelope checked, 403 treated as a ten minute block, retries with jittered backoff |
| Bybit WebSocket quote stream | Complete | orderbook.1, reconnects with resynchronisation, silence treated as death |
| Quote recorder into the tick table | Complete | Batched, retried, flushed on shutdown, tagged with its source |
| Instrument eligibility screen | Complete | Configurable maximum deviation from intended risk, evaluated against live metadata |
| Funding drag measurement | Complete | Reported below |
| cTrader adapter | Not started | |
| Instrument registry from venue metadata | Not started | |
| Resumable Dukascopy backfill | Not started | |
| 72 hour continuous ingestion run | Not started | Begins once the adapters and the registry are working |

### Decisions taken during phase 2

**Crypto product: linear perpetuals, not spot.** Decided by the director on
2026-08-16 after the measurement below. Spot is unleveraged, so at 200 USD of
capital a 2.00 USD risk requires 200 USD of notional at a 1 percent stop, which
is the entire account in a single position, and 400 USD at a 0.5 percent stop,
which is unreachable. That is not a worse option, it is an unworkable one at
this capital.

**BTC/USDT perpetual is excluded from trading.** Not from recording. The
grounds are quantisation, and the numbers are from Bybit's own metadata on
2026-08-16 with BTC at 63,035 and ETH at 1,880 USDT:

| Instrument | Quantity step | Notional per step | Risk per step at a 1 percent stop | Distinct sizes within a 2.00 USD budget | Widest stop affordable at minimum size |
|---|---|---|---|---|---|
| ETH/USDT perpetual | 0.01 ETH | 18.80 USDT | 0.188 USDT | 10 | 10.6 percent |
| BTC/USDT perpetual | 0.001 BTC | 63.03 USDT | 0.630 USDT | 3 | 3.17 percent |

With three usable sizes, the realised risk on a BTC trade can sit up to 31
percent away from the 1 percent the risk engine claims to be enforcing. A limit
that is approximated to within a third is not being enforced, and `SPEC.md`
section 6 already says the answer is to exclude the instrument rather than to
accept the approximation. The exclusion is therefore consistent with the
existing rule rather than a new one.

**The exclusion is a configuration threshold, not a constant.** Expressed as
the maximum acceptable deviation from intended risk and evaluated against live
venue metadata and price, so it re-evaluates on its own as capital grows and
BTC stops binding. Revisit at phase 5. Hardcoding today's answer would leave
the system excluding an instrument for a reason that stopped being true.

**Recording covers both BTC and ETH perpetuals.** Two subscriptions on the
linear stream. Recording is the part that cannot be recovered later, because
Bybit publishes no historical quote data at all: the public archives carry
trades only, so crypto spread history begins when we start recording. BTC is
also the reference asset for the regime and correlation work in phase 4. A
trading exclusion is not a reason to lose the data.

### Funding drag on a 200 USD account

Requested by the director on 2026-08-16. Measured from Bybit's own funding
history, `GET /v5/market/funding/history`, 600 settlements per instrument
covering 199.7 days to 2026-08-16.

| ETH/USDT perpetual, funding rate per 8 hours | Value |
|---|---|
| Mean, signed | 0.001442 percent |
| Mean, absolute | 0.004299 percent |
| Median | 0.001885 percent |
| 95th percentile | 0.009828 percent |
| Worst observed, absolute | 0.020824 percent |
| Settlements where a long pays | 387 of 600 |

**The result does not depend on account size.** Funding is charged on notional
and risk is notional times the stop distance, so

```
funding cost as a fraction of one R  =  funding rate / stop distance
```

The 200 USD account cancels out. What the figure is sensitive to is the stop
distance, because a tighter stop buys more notional per unit of risk, and a
0.5 percent stop therefore doubles the drag of a 1 percent one.

At a 1 percent stop, with one R being the 2.00 USD per-trade risk budget:

| Holding period | Mean rates | 95th percentile rates |
|---|---|---|
| 1 day | 0.43 percent of 1R | 2.95 percent of 1R |
| 3 days | 1.30 percent of 1R | 8.85 percent of 1R |
| 7 days | 3.03 percent of 1R | 20.6 percent of 1R |
| 14 days | 6.06 percent of 1R | 41.3 percent of 1R |

In cash, the position a 200 USD account takes at 1 percent risk and a 1 percent
stop is 0.10 ETH, 188.01 USDT of notional, and funding on it costs 0.008 USDT
per day at mean rates and 0.055 USDT per day at the 95th percentile.

**Conclusion.** Funding is not a material cost for intraday or overnight
holding at this size: a day costs under half a percent of the trade's risk
budget at mean rates. It becomes material somewhere past a week. Against a
hypothetical expectancy of 0.20R per trade, a three day hold costs about 6
percent of the expected profit at mean rates and about 44 percent at 95th
percentile rates, and a seven day hold at 95th percentile rates costs the whole
of it. The 0.20R figure is an assumption for scale only, since no strategy
exists yet to measure; the percentages of 1R above are the measured part.

The recommendation that follows is not a change to the plan: hold periods
beyond roughly five days need funding modelled per position in the cost model
rather than treated as a constant, and the phase 4 backtester must charge
funding on the actual settlement schedule. The funding anchor is already read
from the venue for exactly that reason.

### Open items carried into the rest of phase 2

| Item | Why it matters |
|---|---|
| Dukascopy volume units | The feed does not document them. To be confirmed against a published definition, or cross checked against Pepperstone over an overlapping window with the ratio reported |
| Weekday crypto tick rate | Storage sizing for crypto was deferred to a weekday measurement. A sampling scheme, if one turns out to be warranted, comes with its statistical justification rather than just a rate |

---

## Phases 3 through 9

Not started. See `SPEC.md` section 8 for scope and exit criteria. Do not begin
a phase before the previous phase's exit criteria are all met.

---

## Open questions for the director

| Question | Raised | Resolved |
|---|---|---|
| OANDA live account eligibility for Kenya, needed before phase 8 | Phase 1 | Resolved: OANDA does not accept Kenyan registrations. Venue changed to cTrader via Pepperstone |
| Primary crypto exchange selection, needed before phase 2 | Phase 1 | Resolved: Bybit, inferred from the position model decision. Confirm |
| Instrument universe: which pairs and markets to cover initially | Phase 1 | Resolved: EUR/USD, GBP/USD, USD/JPY, AUD/USD, BTC/USDT, ETH/USDT |
| Base currency for accounting | Phase 1 | Resolved: USD |
| Starting capital, needed to set risk limits in absolute terms | Phase 5 | Resolved: 200 USD, demo funded to match. Risk stays percentage-based, instruments whose minimum exceeds 1 percent risk are excluded and reported |
