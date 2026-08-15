# tradingsys

Foundation for a production automated trading system spanning forex (broker REST and
streaming) and crypto (ccxt) venues.

This repository currently contains **phase 1 only**: the parts of the system that
everything else will be built on. There are deliberately no strategies, no order
placement, and no market data ingestion yet. What exists is complete and tested, not
stubbed.

## What is here

| Area | Module | Summary |
| --- | --- | --- |
| Core domain | `tradingsys.core` | `Money`, `Currency`, `Instrument`, trading schedules, financing conventions. Decimal throughout, no floats. |
| Configuration | `tradingsys.config` | Layered defaults, TOML files, secrets directory, environment. Validated at startup, fails loudly. |
| Venue abstraction | `tradingsys.venues` | `MarketDataSource` and `ExecutionVenue` abstract base classes plus their value objects. Signatures only, no adapters. |
| Persistence | `tradingsys.persistence` | asyncpg pool, TimescaleDB schema, Alembic migrations, hash chained append-only audit log. |
| Observability | `tradingsys.observability` | structlog JSON logging with correlation IDs, Prometheus metrics, `/health` and `/ready`. |
| Runtime | `tradingsys.app` | Startup, dependency wiring, graceful shutdown. |

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose, for the local stack

## Getting started

```bash
uv sync                      # create .venv and install everything
cp .env.example .env         # fill in secrets, never commit this file
scripts/verify.sh            # everything else
```

`scripts/verify.sh` is the single verification path: it starts the stack, creates and
migrates both databases, runs the linters, the type checker, and both test suites, then
brings up the app with Prometheus and Grafana and probes the endpoints. CI runs the same
script, and this README points at it rather than listing its steps, so the documented
path cannot drift away from the working one. `scripts/verify.sh --dry-run` lists what it
will do; `--down` tears the stack down afterwards.

To run the process directly on the host instead:

```bash
set -a; . ./.env; set +a
TRADINGSYS_DATABASE__HOST=localhost TRADINGSYS_REDIS__HOST=localhost uv run tradingsys
```

The `set -a` line exports the contents of `.env` into your shell. It is needed because
the application never reads secrets from a file: `.env` is consumed by docker compose
and by `scripts/verify.sh`, and nothing else looks at it. `TRADINGSYS_DATABASE__HOST` is
overridden because `config/development.toml` points at the compose service names, which
is what the app container itself needs and what does not resolve from the host.

The process serves `/health`, `/ready`, and `/metrics` on the port configured under
`[observability]` (8000 by default):

```console
$ curl -s localhost:8000/ready
{"status":"pass","duration_ms":3.3,"checks":[
  {"name":"database","status":"pass","duration_ms":3.0,"detail":"pool 0/10 in use"},
  {"name":"redis","status":"pass","duration_ms":1.1}],"time":"..."}
```

To bring up the full local stack including Prometheus and Grafana:

```bash
docker compose up -d --build
```

Grafana is then on <http://localhost:3000> (credentials from `.env`) with the
Prometheus datasource and a service health dashboard already provisioned, and
Prometheus on <http://localhost:9090>. The app container migrates nothing on startup;
it refuses to start against an unmigrated database and says so.

## Configuration model

Configuration is resolved from four layers. Later layers win:

1. **Defaults** declared on the pydantic models in `tradingsys/config/settings.py`.
2. **`config/base.toml`**, committed, non-secret values shared by all environments.
3. **`config/{environment}.toml`**, committed, per-environment overrides. The
   environment is chosen by `TRADINGSYS_APP__ENVIRONMENT`.
4. **Secrets directory and environment variables**, never committed.

Environment variables are prefixed and nested with a double underscore, for example
`TRADINGSYS_DATABASE__PASSWORD` or `TRADINGSYS_VENUES__FOREX__CLIENT_SECRET`.

**Secrets are refused from TOML files.** Any field typed as `SecretStr` that appears in
a config file raises `SecretInConfigFileError` at startup, so a credential cannot be
committed by accident. Supply secrets through environment variables, or through a
secrets directory (`TRADINGSYS_SECRETS_DIR`) into which Docker, Kubernetes, or a secret
manager sidecar projects one file per secret.

**The application never reads `.env`, and this is deliberate.** `.env` exists for docker
compose, which reads it automatically, and for `scripts/verify.sh`, which sources it.
The application itself has no dotenv source at all.

The rule is that the application reads secrets only from the environment or the secrets
directory, never from a file it parses. That rule is valuable precisely because it has
no exceptions. The obvious convenience, letting the application read `.env` in
development and test but refusing it in staging and production, makes the guarantee
conditional on the environment selector being correct, which adds a failure mode on the
day it matters most. The invariant stays absolute and the cost is paid in documentation:
one `set -a` line, and a verification script that does it for you.

Startup validation is strict and non-recoverable: unknown keys, missing required
values, and malformed values all abort the process with a report naming every offending
field.

## Development

```bash
scripts/verify.sh            # the full path: stack, migrations, lint, types, all tests
uv run pytest                # fast loop: unit tests only, no services needed
```

Individual gates, if you want to run one on its own, are `uv run ruff format .`,
`uv run ruff check .`, `uv run mypy`, and `uv run pytest`. They are all invoked by
`scripts/verify.sh`, which is what CI runs, so a green script is the definition of a
green tree.

Integration tests are excluded from a bare `uv run pytest` and are run by the script.
They connect to the database and Redis from the compose stack and **fail rather than
skip** when the services are unreachable, so a green run always means the storage layer
was actually exercised. They cover the append-only trigger firing, concurrent audit
appends producing a single unbroken hash chain, hypertable creation, Decimal round
trips, and full process startup and shutdown.

One caveat when resetting the test schema by hand: TimescaleDB refuses to drop a
hypertable in the same statement as any other object, so drop `ohlcv_bars` and `ticks`
one statement at a time. `alembic downgrade base` already does this correctly.

## Design notes

**Money is never a float.** `Money` wraps a `Decimal` and a `Currency`, refuses float
input at construction, and refuses to add or compare two different currencies. There is
no implicit FX conversion anywhere: a conversion needs a rate, and a rate needs a
timestamp and a source.

**Instruments are venue scoped.** The same symbol has different tick sizes, minimum
sizes, and leverage caps at different venues, so instruments are keyed by
`InstrumentId(venue, symbol)`. Tick size and pip size are stored separately, because
they are different quantities and crypto pairs have no pip at all.

**Sizing is convention neutral.** `quantity_unit` and `contract_size` express whether a
venue sizes in units, lots, or contracts. Risk code converts to base units through
`Instrument.to_base_units` and never assumes a convention.

**The venue interfaces are venue neutral, and this has already paid for itself.** They
were first shaped against OANDA v20. Before any adapter existed the forex venue changed
to cTrader Open API, which differs in transport, authentication, sizing convention, and
position model. No interface changed. The differences are expressed as data:
`VenueCapabilities`, `quantity_unit`, `contract_size`, `PositionMode`, and credential
expiry. No venue field name, identifier format, or enum value appears in the base
classes.

**The audit log is append only and tamper evident.** Every decision the system makes is
recorded with its correlation ID and a SHA-256 hash chained to the previous entry.
`UPDATE` and `DELETE` are blocked by a database trigger, not only by convention. Appends
take a transaction level advisory lock so that concurrent writers produce one chain
rather than a fork, and `previous_hash` is unique so a fork cannot be inserted even if
that lock were bypassed. Reading an entry re-verifies its digest, so a row altered in
place raises instead of being returned.

**Liveness and readiness are different questions.** `/health` answers "should this
process be restarted?" and touches nothing external, because restarting will not fix a
database outage and a restart loop makes it worse. `/ready` answers "should this process
be given work?" and runs every dependency check concurrently under a deadline.

## What is deliberately not here yet

No strategies, no order placement, no market data ingestion, and no venue adapters.
`tradingsys.venues` declares the interfaces those adapters must satisfy and nothing
more. Nothing in this repository trades.
