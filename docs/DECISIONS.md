# Decision Log

Companion to `SPEC.md` and `PROGRESS.md`. This is the file `SPEC.md` section 13
step 3 sends a recovering session to.

**What lives here.** Every decision taken during implementation that is not
already stated in `SPEC.md`, with the reasoning that produced it. Decisions are
append-only: a decision that is later reversed gets a new entry recording the
reversal and why, and the original stays where it is. Nothing here is edited to
match a later opinion.

**What does not live here.** Decisions the director took as specification
belong in `SPEC.md`, which is authoritative for them, and are listed at the
bottom of this file as pointers rather than copied. Current state, task status,
and measurements belong in `PROGRESS.md`. There is exactly one authoritative
copy of anything, because two copies eventually disagree and then neither can
be trusted.

**Why this file is separate from `PROGRESS.md`.** Decisions are permanent and
progress is not. Keeping rulings inside a tracker that is rewritten every phase
means they get rewritten with it, and it means a session picking the project up
cold has to read a long mutable document to find the fixed points. Created
2026-08-16, when the recovery protocol was found to reference a file that did
not exist.

---

## Phase 1: Foundation

### The package is named `tradingsys`

The repository directory name is incidental and can change with a checkout. The
package name appears in every import in the project and is expensive to change
later.

### The venue interfaces carry no venue vocabulary

No venue-specific field name, identifier format, or enum value appears in
`MarketDataSource` or `ExecutionVenue`.

This was proven rather than asserted: the forex venue changed from OANDA to
cTrader in the middle of phase 1, and neither interface changed. cTrader differs
from OANDA in transport, authentication, sizing convention, and position model,
so if the abstraction were going to leak it would have leaked there. The standard
this sets is in `SPEC.md` section 3.3: if a venue change forces an interface
change, the interface was wrong.

### The forex venue is cTrader Open API via Pepperstone

OANDA does not accept registrations from Kenya, so it was never available.
Discovering that after building an adapter would have been expensive. Recorded in
full in `SPEC.md` section 3.3.

### OAuth credentials are four flat secret fields, not a nested section

`client_id`, `client_secret`, `access_token`, and `refresh_token` sit directly on
`venues.forex` rather than under a `credentials` sub-section.

A validation failure can then name each field that is absent. Being told
"credentials are required" five times while supplying them one at a time is a
miserable way to configure a venue, and the configuration system's whole purpose
is to fail in a way that tells the operator what to do next.

### Credential expiry and refresh live on `VenueConnection`

`credentials_expire_at`, `refresh_credentials()`, and a `credentials_expire`
capability flag, so callers ask rather than assume.

This is venue neutral rather than a cTrader detail. A static key and secret
reports no expiry and raises `UnsupportedVenueOperationError` on refresh. The
driver is that an OAuth access token expires after about thirty days, so a
process that reads credentials only at startup authenticates perfectly for a
month and then stops. That has to be visible to a supervisor rather than buried
inside an adapter.

### `refresh_credentials` returns the new material instead of absorbing it

cTrader rotates the refresh token when it is used. An adapter that updated itself
in memory would work until the first restart after a rotation and then fail
authentication with no obvious cause, and a dead refresh token needs manual
reauthorisation. The replacement has to be persisted before the process next
restarts, so it is returned to a caller that can persist it.

`token_refresh_margin_seconds` defaults to three days for the same reason:
renewing at expiry leaves no room to retry a failed renewal.

### The audit digest is pinned to a golden value in a test

Every other hash test compared one output of `compute_entry_hash` to another, so
the suite verified that the chain was self-consistent but never that the
algorithm was what it had been. Reordering the hashed fields, changing the
separator, or dropping the timestamp would have left every test green while
silently invalidating every digest already written to the database.

A literal expected digest means a change to the canonical form now fails a test
and forces the question of whether stored logs need rehashing.

### Audit appends are serialised by a transaction level advisory lock, and `previous_hash` is UNIQUE

Two concurrent writers would otherwise fork the hash chain. The lock prevents it
in normal operation and the unique constraint makes it impossible at the storage
layer, so the guarantee does not depend on every future writer remembering to
take the lock.

### TOML for the configuration file layers

Unambiguous typing, and consistent with `pyproject.toml`, which the project
already has.

### `venue_symbol` is stored beside the canonical symbol

`SPEC.md` section 4 requires venue symbol mapping, and the transformation between
a canonical symbol and a venue's own is not derivable by rule.

### Live venues require both `app.allow_live_trading` and the production environment

Copying a production configuration to a developer machine cannot then arm real
orders. Two independent conditions rather than one, because the failure mode
being guarded against is exactly the one where a single flag is set by accident.

### Integration tests fail rather than skip

A suite that skips its only storage coverage when the database is absent reports
green while testing nothing. That is the same defect class as a CI pipeline that
never runs: a check counted as coverage that is not checking anything.

### The application never reads `.env`, in any environment

`.env` is read by docker compose and by `scripts/verify.sh`. The application has
no dotenv source at all, in development, test, staging, or production.

The rule is that the application reads secrets only from the environment or the
secrets directory, never from a file it parses, and it is valuable precisely
because it has no exceptions. The tempting alternative, permitting `.env` in
development and test and refusing it in staging and production, makes the
guarantee conditional on the environment selector itself being correct, which
adds a failure mode on the day it matters most. The invariant stays absolute and
the cost is paid in documentation: one `set -a` line, which `scripts/verify.sh`
runs for you.

### `scripts/verify.sh` is the only verification path, invoked by both the README and CI

A transcribed command sequence drifts from the one that actually runs. That is
not hypothetical here: a README recipe that omitted a required password produced
a 61 error integration run. The README now points at the script, CI invokes the
same script, and `tests/test_verification_path.py` fails if a migration or
integration command reappears in the README.

---

## Phase 2: Market data

### Crypto product is linear perpetuals, not spot

Decided by the director on 2026-08-16, after measurement.

Spot is unleveraged, so at 200 USD of capital a 2.00 USD risk requires 200 USD of
notional at a 1 percent stop, which is the entire account in a single position,
and 400 USD at a 0.5 percent stop, which is unreachable. Spot is not a worse
option at this capital, it is an unworkable one.

### BTC/USDT perpetual is excluded from trading, but not from recording

Decided 2026-08-16 on grounds of quantisation, from Bybit's own metadata with BTC
at 63,035 and ETH at 1,880 USDT.

BTC's 0.001 quantity step is 63.03 USDT of notional, which is 0.630 USDT of risk
at a 1 percent stop, giving three distinct position sizes inside a 2.00 USD
budget. Realised risk on a BTC trade can therefore sit up to 31 percent away from
the 1 percent the risk engine claims to enforce. A limit approximated to within a
third is not being enforced, and `SPEC.md` section 6 already prescribes exclusion
rather than accepting the approximation, so this follows an existing rule rather
than introducing one.

Recording continues for both instruments. Bybit publishes no historical quote
data at all, so crypto spread history begins when we start recording and cannot
be recovered later. BTC is also the reference asset for the regime and
correlation work in phase 4. A trading exclusion is not a reason to lose data.

### The exclusion is a configuration threshold, not a constant

Expressed as the maximum acceptable deviation from intended risk and evaluated
against live venue metadata and price, so it re-evaluates on its own as capital
grows and BTC stops binding. Revisit at phase 5.

Hardcoding today's answer would leave the system excluding an instrument for a
reason that had stopped being true, and nothing would surface that.

### USDT is not treated as USD

The eligibility screen refuses to convert between an instrument's quote currency
and the account currency without an explicit rate, so every Bybit calculation
states the rate it used.

The peg holding is a market observation, not an identity. The same assumption
applied to a JPY quoted forex pair would be wrong by a factor of about 150, and a
screen that silently assumes parity is a screen that will eventually approve a
position it should have refused.

### The crypto venue configuration key is `bybit`

Renamed from `binance`. The position model decision names Bybit, and leaving a
`binance` key configured while the specification said Bybit would be incoherent.
This was an inference from the specification rather than an explicit instruction,
and it was confirmed by the director on 2026-08-16.

### Commits are not GPG signed

Decided by the director on 2026-08-16, recorded so it is not reopened every
session.

GPG cannot reach a TTY in this environment, so signing would need an agent with a
cached passphrase or a passphraseless key kept on disk. On a private
single-author repository that buys no security: a signature proves the commit
came from the key holder, and there is one author, one machine, and no second
party to whom that proof is addressed. The workaround would be a moving part that
fails at inconvenient times and protects against nothing in the threat model.

Worth revisiting if the repository gains a second author or becomes public, at
which point signatures start proving something to someone.

### Phase branches merge when the phase completes, not to satisfy tooling

`main` stays at the phase 1 commit until phase 2 is done and its exit criteria
are met. Merging a phase branch to make CI fire, or to make a status look
tidier, inverts the relationship between the gate and the work it gates.

### The CI trigger is every branch, not `main`

The workflow originally triggered only on pushes to `main`, and `main` had no
`.github/` directory, so it had never executed once while being counted as
coverage. The filter was also wrong in principle: phase branches live for days,
so a check that fires only at merge time reports on work finished a week earlier.

GitHub reads the workflow from the branch being pushed, so each branch is now
checked against its own pipeline.

### Verification probes separate transport failure from content failure, and do not retry

Decided 2026-08-16 after diagnosing CI run 31939779488.

`curl URL | grep -q PATTERN` was replaced by a fetch into a variable followed by a
separate match. The pipeline had two defects. It reported a refused connection,
an HTTP error, a broken pipe, and a body that genuinely lacked the pattern with
one identical message and no trace of what came back, which is why classifying
that run cost a pipeline re-run rather than a reading of the log. And under
`pipefail` it could fail while the pattern was present: `grep -q` exits at its
first match, so a curl still writing takes EPIPE and exits 23. Reproduced
directly; curl exits 23 and prints "Failure writing output to destination". The
series the probe looks for is the first one in the payload, which makes that
window as wide as it can be.

The probe deliberately does not retry. Readiness passes immediately before it,
and `/ready` and `/metrics` are two routes on one app closing over one metric
registry built before the port is bound, so if the port answers at all the series
is already registered and set. There is no window in which it can be absent for
timing reasons, so a retry could only conceal a genuine defect. Enforced by
`TestChecksDoNotDiscardTheirEvidence` in `tests/test_verification_path.py`.

### The decision log lives in `docs/DECISIONS.md`

Decided 2026-08-16. `SPEC.md` section 13 already directed a recovering session to
this path, and the file did not exist, which is a defect in the recovery protocol
itself. Creating it was chosen over amending `SPEC.md` to point at `PROGRESS.md`,
because decisions are permanent and progress is not: rulings kept inside a
tracker get rewritten when the tracker is rewritten, and a cold session should
not have to read a long mutable document to find the fixed points.

### The cTrader protobuf schema is vendored at a pinned commit

Decided 2026-08-16.

Upstream is `spotware/openapi-proto-messages`, MIT licensed, pinned at commit
`3fd8bddfbe0cfc2ecfda079623dc4e498af11e66` dated 2025-11-13. That commit is four
ahead of upstream's own tag `91`, carrying payload removals and a typo fix in the
model messages. A commit SHA is pinned rather than a tag because a tag can be
moved and a SHA cannot.

The four `.proto` files are kept byte identical to upstream and their sha256
digests are recorded in `scripts/generate_ctrader_messages.sh`, which verifies
them before generating anything. Vendored code whose upstream version is unknown
becomes unmaintainable the first time upstream changes, and vendored code that
has been quietly edited is worse, so the digest check makes both visible.

Generated modules and stubs are committed, so a build needs neither `protoc` nor
the network. Regeneration is a script rather than a documented command, for the
same reason `scripts/verify.sh` is: a transcribed command drifts from the one that
was actually run. The script also rewrites protobuf's bare cross module imports to
package qualified ones, because upstream's files import each other by bare
filename and protoc's output cannot otherwise resolve from inside a package. That
rewrite is part of generation, so committed output is reproducible from the script
alone.

### Spotware's own Python package is not used

`ctrader-open-api` is built on Twisted, which would put a second event loop beside
asyncio in a process whose entire architecture is asyncio. The schema is the part
worth taking from upstream; the transport is not.

### Generated protobuf code is excluded from the linter and not from the type checker

`mypy --strict` passes over the generated modules, using stubs from
`mypy-protobuf`. They are excluded from `ruff` only, because they are machine
output: a style fix applied to them would be reverted by the next regeneration,
and hand editing them would break the guarantee that committed output is
reproducible from the generation script.

The distinction is deliberate. Excluding generated code from the type check would
weaken the standard by exception, which is how standards erode. Excluding it from
the formatter costs nothing, because nobody reads or edits it.

### The account is matched by trader login and used by ctidTraderAccountId

The configured `account_id` is the broker's account number, which the venue
reports as `traderLogin`. Every request after the handshake is keyed by
`ctidTraderAccountId`, which is a different number the venue assigns. Confirmed
against the demo account on 2026-08-16: login 5325402 maps to ctid 48268952.

They are not interchangeable, and the mapping only exists at the venue, so the
handshake fetches the account list and resolves one to the other rather than
assuming the configured number is either.

### The live flag assertion fails closed

`isLive` is `optional` in the schema, so an account that never carried the field
reads as `false` through protobuf's default. Treating that as "demo" would
authorise an unknown account against a demo configuration, which is the single
most expensive default this system could take.

The connection therefore refuses when the flag is absent, when it is unreadable,
and when it disagrees with the configured environment, and it refuses before the
account authorisation request is sent rather than after. All four refusals are
tested, and the fail-open mutation was verified to break the test rather than the
test being assumed to cover it.

### A heartbeat that cannot be sent is connection death

Not a warning to log. A connection whose heartbeat write fails cannot carry an
order, and every caller has to learn that from the connection rather than from the
log. The same applies to the read deadline: silence past it kills the connection
and fails every pending request.

This is the shape of the Bybit stream defect, where a reconnect clause omitted the
websockets library's own disconnect exception and the recorder would have exited
on the first disconnection. It is not being rediscovered here.

### The read deadline is sized against the venue's measured heartbeat interval

Measured against demo.ctraderapi.com on 2026-08-16 over a 150 second idle window:
the venue sends a heartbeat every 30 seconds, arriving at t+30.1, 60.1, 90.2,
120.2, 150.1, gaps of 30.0, 30.1, 30.0, 29.9.

`stream_read_timeout_seconds` was 20 seconds, inherited from a value chosen for a
quote stream. An idle forex socket carries nothing but heartbeats, which is its
normal state over a weekend, so a 20 second deadline would have declared every
healthy idle connection dead and reconnected in a loop. It is now 95 seconds,
which declares death only after two consecutive venue heartbeats have been missed.

No unit test could have caught this: the scripted peer sends whatever the test
tells it to. It was found by connecting to the venue, which is the same lesson the
CI-never-ran defect taught. The relationship between the two numbers is now
guarded by a test that reads the shipped configuration, so the deadline cannot be
tightened back under the interval the venue actually sends at.

### Venue credentials do not go into CI, and venue drift is a known gap

Decided by the director on 2026-08-16.

The value of a periodic liveness check on our API assumptions does not justify
putting a live trading credential into a third party's secret store, where it is
reachable by any workflow anyone ever adds to this repository. The same pattern
would tempt us toward live keys at phase 8, which is when it would cost the most.

The consequence is stated rather than hidden: **CI cannot catch venue drift.** If
the broker changes a lot size, a swap convention, a symbol name, or the shape of
its metadata, no pipeline here will notice. The manual control is
`scripts/check_venue_assumptions.py`, run on the host where the credentials
already live, and `PROGRESS.md` records that it must be run before each phase
closes and before any deployment. This is an accepted gap with a control, not an
oversight.

The script asserts rather than prints. A report nobody reads is not a control, so
every check either passes or fails the run.

### An unknown protobuf enum value arrives as an absent field, and is refused

A proto2 closed enum drops a value the client does not know into unknown fields,
so the field reads as its declared default with `HasField` false. For
`tradingMode` that default is `ENABLED` and for `swapCalculationType` it is
`PIPS`, which means a venue that introduces a new mode would silently be read as
tradeable, and a new swap basis silently as pips.

Observed across the live catalogue: the venue sets both fields on every symbol. So
absence is not a legitimate case, it is exactly the unknown value case, and both
are refused. Checking for an unrecognised *value* would never fire, which is why
the check is on presence instead.

### `InstrumentStatus` gained `REDUCE_ONLY`

cTrader publishes `CLOSE_ONLY_MODE` and Bybit has the same notion. Collapsing it
into `HALTED` would mean either believing an open position cannot be closed, which
stops a flatten that would have succeeded, or believing a closed symbol accepts
entries. The difference decides whether the kill switch can act, so it is kept.

### `decimal_from_double` is a second sanctioned float door, and not the same one

`from_binary32` exists for fields whose value genuinely is binary, such as a
Dukascopy tick volume, where the exact expansion is the faithful record.

cTrader publishes swap rates as protobuf doubles, but the broker quotes them as
decimals: a swap of -1.2 pips expands to -1.1999999999999999555910790149937 as a
double, which is not a rate anyone published, and recording it would invent
nineteen digits of precision. The shortest decimal that maps back to the same
double is the value that was meant.

Neither conversion is safe in the other's place. Using the double door on a binary
field discards real precision; using the binary door on a quoted decimal
fabricates it. Both say so in their docstrings.

### The forex sizing verdict comes from the eligibility screen, not from the report

`scripts/check_venue_assumptions.py` calls `evaluate_eligibility`, the same screen
the crypto half was measured with, rather than repeating the arithmetic. A report
that computes eligibility its own way can disagree with the code that enforces it,
and then neither can be trusted.

### Capital independence is a requirement, and is enforced structurally

Directed by the director on 2026-08-16 and recorded in `SPEC.md` section 6.1, with
the phase 8 gate amended to require that paper capital match intended live capital.

Most of it was already true, so the work was making it explicit, tested, and hard
to regress. Three things were added.

`InstrumentScreen` in `src/tradingsys/risk/screen.py` holds risk policy as
fractions only and is handed a balance each time it is called, so the tradeable
set is recomputed rather than cached. Instruments enter and leave as the balance
moves, with no restart and no code change, and the transitions between two
evaluations are first class output so that a changing instrument universe is
reported rather than inferred from orders drying up.

The eligibility result now carries the widest affordable stop, which is the
strategy constraint the verdict implies. It is computed in the same place as the
verdict so a report cannot disagree with the enforcement.

`tests/risk/test_no_absolute_amounts.py` parses the risk package and rejects any
constant that could be an amount of money: a literal reaching a `Money`
constructor, a module scope `Decimal` that is not dimensionless, and any numeric
default argument. Structural rather than arithmetic, because a limit written in
currency produces correct arithmetic over a wrong constant and no test of the
arithmetic can see it.

**The guard was verified by mutation and one mutation defeated it.** An earlier
version inspected only the immediate arguments of a `Money` call, so
`Money(Decimal("200"), currency)` passed, the literal being one call deep. The
test now walks the argument expressions. Worth recording because the hole was
invisible from reading the test and appeared immediately on writing the mutation.

### Capital independence is tested by property, not by a table of sizes

Balances are generated across eight orders of magnitude and the assertions are the
identities that define the arithmetic: the budget is the stated fraction of equity,
the intended size loses exactly the budget at the stop, the tradeable size sits on
the venue grid and never above intended, the verdict follows from the numbers, and
multiplying the balance multiplies the size by the same factor.

A fixture of chosen values passes for exactly the sizes whoever wrote it thought
of, which is the wrong shape of evidence for a claim that the system accepts *any*
account size.

Linearity is asserted to within a relative tolerance of 1e-30 rather than exactly.
Decimal arithmetic carries 34 significant digits, so dividing then multiplying can
differ from the direct computation in the last place. The tolerance is wide enough
for that reassociation and far too narrow for a threshold, an offset, or a rounding
to a venue step to hide inside.

### Strategy classes are compared against declared assumptions, and labelled as such

`scripts/check_venue_assumptions.py` reports which strategy classes the stop
ceiling admits, because SPEC 6.1 requires the implication rather than only the
verdict. The reference stop distances it compares against are **assumptions, not
measurements**, and the output says so on every run.

Phase 4b ingests the economic calendar and measures what high importance releases
actually move. At that point the comparison becomes evidence and the assumed
figures are replaced. Stating them as assumptions now is better than leaving the
operator to infer the consequence from an exclusion list, and better than quietly
presenting a guess as a finding.

### Trend becomes phase 4a and macro events phase 4b

Directed by the director on 2026-08-17. Recorded in `SPEC.md` sections 5.1 and 8.

Two reasons, deliberately kept separate because only one of them can change.

**Readiness, the larger reason.** Trend runs on data already being recorded and
can be walk-forward tested as soon as the backtest engine exists. Macro needs a
paid economic calendar API that has not been purchased, and a surprise to
direction mapping derived from history that has not been collected. Trend is
readier regardless of capital.

**Capital, the reason that can change.** At 200 USD against a 1000 unit venue
minimum the stop ceiling is about 20 pips on a USD quoted pair, which is inside an
intraday trend stop and outside what a high importance release routinely moves.

The section 5.1 rationale for building macro first was **not deleted and is not
withdrawn**. Nothing about it has been shown wrong; it has been shown unaffordable,
and those are different findings with different remedies. If capital rises or a
venue with a smaller minimum lot is adopted, macro moves back up the order and
that rationale is what it moves back on.

The phase 8 gate now states what a fail means when only the trend leg exists: a
pass is a pass, but a fail is a verdict on one leg rather than on the design, and
the macro leg has to be made reachable and evaluated before the design is
abandoned.

### Defect class: a liveness check that proves existence, not correct future action

Named by the director on 2026-08-17 after the crypto capture drifted nine hours
while every check said it was healthy.

**What happened.** The capture was armed with `sleep N`, where N was computed
against the wall clock. WSL2 suspended the VM overnight. The process kept its place
in the sleep while the wall clock advanced, so a capture armed for 23:59:30Z was
still sleeping at 06:45Z with 7870 seconds to go, and would have fired at 08:57Z.

**Why it was checked twice and passed twice.** Both checks asked whether the process
existed. `ps` reported a live PID, a live `sleep` child, an owned session, and the
right working directory. Every one of those was true and none of them was the
question. The only observable that would have caught it was elapsed time against
wall clock: four hours of VM uptime behind thirteen hours of the world.

**The class.** A check that confirms a process exists proves nothing about whether
it will act, or act at the right time. Existence and correct future action are
different properties, and for anything driven by a timer, a deadline, or a schedule
it is the second one that matters. Confirming the first and reporting health is how
a check becomes a source of false confidence rather than assurance.

**The rule.** Wherever this system depends on a timer, a deadline, or a scheduled
action, the check verifies that the thing will happen at the right time. In
practice that means comparing the scheduled instant against the wall clock now, not
inspecting the process that holds it, and it means any waiting is done by re-reading
the clock rather than by trusting an interval. `scripts/arm_crypto_capture.sh` polls
the clock every thirty seconds for exactly this reason: a suspend costs one poll of
lateness rather than the whole of its duration.

**Where this binds before it is rediscovered.** Two places, noted now so they are
designed rather than repaired.

*Phase 5, the reconciliation loop.* It runs on a fixed interval and its whole
purpose is to notice divergence between our position state and the venue's. A
suspended host silently stops reconciling while the process stays up and the health
endpoint stays green, which is precisely the window in which a divergence would go
unnoticed. Reconciliation must therefore assert its own recency: the check is when
the last reconciliation completed relative to now, and a loop that has not run
within its interval is a fault regardless of whether its task object is alive.

*Phase 7, the thirty day paper run.* Its exit criteria are calendar based. A host
that suspends for nine hours produces a run that believes it covered thirty days
and covered less, with a gap that no unhandled exception marks. The run has to
measure elapsed wall clock coverage rather than count iterations, and a suspension
gap has to be visible in the record rather than absorbed by it.

The same reasoning applies to the stale claim recovery already built into
`backfill_hours`: an hour left `in_progress` by a dead or frozen worker is reclaimed
on wall clock age, not on any belief about whether that worker is still running.

### The quantisation tolerance is 5 percent, derived rather than chosen

Directed by the director on 2026-08-17: the previous 10 percent was written into a
prompt without analysis when BTC was excluded, and had become load-bearing.

**The structure of the error.** Position size is rounded **down** to the venue's
quantity step, so realised risk is always at or below intended risk. The error is
therefore one-directional: the stated limit is never breached, only undershot. It is
also bounded, because the shortfall is less than one step: as a fraction of the risk
budget it is less than `step / intended`, which is the deviation the screen reports.

So realised per-trade risk lies in `((1 - d) x r, r]` where `r` is the stated limit
and `d` the deviation. Safety is not what binds, since nothing exceeds `r`. What
breaks is the **truth of the statement**.

**The derivation.** `SPEC.md` section 6 states the per-trade ceiling as **1.0
percent** and the hard cap as **2.0 percent**, both to one decimal place. A quantity
stated to one decimal place asserts that the true value lies within half a unit of
the last place: 1.0 percent asserts the interval [0.95, 1.05].

Realised risk lies in `((1 - d) x 1.0, 1.0]`. For every attainable value to be
truthfully described by the stated figure, the lower end must not fall out of that
interval:

```
(1 - d) x 1.0  >=  0.95
             d  <=  0.05
```

**The tolerance is 5 percent.** Above it, a system that says it risks 1.0 percent per
trade is taking an amount that no longer rounds to 1.0 percent, which is the precise
sense in which the limit stops meaning what it says.

**What it costs.** If the fractional part of intended-over-step is uniform, the mean
shortfall is half a step, so expected deployed risk is `1 - d/2`, or 97.5 percent of
intended. The system systematically forgoes about 2.5 percent of its intended
exposure and therefore about 2.5 percent of its expected edge. Bounded and stated,
in the manner section 5.5 requires of funding.

**Why not the other framings**, recorded so they are not relitigated:

*Risk-adjusted performance is unaffected, and that is not the point.* Rounding down
scales exposure, so expected return and volatility fall together and the Sharpe ratio
is untouched. The concern is whether the stated limit is true, not whether the
account is efficient.

*A statistical framing gives a weaker bound and rests on a number that does not
exist.* Requiring the risk unit to be stable enough to measure an expectancy to
within ten percent relative admits roughly 20 percent deviation, but it depends on an
assumed expectancy, and no strategy exists yet to supply one. A threshold derived
from an assumption is not derived.

*Computing realised R per trade from actual fills fixes measurement, not the
statement.* The audit log does record actual size, so performance reporting can use
exact realised risk. It does not help an operator reading "1 percent per trade" in
the runbook, who must be able to trust the figure without recomputing it.

**It moved against convenience, which is the test that it was derived.** 5 percent is
tighter than the 10 percent it replaces, so it makes the Pepperstone minimum lot
worse rather than better and rules out more of the instrument universe at this
capital. `SPEC.md` section 6 forbids raising a limit to fit a venue one level up, and
the same rule applies here.

### Reasoning error: treating cost and sizing as one constraint

Recorded at the director's instruction on 2026-08-17, because the conflation would
otherwise be repeated by anyone reading the exchange.

The instruction was: measure the cost of a 5 pip stop, and if it is a large fraction
of risk, stop pursuing brokers for forex at this capital. The premise was that
scalping was the only class that cleared the sizing screen at 200 USD, so if costs
killed scalping they killed forex.

**The premise conflated two constraints that turn out to be orthogonal.** Cost as a
fraction of risk cancels position size exactly: spread cost, commission and risk all
scale linearly with units, so the ratio is 12 percent at a 5 pip stop whether the
position is 10 units or 100,000. Verified across four orders of magnitude. Sizing
eligibility, by contrast, depends only on capital and the venue's step and not at all
on cost.

So they resolve independently, and killing the tight stop does not kill forex. It
moves the target: cost rules out stops tighter than about 20 pips, and sizing then
asks what capital a 20 pip stop needs, which is 40 USD on a 10 unit step. The class to
aim at became 20 pip intraday rather than 5 pip scalping, and the broker requirement
tightened from 200 units to 50.

**The general shape of the error is worth keeping.** Two constraints that both bind on
the same decision are not necessarily the same constraint, and the tell here was that
one of them cancelled a variable the other depended on. Had the instruction been
followed as written, the demos would have been abandoned on a conclusion that the
measurement contradicts.

### Slippage is an unmeasured term and must be stated as one

Directed 2026-08-17. The cost measurement covers spread and commission only.

**Why it is not measured yet.** Slippage is the difference between the price an order
was sent at and the price it filled at. It exists only in fills, and this system has
never submitted an order: execution is phase 6 and paper trading is phase 7. Nothing
in historical tick data or venue metadata contains it, so no amount of work before
phase 6 produces the number.

**What it would take.** Recorded fills with, for each one, the quote at submission, the
venue's fill price, the order type, the size, and the instant, so that slippage can be
separated from spread and attributed to conditions such as session, release proximity,
and size. That is a phase 6 or phase 7 dataset by construction.

**What phase 3 must do until then.** The backtest cost model states slippage as an
explicit unmeasured term rather than omitting it. An omitted term reads as zero, and
zero is the one value it certainly does not have. At a 20 pip stop, one pip of
slippage is 5 percent of risk, which is comparable to the entire commission cost, so
the term is the same order of magnitude as one that is measured. A cost model that
reports spread and commission precisely and slippage not at all is more misleading
than one that carries a stated range, because it invites the reader to believe the
total is complete.

### The demo spread figures are a lower bound, not a measurement of live pricing

Found 2026-08-17 while testing the earlier spread result against a high impact release.

The director's challenge was that a median spread of 0.000 pips on a raw account is
believable but a median that stays 0.000 through a non-farm payrolls release would
not be. It does stay 0.000, on EUR/USD through the 2026-08-07 13:30 UTC release.

**The method was checked first and is sound.** The obvious way for exact timestamp
pairing to lie is by discarding fast moments, when bid and ask might tick milliseconds
apart, leaving a sample biased toward calm. Measured: pairing coverage is 92.1 percent
in a quiet window, 92.9 percent in the release minute, and 95.9 percent over the five
minutes after it. Coverage does not collapse, so the surviving sample is not selected
for calm and the result is real data.

**The remaining explanation is the account.** This is a demo, and a demo feed is not
obliged to reproduce live pricing. Zero widening through NFP is far more consistent
with a feed that does not model it than with a live raw spread book.

So the demo spread figures are treated as a **lower bound on live spread**, and phase 3
must not rest its cost model on them. The direction of the error is known, which is
worth something: real spreads can only be wider, so scalping is at least as dead as
measured, and the 3 percent at a 20 pip stop can only rise.

**How it was settled, and the answer was not the expected one.** Dukascopy tick data
carries bid and ask in the same record, so it involves no alignment reconstruction, and
`core/provenance.py` marks it research only so nothing from it may enter an execution
cost model. It was used only to ask whether any real feed widens at NFP.

**It does not.** EUR/USD on 2026-08-07: quiet hour median 0.300 pips, release minute
median 0.200, widest tick in the release minute 0.500 against a quiet maximum of 0.600.
Tick rate rises about fourfold across the release and quoted spread does not move.

So the hypothesis that the demo feed fails to model widening is **not confirmed, and is
withdrawn**. Two independent feeds show the same absence. Either EUR/USD genuinely does
not widen much in median terms at NFP, which is plausible for the most liquid pair in
the world against a book that recovers in milliseconds, or both sources are aggregated
in ways that smooth it. Neither can be distinguished from quote data.

**What survives, and it is the more durable argument.** Quote data cannot measure
execution cost at all. What degrades at a release is the size executable at the quoted
price, and therefore slippage and rejection, not the quote. A book can hold a 0.2 pip
spread while the volume behind it collapses, and no quantity of quote data from any
feed reveals that. The cost understatement is therefore real but its mechanism is the
unmeasured slippage term rather than an understated spread.

The `SPEC.md` phase 8 entry was corrected to say this. It had been written on the
withdrawn reason before the cross-check ran, which is the argument for running it.


### Broker platform availability comes from the account opening form, nothing else

Established 2026-08-18, at the cost of a candidate.

RoboForex was shortlisted as the only plausible combination of a cent account with
cTrader, on the strength of several broker comparison sites listing it as a cTrader
broker. The account opening form offers MetaTrader 4, MetaTrader 5, and R StocksTrader.
There is no cTrader, on any account type. The sites were simply wrong.

**The rule.** Platform availability is established only from the broker's own account
opening form, where the platform is actually selected. Not from comparison sites, not
from the broker's own marketing pages either, because those list platforms per broker
while availability is per account type, and the cent account is exactly the type most
likely to be excluded.

The same caution applies to every other claim in that class: minimum lot, commission,
jurisdiction acceptance. The API answers the first two once an account exists, and the
signup form answers the third. Nothing is taken from a page that is trying to rank
brokers.

**It cost a candidate and would have cost more.** The shortlist had already been
narrowed on this basis and the next step was to open the account. Had the claim been
believed one step further, the cost would have been a second adapter written against a
platform the broker does not offer.

### The NFP sequence: a conclusion written before its evidence, then corrected by it

Recorded at the director's instruction on 2026-08-18. The value is in the order the
steps happened, not in the answer.

**What happened, in sequence.** The Pepperstone demo showed no spread widening at a
non-farm payrolls release. The inference drawn was that a demo feed which does not widen
at the most violent scheduled event of the month does not widen anywhere, and is
therefore unrepresentative of live pricing. The `SPEC.md` phase 8 gate entry was written
on that reason, stating that paper results carry an optimistic bias because spread is
understated.

Then the cross-check ran. Dukascopy, an independent feed whose records carry bid and ask
together so no reconstruction is involved, shows the same absence: a quiet-hour median
of 0.300 pips against 0.200 in the release minute, with the widest tick in that minute
narrower than the widest in the quiet hour, while tick rate rises about fourfold.

**The premise was refuted and the entry was rewritten rather than kept.** The demo feed
is not shown to be unrepresentative on spread, and that inference is withdrawn.

**The surviving argument is stronger than the one it replaced.** Quote data cannot
measure execution cost at all, because what degrades at a release is the size executable
at the quoted price rather than the quote itself. A book can hold a 0.2 pip spread while
the volume behind it collapses, and no quantity of quote data from any feed reveals that.
So the optimistic bias at the phase 8 gate is real, but its mechanism is the unmeasured
slippage term rather than an understated spread, and that holds regardless of which feed
is used or how well it models widening.

**Why the sequence is worth recording.** The stronger argument was not reachable by
reasoning from the first result. It became visible only when the obvious explanation was
tested and failed, which forced the question of what quote data can establish at all. A
conclusion that had been left resting on its original premise would have been correct by
accident, for a reason that is false, and would have been defended on that reason the
next time it was challenged.

The general form: when a conclusion is written before the evidence that would test it,
the test is worth running even when the conclusion is expected to survive, because what
it changes may be the reasoning rather than the answer.

### The same clock defect a third time, now inside the measurement itself

Found 2026-08-18. The capture was armed correctly and still failed, because the fix had
been applied to the launcher and not to the thing being launched.

`scripts/arm_crypto_capture.sh` was corrected to wait on wall clock after a suspend
drifted it nine hours. It then started the capture at 07:00:08Z exactly as intended. But
`measure_crypto_rate.py` set its own deadline from `asyncio.get_running_loop().time()`,
which is monotonic and does not advance while the host is suspended. Measured 24 hours
later: **9.1 hours of loop time against 24.1 hours of wall clock**, so the host had been
suspended for about 15 hours and the run needed nearly 15 more hours of loop time to
finish. It would have run for days and still produced a day with a hole in it.

**Two further defects surfaced with it**, both of which made the failure worse than it
needed to be.

*No coverage record.* Counts were kept per UTC hour with no record of how long the socket
was actually connected during each, so an hour observed for ten minutes is
indistinguishable from a quiet hour. An outage understates the rate instead of appearing
as a gap, which is precisely the shape of error that gets averaged into a conclusion.

*No incremental persistence.* The summary was written only on completion, so stopping the
drifted run discarded every hour it had collected. A measurement that runs for a day and
persists nothing until the end has made its own interruption maximally expensive.

**The fixes.** Deadline against wall clock. Per hour coverage in seconds, with rates
computed per covered second rather than per elapsed second, so a partial hour reports the
rate it actually saw and its coverage beside it. Summary written every five minutes.
Reconnect log lines now carry timestamps, because the previous log recorded 43 reconnects
and no times, so the outage could not be located from it.

**The lesson that generalises past this script.** The first fix was applied where the
defect was found rather than everywhere the defect class applies. A launcher that starts
on time and a run that measures its own duration on a clock that stops are the same bug in
two places, and fixing one made the system look correct while leaving the failure intact.
When a defect class is named, the question is which other code makes the same assumption,
not whether the reported instance is repaired.

The general form applies directly to the two places already flagged: phase 5's
reconciliation loop and phase 7's paper run must both measure elapsed wall clock rather
than count iterations or trust a monotonic timer, and this is now the second piece of
evidence that the distinction is not theoretical on this host.

### On this host `CLOCK_BOOTTIME` is `CLOCK_MONOTONIC`, so the portable remedy does not work

Measured 2026-08-18T07:34Z. `time.clock_gettime(CLOCK_MONOTONIC)` returned 133629.048039
and `time.clock_gettime(CLOCK_BOOTTIME)` returned 133629.048047, a difference of eight
microseconds against an uptime of 37 hours. They are the same clock here.

**Why this is worth writing down.** The textbook fix for a timer that stops during
suspend is to move it from `CLOCK_MONOTONIC`, which excludes suspended time, to
`CLOCK_BOOTTIME`, which includes it. That is the first thing a session will reach for
after reading the three clock failures above, and on this host it changes nothing. WSL2
does not advance either clock across a host suspend, so a deadline built on `BOOTTIME`
drifts exactly as far as one built on `MONOTONIC`.

**The consequence, which is the rule.** Every fix in this defect class goes through
`Clock.now()` and wall clock arithmetic. There is no monotonic clock available here that
survives suspend, so the choice is not between two monotonic clocks, it is between wall
clock and being wrong.

**A related limit worth knowing.** Past suspend cannot be measured after the fact on this
host either. `/proc/stat` `btime` is recomputed as `now - uptime`, so it agrees with the
monotonic clock by construction and can never witness a gap. The only way a suspension
becomes observable is for a running process to have recorded wall clock instants across
it, which is why coverage recording is a requirement of any long measurement here and not
a nicety.

### A component is complete when something that runs constructs it, not when its file exists

Directed by the director on 2026-08-18, after `PROGRESS.md` carried "Complete" for
`app/ingest.py`, which no code constructs, no configuration configures, and no test
exercises. It measured 0 percent coverage across 71 statements.

**The definition.** A component is complete when all three hold:

1. It is **constructed by something that runs**, meaning a production entry point rather
   than only a test or a script.
2. It is **configured**, meaning its parameters come from the configuration system rather
   than from a constructor argument with no caller.
3. It is **tested**, meaning behaviour and failure modes, not import.

Writing the module satisfies none of these. `SPEC.md` section 12 already required real
implementation and tests; what it did not say, and what was exploited without anyone
intending to, is that a file can satisfy every line of a definition of done while being
unreachable from the running system.

**Why the tracker is where this bites.** This is the second false claim `PROGRESS.md` has
carried, after it recorded CI as enforcing gates during a period when the workflow had
never triggered once. Both have the same shape: a written status that no observation
supports, in a file whose whole purpose is to be trusted by a session that cannot check
everything. The rule that follows is that a status line is a claim about an observation,
and the observation is named in the row or the row does not say Complete.

### Bybit is fronted by CloudFront, so we never hold a connection to Asia

Measured 2026-08-18, while deciding which region to deploy the recorder in.

```
stream.bybit.com  CNAME  d2mo22rbksh9yz.cloudfront.net
api.bybit.com     CNAME  d3d4ij29qlbhtu.cloudfront.net
via: 1.1 ...cloudfront.net (CloudFront)
x-amz-cf-pop: NBO50-P2        (the Nairobi edge, from this host)
```

**The mechanism, which is the part worth carrying.** Both the WebSocket stream and the
REST API are CNAMEd to CloudFront distributions. The TCP and TLS connection the recorder
holds therefore terminates at the nearest CloudFront edge, not at Bybit's origin. The
path splits into two legs with different risk profiles: client to edge, which carries
public internet transit risk and is short from any region we would choose, and edge to
origin, which carries the real distance to Bybit and runs on AWS's private backbone.
Choosing a region moves the second leg, which is the more reliable one, and leaves the
first leg short everywhere.

**Why that answers the region question.** Three separate arguments, none of which
depends on where the origin is:

*Message loss cannot happen silently.* WebSocket runs over TCP, so a frame either
arrives intact or the connection fails. Distance cannot produce a gap in recorded data
without producing a visible disconnection, which moves `connections`, `failures`,
`resyncs` and `last_error` in `StreamStats`.

*Latency cannot trip the silence detector.* The stream pings every 20 seconds against a
30 second receive timeout. The round trip difference between a London edge and a
Singapore edge is on the order of 200 milliseconds, which the silence budget absorbs
about 150 times over.

*The reconnect gap is dominated by our own backoff.* Reconnecting costs a few round trips
to a nearby edge, so tens of milliseconds, against a reconnect backoff of one second base
with full jitter. Ten reconnects a day at 200 milliseconds of extra round trip is two
seconds of gap in 86,400, which is 0.002 percent of coverage.

**This reasoning is mechanism-based and not measured, and that limit is part of the
record.** The recorder has never run from a datacenter. The only reconnect data we hold
is 43 reconnects with 23 DNS resolution failures from this host in Kenya on WSL2, which
measures a domestic link and not a datacenter path. Probes from here confirm only that
this host cannot answer the question: five TCP connects to one endpoint ranged from 59 to
305 milliseconds, so the link's own variance exceeds the difference between venues.

**What would settle it.** `StreamStats` already records `connections`, `failures`,
`resyncs`, `silence_timeouts`, `reconnect_delays` and `last_error`. The 72 hour run
produces exactly that dataset, and running the same recorder in two regions for a week
and comparing those counters is the experiment if the question is ever reopened.

### The recorder deploys to DigitalOcean Singapore, and the region is revisitable

Decided by the director on 2026-08-18.

**Region is Singapore, not London.** London was specified when forex was the primary leg.
Forex is deferred, the only recorded and traded instruments are on Bybit, and the finding
above establishes that neither region measurably affects data quality. So the region is
chosen for what is actually traded rather than for a leg that is not running.

The justification London had for forex is weaker than it appeared in any case. Both
cTrader Open API endpoints resolve into Oracle Cloud address space, `live.ctraderapi.com`
at 143.47.254.136 in ORACLE-IE and `demo.ctraderapi.com` at 145.241.247.143 in
SE-ORACLE-SE, both registered to Oracle Svenska AB. That is a cloud hosted API gateway
and not a broker matching engine colocated at Equinix LD4, which is the assumption that
usually motivates London for retail forex. The registration country does not establish
which Oracle region the addresses are deployed in, and no such claim is made here.

**The region decision is revisitable and costs a rebuild rather than a migration**,
because nothing but the recorder runs there and the host is reproducible from
`deploy/provision`. Recorded so that a later session treats it as a choice rather than as
a constraint.

**DigitalOcean over Vultr** on bandwidth enforcement. Vultr meters the transfer allowance
hourly, so a 2 TB monthly allowance behaves as roughly 3 GB per hour and a burst triggers
overage immediately, which is a live risk for a process holding a 24/7 stream.
DigitalOcean bills per second with a cap at the monthly rate and applies the allowance at
the month rather than the hour. Hetzner was cheapest by a wide margin and was excluded on
region: its six cloud locations are Falkenstein, Nuremberg, Helsinki, Ashburn, Hillsboro
and Singapore, with no London presence.

**The size is 2 vCPU, 4 GB, 80 GB, with `db_data` on a separate block storage volume from
day one.** The volume is not for capacity today, it is so that every future expansion is
an online resize rather than a maintenance window: moving an existing PGDATA onto a
volume later means stopping Postgres, copying and remounting. The margin that a larger
droplet would have bought is not needed, because on DigitalOcean a droplet resize covering
CPU and RAM is reversible and a volume attaches and resizes freely, so being wrong about
either costs a resize rather than a migration. Only in place disk expansion is one way,
which is precisely what putting `db_data` on the volume avoids.

**The 80 percent rule is why 80 GB is not 80 GB.** A Postgres volume should not run much
past 80 percent full, because `compress_chunk` writes the compressed chunk before dropping
the uncompressed one, so the daily compression job peaks above steady state, and
autovacuum and any local dump need working room. Effective capacity is therefore about
64 GB, which at the measured 243.81 and 21.90 bytes per tick row carries about 22 months
at a 40 per second combined rate rather than the full 24.

### Sizing error: the droplet disk was read as the database disk

Recorded by the director on 2026-08-19 as a director-side error, because the same
conflation can recur every time the volume is resized.

**What happened.** The host was specified as 2 vCPU, 4 GB, 80 GB, and the retention
arithmetic was done against that 80 GB. The droplet does have an 80 GB disk, but
`db_data` is bound to a separate block storage volume, which was created at 20 GB. The
number the retention horizon depended on was therefore four times the real one, and the
runway at a 40 per second combined rate fell from about 22 months to about 4.6.

**Why it was not obvious.** Both numbers are real and both describe this host. Nothing
was wrong except which of the two the database writes to, and that is a property of the
compose overlay rather than of the droplet. A sizing figure that names a machine rather
than a mount point does not carry the distinction, so it survives review.

**The rule.** Storage arithmetic names the **mount point** it applies to, never the host.
`deploy/provision/RUNBOOK.md` states the runway against `/mnt/tradingsys_db` explicitly,
and `deploy/provision/healthcheck.sh` asserts against that mount and refuses to report on
it if it turns out to be the root device, which is the same error appearing as a
monitoring result rather than as a plan.

**Where it recurs.** At every resize. Growing the volume changes the runway and growing
the droplet does not, and the two are bought in different places in the provider's
interface. The forecast in `healthcheck.sh` is deliberately expressed in days remaining
rather than percent used for this reason: a percentage of the wrong device still looks
like an answer, while a projection that disagrees with the recorded arithmetic does not.

### Storage headroom is monitored as a projection, not as a percentage

Directed by the director on 2026-08-19, on the reasoning that headroom alerting was a
nicety while the horizon was two years and is load bearing at four months.

Three changes, each with its own reason.

**It asserts against the volume, not the root disk.** A check that reads whichever device
happens to hold the path is the sizing error above, recurring as a green dashboard. The
check resolves the device behind the mount and fails if it is the root device, because at
that point the number it would report is true of the wrong disk.

**It alerts at 70 percent rather than 80.** 80 percent is where Postgres becomes
constrained, since `compress_chunk` writes the compressed chunk before dropping the
uncompressed one and the daily job peaks above steady state. An alert at the point of
constraint leaves no time to act, so the alert moves to 70 and the operating limit stays
at 80.

**It reports projected days to full at the observed rate.** A percentage says where the
volume is; a projection says whether to act this week. The projection uses the measured
21.90 bytes per compressed tick row against rows actually written in the last 24 hours,
because in steady state each day adds one compressed day while the seven day uncompressed
window stays a constant size. Before day seven the growth is the uncompressed 243.81 and
the projection says so rather than flattering itself.

### Retention is a lever on storage, and is priced against the volume rather than assumed

Framed by the director on 2026-08-19. The 24 month retention in `SPEC.md` section 4.0 was
chosen when storage looked free, and it is a configuration value rather than a constant
precisely so that it can be reconsidered.

**The tradeoff.** Shortening retention costs research depth on old data, once. Resizing
the volume costs money every month, forever. Neither is obviously right, and which one is
cheaper depends on a number that is not yet measured.

**What is owed when the weekday capture lands**, so the decision is framed rather than
defaulted into: the volume size required for 24 months at the measured p95 hour rate, and
the retention window that fits comfortably inside 60 GB at that same rate, both priced.
The default of buying disk is not to be taken silently.

---

## Rejected, with the reason, so they are not revisited

### Trading macro events on the crypto leg to route around the stop ceiling

Rejected by the director on 2026-08-17.

The proposal was tempting for arithmetic reasons: the ETH stop ceiling is 10.6
percent against forex's 0.17 percent, so a macro width stop fits easily there.

It is a category error. ETH has no scheduled release with a published consensus
forecast, and the surprise term, meaning the deviation of an actual release from
consensus, is the entire mechanism that makes a macro signal measurable and
testable. Without it what would be built is a differently named strategy sharing
none of the property that justified this one, evaluated as though it were evidence
about the macro hypothesis.

The stop ceiling is a constraint on where macro can be traded. It is not a reason
to redefine what macro means.

---

## Decisions that live in `SPEC.md`

These were taken by the director as specification. `SPEC.md` is authoritative and
they are not copied here, because two copies eventually disagree.

| Decision | Where |
|---|---|
| Instrument universe, and USD as base accounting currency | Section 4.0 |
| Capital of 200 USD, with risk limits staying percentage-based | Section 4.0 |
| Instruments whose minimum size exceeds 1 percent risk are excluded, never the limit raised | Sections 4.0 and 6 |
| Tick and bar retention windows, and higher timeframes derived on read | Section 4.0 |
| Bid and ask stored as separate series, mid computed on read and never used for fills | Section 4.0 |
| Both netting and hedging modelled, netting enforced as system policy | Section 4.0 |
| Holding period constraint on the crypto leg, and funding as a first-class backtest result line | Section 5.5 |
| Forex venue is cTrader via Pepperstone, and the venue abstraction's standard | Section 3.3 |
| Process topology and the risk engine boundary | Sections 3.1 and 3.2 |
| Engineering standards, and the definition of done per task | Sections 2 and 12 |

---

## Open, not yet decided

Carried here so that an open question is not mistaken for a settled one.

| Question | Waiting on |
|---|---|
| Whether `/health` should register a liveness check such as a stalled loop detector | The director, with evidence to be brought |
| Whether unchanged Bybit snapshot repeats should be stored as rows | The measured row counts from the weekday capture |
| Dukascopy volume units | A published definition, or a cross check against Pepperstone over an overlapping window |
| Crypto retention window | The weekday capture, which no earlier sample may substitute for |
