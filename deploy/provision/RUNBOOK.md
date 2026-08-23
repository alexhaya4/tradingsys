# Provisioning runbook

The recorder host is rebuildable from this repository. A host configured by hand is a
single point of failure that cannot be reconstructed, so every step below is either a
committed file or a command that reads one. If you find yourself running something not
written here, add it here.

**Target**, decided 2026-08-18 and recorded in `docs/DECISIONS.md`:

| | |
|---|---|
| Provider | DigitalOcean |
| Region | Singapore (`sgp1`) |
| Droplet | 2 vCPU, 4 GB RAM, 80 GB disk |
| Block storage | Separate volume for `db_data`, from day one |
| Image | Ubuntu LTS |

The region is chosen for Bybit, which is what we actually record and trade. It is
revisitable and costs a rebuild rather than a migration, because nothing but the recorder
runs there and this runbook reproduces the host.

---

## 1. Create the volume and the droplet

Create the **block storage volume first**, in the same region.

**Two arrangements are both correct, and which one you get depends on when the volume is
attached.** Neither is a mistake, and `bootstrap.sh` adopts either.

*Attached during droplet creation.* DigitalOcean's automatic format and mount writes a
real systemd `.mount` unit, conventionally `mnt-<volume\x2dname>.mount`, mounted at
`/mnt/<volume-name>`. **There is no `/etc/fstab` entry and none is needed.** The unit is
the single owner. Adding fstab alongside it would be two definitions of one device.

*Attached after the droplet exists.* The platform writes nothing, so an operator adds the
`/etc/fstab` entry and **systemd generates the `.mount` unit from it**. That is still one
definition: the unit is the generated form of the fstab line, not a competitor to it.
This is the arrangement on the current host.

**How to tell them apart, which is what matters:**

```bash
systemctl show mnt-tradingsys_db.mount -p SourcePath --value
```

`/etc/fstab` means the unit was generated from fstab, so one definition. Empty means a
real unit file, and an fstab entry beside it would then be two. `bootstrap.sh` keys on
this rather than on the presence of both, because warning about a generated unit and the
line it came from is a false positive, and a warning that cries wolf is one an operator
learns to ignore before the real one arrives.

The failure being guarded against is real in the second case: a typo in the device name
fails silently, the volume simply does not mount, and `df` reports the root disk while
everything appears to work.

Create the droplet with `deploy/provision/cloud-init.yaml` pasted into the **User data**
field, after replacing the SSH public key placeholder. Attach the volume to the droplet.

**Volume sizing.** The volume was resized from 20 GB to 60 GB on 2026-08-19, online and
without downtime: `xfs_growfs` took the filesystem from 5,242,880 to 15,728,640 blocks and
`df` reports 60G with 59G available. **Read the table below against 60 GB.**

At the measured 243.81 bytes per uncompressed tick row and 21.90 compressed, and
respecting the 80 percent Postgres operating limit, 48 GB is available and 42.66 GB of it
is left for ticks once WAL, one minute bars and the Postgres baseline are subtracted.
Everything in that list lives inside PGDATA and therefore on this volume; the Docker
images do not, and sit on the droplet's own 80 GB disk.

| Combined rate | Runway on 60 GB | Volume for a full 24 months |
|---|---|---|
| 20/s | 34.7 months | 45 GB |
| 30/s | 22.4 months | 64 GB |
| 40/s | 16.2 months | 82 GB |
| 60/s | 10.0 months | 120 GB |
| 80/s | 6.9 months | 158 GB |

The rate is unmeasured until the weekday capture lands. 60 GB covers the capture, the 72
hour run and phase 3 without a decision in the middle of any of them, which is what it was
bought for. If the measured p95 rate lands above about 30 per second, the choice at that
point is between growing the volume again and shortening retention, and both are to be
priced then rather than defaulted into. Volumes grow online and never shrink.

**Monthly cost: 30 USD.** 24 for the droplet, 6 for the 60 GB volume at 0.10 per GB.

## 2. Get the repository onto the host

```bash
ssh tradingsys@143.198.222.50      # the current host; see the table at the end
git clone https://github.com/alexhaya4/tradingsys.git /opt/tradingsys
cd /opt/tradingsys
git checkout phase-2-market-data
```

The repository is private, so use a deploy key or a personal access token. A deploy key
scoped to this repository is preferable: it cannot be used to reach anything else.

## 3. Secrets

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Every required value must be set and none may remain `change-me`. `bootstrap.sh` refuses
to continue otherwise, because a stack started with a placeholder password is a stack with
a known password.

Generate the database and Grafana passwords on the host rather than pasting them from
elsewhere:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`POSTGRES_PASSWORD` and `TRADINGSYS_DATABASE__PASSWORD` must be the same value: one is
read by the Postgres container, the other by the application.

**The three alerting values are required, not optional**, and `bootstrap.sh` refuses to
continue without them for the same reason it refuses a placeholder password. A recorder
that fails without telling anyone is the configuration this project has already decided
not to operate: the first deployment crash looped for four hours behind a unit reporting
success, and every assertion added since reports through this path.

| Value | Where it comes from |
|---|---|
| `TRADINGSYS_ALERT_TELEGRAM_TOKEN` | BotFather, when the bot is created |
| `TRADINGSYS_ALERT_TELEGRAM_CHAT_ID` | The destination chat, not the bot. Message the bot, then read `https://api.telegram.org/bot<TOKEN>/getUpdates` and take `message.chat.id` |
| `TRADINGSYS_ALERT_DEADMAN_URL` | The ping URL of the external dead man switch. See "Alerting" below |

The cTrader credentials are optional for the crypto-only recorder. Leave them unset until
the forex leg re-enters, and note that the access token expires about 30 days from issue
and the refresh call is not yet written.

## 4. Bootstrap

```bash
deploy/provision/bootstrap.sh                      # adopts /mnt/tradingsys_db
deploy/provision/bootstrap.sh --data-root /mnt/OTHER_VOLUME_NAME
```

Idempotent. Run it again after any change to this directory, and re-run it rather than
editing `/etc/systemd/system/tradingsys.service` in place, or the host stops being
reproducible from the repository.

It adopts the mount the platform created, creates `postgres` and `captures` directories
with the ownership the containers need, checks the secrets, prepares the alerting state
directory at `/var/lib/tradingsys`, puts the service user in the `systemd-journal` group
so the alert handler can read a failed unit's journal, installs and enables every systemd
unit in `deploy/provision`, applies the migrations, starts the stack, and only then
starts the assertion timers.

**Enabling and starting are separated deliberately.** Enabling is what makes the timers
return after a reboot. Starting them is what makes them assert now, and an assertion is
only meaningful once the thing it asserts about exists, so `--skip-start` leaves them
enabled and stopped and says so.

**It installs the directory rather than a list of units.** A list here would be a second
definition of what the deployment consists of, and the way that fails is that a unit is
written, wired, tested, and never reaches the host.

**It does not format and it does not mount.** Removing `mkfs` also removed the only line
in the script that could destroy recorded history.

**It asks the image which uid the data directory must be owned by, and it refuses rather
than repairing an existing cluster.** The uid was written into a comment as 999 for five
days while the image ran as 70, and nothing could catch the disagreement because the
assumption and the code implementing it were the same idea written twice. The image is
now asked, through the tag compose already pins.

On an existing cluster it verifies and stops. Chowning underneath a running postmaster is
not a repair: its open files keep working while every new backend fails to open
`global/pg_filenode.map`, which is how this host spent a day reporting healthy and
serving nothing. If it refuses, it prints the repair, which is a `chown -R` with the
stack stopped. Restarting the `db` service is an alternative, because the image's own
entrypoint runs as root and chowns what it does not own at start.

## 5. Verify

```bash
deploy/provision/healthcheck.sh
```

Eleven checks: the volume is its own device and has headroom, the projection says how
many days of runway are left, the unit is enabled so the stack returns after a reboot,
the containers are up, liveness and readiness answer, the metrics carry
`tradingsys_build_info`, the migrations are at head, the venue is reachable, and the
clock is sane. Each reports why it failed, not only that it did.

**The `db` healthcheck runs a query rather than `pg_isready`.** Everything downstream
reads that one result: compose's `depends_on: service_healthy`, the systemd unit's
`up -d --wait`, and `assert_healthy.sh`. `pg_isready` reports that the postmaster answered
a connection attempt, which stays true of a server whose every backend fails, and on
2026-08-24 it reported "accepting connections" for hours beside "could not open file
global/pg_filenode.map: Permission denied" from every client.

`healthcheck.sh` is the operator's check, run by hand. The two assertions below are the
unattended ones, run by timers, and they are what alert:

```bash
deploy/provision/assert_healthy.sh      # every minute: containers exist and are healthy
deploy/provision/assert_recording.sh    # every five minutes: ticks are landing, volume has room
```

Then run the venue assumptions check, which CI cannot do because venue credentials do not
go into CI:

```bash
set -a; . ./.env; set +a
uv run python scripts/check_venue_assumptions.py
```

## 6. Reaching the operational endpoints

Every port binds to `127.0.0.1` and the firewall allows only SSH. Nothing is exposed.
Reach Grafana, Prometheus and the app through a tunnel from your workstation:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 8000:127.0.0.1:8000 tradingsys@143.198.222.50
```

---

## Running the weekday crypto capture

The capture writes into the block storage volume, not the root disk, so an interrupted
run does not fill `/`.

```bash
cd /opt/tradingsys
set -a; . ./.env; set +a
setsid nohup scripts/arm_crypto_capture.sh '2026-08-24 00:00:00' \
    >> /mnt/tradingsys_db/captures/arm.log 2>&1 &
```

The arming script polls the wall clock rather than sleeping an interval, and
`measure_crypto_rate.py` sets its deadline against wall clock, records coverage per hour
in seconds, and writes its summary every five minutes. All three fixes exist because this
failed three times on a host that suspended. **This host does not suspend**, which is why
it exists, but the fixes stay: they are correct independently of the host, and
`healthcheck.sh` reports the clock so a recurrence is visible rather than inferred.

Read the result while it runs:

```bash
cat /mnt/tradingsys_db/captures/*.json | python3 -m json.tool
```

Coverage per hour is reported in seconds beside each rate. An hour with low coverage is a
gap, not a quiet market, and the two must not be averaged together.

## Alerting

**The alerting path is the only component that cannot verify itself.** Everything else on
this host asserts something and reports through it; it has nothing to report through. No
test establishes that a message reaches a phone in Kenya, so its verification is the
arrival of a message rather than any code we write. That is what the canary is.

### What alerts, and what only logs

| Condition | How it is detected | What happens |
|---|---|---|
| A unit fails, for any reason including dying or timing out | systemd `OnFailure=` | Telegram, with the failed unit's last 30 journal lines |
| Containers missing, stopped, or unhealthy | `assert_healthy.sh`, every minute | The unit fails, so the above |
| No tick recorded for longer than the stall threshold | `assert_recording.sh`, every five minutes | The unit fails, so the above |
| Data volume at or past the alert percentage | `assert_recording.sh`, every five minutes | The unit fails, so the above |
| Venue API failure rates, reconnect churn, gap findings | Application logs and `StreamStats` | Logged only, until the 72 hour run completes |

The last row is a decision rather than an omission. Thresholds set before there is a day
of data are guesses, and a threshold that fires when nothing is wrong is the one that
gets ignored first.

### Repeats and recoveries

A condition alerts once, then at most once every `TRADINGSYS_ALERT_REPEAT_MINUTES`, which
defaults to 30. The assertion runs every minute, so without suppression a four hour
outage would deliver 240 identical messages, and a channel that does that gets muted. A
muted channel is the same as no channel.

When the condition clears, a recovery message is sent. Being told a thing broke and never
being told it healed means going to look, and a channel you have to verify by hand is not
one you trust at three in the morning.

Raising and clearing happen in different places on purpose. A failure is raised from
outside the failing unit, by `OnFailure=`, because a unit that fails by dying cannot
report its own death. A recovery is cleared from inside the assertion, because passing is
an event only the assertion can observe.

### The canary and the dead man switch

`tradingsys-canary.timer` sends one message a day at 06:00 UTC, which is 09:00 in
Nairobi. Each message carries a sequence number, the deployed commit, and the schedule.

**A canary that arrives proves the path works. A canary that does not arrive proves
nothing**, because silence is indistinguishable from a working system with nothing to
report. So on every successful delivery the canary pings an external dead man switch,
which alerts when the ping does not arrive.

The switch is deliberately off this host and off Telegram, because the failures it exists
to catch include this host being gone and the bot token being wrong, and either of those
silences anything that runs here or sends there. One missing ping covers every way the
path can break: host down, network down, timer disabled, unit never installed, token
revoked, chat id wrong, Telegram unreachable.

To set it up:

1. Create a check on the dead man switch service with a period of **1 day** and a grace
   of **25 hours**, notifying by **email**, not Telegram. The channel has to differ from
   the one it is watching.

   **The grace is sized against the canary interval and it is not a detail.** A grace
   shorter than the interval alerts on any single delayed send rather than on a broken
   path, which reproduces inside the alerting layer the exact false positive the whole
   design is built to avoid. One hour against a daily canary was tried on 2026-08-23 and
   corrected the same day.

2. Put its ping URL in `.env` as `TRADINGSYS_ALERT_DEADMAN_URL`. Treat it as a secret:
   anyone holding it can suppress the alert by pinging it themselves. That is a
   suppression risk rather than a data risk, which is why a third party is acceptable
   here and would not be in the trading path.

3. Run the canary once by hand and confirm both halves land:

```bash
set -a; . ./.env; set +a
deploy/provision/canary.sh          # a message on your phone, a ping on the switch
```

4. **Unpause the check, and confirm on the dashboard that it is running rather than
   paused.** A paused check accepts pings and reports nothing when they stop, so it is
   silently useless, which is precisely the failure it exists to prevent: an absence that
   nobody is watching for looks exactly like a system with nothing to report. Pausing is
   the right thing to do while nothing is pinging it, and un-pausing is therefore a step
   in every path that starts the canary, not something to remember afterwards.

   The state to confirm is on the switch's own dashboard, because nothing on this host
   can see it. `canary.sh` gets HTTP 200 from a paused check exactly as it does from a
   running one, so a successful ping is not evidence that anyone is watching.

### Closing the 72 hour run: the cadence changes, both halves together

Daily until the run completes, then weekly. A path broken on Monday and discovered on
Sunday would have covered the whole run.

1. Change `OnCalendar=*-*-* 06:00:00` to `OnCalendar=Mon *-*-* 06:00:00` in
   `deploy/provision/tradingsys-canary.timer`.
2. Widen the dead man switch's period to 7 days and its grace to 8 days.
3. Re-run `deploy/provision/bootstrap.sh --skip-start` and confirm with
   `systemctl list-timers tradingsys-canary.timer`.

Both halves or neither. A weekly canary against a 25 hour grace reports an absence every
week that nothing is wrong, which is how the switch itself gets ignored.

### Testing the path without waiting for a failure

```bash
set -a; . ./.env; set +a

# The last hop, on its own.
deploy/provision/notify.sh "test from $(hostname)" "ignore this"

# The whole handler, as OnFailure would run it.
deploy/provision/alert_unit_failed.sh tradingsys-health.service

# A real failure, end to end: stop a container and wait for the assertion.
docker compose -f docker-compose.yml -f deploy/provision/docker-compose.prod.yml stop redis
journalctl -u tradingsys-health.service -f          # the assertion fails, the alert goes
docker compose -f docker-compose.yml -f deploy/provision/docker-compose.prod.yml start redis
                                                    # then the recovery arrives
```

The last one is the only test that proves the wiring rather than the pieces, and it is
worth running once after any change to the units.

## Continuous operation, and the run that is the exit criterion

**These are two different things and this section keeps them apart, because "72 hours
elapsed" and "the phase 2 exit criterion is met" are different claims.**

*Continuous operation starts as soon as a deploy verifies.* The stack runs under
`tradingsys.service` and records crypto continuously. Nothing needs to be started by
hand: the ingest process is assembled at startup and supervised, and the two assertion
timers report when it stops. Crypto spread history only accumulates forward, because
Bybit publishes none of it, so every hour not recording is permanently lost and there is
no reason to wait.

*The exit criterion run is not this.* `SPEC.md` section 8 requires continuous ingestion
across **both** venues for 72 hours. The forex leg contributes backfill history and no
live quotes, because a stream without reconnection would die on its first disconnection
and `app/assembly.py` deliberately does not wire one. So the criterion run happens after
reconnection with resynchronisation lands, with both legs live, and a crypto-only run
before then is operational evidence rather than the gate.

What the crypto-only run is worth, which is not nothing: it exercises supervision,
alerting, storage growth and the reconnect counters in `StreamStats` under real
conditions, before the run that counts depends on all four.

Watch it with the assertions rather than by tailing logs:

```bash
systemctl list-timers 'tradingsys-*' --no-pager
journalctl -u tradingsys-recording.service --since '1 hour ago' --no-pager
```

---

## Rebuilding the host from scratch

Destroy the droplet, keep the volume. Create a new droplet from step 1, attach the same
volume, and run steps 2 through 5. `bootstrap.sh` sees the volume is already formatted and
leaves the data alone, so the recorded history survives the rebuild.

This is also the answer to changing region: it is a rebuild, not a migration, for as long
as nothing but the recorder runs on the host.

## Reboots and updates

The systemd unit is enabled, so a reboot returns the stack without anyone logging in.
`RequiresMountsFor` on the data root means the unit refuses to start if the volume is not
mounted, rather than letting Postgres create a fresh database on the root disk, which
would be silent.

**The restart policies differ by service and the difference is deliberate.** The
datastores carry `restart: unless-stopped` from the base compose file. The app carries
`restart: on-failure:20` from the production overlay, because its two failure modes are
not the same thing: a venue outage is transient and retrying is right, while a
configuration or wiring error is deterministic and infinite retry converts a loud failure
into a quiet one. Twenty attempts against Docker's own backoff spans roughly fifteen
minutes, after which the container stops and `assert_healthy.sh` reports it.

### Deploying a new commit

Every step states what it asserts. The sequence exists in this shape because a deploy
that asserts less than it appears to is what cost four hours on 2026-08-19.

```bash
ssh tradingsys@143.198.222.50
cd /opt/tradingsys

# 1. Stop the assertions before changing what they assert about, or they fire during
#    your own deploy window and alert you about yourself.
sudo systemctl stop tradingsys-health.timer tradingsys-recording.timer

# 2. Pull, and confirm what you have rather than what you expect.
git fetch origin
git status                                        # on phase-2-market-data, clean
git pull --ff-only origin phase-2-market-data
git log --oneline -3

# 3. Add any new required value to .env before bootstrap checks for it. A deploy that
#    introduces one stops here otherwise, halfway through, which is a worse place to
#    discover it than before starting. The alerting values arrived on 2026-08-23.
grep -c . .env
$EDITOR .env

# 4. Re-run bootstrap without starting. It installs every unit in deploy/provision,
#    re-checks that .env holds no placeholder, and converges the host. Idempotent.
#
#    --skip-start now means what it says. Until 2026-08-24 this step enabled the
#    assertion timers with --now, so it started them here, undoing step 1 two steps
#    before the stack came back, and they then failed every minute against a stack
#    that was deliberately down. The timers are enabled and left stopped; step 9
#    starts them.
deploy/provision/bootstrap.sh --skip-start

# 5. Migrations before the restart, never after. The app refuses to start against an
#    unmigrated database, and the next step blocks until the app is healthy, so a
#    pending migration would present as a start timeout rather than as itself.
set -a; . ./.env; set +a
export TRADINGSYS_DATA_ROOT=/mnt/tradingsys_db
docker compose -f docker-compose.yml -f deploy/provision/docker-compose.prod.yml up -d db
docker compose -f docker-compose.yml -f deploy/provision/docker-compose.prod.yml \
    run --rm app alembic upgrade head

# 6. Restart, not start. On an already active oneshot, start is a no-op that returns
#    zero and leaves the old image running. This rebuilds and blocks on --wait until
#    every service with a healthcheck reports healthy.
sudo systemctl restart tradingsys.service
systemctl status tradingsys.service --no-pager

# 7. Assert by hand before handing the assertions back to their timers.
deploy/provision/assert_healthy.sh
deploy/provision/assert_recording.sh

# 8. Confirm the stack is recording, which is a different fact from being healthy.
docker compose -f docker-compose.yml -f deploy/provision/docker-compose.prod.yml \
    exec -T db psql -qtAX -U tradingsys -d tradingsys \
    -c "SELECT source, count(*), max(ts) FROM ticks GROUP BY source"

# 9. Hand the assertions back, and confirm they are actually scheduled.
sudo systemctl start tradingsys-health.timer tradingsys-recording.timer
systemctl list-timers 'tradingsys-*' --no-pager   # NEXT populated for all three

# 10. The operator's full check, and the venue drift check CI cannot do.
deploy/provision/healthcheck.sh
uv run python scripts/check_venue_assumptions.py

# 11. Prove the alerting path rather than assuming the deploy left it working, and
#     confirm the dead man switch is running rather than paused. A paused check accepts
#     the ping and reports nothing when it stops.
deploy/provision/canary.sh
systemctl list-timers tradingsys-canary.timer --no-pager
#     Then look at the switch's dashboard. Nothing on this host can see its state.
```

Step 11 is not optional on a deploy that changed anything under `deploy/provision`. The
alerting path is the one component that cannot verify itself, so a deploy that leaves it
broken looks identical to a deploy that leaves it working until the day something else
fails and nothing arrives.

Step 7 before step 9 is deliberate. The recording assertion fires as soon as its timer
starts, and on a host whose database is empty because the app has been crash looping, it
would fail on the first pass while the stream was still establishing itself. Confirming
by hand that ticks are landing, and only then handing the assertion to its timer, is the
difference between a first alert that means something and one that trains you to ignore
the next.

Rollback is `git checkout <previous sha>` and the restart step again. It is only that simple when
no migration was applied in between: a schema change is not symmetric, and the downgrade
has to be run deliberately before the older image starts against a newer schema.

Run `scripts/verify.sh` before pushing anything that will land here. It is the only
verification path and CI runs the same script.

---

## The host, as provisioned

Recorded so that a session that did not create it does not have to infer it.

**The address is an observation, not a configuration value.** Nothing in this repository
sets it, no test can check it, and CI cannot know it: a droplet is created by a person in
a panel and the number that comes back is a fact about the world. It therefore lives in
this table with the other observed state rather than in prose, where a stale copy reads
like an instruction. The first droplet, at `206.189.147.213`, was destroyed and rebuilt
because it had been created without pasting `cloud-init.yaml` and so had neither Docker
nor the service user.

| | |
|---|---|
| Address | 143.198.222.50 |
| Region | DigitalOcean SGP1, Singapore |
| Image | Ubuntu 24.04 |
| Droplet | 2 vCPU, 4 GB RAM, 80 GB disk |
| Volume | 60 GB, XFS. Resized from 20 GB on 2026-08-19 |
| Volume mount | `/mnt/tradingsys_db`, owned by `mnt-tradingsys_db.mount` |
| Mount ownership | `mnt-tradingsys_db.mount`, generated by systemd from `/etc/fstab`, because the volume was attached after droplet creation. `SourcePath` is `/etc/fstab`. One definition. See step 1 |
| Monthly cost | 30 USD: 24 droplet, 6 volume |

The filesystem is XFS rather than ext4, which is what DigitalOcean's automatic format
produces. It makes no difference to anything here: `bootstrap.sh` no longer formats, and
XFS grows online with `xfs_growfs` exactly as the resize path needs.

**Live venue observation should be run from this host and not from a workstation.** Four
TCP connects from the development machine to the cTrader endpoint measured 211, 231, 219
and 6461 ms, with three outright connection failures across several attempts, which is
the same shape as the 23 DNS resolution failures in the last crypto capture. A
measurement taken from there is a measurement of that link.
