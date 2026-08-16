# Progress Tracker

Companion to `SPEC.md`. This file is updated as work completes. `SPEC.md` is
not modified except by explicit direction from the director.

**Current phase:** 2, Market data
**Status as of 2026-08-16T17:45Z:** in progress. CI is green on HEAD. Phase 1 is
complete and accepted; its record is kept below unchanged.

**Decisions taken during implementation now live in `docs/DECISIONS.md`.** They
were moved there on 2026-08-16 because `SPEC.md` section 13 already sent a
recovering session to that path and the file did not exist. Rulings are permanent
and this tracker is not, so keeping them here meant rewriting them every time the
tracker was rewritten.

**Every status claim in this file states the time it describes.** The previous
capture did not, and it arrived stale: it was written while a pipeline was still
running, recorded CI as red when the next run had already turned it green, and
counted two CI runs when a third existed. Both claims were false by the time they
were read. A tracker written mid-run either dates its claims or misleads the
session it was written for.

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

**Status:** in progress. Written to be read cold. Everything a session needs to
resume is in this section; nothing depends on remembering a conversation.

### The CI failure of 2026-08-16T09:44Z, diagnosed and recorded as unexplained

**Status as of 2026-08-16T17:45Z: closed as unexplained, not as fixed.** The
probe that reported it has been repaired, and the underlying event has no
established mechanism. Recorded that way deliberately: a tracker that says
"fixed" when nobody found the cause is how the same failure gets misdiagnosed
the next time it appears.

The failure was run 31939779488, commit `aa3e501`:

```
verify: /metrics did not expose tradingsys_build_info
Error: Process completed with exit code 1
```

**Classification: a race, not a regression. Proven, not inferred.**

| Run | Commit | Result |
|---|---|---|
| 31939273660 | `e01e36d` | success, 2m19s |
| 31939779488 | `aa3e501` | **failure**, 1m50s |
| 31941861860 | `7a3617a` | success, 2m24s |
| 31939779488, re-run 2026-08-16T10:44Z | `aa3e501`, unchanged | **success** |

The same commit passes on re-run. The commits either side changed markdown only.

**The original hypothesis was wrong, and this is the part worth carrying.** The
previous capture proposed that readiness was lying: that the app reported ready
before the metric was registered. That cannot happen. `/ready` and `/metrics` are
two routes on one Starlette app built by `build_operational_app`, both closing
over a single `Metrics` object created in `Application.build`
(`src/tradingsys/app/runtime.py:94`), and `build_info.labels(...).set(1)` runs
synchronously inside `Metrics.create` (`metrics.py:120`), before `start()`
connects anything and long before `serve()` binds the port. One call site, one
registry. If the port answers at all, the series is already registered and set.

Tested rather than only read:

| Test | Result |
|---|---|
| 40 app restarts, readiness polled exactly as `verify.sh` does, then the probe | 40/40 exposed the series; ready in 1 to 2s; body 5825 to 5828 bytes |
| 4000 requests at concurrency 60, interleaved with `/ready` evaluations mutating the same registry | 4000 x HTTP 200, zero responses missing the sample line |
| The exact probe pipeline, 300 runs, GNU grep | zero failures |

**What the evidence still does not explain.** curl completed a round trip in
13.4ms, the same as the successful runs, exited zero, printed nothing, and the
match failed. The container logs show one process, one startup, no restart, no
exception. The `pipefail` broken-pipe mechanism described below is real and was
reproduced, but it announces itself with `curl: (23) Failure writing output to
destination`, and that string appears nowhere in the run log, which does capture
stderr. No mechanism in this codebase produces that body, and it did not
reproduce locally in roughly 4400 requests and 40 fresh starts.

**What was actually defective, and is now fixed.** The probe itself, at
`scripts/verify.sh`. It piped curl into `grep -q`, which had two defects: it
reported a refused connection, an HTTP error, a broken pipe, and a genuinely
missing series with one identical message and no trace of what came back, and
under `pipefail` it could fail while the series was present. See
`docs/DECISIONS.md` for the ruling and `TestChecksDoNotDiscardTheirEvidence` in
`tests/test_verification_path.py` for the guard.

The practical consequence: if this recurs, the log will carry the byte count, the
HTTP status or curl's exit code, and the first twenty lines of the body. It will
be diagnosable from the log rather than by re-running the pipeline.

### Still open for the director: what `/health` asserts

Unchanged and not answered by the above. `/health` returned `"checks":[]` on the
failing run while `/ready` reported database and redis passing. The empty list is
intended: see `src/tradingsys/observability/server.py:89`, where liveness returns
an empty report when no liveness checks are registered, and the docstring at line
84 explains why. Liveness must not depend on anything external, or a database
blip restarts a perfectly healthy process. That is the standard split.

The director's concern is not addressed by that: an endpoint that asserts nothing
reports healthy through an outage and gets trusted for more than it checks. What
`/health` currently asserts is real but narrow, namely that the process is
running, the event loop is turning, and the server can accept a connection and
serve a response. What it does not assert is any internal invariant. Whether to
register one, such as a stalled loop detector, is the director's decision and is
to be brought with evidence rather than resolved quietly.

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
| CI probe diagnosis and repair | Complete | Race, not regression, proven by re-run. Cause unexplained; probe repaired so a recurrence is diagnosable from the log |
| cTrader adapter: transport | Complete | TLS, framing, handshake, live flag assertion, heartbeat. Verified against the real demo venue |
| cTrader adapter: symbol metadata | Complete | Digits, pip position, volumes, swap rates and charging convention, trading mode, schedule. Verified against the live catalogue |
| Sizing deliverable, forex half | Complete | Reported below. All four pairs excluded at a 1 percent stop |
| Instrument registry from venue metadata | Not started | Blocked on the cTrader adapter for the forex half |
| Resumable Dukascopy backfill | Not started | Reader and gap detector are done; the runner over `backfill_hours` is not |
| 72 hour continuous ingestion run | Not started | Begins once the adapters and the registry are working, and is reported before it starts |

### Outstanding work, in the order it should be done

1. ~~**Diagnose the CI failure.**~~ Done 2026-08-16. Race not regression, cause
   unexplained, probe repaired. See the section above.
2. **cTrader adapter.** Transport done 2026-08-16, see the section below.
   Remaining: symbol metadata, meaning pip position, digits, minimum volume,
   volume step, swap rates and the charging convention. Rotating refresh tokens
   are modelled in configuration but the refresh call itself is not written yet.
3. **Instrument registry from venue metadata**, then the sizing deliverable the
   director asked for: minimum position size, tick value, and whether 1 percent
   of a 200 USD account produces a viable size, for all six instruments. The
   crypto half is already measured and is in this file. The forex half needs
   the adapter above. Where 1 percent does not reach a viable size, the
   instrument is excluded and the exclusion is reported; the machinery for that
   decision exists in `src/tradingsys/risk/eligibility.py`.
4. **Resumable Dukascopy backfill** over the `backfill_hours` table from
   migration 0002. Concurrency capped at 3, retries with backoff. An hour that
   fails after its retries is recorded as failed and retried later, never
   skipped and never silently treated as complete.
5. **Weekday crypto rate**, then set crypto retention. The capture is scheduled;
   see the section on it below.
6. **72 hour continuous ingestion run.** Report to the director when the
   adapters and the registry work, before starting it.

### Where the phase 2 code lives

| Concern | Module |
|---|---|
| Gap detection | `src/tradingsys/marketdata/gaps.py`, with `TradingSchedule.open_intervals` in `src/tradingsys/core/schedule.py` |
| Dukascopy reader | `src/tradingsys/marketdata/dukascopy.py` |
| Quote recorder | `src/tradingsys/marketdata/recorder.py` |
| Bybit metadata, REST, stream | `src/tradingsys/venues/bybit/{instruments,rest,book,stream}.py` |
| Shared request limiter | `src/tradingsys/venues/ratelimit.py` |
| Eligibility screen | `src/tradingsys/risk/eligibility.py` |
| Exact float32 conversion | `from_binary32` in `src/tradingsys/core/numeric.py` |
| Recorded venue fixtures | `tests/venues/bybit/data/`, `tests/marketdata/data/` |

### Decisions taken during phase 2

Moved to `docs/DECISIONS.md` on 2026-08-16, with their reasoning intact: linear
perpetuals rather than spot, the BTC/USDT trading exclusion, the exclusion being
a configuration threshold rather than a constant, recording both instruments
regardless of the exclusion, USDT not being treated as USD, the `bybit`
configuration key, unsigned commits, the CI trigger, and the verification probe
ruling taken today.

The measurement that produced the BTC exclusion stays here, because it is
evidence rather than a ruling. From Bybit's own metadata on 2026-08-16, with BTC
at 63,035 and ETH at 1,880 USDT:

| Instrument | Quantity step | Notional per step | Risk per step at a 1 percent stop | Distinct sizes within a 2.00 USD budget | Widest stop affordable at minimum size |
|---|---|---|---|---|---|
| ETH/USDT perpetual | 0.01 ETH | 18.80 USDT | 0.188 USDT | 10 | 10.6 percent |
| BTC/USDT perpetual | 0.001 BTC | 63.03 USDT | 0.630 USDT | 3 | 3.17 percent |

With three usable sizes, the realised risk on a BTC trade can sit up to 31
percent away from the 1 percent the risk engine claims to be enforcing.

### The cTrader transport, and what connecting to the venue revealed

**Status as of 2026-08-16T20:00Z: the transport is complete and verified against
the real demo endpoint.** Rulings are in `docs/DECISIONS.md`; what is recorded
here is the evidence and the defect that only a real connection could expose.

| Concern | Module |
|---|---|
| Vendored schema and generated modules | `src/tradingsys/venues/ctrader/messages/`, provenance in its `__init__.py` |
| Regeneration | `scripts/generate_ctrader_messages.sh`, verifies sha256 digests before generating |
| Length prefixed framing and the envelope | `src/tradingsys/venues/ctrader/framing.py` |
| TLS channel, handshake, heartbeat, death | `src/tradingsys/venues/ctrader/connection.py` |
| Hand written venue peer for tests | `tests/venues/ctrader/scripted_venue.py` |

**Observed against demo.ctraderapi.com on 2026-08-16**, not inferred:

| Observation | Value |
|---|---|
| Handshake | Application auth, account list, live flag assertion, account auth, all completed |
| Account number 5325402 maps to ctidTraderAccountId | 48268952 |
| `isLive` for that account | false, matching the practice configuration |
| Venue heartbeat interval | 30.0s, arrivals at t+30.1, 60.1, 90.2, 120.2, 150.1 |
| 150s idle on the shipped 95s deadline | still authenticated, no false death |

**The defect that only connecting could find.** `stream_read_timeout_seconds` was
20 seconds. The venue sends a heartbeat every 30. An idle forex socket carries
nothing else, which is its normal state over a weekend, so the client would have
declared every healthy idle connection dead and reconnected in a loop. No unit
test could have caught it, because the scripted peer sends whatever the test tells
it to. It is now 95 seconds, which is two missed venue heartbeats plus margin, and
`tests/venues/ctrader/test_shipped_configuration.py` reads the shipped
configuration and fails if the deadline is ever brought back under the interval
the venue actually sends at.

This is the second time the same lesson has paid: anything asserted about the
world outside the repository has to be observed happening at least once.

**The refusals are tested, and the tests were checked by breaking the code.** Two
mutations were applied and each was caught by exactly one test: reading an absent
`isLive` as demo, and logging a heartbeat write failure instead of dying. The
first mutation logged `ctrader account authenticated ... environment=practice` for
an account carrying no live flag at all, which is the fail-open the assertion
exists to prevent.

**Not yet done in this adapter.** Symbol metadata, the refresh token call, and
reconnection with resynchronisation. The connection currently reports death and
stops; nothing reconnects it yet. That is deliberate, since reconnection policy
belongs with the subscription state it has to restore.

### Venue drift cannot be caught by CI, and the control for it

**Decided by the director on 2026-08-16: no venue credentials in GitHub Actions.**
The reasoning is in `docs/DECISIONS.md`. What matters operationally is the gap it
leaves and the control that covers it.

**The gap.** If Pepperstone changes a lot size, a swap convention, a symbol name,
or the shape of its metadata, no pipeline in this repository will notice. Every
test here runs against a peer we wrote or a fixture we recorded. CI proves the
client is self consistent; it proves nothing about the venue.

**The control.** `scripts/check_venue_assumptions.py`, run on the host where the
credentials already live:

```bash
set -a && . ./.env && set +a
uv run python scripts/check_venue_assumptions.py
```

It asserts rather than prints, and exits non zero on any failure. It covers the
handshake, the live flag against the configured environment, the trader login to
ctidTraderAccountId mapping still resolving, the venue heartbeat interval being
within bounds of the 30 seconds the read deadline is sized against, the connection
surviving an idle period on the shipped deadline, and the symbol metadata shape
for every instrument in the universe.

**It must be run before each phase closes and before any deployment.** That is the
whole of the control. It is not automated and cannot be, so it belongs in the
phase checklist rather than in a pipeline.

First full run, 2026-08-16: **33 checks, 33 passed, 0 failed.**

### The sizing deliverable

Requested by the director. Measured 2026-08-16 against live venue metadata and
live prices, using `evaluate_eligibility` from `src/tradingsys/risk/eligibility.py`
rather than arithmetic repeated in the report.

Account 200 USD, risk 1 percent, so a budget of **2.00 USD per trade**. The verdict
column is at a 1 percent stop, the same figure the crypto leg was measured at.

| Instrument | Price | Minimum size | Step | Tick | Tick value at minimum | Widest affordable stop | Verdict at a 1 percent stop |
|---|---|---|---|---|---|---|---|
| EUR/USD | 1.15692 | 1000 units | 1000 | 0.00001 | 0.01000 USD | 0.173 percent, 20.0 pips | **Excluded**, needs 172.87 units |
| GBP/USD | 1.35334 | 1000 units | 1000 | 0.00001 | 0.01000 USD | 0.148 percent, 20.0 pips | **Excluded**, needs 147.78 units |
| AUD/USD | 0.70837 | 1000 units | 1000 | 0.00001 | 0.01000 USD | 0.282 percent, 20.0 pips | **Excluded**, needs 282.34 units |
| USD/JPY | 159.326 | 1000 units | 1000 | 0.001 | 1.000 JPY, 0.006276 USD | 0.200 percent, 31.9 pips | **Excluded**, needs 200.00 units |
| ETH/USDT perpetual | 1880 | 0.01 ETH | 0.01 | n/a | n/a | 10.6 percent | **Tradeable**, 10 distinct sizes |

**All four forex pairs are excluded at a 1 percent stop, and the reason is the same
for each.** The minimum position is 1000 units on every pair, and 2.00 USD of risk
spread over 1000 units is 0.002 USD per unit. For any USD quoted pair that is
exactly **20 pips**, whatever the price. A stop wider than that needs a position
smaller than the venue will accept, and `SPEC.md` section 6 says the answer is to
exclude the instrument rather than raise the limit.

USD/JPY works out at 0.3187 JPY, about 32 pips, because its risk is priced in JPY
and converted at 159.326.

**This is not the same finding as the BTC exclusion.** BTC was excluded for
quantisation, meaning too few distinct sizes within the budget. These are excluded
for the minimum itself: the smallest position the venue accepts already risks more
than 2.00 USD at a 1 percent stop.

**What it means, and what it does not.** It does not mean forex is untradeable on
this account. It means any forex strategy here must use a stop at or inside 20 pips
on a USD quoted pair, and inside about 32 pips on USD/JPY. That is a tight but not
unreasonable intraday stop, and it is a strategy design constraint of the same
kind as SPEC 5.5 imposes on the crypto leg. It is the director's call whether to
accept that constraint, trade fewer pairs, or revisit at a larger account. Raising
the risk limit to fit is forbidden by SPEC 6 and is not on the list.

The exclusion re-evaluates on its own: it is arithmetic against live metadata and
live price, so it changes when the account grows or the broker changes a minimum.

### Not a decision yet: unchanged snapshot repeats become tick rows

**Open. Waiting on the measured row counts, and then on the director.**

Every venue message currently becomes a tick row, including unchanged repeats.
This is the present behaviour rather than a decision, and it needs one. Bybit
documents that a level 1 topic repeats its snapshot with the *same* `u` when
nothing has changed for three seconds, and `BookState.apply` emits a quote for
each such message. Storage deduplicates on instrument, source, and timestamp, and
a repeat carries a new timestamp, so it lands as a new row: up to 28,800 rows per
instrument per day carrying no information during a quiet market.

The detection rule is exact and needs no heuristic, since an unchanged repeat
reuses the update id. Suppressing them is not done yet because it interacts with
the retention question that the weekday capture is meant to settle, and because
the argument for keeping them is not empty: a row per three seconds is also
evidence the feed was alive. That evidence already exists in the ping, the receive
deadline, and the recorder counters, so the likely answer is to suppress and rely
on those, but it is the director's call and it should be taken with the measured
row counts in hand rather than before them.

### Defects found this session, with their diagnoses

Recorded because the diagnosis is worth more than the fix. Three were found by
writing tests, and the fourth by pushing to a remote for the first time.

**1. The rate limiter livelocked, inside a lock.** The first implementation of
`RateLimiter.acquire` computed how many tokens were missing, slept for exactly
that long, then re-measured the clock and checked again. The refill is
`(now - updated) * rate` in binary floating point, so the recomputed balance
can land a fraction of an ulp below the requested cost. The next wait is then
a few nanoseconds, and the one after that smaller still, until the delay is too
small to change the clock at all and the loop spins forever holding the
`asyncio.Lock`. Two tests written before the fix did not fail, they hung, which
is why this is worth remembering: a livelock does not report itself. The fix is
to reserve the tokens against the instant they will exist rather than
re-measuring, which makes progress arithmetic rather than hopeful and keeps the
sustained rate exact. `TestProgressUnderFloatingPointRefill` in
`tests/venues/test_ratelimit.py` covers rates whose intervals are not
representable in binary.

**2. The stream's reconnect catch tuple omitted the only exception that
matters.** It caught `(VenueConnectivityError, VenueResponseError, OSError,
TimeoutError)`, which reads as careful and comprehensive. The `websockets`
library signals a dropped connection with its own exception type, which does
not inherit from `OSError`, so the recorder would have exited on the first
disconnection rather than reconnecting, on a market that never closes and has
no historical quote source to backfill from. Found because the test socket
raises a custom exception rather than a real one, which is the case a
handwritten scripted double covers and a mock of the real library would have
hidden. It now catches `Exception`, counts it, and keeps the type and message
in `StreamStats.last_error`. `CancelledError` is a `BaseException` and still
propagates, so shutdown is unaffected.

**3. A nanosecond epoch divided in a float.** `BybitRestClient.server_time`
computed `nanoseconds / 1_000_000_000` as a float. A double has 53 bits of
mantissa and a nanosecond epoch needs about 61, so the low digits were being
discarded silently and the value was wrong by a variable sub-microsecond
amount. Now integer `divmod`, with the remainder truncated to microseconds
rather than rounded, so the recorded instant never lands after the instant the
venue reported. The same class of error is why prices are parsed from the
venue's decimal strings and never from JSON numbers.

**4. CI existed and had never run once.** The worst of the four, because it was
counted as coverage. The workflow triggered on pushes to `main`, and `main` is
still the phase 1 commit, which has no `.github/` directory at all: the
workflow file was added later, on the phase branch. So a push to the branch
where the work happens matched no trigger, and a push to `main` would have
found no workflow to run. From phase 1 until the remote existed, every claim
about CI enforcing anything was untrue, and nothing revealed that, because a
pipeline that never runs produces no red. The filter was wrong in principle
too: phase branches live for days, so a check that fires only at merge time
reports on work finished a week earlier. The trigger is now every branch, and
GitHub reads the workflow from the branch being pushed, so each branch is
checked against its own pipeline. Verified by observing an actual run complete,
not by reading the YAML.

The lesson worth carrying: the first three were found by tests that exercised
the real failure shape, and the fourth was invisible to every test in the
repository because it was a fact about the world outside it. Anything asserted
about infrastructure needs to be observed happening at least once.

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

### The repository has a remote

`https://github.com/alexhaya4/tradingsys`, private, created 2026-08-16. Both
branches pushed: `main` at the phase 1 commit, `phase-2-market-data` carrying
everything since. Verified through the API rather than assumed from the create
command: `private: true`, `visibility: private`, both remote branch heads equal
to the local ones, and `.env` returning 404 on the contents endpoint.

**History was scanned before the first push, not after.** The repository was
initialised after `.env` already existed, so absence had to be checked rather
than assumed. Every value in `.env` was searched for across every blob in every
commit and across every commit message, plus patterns for GitHub tokens, AWS
keys, Slack tokens, and PEM private key headers. Result: no credential value
appears anywhere in the history.

One thing did turn up and is recorded rather than buried. The Pepperstone demo
account number appears in `SPEC.md`, where the director put it deliberately,
and appeared in two config test fixtures, which now use a placeholder of the
same shape. It is an identifier rather than a credential: nothing authenticates
with it, the configuration model classifies `account_id` as non-secret, and the
account is a demo. The push went ahead on that basis. The historical commits
still contain it in the test files, which is only worth rewriting if this
repository ever stops being private.

### CI now runs, and is green

**As of 2026-08-16T17:45Z.** The workflow had never executed once before the
previous session. That is recorded as defect 4 above, with its diagnosis, because
a check counted as coverage that has never run is worse than no check.

It runs on every branch now. Three runs existed at the previous capture, not two,
and the third had already turned CI green before that capture was read:

| Run | Commit | Result |
|---|---|---|
| 31939273660 | `e01e36d` | success |
| 31939779488 | `aa3e501` | failure on the metrics probe, since diagnosed |
| 31941861860 | `7a3617a` | success |

The failure was classified as a race and its probe repaired. See the diagnosis
section at the top of this phase.

**The phase branch is not merged into `main`, and merging it is not a way to make
CI fire.** Phase branches merge when the phase completes and its exit criteria are
met, not to satisfy tooling. `main` therefore stays at the phase 1 commit until
phase 2 is done and accepted.

### Open items carried into the rest of phase 2

| Item | Why it matters |
|---|---|
| Dukascopy volume units | The feed does not document them. To be confirmed against a published definition, or cross checked against Pepperstone over an overlapping window with the ratio reported |
| Weekday crypto tick rate | Capture scheduled, see below. Crypto retention is not set until it lands. A sampling scheme, if one turns out to be warranted, comes with its statistical justification rather than just a rate |
| Unchanged snapshot repeats stored as rows | Described in its own section above. Decide with the measured row counts in hand, not before |
| `/health` asserts no internal invariant | Intended, but the director asked whether it should stay that way. Still open; see the section on it above |

### The weekday crypto capture is scheduled, aligned to the hour

`scripts/measure_crypto_rate.py` counts top of book updates and trades per UTC
hour rather than reporting a single mean, because a busy hour extrapolated to a
day overstates storage and a quiet one understates it, and both look like a
measurement.

**Relaunched aligned on 2026-08-16T17:40Z.** The first arming was started by
elapsed sleep rather than against the clock, so it would have begun at
00:30:17Z and produced two half-length buckets at the ends of the series. The
script buckets by absolute `%Y-%m-%dT%H`, so that would have left Monday hour 00
reading at roughly half its true rate with a second half-hour of it filed under
Tuesday. This measurement sizes retention, which is decided once, and a dataset
with two half-length buckets at its ends is a trap for whoever reads it later.

| Property | Value |
|---|---|
| PID | 34322, own session, detached, cwd is the repository |
| Starts | 2026-08-16T23:59:30Z, thirty seconds early so the socket is streaming before the hour turns |
| Runs for | 24.01 hours |
| Ends | 2026-08-18T00:00:06Z |
| Writes | `/var/tmp/tradingsys/crypto-rate-weekday.json`, log beside it |

**Monday 2026-08-17 hours 00 through 23 are therefore all complete.** The series
also carries two slivers that are to be discarded rather than averaged in: about
30 seconds filed under Sunday hour 23, and about 6 seconds under Tuesday hour 00.
They are obvious at a glance because they are seconds rather than half hours,
which is the point of aligning it this way.

That process does not survive a host restart, since WSL2 stops with it. If the
JSON file is absent after Tuesday, the capture did not run. Relaunching then
means picking the next Monday rather than starting immediately, because the whole
purpose is a weekday profile:

```bash
sleep_for=$(( $(date -u -d '<next Monday> 00:00:00' +%s) - $(date -u +%s) - 30 ))
setsid nohup bash -c "sleep ${sleep_for}; exec uv run python \
  scripts/measure_crypto_rate.py --hours 24.01 \
  --out /var/tmp/tradingsys/crypto-rate-weekday.json" \
  > /var/tmp/tradingsys/crypto-rate-weekday.log 2>&1 &
```

**None of the crypto rate samples taken so far may be used to size retention.**
There are three, all from Sunday 2026-08-16, and they contradict each other by a
factor of four on the instrument ratio. They are recorded to show that the
question is open, not to be averaged, interpolated, or picked from:

| Sample | BTC quotes/s | ETH quotes/s | ETH as a multiple of BTC |
|---|---|---|---|
| Sunday 2026-08-16, 60 minutes | 6.2 | 14.8 | 2.4 |
| Sunday 2026-08-16, 18 seconds | 7.7 | 5.2 | 0.68 |
| Sunday 2026-08-16, 4 minutes | 8.1 | 4.8 | 0.60 |

ETH at 2.4 times BTC in one hour and 0.6 times BTC twenty minutes later cannot
both be a property of the instruments, so at most one of them is, and probably
neither. Two independent facts point away from the largest figure. Bybit's own
24 hour turnover has BTC ahead of ETH, 510M against 367M USDT, so ETH is not
the busier market by value. And BTC's tick is finer relative to its price, 0.16
basis points against 0.53, so BTC's top of book has more distinct prices to
move between, which should produce more updates rather than fewer.

Crypto retention stays unset until the 24 hour hourly breakdown exists. If that
capture shows a rate high enough that sampling has to be considered, the
director wants the scheme and its statistical justification, not a rate.

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
