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
