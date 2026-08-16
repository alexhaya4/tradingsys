# tradingsys: Master Specification and Roadmap

**Status:** Living document. Authoritative.
**Owner:** Alex (director). Implementation delegated to Claude Code sessions.
**Last structural revision:** End of phase 1. Forex venue changed from OANDA to
cTrader; capital, instrument universe, retention, price component, and position
model decisions recorded in sections 3.3, 4.0, and 6.

---

## 0. How to use this document

This file exists so that any new session, human or model, can be brought to
full context by reading it alone. If you are a Claude Code session picking this
project up cold:

1. Read this document end to end before writing any code.
2. Read `PROGRESS.md` in the repo root to find the current phase and the last
   completed task.
3. Do not begin work outside the current phase. Phases are gated deliberately.
4. If a decision is required that this document does not answer, stop and ask
   the director. Do not guess, and do not implement a temporary answer.

When a phase completes, update `PROGRESS.md`, never this file, unless the
director explicitly revises scope.

---

## 1. What is being built

An automated trading system that operates on foreign exchange and
cryptocurrency markets. The system ingests market data and scheduled
macroeconomic events, derives directional signals, sizes positions under
explicit risk constraints, executes orders through broker and exchange APIs,
and manages open positions to defined take-profit and stop-loss outcomes.

This is a real application intended to trade real capital. It is not a
prototype, a demonstration, or a learning exercise.

### 1.1 What it is not

The system is not a high-frequency or latency-arbitrage strategy. It does not
compete on execution speed. It operates on timeframes where a round trip of
tens to hundreds of milliseconds is immaterial.

The system is not a market-making or liquidity-provision strategy.

The system does not use leverage beyond what is explicitly configured and
enforced by the risk engine.

---

## 2. Non-negotiable engineering standards

These apply to every line of code in this repository, in every phase, without
exception.

**Production grade only.** No toy solutions, no placeholder logic, no demo
code, no experimental packages. No mocked behavior standing in for real logic
in shipped code paths. No hardcoded values. No "good enough for now."

**Stop rather than shortcut.** If a correct implementation requires more scope,
more time, or more information than the current phase allows, say so and stop.
Do not ship a degraded version and flag it for later. Later does not come.

**No silent failure.** Every error path is either handled explicitly or
propagated loudly. No bare `except`. No swallowed exceptions. No function that
returns a default value when it could not do its job.

**Decimal for money, always.** Floating point never touches a price, a
quantity, a balance, or a profit-and-loss figure. This is enforced by type
signature and by test.

**No em-dashes** in code, comments, documentation, commit messages, or any
generated artifact.

**Everything is tested.** New code arrives with unit tests that exercise real
behavior, including failure modes. Integration tests run against venue sandbox
environments, not against fabricated response fixtures alone.

**Typed and linted.** `mypy --strict` passes. `ruff` passes. Both run in CI and
both block merge.

**Auditability.** Every decision the system makes, from signal generation to
order submission to position closure, is written to an append-only audit log
with a correlation ID that links the full causal chain. If the system takes an
action that cannot be explained afterward from the audit log, that is a defect.

**Reproducibility.** Dependencies are pinned via lockfile. Builds are
containerized. A backtest run is reproducible from its configuration and a
data snapshot.

---

## 3. Architecture

### 3.1 Process topology

The system runs as separate supervised processes, not as one monolith. Process
isolation is a safety property, not an aesthetic choice: a fault or bug in
strategy code must not be able to compromise risk enforcement.

```
  ingest        market data and news collection, writes to store
  strategy      reads store, emits signal intents
  risk          validates every intent against limits, holds the kill switch
  execution     submits and manages orders, reconciles against venue truth
  monitor       health, metrics, alerting, reconciliation checks
```

Processes communicate through Redis streams with explicit message contracts.
No process reaches into another's internal state.

### 3.2 The risk engine boundary

This is the most important design rule in the project.

The risk engine is the only component permitted to authorize an order. It
maintains its own view of open positions and account balance, sourced by
polling the venue directly, never by trusting what the strategy process
believes. Where the strategy's view and the venue's view disagree, the venue
wins and the discrepancy raises an alert.

The risk engine holds a kill switch that halts all new order submission and,
on command, flattens all open positions. The kill switch is triggerable
manually and automatically. Automatic triggers include daily loss limit
breach, position count anomaly, reconciliation mismatch, stale market data
beyond threshold, and repeated venue API failures.

The risk engine defaults to refusing. If it cannot verify state, it does not
authorize.

### 3.3 Venue abstraction

Two interfaces, both venue neutral:

`MarketDataSource` covers instrument metadata, historical bar retrieval, and
live streaming subscription.

`ExecutionVenue` covers account state, position query, order submission, order
modification, order cancellation, and fill notification.

The abstraction must accommodate the real differences between foreign exchange
and cryptocurrency venues:

| Concern | Forex | Crypto |
|---|---|---|
| Sizing | units or lots, instrument-specific | base asset quantity |
| Precision | pip and pipette, varies by pair | tick size, varies by market |
| Financing | swap, charged at rollover | funding rate, perpetuals only |
| Hours | sessions, weekend closure, holidays | continuous |
| Leverage | broker-set, jurisdiction-capped | venue and product dependent |
| Settlement | none, margin positions | spot settles, perpetuals do not |

No venue-specific field name, identifier format, or enum value appears in the
base interfaces. Implementation targets are cTrader Open API for foreign
exchange and ccxt with a native WebSocket client for cryptocurrency.

The abstraction must also accommodate credentials that expire. A static key and
secret does not; an OAuth2 credential set does, and refreshing one can rotate
the refresh token itself. Expiry and renewal are therefore part of the venue
interface, and a renewal returns the new material to its caller rather than
absorbing it, because the replacement has to be persisted before the process
next restarts.

**Forex venue: cTrader Open API via Pepperstone.** Decided during phase 1,
before any adapter was written.

OANDA is out. It does not accept registrations from Kenya, so it was never
available, and discovering that after building an adapter would have been
expensive. The account is a Pepperstone demo, number 5325402, Razor account
type, denominated in USD, with broker leverage of 1:400. That leverage figure
is what the broker permits, not what this system uses: the risk engine enforces
its own cap far below it, and the broker number appears here only so that
nobody mistakes the venue limit for the system limit.

This change is the venue abstraction earning its keep on day two. cTrader
differs from OANDA in transport, authentication, sizing convention, and
position model, and the change required no alteration to `MarketDataSource` or
`ExecutionVenue`. The interfaces stay free of any single venue's assumptions,
and this is the standard they are held to: if a venue change forces an
interface change, the interface was wrong.

### 3.4 Technology decisions

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12, asyncio | Latency is not the edge. Ecosystem is. |
| Package manager | uv with lockfile | Fast, deterministic, standards-based. |
| Config | pydantic-settings, TOML plus env | Typed, validated, secrets separated. |
| Database | PostgreSQL 16 with TimescaleDB | Time-series plus relational in one engine. |
| DB driver | asyncpg | Async, no ORM overhead in hot paths. |
| Migrations | Alembic | Versioned, reversible schema. |
| State and IPC | Redis 7 | Streams, position cache, idempotency keys. |
| Crypto venue | Bybit, through ccxt plus native WebSocket | Unified REST, direct stream for latency. Supports both position models. |
| Forex venue | cTrader Open API, via Pepperstone | Available from Kenya, which OANDA is not. Demo and live share one API shape. |
| Logging | structlog, JSON | Machine parseable, correlation IDs. |
| Metrics | Prometheus plus Grafana | Standard, self-hosted, no vendor lock. |
| Testing | pytest, pytest-asyncio, hypothesis | Property tests for money and sizing math. |
| Deployment | Docker Compose, systemd supervision | Reproducible, restartable, simple. |

Rejected: OANDA v20, which does not accept registrations from Kenya. Rejected:
Rust for the core, because it buys latency the strategy does not
need at the cost of the entire data and language-processing ecosystem. Rejected:
any hosted backtesting framework, because opaque cost modeling invalidates the
go/no-go decision that the whole project depends on.

---

## 4. Data model

Core entities. Full DDL lives in Alembic migrations; this is the conceptual
map.

### 4.0 Scope, capital, and storage policy

Decided during phase 1.

**Instrument universe.** EUR/USD, GBP/USD, USD/JPY, and AUD/USD on forex;
BTC/USDT and ETH/USDT on crypto. Base accounting currency is USD.

**Capital: 200 USD, as configuration rather than as an assumption.** The demo
account is funded to match the intended live capital deliberately, so that paper
results at the phase 8 gate are comparable to what live trading would have
produced rather than flattering it. Risk limits stay percentage-based, never
absolute.

The figure above is the current intent, not a constant the system is built
around. See section 6.1: the system accepts any account size, and 200 USD is
what this deployment happens to be funded with.

Small capital interacts with venue minimums, and the interaction is a
correctness problem rather than an inconvenience. Before an instrument is
traded, the system validates that 1 percent per-trade risk yields a position
size at or above that instrument's minimum, at its current price and step size.
Where it does not, **the instrument is excluded and the exclusion is reported.**
Raising the risk limit to make an instrument tradeable is forbidden: that
inverts the relationship between risk policy and venue constraints, and it is
how an account gets sized by what the broker will accept rather than by what
the strategy can afford to lose.

**Retention.** Ticks are compressed after 7 days and retained for 24 months.
One minute bars are never dropped. Higher timeframes are derived on read
through continuous aggregates rather than stored as separate raw series, so
there is exactly one authoritative copy of any given observation and no
possibility of two timeframes disagreeing. Both windows are configuration
values, not constants.

**Price components.** Bid and ask candles are stored as separate series. Mid is
computed on read, is used only for signal generation, and is never stored as
authoritative and never used for fill simulation. Fills use the side crossed:
a buy fills at the ask, a sell fills at the bid. Backtesting on mid while
executing on ask is a systematic bias in the direction of flattering results,
which is precisely the error the phase 8 gate exists to catch.

**Position model.** Both netting and hedging are modelled at the venue
abstraction, because cTrader is natively hedging and Bybit supports both. The
system's own policy is netting: at most one open position per instrument,
enforced by the risk engine regardless of what the venue permits. A venue that
allows two opposing positions in one instrument allows an accounting state the
risk engine cannot reason about, so the constraint is imposed above the venue
rather than inherited from it.

**instrument** Canonical instrument registry. Venue-neutral symbol, venue
symbol mapping, asset class, quote currency, price precision, minimum size,
size increment, financing model.

**ohlcv** Hypertable. Instrument, venue, timeframe, price component, open time,
OHLC as Decimal, volume, and a completeness flag. Partitioned by time. Bid and
ask are separate series; see section 4.0.

**tick** Hypertable. Instrument, venue, timestamp, bid, ask, bid size, ask
size. Retained at higher resolution for a shorter window than bars.

**macro_event** Scheduled economic releases. Currency affected, event name,
scheduled time, importance, consensus forecast, previous value, actual value
once released, revision flag.

**news_item** Unstructured headlines. Source, published time, ingested time,
headline, body, content hash for deduplication, resolved instrument links.

**signal** Every directional intent the strategy produces, whether or not it
became an order. Instrument, direction, conviction, generating strategy
version, input snapshot reference, timestamp.

**risk_decision** Every authorization or refusal by the risk engine, with the
specific limit evaluated and the outcome.

**order** Submitted orders with venue order ID, idempotency key, requested
parameters, and lifecycle state transitions.

**fill** Executions against orders, with actual price, quantity, fee, and
venue timestamp.

**position** Open and historical positions, entry, exit, size, realized and
unrealized profit and loss, financing charges accrued.

**audit_log** Append-only. Correlation ID, process, event type, structured
payload. Never updated, never deleted.

Referential rule: a fill traces to an order, an order traces to a risk
decision, a risk decision traces to a signal, a signal traces to the market
data and events that produced it. This chain must be queryable.

---

## 5. Strategy and signal design

### 5.1 Sequencing rationale

Scheduled macroeconomic events are built before unstructured news, for a
concrete reason: they are timestamped in advance, they have a consensus
forecast against which the actual release can be measured as a surprise, and
their price impact on foreign exchange is well documented and measurable. They
give a testable signal. Headlines give a research problem.

### 5.2 Macro event signals

Signal derives from the surprise, meaning the deviation of the actual release
from consensus, normalized by the historical distribution of surprises for
that event type. Direction is mapped per event category and per currency, and
that mapping is derived from historical data in the backtest, not asserted.

Event handling must account for scheduled release blackout windows: the system
does not hold positions into high-importance releases unless the strategy
explicitly trades the release, and it does not open positions during the
spread-widening window immediately following one.

### 5.3 Trend signals

Standard technical trend estimation on multiple timeframes. Specific indicator
selection is deferred to the backtest phase and must be chosen by out-of-sample
performance, not by preference. Any indicator set adopted must survive
walk-forward validation.

### 5.4 News signals

Deferred until phase 4b and explicitly optional. If headline processing cannot
be shown to add out-of-sample edge over the macro and trend layers, it does not
ship. Sentiment scoring that looks plausible but does not improve the equity
curve is decoration.

### 5.5 Holding period constraint on the crypto leg

Perpetual funding is charged every eight hours on notional, and risk is
notional times the stop distance, so funding cost as a fraction of the
per-trade risk budget is the funding rate divided by the stop distance. That
ratio does not depend on account size. Measured on Bybit ETH/USDT over 600
settlements spanning 199.7 days to 2026-08-16: at a 1 percent stop, one day of
holding costs 0.43 percent of one R at mean rates and 2.95 percent at 95th
percentile rates; seven days costs 3.0 percent and 20.6 percent respectively.

Two consequences bind strategy design and are not advisory:

- **Every crypto strategy declares a maximum holding period, and the funding
  drag implied by it must be a stated and bounded fraction of that strategy's
  expected edge.** A strategy whose expected edge is not stated cannot satisfy
  this, which is the intent: the cost is knowable in advance and must be
  budgeted for in advance.
- **Tight stops and long holds are incompatible on perpetuals.** Halving the
  stop distance doubles the notional carried per unit of risk and doubles the
  funding drag with it. A design that pairs a sub-1 percent stop with a
  multi-day hold is rejected at design time rather than discovered in the
  backtest.

Phase 3 reports funding cost as a first-class result line for every crypto
backtest, beside net return and drawdown, never as a footnote or an aggregate
buried in total costs. Results must be reported both gross and net of funding
so the size of the effect is visible rather than absorbed. **If a crypto
strategy is profitable only when funding is ignored, it is not profitable.**

The forex leg is unaffected: swap is charged at rollover on a different basis
and is covered by the cost model in section 7.

### 5.6 Overfitting controls

Mandatory, not advisory:

- Walk-forward analysis with strictly out-of-sample test windows.
- Parameter count declared and justified; parameter sweeps logged in full,
  including the discarded configurations.
- No test-set iteration. Once a configuration touches the final holdout, that
  holdout is burned.
- Multiple-testing correction applied when comparing many configurations.
- Results reported with confidence intervals, never as point estimates.

---

## 6. Risk framework

Every constraint below is configured, enforced by the risk engine process, and
tested.

### 6.1 Capital independence

**The system accepts any account size.** There is no threshold below which it
stops working, no assumed floor, and no capital figure written into code. Capital
is configuration, and every quantity that depends on it is derived at runtime.

This is a correctness requirement, not an aspiration, and it decomposes into four
rules that are individually testable:

**Balance comes from the venue, not from configuration.** Eligibility is evaluated
against the account balance the venue reports, not against a configured constant.
A configured figure is a statement of intent; the venue's figure is the fact, and
where they disagree the venue wins, as everywhere else in section 3.2.

**The tradeable instrument set is dynamic.** Because eligibility is arithmetic
against live balance, live price, and live venue metadata, instruments enter and
leave the tradeable set as any of those change. An instrument excluded at one
balance and eligible at another must be handled without a code change and without
a restart. Entry and exit are logged and audited, because a silently changing
instrument universe is indistinguishable from a bug.

**Every risk limit stays percentage-based.** No absolute currency amount appears
as a constant anywhere in the risk engine. A limit expressed in currency is a
limit that is wrong at every account size except the one it was written for, and
it fails silently rather than loudly when the account changes.

**The strategy implication is reported, not just the verdict.** An exclusion list
says which instruments cannot be traded. It does not say what the account can
still do, and that is the question an operator actually has. For a given balance
the system reports the widest affordable stop per instrument, which is the real
constraint: a stop ceiling below what a macro event routinely moves rules out an
entire strategy class, and that has to be stated rather than inferred from an
absence.

Correctness evidence transfers across account sizes. Performance evidence does
not. See the phase 8 gate.

**Per-trade risk.** A fixed fraction of account equity at risk per position,
determined by stop distance, not by a fixed lot size. Default ceiling: 1.0
percent. Hard cap: 2.0 percent. Where 1 percent does not reach an instrument's
minimum size, that instrument is excluded and the exclusion is reported. The
limit is never raised to fit a venue minimum.

**One position per instrument.** Enforced by the risk engine as system policy,
independent of whether the venue is netting or hedging.

**Stop loss.** Mandatory on every position. Placed as a venue-side order at
submission time, not maintained only in local memory, so that a process crash
cannot leave a position unprotected.

**Take profit.** Defined per strategy as a multiple of stop distance. Minimum
acceptable reward-to-risk ratio is declared in configuration and enforced at
authorization time.

**Daily loss limit.** Cumulative realized plus unrealized loss threshold that
halts all new entries for the session and alerts.

**Maximum drawdown limit.** Peak-to-trough equity threshold that triggers full
shutdown and requires manual re-enable.

**Correlated exposure cap.** Foreign exchange pairs sharing a currency are
correlated. Aggregate exposure per currency is capped, not just per pair.

**Position count cap.** Absolute maximum concurrent open positions.

**Leverage cap.** Enforced independently of whatever the venue permits.

**Stale data guard.** If market data for an instrument exceeds a staleness
threshold, no new positions in that instrument, and existing positions are
flagged.

**Reconciliation.** Position and balance state reconciled against the venue on
a fixed interval. Any mismatch halts trading and alerts immediately.

---

## 7. Execution requirements

**Idempotency.** Every order carries a client-generated idempotency key. A
retry after a network failure must never produce a duplicate position. This is
tested by fault injection, not assumed.

**Order state machine.** Explicit states with legal transitions. Unknown venue
responses move the order to an indeterminate state that triggers reconciliation,
never to an assumed success or failure.

**Partial fills.** Handled as a first-class case in position accounting.

**Rate limiting.** Client-side budget per venue, respecting published limits
with margin. Exceeding a venue rate limit is a defect, not an accident.

**Reconnection.** Streaming connections reconnect with exponential backoff and
jitter, and on reconnect they resynchronize state rather than assuming
continuity.

**Cost accounting.** Spread, commission, slippage, swap, and funding are
recorded per position and included in all performance reporting. Gross
performance figures are not reported without net figures alongside them.

---

## 8. Phase plan

Each phase has explicit exit criteria. A phase is not complete until every
criterion is demonstrably met. Do not begin the next phase early.

### Phase 1: Foundation
Project structure, configuration system, venue abstractions, core domain types
including Money and Instrument, database schema and migrations, structured
logging, metrics and health endpoints, local Docker Compose environment.

*Exit criteria:* `mypy --strict` and `ruff` clean. Test suite green. Compose
stack starts and all health checks pass. Configuration fails loudly on missing
required values, verified by test. No adapter implementations present.

### Phase 2: Market data
Live and historical ingestion for both venues. Gap detection and backfill.
Instrument registry populated from venue metadata. Storage validated for
precision and completeness.

*Exit criteria:* Continuous ingestion sustained for 72 hours with no data loss.
Gap detection proven by deliberate disconnection. Stored values verified
bit-exact against venue-reported values for a sampled set. Reconnection tested
under forced network failure.

### Phase 3: Backtest engine
Event-driven simulation with realistic cost modeling: spread from recorded
bid-ask, slippage model calibrated against observed fills, commission, swap and
funding. Walk-forward harness. Performance and risk metrics reporting.

*Exit criteria:* Engine reproduces a known trade sequence exactly. Cost model
validated against real historical spreads. Funding charged per settlement on
the venue's own schedule rather than approximated, and reported as its own
result line for every crypto run, gross and net, per section 5.5. Look-ahead
bias tested for explicitly, including a deliberate look-ahead injection that
the harness must detect. Reports include confidence intervals.

### Phase 4a: Macro event signals
Economic calendar ingestion, surprise computation, event-to-direction mapping
derived from history, blackout window logic.

*Exit criteria:* Calendar coverage verified against an independent source.
Surprise calculation validated on historical releases. Out-of-sample results
reported honestly, including if the edge is absent.

### Phase 4b: Trend signals and optional news layer
Trend estimation, indicator selection by out-of-sample performance. Headline
ingestion and scoring only if it demonstrably adds edge.

*Exit criteria:* Walk-forward results with parameter counts declared. News
layer ships only if it improves out-of-sample net performance; otherwise it is
cut and that decision is recorded.

### Phase 5: Risk engine and position management
Full risk framework as specified in section 6. Kill switch. Reconciliation
loop. Position lifecycle management.

*Exit criteria:* Every limit in section 6 has a test proving it blocks the
action it is meant to block. Kill switch tested under live paper conditions.
Reconciliation mismatch tested by deliberate divergence injection.

### Phase 6: Execution layer
Venue adapters, order state machine, idempotency, partial fill handling, rate
limiting, reconnection.

*Exit criteria:* Duplicate order prevention proven by fault injection.
Adapters tested against both venues' sandbox environments. Rate limit
compliance verified under load.

### Phase 7: Paper trading
Full pipeline running against live market data with simulated execution, for a
minimum of thirty consecutive calendar days covering varied market conditions.

*Exit criteria:* Thirty days completed with no unhandled exceptions, no
reconciliation failures, and no risk limit breaches. Complete performance
report produced with net-of-cost figures.

### Phase 8: Go/no-go gate

**This gate is real and it can end the project.**

The decision to deploy capital is made here, on evidence, by the director. The
criteria are set before the paper trading period begins, not after, so that
they cannot be adjusted to fit the result.

**Paper capital must match intended live capital at the time of the gate.**
Correctness evidence transfers across account sizes and performance evidence does
not: the same code sizing the same signal on a different balance produces a
different instrument universe, different quantisation, and a different cost
ratio, so a paper result at one capital says nothing reliable about live results
at another. Current intent is 200 USD and the demo is funded to match. If that
intent changes before phase 7, the demo is refunded to match **before** the paper
period begins, not after, because a period that begins at the wrong capital
cannot be repaired retrospectively and has to be rerun.

Deployment requires all of the following:
- Paper capital equal to intended live capital for the whole paper period.
- Net-of-cost profitability over the paper period.
- Maximum drawdown within the configured tolerance.
- Live paper results consistent with backtested expectations for the same
  window. Large divergence means the backtest is wrong, and a wrong backtest
  invalidates the entire evidence base.
- Zero unresolved defects in risk enforcement or execution.
- Operational stability demonstrated: no unexplained restarts, no data gaps.

If the criteria are not met, the outcome is redesign or termination. Extending
the paper period to search for a favorable window is not permitted, because
that is data mining the go decision itself.

### Phase 9: Live deployment
Minimum viable capital. Production infrastructure with monitoring and alerting.
Documented runbook covering start, stop, kill switch, reconciliation failure,
and venue outage. Capital scaling only against demonstrated live performance
over a defined period.

*Exit criteria:* Runbook validated by executing every procedure in it. Alerting
verified by triggering each alert condition. Live results tracked against paper
expectations with an explicit stop-down rule if they diverge.

---

## 9. Security

Credentials live in environment variables or a secret manager. Never in files,
never in the repository, never in logs. Secret scanning runs in CI.

API keys are scoped to the minimum permission set. Withdrawal permissions are
never granted to a trading key.

Production database access is restricted. The application role has no schema
modification rights.

The audit log is append-only at the database permission level, not merely by
convention.

Dependency vulnerability scanning runs in CI and blocks on high severity
findings.

Server access is key-based only. The trading host runs nothing but this system.

---

## 10. Operations

**Monitoring.** Prometheus scrapes every process. Grafana dashboards cover
system health, data freshness, position state, and performance. Alerts route to
a channel the director actually reads.

**Alert conditions.** Process down, data staleness, reconciliation mismatch,
risk limit breach, venue API failure rate, daily loss threshold approach,
unhandled exception.

**Backups.** Database backed up on a schedule, with restore tested. An untested
backup is not a backup.

**Deployment.** Versioned releases. Rollback procedure documented and tested.
Configuration changes are version controlled and reviewed.

**Runbook.** Lives at `docs/RUNBOOK.md`. Covers every operational procedure and
every alert response. Written before live deployment, not after the first
incident.

---

## 11. Regulatory and jurisdictional constraints

Broker account eligibility must be confirmed for the director's jurisdiction
before phase 8. Practice environments are generally open, so phases 1 through 7
proceed regardless, but the venue choice must be validated before capital is
committed. If the reference broker is unavailable, the venue abstraction
absorbs the substitution without architectural change. This is a stated reason
for the abstraction existing.

Tax treatment of trading profits is the director's responsibility and outside
system scope, but the audit log must contain sufficient detail to support
whatever reporting is required.

---

## 12. Definition of done, per task

A task is complete when all of the following hold:

- Implementation is real, with no placeholder, stub, or mocked logic in
  production paths.
- Unit tests cover behavior and failure modes, and they pass.
- `mypy --strict` passes. `ruff` passes.
- Errors are handled explicitly or propagated deliberately.
- Money and quantity values use Decimal throughout.
- Decisions and actions are written to the audit log with correlation IDs.
- Configuration is externalized, with no hardcoded values.
- Documentation reflects the change.
- No em-dashes anywhere in the output.

If any of these cannot be satisfied within the current scope, stop and report
it. Do not ship the task partially satisfied.

---

## 13. Session recovery protocol

When a session is lost or begins degrading:

1. Read this document in full.
2. Read `PROGRESS.md` for the current phase and last completed task.
3. Read `docs/DECISIONS.md` for the log of decisions made and their reasons.
4. Run the test suite to establish the actual state of the code, which is more
   reliable than any written claim about it.
5. Resume at the next incomplete task within the current phase.

Do not restructure completed work, do not "improve" code outside the current
task, and do not skip forward to a later phase because it seems more
interesting. If the plan appears wrong, raise it with the director rather than
unilaterally changing course.
