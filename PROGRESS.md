# Progress Tracker

Companion to `SPEC.md`. This file is updated as work completes. `SPEC.md` is
not modified except by explicit direction from the director.

**Current phase:** 2, Market data
**Status as of 2026-08-23T09:20Z:** in progress and not complete. CI is green on
`f9e721e`, which is HEAD and is pushed. The recorder is deployed to a DigitalOcean
droplet in Singapore and is running code that predates the two fixes on this branch.
The run that closes the phase needs live forex, which needs reconnection with
resynchronisation, which is not built. Phase 1 is complete and accepted; its record is
kept below unchanged.

**Read the phase 2 "Start here" section before doing anything.** It names the single
next task and the two findings a fresh session would otherwise waste time
rediscovering.

**Decisions taken during implementation live in `docs/DECISIONS.md`.** That is the file
`SPEC.md` section 13 directs a recovering session to, and it was created on 2026-08-16
because the path was referenced and the file did not exist. Rulings are permanent and
this tracker is not, so keeping them here meant rewriting them whenever the tracker was
rewritten.

**Every status claim in this file states the time it describes.** An earlier capture did
not and arrived stale: written mid-run, it recorded CI as red when the next run had
already turned it green, and counted two CI runs when a third existed.

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

Moved to `docs/DECISIONS.md` on 2026-08-16, with their reasoning intact. That file
is the decision log `SPEC.md` section 13 step 3 directs a recovering session to.
Fifteen decisions were recorded for this phase, including why the application
never reads `.env` in any environment, why `refresh_credentials` returns the new
material instead of absorbing it, and why `scripts/verify.sh` is the only
verification path.

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

**Every status claim in this section states the time it describes.** An earlier capture
of this file did not, and arrived stale: it recorded CI as red when the next run had
already turned it green, and counted two CI runs when a third existed. A tracker written
mid-run either dates its claims or misleads the session it was written for.

### Start here, as of 2026-08-23T09:20Z

**Phase 2 is not complete and its exit criteria are not met.** The gate is a 72 hour
continuous ingestion run across both venues. Crypto is live and forex is not, so the run
that closes the phase is blocked on reconnection with resynchronisation rather than on
the spot subscription, which is built.

| | |
|---|---|
| Branch | `phase-2-market-data` at `f9e721e`, pushed. `main` is still the phase 1 commit `49512cf` and phase 2 is not merged into it |
| CI | Green on HEAD. Runs `scripts/verify.sh --fresh --down` on every branch |
| Tests | 1409 unit, 105 integration. `ruff` format and check clean, `mypy --strict` clean over 138 files |
| Host | Deployed at 143.198.222.50, DigitalOcean Singapore. **Running code older than this branch:** the startup deadlock fix and the health assertion are committed and not yet deployed |

**The single next task is reconnection with resynchronisation for the cTrader spot
stream.** Until it exists the assembly deliberately wires no live forex quotes, because a
leg that dies on its first disconnection is worse than an absent one, so the exit
criterion run cannot start.

**Three things a fresh session would otherwise waste time rediscovering:**

1. The development host suspends. Anything measuring elapsed time must use wall clock,
   never a monotonic clock, and must record its own coverage. This has cost three
   separate failures. See "The clock defect class" below before writing anything with a
   timer. The droplet does not suspend, which is why it exists.
2. Forex is not tradeable at 200 USD on the current venue, at any stop distance. That is
   a capital finding rather than a bug, and the arithmetic is in "What the account can
   actually trade" below. Do not try to fix it in code.
3. A crypto-only 72 hour run is continuous operation and **not** the exit criterion. The
   two are different claims and this tracker has conflated three things of that shape
   already. See "Continuous operation is not the exit criterion" below.

### Task status, as of 2026-08-19T00:20Z

**Every row names what constructs the component and what the component constructs or
feeds.** A row that cannot name both has an unbuilt path, and that is the defect this
column exists to surface: three times now a component has been marked Complete while
nothing reached it. `NOTHING` in the constructed-by column is a finding, not a formatting
placeholder.

The assembly referred to below is the runtime wiring that does not yet exist. Almost
every live-ingest component terminates there, so these are one missing root rather than
many missing links.

| Task | Status | Constructed by | Feeds | Notes |
|---|---|---|---|---|
| Schedule-aware gap detection | Complete | `GapMonitor` in `app/gapcheck.py`, constructed by `assemble_ingest` | Logged and audited gap findings | `find_gaps` now has a production caller, closed 2026-08-19. Coverage comes from `MarketDataRepository.tick_coverage`, computed in SQL, and the integration suite runs both implementations over the same stored ticks and asserts they agree |
| Dukascopy `.bi5` reader | Complete | `marketdata/backfill.py` | Backfill runner | Byte for byte round trip against a recorded hour |
| Bybit instrument metadata | Complete | `BybitInstrumentSource` | `RegistrySync` | Mapping functions, now presented as an `InstrumentSource` |
| Bybit public REST client and rate limiter | Complete | `assemble_ingest` | `BybitInstrumentSource` | Envelope checked, 403 as a ten minute block |
| Bybit WebSocket quote stream | Complete | `assemble_ingest` | `QuoteRecorder` | `orderbook.1`, resynchronises, silence treated as death |
| Quote recorder into the tick table | Complete | `IngestProcess`, injected | `MarketDataRepository.store_ticks` | Batched, retried, flushed on shutdown |
| Instrument eligibility screen | Complete | NOTHING | Reporting and phase 5 | Evaluated against live metadata |
| Funding drag measurement | Complete | Measurement, not a component | `SPEC.md` 5.5 | Reported below |
| CI probe diagnosis and repair | Complete | `scripts/verify.sh` | CI | Cause unexplained; probe repaired so a recurrence is diagnosable |
| cTrader adapter: transport | Complete | `scripts/check_venue_assumptions.py`, assembly | Symbols, spots, source | Verified against the real demo account |
| cTrader adapter: symbol metadata | Complete | `venues/ctrader/source.py` | Instrument definitions | Digits, volumes, swap, schedule |
| cTrader spot subscription | Complete | NOTHING | Assembly, then `QuoteRecorder` | Verified live: 146 events, 146 quotes across four pairs. Half-quote path proven by test only, since the venue sent both sides on every event |
| Instrument registry from venue metadata | Complete | `assemble_ingest`, which seeds it before resolving any row id | `InstrumentRepository` | Both sources exist and are tested. The seeding order is what broke on 2026-08-19: assembly resolved row ids before the sync that creates them had ever run, so a fresh database could never populate itself |
| Resumable Dukascopy backfill | Complete | `BackfillJob` | Tick storage | `HttpHourFetcher` has **no tests** and is constructed only by `scripts/crosscheck_release_spread.py` |
| Backfill caller | Complete | NOTHING | Backfill runner and queue | Queues missing open hours from the instrument's own schedule |
| Capital independence, SPEC 6.1 | Complete | `risk/screen.py` | Eligibility verdicts | Property tests over eight orders of magnitude |
| Sizing deliverable | Complete | Measurement, not a component | `SPEC.md` 6.2 | No forex class is eligible at 200 USD |
| Round trip cost measurement | Complete | Measurement, not a component | Phase 3 cost model | Commission dominates |
| Supervised ingest process | Complete | `assemble_ingest`, from `Application.start` | `Supervisor`, recorder, registry, backfill, gap monitor | Four supervised activities: `crypto_quotes`, `instrument_registry`, `dukascopy_backfill`, `gap_detection` |
| Ingest configuration | Complete | `Settings` | `IngestPlan.from_settings` | Deadline against interval, and window against deadline, validated at load |
| Runtime assembly | Complete | `Application.start` in `app/runtime.py` | Everything above | Was the missing root that nearly every live-ingest component terminated at. Built 2026-08-19, tested against a real database before deploying |
| `BybitInstrumentSource` | Complete | `assemble_ingest` | `RegistrySync` | Scope addition found 2026-08-19 by tracing. Built the same day |
| cTrader refresh token call | Not started | Would be `CTraderConnection` | Credential rotation | Access token expires about 30 days from issue |
| Reconnection with resynchronisation | Not started | Would be assembly | Spot stream, `forget()` | `CTraderSpotStream.forget()` exists for it. Forex ingest waits on this rather than shipping a leg that dies on first disconnect |
| Weekday crypto rate capture | **Failed, not rerun** | Operator, on the VPS | Retention sizing, repeat decision | Three attempts lost to the clock defect |
| Crypto retention window | **Blocked** | Decision | Volume sizing | Priced against the volume when the capture lands |
| 72 hour continuous ingestion run | **Blocked** | Operator, on the droplet | Phase 2 exit | Needs live forex, so it needs reconnection with resynchronisation. A crypto-only run is continuous operation and not this. See below |
| Stack health assertion | Complete | `tradingsys-health.timer`, every minute | `OnFailure=`, then Telegram | Containers exist, none stopped after exhausting the restart cap, healthchecks pass |
| Alerting delivery and suppression | Complete | `tradingsys-alert@.service`, from `OnFailure=` on every assertion | Telegram | First occurrence sends, repeats every 30 minutes, recovery on clear. Delivery tested over real HTTP against a local server |
| Alerting canary | Complete | `tradingsys-canary.timer`, daily at 06:00 UTC | Telegram, then the dead man switch | The path cannot verify itself, so its verification is a message arriving. Weekly after the 72 hour run, as a runbook step |
| Absence detection | Complete | `canary.sh`, on successful delivery only | External dead man switch, by email | Off this host and off Telegram, because it has to survive the failures it reports |
| Recording assertion | Complete | `tradingsys-recording.timer`, every five minutes | `OnFailure=`, then Telegram | Tick row age and volume headroom. The SQL is executed by the integration suite against the real schema |
| Tier 3 alert conditions | Deliberately deferred | Application logs | Nothing, until the run completes | Venue failure rates, reconnect churn, gap findings. Thresholds set before a day of data are guesses |
| Instrument deferral | Complete | `assemble_ingest`, derived from the sources it built | A warning, the `ingest assembled` line, and an audit entry | Instruments whose venue nothing can define are deferred rather than assembled. Reverses on its own when a source for that venue exists |
| Ingest counter export | Complete | `assemble_ingest`, run as the `stats_export` activity | Prometheus, and a log line every minute | `StreamStats` and `RecorderStats` were incremented from the day they were written and read by nothing. Fixed 2026-08-23, with the false claim in `docs/DECISIONS.md` corrected in the same change |

### Outstanding work, in the order it should be done

1. **Deploy the current branch to the droplet.** It is running code that predates the
   startup deadlock fix and the health assertion. The sequence is in the runbook under
   "Deploying a new commit" and it is not the old three-line recipe.
2. **Start continuous crypto recording**, which the deploy does by itself. Every hour not
   recording is permanently lost, because Bybit publishes no historical quote data.
3. **Reconnection with resynchronisation**, built once against the subscription state it
   has to restore rather than twice. This is what the exit criterion run waits on.
4. **Tier 2 metrics**, after the run's first day, with thresholds set from a day of
   observed data rather than guessed before there is any.
5. **Re-run the weekday crypto capture** on the fixed script, then size crypto retention
   and decide the snapshot repeat question with the row counts in hand.
6. **The exit criterion run**: 72 hours with both legs live. Report to the director
   before starting it.
7. The cTrader refresh token call, before any deployment that outlives the access token,
   which expires about thirty days from issue.

### What is only ever exercised against a simplified fixture

Surveyed 2026-08-24, when the shipped universe assembly test was added. **This is a list
to work from, not a sweep to perform**, and it is ordered by what a first execution would
cost. The rule it comes from is in `docs/DECISIONS.md`: where a fixture exists for
readability, at least one test runs the real configured artifact.

| What | What is exercised | What is not | Cost of the gap |
|---|---|---|---|
| `deploy/provision/docker-compose.prod.yml` | Nothing. `scripts/verify.sh` and CI bring up `docker-compose.yml` alone | The whole production overlay: the `db_data` bind to the block volume, `restart: on-failure:20`, log rotation, and the Postgres tuning of `shared_buffers`, `max_wal_size` and `maintenance_work_mem` | **Highest.** Every deploy is the first execution of that file, and two of the three host failures sat next to it |
| The provisioning scripts | Syntax, structure, and for the alerting path real delivery over HTTP | None of `bootstrap.sh`, `assert_healthy.sh`, `healthcheck.sh` has ever been **run against a compose stack** by anything but an operator | High. The bootstrap timer defect was invisible for exactly this reason |
| `Application.start` to `assemble_ingest` | Both ends. The assembly has its own integration test; the runtime has a lifecycle test | The edge between them. No test enables a crypto venue in the `test` environment, so `_start_ingest` returns early in every run and the call is checked by the type checker and nothing else | Medium, and it is the two-complete-components shape again |
| Venue catalogue scale | Recorded real responses, so the shape is honest | Scale. Tests resolve 2 symbols out of a 2 entry catalogue; production resolves 2 out of 833 | Low today, listed because it is the same category |
| The `[ingest]` configuration section | The real `config/base.toml`, by every integration test through `live_settings` | The unit suite reads an embedded copy of that section in `tests/config/test_loader.py`. Two definitions of one thing: adding `stats_interval_seconds` required editing both | Low and bounded, but it is a drift mechanism in miniature |
| `HttpHourFetcher` | Nothing | It has no tests at all and is the component that performs every backfill fetch | Deferred with the forex leg, and it re-enters with it |

**The configuration layer is the counter-example and is worth naming as one.**
`tests/config/test_loader.py` loads the real `config/` directory and asserts what the
shipped production configuration actually does, including that it arms nothing. That is
the pattern the rest of this table is missing.

### Continuous operation is not the exit criterion

**Two claims, kept apart deliberately, because this tracker has already conflated three
things of this shape.**

*Continuous operation* starts the moment a deploy verifies. The stack records crypto
under `tradingsys.service`, the assembly wires the ingest process at startup, and two
timers assert that it is still working. Nothing about it needs a decision.

*The exit criterion* is `SPEC.md` section 8: continuous ingestion across **both** venues
for 72 hours. The forex leg contributes backfill history and no live quotes, because
`app/assembly.py` deliberately wires no stream that would die on its first disconnection.
So a crypto-only run of any length does not close phase 2.

Directed by the director on 2026-08-23: run it crypto-only anyway, and do not call it the
exit criterion. Waiting costs data that cannot be recovered, and the run exercises
supervision, alerting, storage growth and the reconnect counters before the run that
counts depends on all four.

### Where the phase 2 code lives

| Concern | Module |
|---|---|
| Gap detection | `src/tradingsys/marketdata/gaps.py`, with `TradingSchedule.open_intervals` in `src/tradingsys/core/schedule.py` |
| Dukascopy reader | `src/tradingsys/marketdata/dukascopy.py` |
| Backfill queue | `src/tradingsys/persistence/backfill.py` |
| Backfill runner and HTTP fetcher | `src/tradingsys/marketdata/backfill.py` |
| Backfill caller | `src/tradingsys/marketdata/backfill_job.py` |
| Quote recorder | `src/tradingsys/marketdata/recorder.py` |
| Instrument registry sync | `src/tradingsys/marketdata/registry.py` |
| Supervision and ingest process | `src/tradingsys/app/supervisor.py`, `src/tradingsys/app/ingest.py` |
| Bybit metadata, REST, stream | `src/tradingsys/venues/bybit/{instruments,rest,book,stream}.py` |
| cTrader transport | `src/tradingsys/venues/ctrader/{framing,connection}.py` |
| cTrader symbols and instruments | `src/tradingsys/venues/ctrader/{symbols,instruments,source}.py` |
| cTrader historical tick data | `src/tradingsys/venues/ctrader/tickdata.py` |
| Vendored cTrader protobuf schema | `src/tradingsys/venues/ctrader/messages/`, provenance in its `__init__.py` |
| Eligibility screen and dynamic instrument screen | `src/tradingsys/risk/{eligibility,screen}.py` |
| Shared request limiter | `src/tradingsys/venues/ratelimit.py` |
| Exact float conversions | `from_binary32` and `decimal_from_double` in `src/tradingsys/core/numeric.py` |

**Scripts that are measurements or controls, not part of the running system:**

| Script | What it does |
|---|---|
| `scripts/verify.sh` | The only verification path. CI runs this same script |
| `scripts/check_venue_assumptions.py` | The manual control for venue drift. Run before each phase closes and before any deployment |
| `scripts/measure_forex_costs.py` | Spread by session and round trip cost as a fraction of risk |
| `scripts/crosscheck_release_spread.py` | Whether an independent feed widens at a release |
| `scripts/measure_crypto_rate.py` | The weekday crypto rate capture |
| `scripts/arm_crypto_capture.sh` | Arms the capture against a wall clock instant |
| `scripts/generate_ctrader_messages.sh` | Regenerates the vendored protobuf modules |

---

### What the account can actually trade, and why

This is the most consequential finding of phase 2 and it is arithmetic rather than
opinion. Decisions are in `docs/DECISIONS.md`; the numbers are here.

**The quantisation tolerance is 5 percent, derived.** Position size rounds down to the
venue grid, so the error is one directional and bounded by one step, and realised risk
lies in `((1 - d) x r, r]`. Safety never binds because nothing exceeds the limit. What
breaks is the truth of the statement: `SPEC.md` section 6 states the ceiling as 1.0
percent, to one decimal place, which asserts [0.95, 1.05], so `d <= 0.05`. It replaced a
10 percent figure that had been written without analysis, and it moved against
convenience, which is the evidence it was derived.

**No forex strategy class is eligible at 200 USD** on the current venue. Measured
2026-08-17 on EUR/USD at 1.15692, 1 percent risk, 5 percent tolerance:

| Stop | Intended size | Verdict on a 1000 unit minimum |
|---|---|---|
| 5 pips | 4000 units | excluded, step is 25 percent of intended |
| 20 pips | 1000 units | excluded, step is 100 percent of intended |
| 50 pips | 400 units | excluded, below the minimum |
| 80 pips | 250 units | excluded, below the minimum |

For the step to sit inside the tolerance the intended size must exceed 10,000 units,
which needs a stop of 2 pips or tighter, which is inside the spread.

**What each class costs in capital**, now `SPEC.md` section 6.2. Each figure checked at
its boundary: eligible at the stated balance, excluded one percent below it.

| Venue minimum and step | Scalp 5p | Intraday 20p | Swing 50p | Macro 80p |
|---|---|---|---|---|
| 1000 units, 0.01 lot, today | 1,000 USD | 4,000 USD | 10,000 USD | 16,000 USD |
| 100 units, 0.001 lot | 100 USD | 400 USD | 1,000 USD | 1,600 USD |
| 10 units, 0.0001 lot | 10 USD | 40 USD | 100 USD | 160 USD |

`balance = step x stop_in_pips x 0.2` for a USD quoted pair. The levers are a smaller
step, a tighter stop, or more capital. Raising either limit is not among them.

**Round trip cost, measured 2026-08-17 from the venue's own tick data and its own
published commission**, for Friday 2026-08-14. Spread is near zero because this is a raw
spread account: EUR/USD median 0.000 pips across every session, GBP/USD and USD/JPY
0.100, widening to 0.2 or 0.3 in the late New York hour. The cost is commission, at 3.00
USD per standard lot per side from `preciseTradingCommissionRate`.

| Stop | EUR/USD median | EUR/USD p95 | GBP/USD | USD/JPY |
|---|---|---|---|---|
| 5 pips | 12.0 percent | 16.0 percent | 14.0 percent | 21.1 percent |
| 10 pips | 6.0 percent | 8.0 percent | 7.0 percent | 10.5 percent |
| 20 pips | 3.0 percent | 4.0 percent | 3.5 percent | 5.3 percent |

**Cost as a fraction of risk is exactly independent of position size and capital**,
verified across four orders of magnitude, because spread, commission and risk all scale
linearly with units and the ratio cancels. So cost depends only on stop distance and
sizing depends only on capital and venue step: **the two constraints are orthogonal**.
Scalping at 5 pips is not viable on this venue; 20 pip stops cost 3.0 to 5.3 percent,
which is in the range this project already accepts for funding.

**Slippage is excluded from every figure above and is unmeasured.** It exists only in
fills, so it is phase 6 or 7 data by construction. At a 20 pip stop one pip of it is 5
percent of risk, comparable to the entire commission cost. Phase 3 states it as an
explicit unmeasured term rather than omitting it, because an omitted term reads as zero.

### The broker search, and why it is over

**Conclusion, reached 2026-08-18: there is no cTrader route to a small enough minimum,
and forex at 200 USD needs either more capital or a second venue adapter.** Do not open
further demo accounts to confirm this.

**RoboForex is out.** Its account opening form offers MetaTrader 4, MetaTrader 5 and R
StocksTrader. There is no cTrader on any account type. Several broker comparison sites
listed it as a cTrader broker and they were simply wrong. The rule that follows is in
`docs/DECISIONS.md`: **platform availability comes from the broker's own account opening
form**, never from a ranking site, and not from broker marketing either, because those
list platforms per broker while availability is per account type, and a cent account is
the type most likely to be excluded.

**Two of the three search filters turned out to be useless.** Spotware states the Open
API "is supported by all trading accounts of any cTrader-affiliated brokers", so
requiring it narrows nothing. And `minVolume` is per symbol broker configuration
published only over the API, so no directory can be filtered on it.

**So it was answered from data.** Every symbol on the Pepperstone catalogue was fetched,
all 1939, and the minimums examined:

| Minimum, units | Symbols |
|---|---|
| 0.01 | 13 |
| 0.1 | 964 |
| 1 | 780 |
| 10 to 100 | 50 |
| 1000 | 115 |
| above 1000 | 17 |

The platform clearly permits small minimums and this broker uses them, but not on
currency pairs: the 0.01 and 0.1 entries are indices, metals and crypto CFDs, where a
unit is a contract rather than a unit of base currency and the figure is not comparable
to FX sizing. **Filtering to instruments whose base and quote are both currencies gives
90 pairs, and every one is minVolume 1000, step 1000, without exception.**

So the 1000 unit floor is not a cTrader platform limit, since the same broker configures
0.01 elsewhere, and it is not a per symbol quirk. It is a uniform FX policy, and 0.01
lots is the universal retail FX convention. **Sub-0.01-lot FX comes from cent accounts,
and cent accounts are an MT4 and MT5 construct**, which is consistent with RoboForex
running its cent accounts there and offering no cTrader at all.

**The three ways forward, none taken:**

| Option | Cost | Buys |
|---|---|---|
| More capital | 4,000 USD for 20 pip intraday, twenty times the current account | Forex on the venue already built, no new code |
| A second venue adapter | FXOpen Micro has the arithmetic at a 10 unit effective step and needs 40 USD, but speaks MT4, MT5 and TickTrader. A whole adapter plus its share of phase 6 | Forex at current capital |
| Crypto only for now | Nothing. ETH/USDT perpetual already clears at 200 USD | Defers the decision; the system is capital independent so forex enters on its own when the balance supports it |

The second is real work and is the director's decision to take deliberately rather than
drift into.

---

### The clock defect class: read this before writing anything with a timer

**This host suspends, and it has now caused three separate failures.** The class is
recorded in `docs/DECISIONS.md`; the operational summary is here because a fresh session
will otherwise repeat it.

**The class.** A check that confirms a process exists proves nothing about whether it
will act, or act at the right time. Existence and correct future action are different
properties, and for anything driven by a timer or a deadline it is the second that
matters.

**The three failures, in order:**

1. The crypto capture was armed by counting a fixed number of seconds from launch, so it
   would have begun at 00:30 rather than 00:00 and put half length buckets at both ends
   of a series that buckets by absolute UTC hour.
2. It was rearmed with `sleep N` computed against a wall clock target. The host suspended
   overnight; the process kept its place in the sleep while the wall clock advanced about
   nine hours, so a capture armed for 23:59:30Z was still sleeping at 06:45Z the next
   morning. Every check said healthy throughout: a live PID, a live sleep, an owned
   session. Only elapsed time against wall clock revealed it.
3. `scripts/arm_crypto_capture.sh` was fixed to poll the wall clock, and started the run
   on time. **But `measure_crypto_rate.py` set its own deadline from the event loop
   clock**, which is monotonic and does not advance during suspend. Measured 24 hours
   later: **9.1 hours of loop time against 24.1 hours of wall clock**, so the host had
   slept about 15 hours and the run needed nearly 15 more hours of loop time to finish.

**The generalisation, which is the part worth carrying.** The second fix was applied
where the defect was found rather than everywhere the defect class applies. A launcher
that starts on time and a run that measures its own duration on a clock that stops are
the same bug in two places, and repairing one **left the class alive while making the
system look repaired**. When a defect class is named, the question is which other code
makes the same assumption, not whether the reported instance is fixed.

**Where this binds next, noted so it is designed rather than rediscovered:**

- *Phase 5 reconciliation* must assert its own recency. A suspended host silently stops
  reconciling while the process stays up and readiness stays green, which is exactly the
  window a divergence would hide in. The check is when the last reconciliation completed
  relative to now, not whether its task is alive.
- *Phase 7 paper run* must measure elapsed wall clock coverage rather than count
  iterations, or a suspension produces a run that believes it covered thirty days and
  covered less, with no unhandled exception marking the gap.

`src/tradingsys/app/supervisor.py` is this made executable for the ingest process: it
reports on progress rather than liveness, and an activity past its deadline is unhealthy
even though its task is alive and its failure count is zero.

### No usable weekday crypto profile exists, and crypto retention is unsized

**Status as of 2026-08-18T07:20Z: three capture attempts, none successful, none rerun.**
The third was stopped after the diagnosis above. Because the script wrote its summary
only on completion, stopping it discarded all nine hours it had collected.

**Crypto retention therefore remains unsized**, and so does the related question of
whether unchanged Bybit snapshot repeats should be stored as rows. Both wait on a
successful capture. The three samples taken on Sunday 2026-08-16 contradict each other by
a factor of four on the instrument ratio and **may not be used to size anything**:

| Sample | BTC quotes/s | ETH quotes/s | ETH as a multiple of BTC |
|---|---|---|---|
| Sunday 2026-08-16, 60 minutes | 6.2 | 14.8 | 2.4 |
| Sunday 2026-08-16, 18 seconds | 7.7 | 5.2 | 0.68 |
| Sunday 2026-08-16, 4 minutes | 8.1 | 4.8 | 0.60 |

ETH at 2.4 times BTC in one hour and 0.6 times BTC twenty minutes later cannot both be a
property of the instruments. Bybit's own 24 hour turnover has BTC ahead of ETH, and BTC's
tick is finer relative to its price, so both point away from the largest figure.

**The script is now fixed** and a rerun should produce a usable result even on a host that
suspends: the deadline is wall clock, coverage is recorded per hour in seconds with rates
computed per covered second, the summary is written every five minutes, and reconnect log
lines carry timestamps. The previous run logged 43 reconnects with no times, of which 23
were DNS resolution failures, meaning the host lost networking rather than the venue
dropping the connection.

To rearm, choosing a weekday:

```bash
setsid nohup scripts/arm_crypto_capture.sh '2026-08-24 00:00:00' >/dev/null 2>&1 &
```

---

### Decisions and findings from the 2026-08-16 to 2026-08-18 session

Full reasoning for each is in `docs/DECISIONS.md`, which is the file `SPEC.md` section 13
directs a recovering session to. It was created during this session because that path was
referenced and did not exist.

| Decision or finding | Where |
|---|---|
| Quantisation tolerance derived at 5 percent, replacing an unanalysed 10 | `docs/DECISIONS.md`, `SPEC.md` 6.1 |
| Capital independence made a requirement, with capital cost per strategy class | `SPEC.md` 6.1 and 6.2 |
| Phases 4a and 4b reordered: trend first, macro second, with what reverses it | `SPEC.md` 5.1 and 8 |
| Trading macro on crypto rejected as a category error | `docs/DECISIONS.md`, rejected options |
| Paper results carry a one directional optimistic bias; the gate needs a stated margin | `SPEC.md` phase 8 gate |
| Venue credentials stay off CI; venue drift is an accepted gap with a manual control | `docs/DECISIONS.md` |
| Broker platform availability comes only from the account opening form | `docs/DECISIONS.md` |
| The clock defect class, and fixing where found rather than where it applies | `docs/DECISIONS.md` |
| The NFP sequence: a conclusion written before its evidence, then corrected by it | `docs/DECISIONS.md` |
| cTrader schema vendored at a pinned commit, generated code committed | `docs/DECISIONS.md` |
| An unknown protobuf enum arrives as an absent field and is refused | `docs/DECISIONS.md` |
| `decimal_from_double` as a second sanctioned float door, distinct from `from_binary32` | `docs/DECISIONS.md` |

**Corrections made during the session, kept because the sequence matters more than the
answer:**

- A stop ceiling was first reported as though it settled viability. It does not: it
  measures only where size falls below the venue minimum and says nothing about
  quantisation. Corrected to the full screen, which excluded every forex class.
- The demo feed was inferred to be unrepresentative because it does not widen at a
  release. A Dukascopy cross-check found the same absence, so the inference was withdrawn
  and the `SPEC.md` entry rewritten. The surviving argument is stronger and was not
  reachable by reasoning from the first result: quote data cannot measure execution cost
  at all, because what degrades at a release is the size executable at the quoted price
  rather than the quote itself.
- Cost and sizing were treated as one constraint. They are orthogonal, because cost as a
  fraction of risk cancels position size. Killing 5 pip scalping therefore moved the
  target rather than closing the question.

### Still open for the director

| Item | What it needs |
|---|---|
| Whether `/health` should register an internal invariant such as a stalled loop detector | A decision. The empty check list is deliberate: liveness must not depend on anything external. The concern that an endpoint asserting nothing gets trusted for more than it checks is not answered by that |
| Unchanged Bybit snapshot repeats stored as rows | The measured row counts from a successful capture, then a decision. Up to 28,800 rows per instrument per day carry no information in a quiet market. The detection rule is exact, since an unchanged repeat reuses the update id |
| Which of the three forex options to take | A decision. A second venue adapter is real work and should be chosen rather than drifted into |
| Retention: volume size versus retention window | **Decided 2026-08-31**, with the host's own compression ratio in hand. Nothing is dropping data meanwhile, because no retention policy exists, so there is no urgency and that is the right position to decide from. Director's stated preference, given in advance: shorten retention rather than buy disk. Analysis below |
| Retention, superseded framing | A full weekday of hourly rates from the recorder, due 2026-08-25. First honest sample is 54.3 quotes per second over 16.5 minutes on a Sunday evening, which is near the 60 per second row and therefore about ten months on 60 GB rather than twenty. To be priced both ways: the volume for 24 months at the measured p95, and the retention that fits comfortably in 60 GB |
| Tier 2 metric thresholds | A day of observed data from the crypto run, then figures. Set earlier they are guesses, and a threshold that fires when nothing is wrong is the one that gets ignored |
| Tier 3 conditions promoted from logs to alerts | The run completing. Venue failure rates, reconnect churn and gap findings log today, by decision |
| The margin above break even that the phase 8 gate requires | A stated figure, set before the paper period begins. It cannot be derived from quote data and needs real fills, so it is a judgement recorded at the gate |
| Dukascopy volume units | A published definition, or a cross check against Pepperstone over an overlapping window with the ratio reported |
| Whether to rerun the crypto capture on this host or somewhere that stays awake | A decision. The fixed script makes a partial capture usable rather than misleading, but a contiguous 24 hours may not be achievable here |

---

### Storage: what is measured, and what the retention decision turns on

**The rate profile, 2026-08-24, from the recorder itself.** Fourteen and a half hours,
partial end hours dropped, no gaps in the hourly sequence.

```
31.0 31.3 32.4 33.7 35.7 38.1 38.5 39.2 40.1 42.2 46.4 49.3 49.4 61.2   per second
```

Mean 40.6, median 38.9, range 31.0 to 61.2, so a diurnal factor of 1.97. p95 is 61.2 by
nearest rank on fourteen samples and 53.5 interpolated; sizing below uses 61.2.

A 24 hour row count taken separately gives 3,357,304 rows, which is 38.9 per second
combined, so the profile is stable rather than a one day artifact. It is one weekday and
still not a week: a volatility event is a regime this has not sampled.

**The row footprint, measured twice, disagreeing by 29 percent.** Reproduce with
`scripts/measure_storage_footprint.py`. Reasoning in `docs/DECISIONS.md`.

| Run | Sample | Uncompressed | Compressed | Ratio |
|---|---|---|---|---|
| 2026-08-18 | 300s live Bybit | 243.81 B/row | 21.90 B/row | 11.1x |
| 2026-08-24 | 180s live Bybit, 12,938 rows | 203.88 B/row | 25.96 B/row | 7.85x |

Roughly half of the uncompressed figure is the primary key index, which is most of why
compression gains so much: compressed chunks do not carry it in the same form.

**The consequences, at the pessimistic end of both runs.** 60 GiB volume, 80 percent
Postgres operating limit, WAL at 4.5 GB, one minute bars at about 0.5 GB over two years.

| At | Retention that fits | Volume needed for 24 months |
|---|---|---|
| Measured day, 3.36M rows | 475 days, 15.6 months | 92 GB, so buy 90 at 9.00 USD/month |
| p95 hour sustained, 5.29M rows | 280 days, 9.2 months | 142 GB, so buy 140 at 14.00 USD/month |

At the optimistic end, 11.1x, those become 18.5 months and 80 GB, and 10.9 months and
122 GB. **The spread between the two measurements is larger than the difference between
the options**, which is why the decision waits for the host's own figure on 2026-08-31.

**What the recorder is doing until then.** Growing at the uncompressed rate, roughly
0.86 GB a day, with the first chunks due to compress at day seven. Until that happens the
70 percent alert is about seven weeks out, and the whole steady state case rests on a job
that has never run here, which is what the compression check now watches.

**The recommendation on the table**, accepted in principle by the director and to be
confirmed with the day eight figures: keep the 60 GB volume, set tick retention to twelve
months, keep the one minute bars indefinitely. The research cost is not material because
retention drops tick resolution and not history: the per-side minute bars are separate
materialised objects and survive, which is pinned by test rather than read from
documentation. What needs ticks specifically is cost model calibration, and old ticks are
the least representative input to it.

### Measurements worth not repeating

**Funding drag on a 200 USD account.** Measured 2026-08-16 from Bybit's own funding
history, 600 settlements per instrument covering 199.7 days. ETH/USDT perpetual, funding
per 8 hours: mean signed 0.001442 percent, mean absolute 0.004299, median 0.001885, 95th
percentile 0.009828, worst observed 0.020824. A long pays in 387 of 600 settlements.

The result does not depend on account size, because funding is charged on notional and
risk is notional times stop distance, so funding cost as a fraction of one R is the
funding rate divided by the stop distance. At a 1 percent stop: one day costs 0.43 percent
of 1R at mean rates and 2.95 percent at the 95th percentile; seven days cost 3.03 and 20.6
percent. This produced `SPEC.md` section 5.5.

**BTC/USDT perpetual is excluded from trading but not from recording.** From Bybit
metadata on 2026-08-16 with BTC at 63,035 and ETH at 1,880 USDT: BTC's 0.001 step is
63.03 USDT of notional and gives three distinct sizes inside a 2.00 USD budget, so
realised risk can sit up to 31 percent from the intended 1 percent. ETH's 0.01 step gives
ten sizes and a 10.6 percent widest affordable stop. Recording continues for both, because
Bybit publishes no historical quote data at all, so crypto spread history begins when we
start recording and cannot be recovered later.

**The venue assumptions check.** `scripts/check_venue_assumptions.py`, last run
2026-08-17: 35 checks, 35 passed. It asserts the handshake, the live flag against the
configured environment, the trader login to ctidTraderAccountId mapping, the account
balance and its `moneyDigits` exponent, the venue heartbeat interval, survival of an idle
period, and the metadata shape of every instrument in the universe. **It must be run
before each phase closes and before any deployment**, because CI cannot catch venue drift.

**Facts about the venue that took work to establish:**

| Fact | Value |
|---|---|
| Account number to venue account id | 5325402 maps to ctidTraderAccountId 48268952. Different numbers; only the venue can supply the mapping |
| Account balance | 200 USD, reported with `moneyDigits` 2 |
| Venue heartbeat interval | 30.0 seconds, measured over a 150 second idle window |
| Read deadline | 95 seconds, sized at two missed venue heartbeats plus margin. It was 20 seconds and would have killed every healthy idle connection |
| Catalogue size | 1939 symbols, 2583 assets |
| FX minimum and step | 1000 units on all 90 currency pairs |
| Commission | 3.00 USD per standard lot per side |
| Trendbar and tick prices | Integers scaled by ten to the fifth, regardless of the symbol's own digits |
| Historical tick data | Delta encoded and newest first, one series per quote type. 92.8 percent of bid and ask ticks share an exact timestamp |

---

### Infrastructure notes

**The recorder is deployed, and the host address is an observation rather than a
setting.** 143.198.222.50, DigitalOcean Singapore, user `tradingsys`, repository at
`/opt/tradingsys`, 60 GB XFS volume at `/mnt/tradingsys_db`. Nothing in this repository
sets that address and no test can check it: a droplet is created by a person in a panel
and the number that comes back is a fact about the world. The first droplet,
`206.189.147.213`, was destroyed and rebuilt because it had been created without pasting
`cloud-init.yaml` and therefore had neither Docker nor the service user. The runbook's
"host, as provisioned" table is the single place this is recorded.

**The deployed host is behind this branch.** The startup deadlock fix at `b9a5ffe`, the
healthcheck export, and the health assertion at `f9e721e` are all committed, pushed, CI
green, and not on the host.

**The repository has a remote.** `https://github.com/alexhaya4/tradingsys`, private,
created 2026-08-16. History was scanned for credentials before the first push, not after:
every value in `.env` was searched across every blob in every commit and every commit
message, plus patterns for GitHub, AWS and Slack tokens and PEM headers. No credential
value appears anywhere in the history. The Pepperstone demo account number appears in
`SPEC.md` deliberately and appeared in two config test fixtures, which now use a
placeholder of the same shape; it is an identifier rather than a credential and the
account is a demo.

**CI runs `scripts/verify.sh --fresh --down` on every branch**, and reads the workflow
from the branch being pushed. It had never executed once before 2026-08-16 while being
counted as coverage, which is recorded as a defect because a check that never runs
produces no red. **CI cannot catch venue drift**, which is an accepted gap with
`scripts/check_venue_assumptions.py` as its manual control.

**Commits are not GPG signed**, deliberately, and `commit.gpgsign` is set to false in the
repository-local git config so the repository matches its own recorded decision rather
than every session passing a flag and rediscovering why.

**The phase branch is not merged into `main`, and merging it is not a way to make CI
fire.** Phase branches merge when the phase completes and its exit criteria are met.

### Defects found this session, with their diagnoses

Recorded because the diagnosis is worth more than the fix.

**1. The rate limiter livelocked, inside a lock.** `RateLimiter.acquire` computed the
missing tokens, slept exactly that long, then re-measured the clock. The refill is
`(now - updated) * rate` in binary floating point, so the recomputed balance can land a
fraction of an ulp below the requested cost, and the next wait is a few nanoseconds, then
smaller, until the loop spins forever holding an `asyncio.Lock`. Two tests written before
the fix did not fail, they hung: a livelock does not report itself. Fixed by reserving
tokens against the instant they will exist rather than re-measuring.

**2. The stream's reconnect catch tuple omitted the only exception that mattered.** It
caught `(VenueConnectivityError, VenueResponseError, OSError, TimeoutError)`, which reads
as comprehensive. The `websockets` library signals a dropped connection with its own type,
which does not inherit from `OSError`, so the recorder would have exited on the first
disconnection, on a market that never closes and has no historical quote source to
backfill from.

**3. A nanosecond epoch divided in a float.** `BybitRestClient.server_time` computed
`nanoseconds / 1_000_000_000` as a float. A double has 53 bits of mantissa and a
nanosecond epoch needs about 61, so low digits were silently discarded. Now integer
`divmod` with the remainder truncated rather than rounded, so the recorded instant never
lands after the instant the venue reported.

**4. CI existed and had never run once.** The workflow triggered only on pushes to `main`,
and `main` has no `.github/` directory, so a push to the phase branch matched no trigger.
Every claim about CI enforcing anything was untrue and nothing revealed it, because a
pipeline that never runs produces no red.

**5. The CI metrics probe, diagnosed and recorded as unexplained.** Run 31939779488 failed
on `/metrics did not expose tradingsys_build_info`. Classification proven: a race, not a
regression, because the same commit passes on re-run and the commits either side changed
markdown only. The original hypothesis, that readiness was lying, was disproven: `/ready`
and `/metrics` are two routes on one app closing over one registry built before the port
is bound, verified by 40 fresh starts, 4000 concurrent requests and 300 pipeline runs.
**The cause is still unexplained and is recorded as such rather than as fixed.** What was
defective is the probe, which conflated a refused connection, an HTTP error, a broken pipe
and a genuinely missing series into one message, and which under `pipefail` could fail
while the series was present. It now reports the byte count and the first twenty lines of
what came back.

**6. The read deadline was shorter than the venue's heartbeat.** 20 seconds against a
measured 30, so every healthy idle connection would have been declared dead and
reconnected in a loop. No unit test could have caught it, because the scripted peer sends
whatever the test tells it to.

**7. The clock defect class**, three instances, described above.

The lesson across all of them: anything asserted about the world outside the repository
has to be observed happening at least once.

## Phases 3 through 9

Not started. See `SPEC.md` section 8 for scope and exit criteria. Do not begin
a phase before the previous phase's exit criteria are all met.

---

## Open questions for the director, historical

**These are all resolved and are kept as a record.** The questions that are actually
open as of 2026-08-18 are in the phase 2 section under "Still open for the director".

| Question | Raised | Resolved |
|---|---|---|
| OANDA live account eligibility for Kenya, needed before phase 8 | Phase 1 | Resolved: OANDA does not accept Kenyan registrations. Venue changed to cTrader via Pepperstone |
| Primary crypto exchange selection, needed before phase 2 | Phase 1 | Resolved: Bybit. Inferred from the position model decision, then confirmed by the director |
| Instrument universe: which pairs and markets to cover initially | Phase 1 | Resolved: EUR/USD, GBP/USD, USD/JPY, AUD/USD, BTC/USDT, ETH/USDT |
| Base currency for accounting | Phase 1 | Resolved: USD |
| Starting capital, needed to set risk limits in absolute terms | Phase 5 | Resolved: 200 USD, demo funded to match. Risk stays percentage-based, instruments whose minimum exceeds 1 percent risk are excluded and reported |
