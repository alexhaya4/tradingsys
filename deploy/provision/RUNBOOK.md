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

Create the **block storage volume first**, in the same region, and take DigitalOcean's
**automatic format and mount**. That creates a systemd `.mount` unit, conventionally
`mnt-<volume\x2dname>.mount`, mounted at `/mnt/<volume-name>`, and that unit is the single
owner of the mount.

**There is no `/etc/fstab` entry and none is needed. That is correct, not missing.**
Adding one alongside the `.mount` unit is not redundancy, it is two definitions for one
device, and a typo in the device name there fails silently: the volume simply does not
mount and `df` reports the root disk while everything appears to work. `bootstrap.sh`
therefore adopts the existing mount and refuses to create one, and warns if it finds an
fstab entry for the same path.

Create the droplet with `deploy/provision/cloud-init.yaml` pasted into the **User data**
field, after replacing the SSH public key placeholder. Attach the volume to the droplet.

**Volume sizing, and why the current 20 GB is a first interval rather than a final
answer.** At the measured 243.81 bytes per uncompressed tick row and 21.90 compressed,
and respecting the 80 percent Postgres operating limit, a 20 GB volume holds:

| Combined rate | Runway on 20 GB | Volume for a full 24 months |
|---|---|---|
| 20/s | 11.6 months | 38 GB |
| 30/s | 6.9 months | 57 GB |
| 40/s | 4.6 months | 76 GB |
| 60/s | 2.3 months | 114 GB |
| 80/s | 1.1 months | 152 GB |

The rate is unmeasured until the weekday capture lands, which is why the volume was not
sized to a guess. DigitalOcean volumes resize upward online at 0.10 USD per GB per month,
so growing to 76 GB costs about 7.58 USD a month and is done without downtime. That is
the escape hatch working as designed. What it does mean is that **the volume needs a
decision within a few months of the recorder starting, not within two years.**

## 2. Get the repository onto the host

```bash
ssh tradingsys@YOUR_DROPLET_IP
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
with the ownership the containers need, checks the secrets, installs and enables the
systemd unit, applies the migrations, and starts the stack.

**It does not format and it does not mount.** Removing `mkfs` also removed the only line
in the script that could destroy recorded history.

## 5. Verify

```bash
deploy/provision/healthcheck.sh
```

Ten checks: the volume is mounted and has headroom, the unit is enabled so the stack
returns after a reboot, the containers are up, liveness and readiness answer, the metrics
carry `tradingsys_build_info`, the migrations are at head, the venue is reachable, and the
clock is sane. Each reports why it failed, not only that it did.

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
ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 8000:127.0.0.1:8000 tradingsys@YOUR_DROPLET_IP
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

## Running the 72 hour continuous ingestion run

**Not yet possible, and this section states why rather than implying otherwise.** The run
is the phase 2 exit criterion and needs three things that do not all exist:

1. The cTrader spot subscription, so there is a live forex quote stream. In progress.
2. The ingest process wired into an entry point, configured, and tested. `app/ingest.py`
   is written but constructed by nothing and measures 0 percent coverage.
3. A host that does not suspend. This runbook is that.

When the first two land, the run is a `systemctl` unit like the stack itself rather than a
foreground process in an SSH session, and this section gets the command.

---

## Rebuilding the host from scratch

Destroy the droplet, keep the volume. Create a new droplet from step 1, attach the same
volume, and run steps 2 through 5. `bootstrap.sh` sees the volume is already formatted and
leaves the data alone, so the recorded history survives the rebuild.

This is also the answer to changing region: it is a rebuild, not a migration, for as long
as nothing but the recorder runs on the host.

## Reboots and updates

The systemd unit is enabled, and every service carries `restart: unless-stopped`, so a
reboot returns the stack without anyone logging in. `RequiresMountsFor` on the data root
means the unit refuses to start if the volume is not mounted, rather than letting Postgres
create a fresh database on the root disk, which would be silent.

To deploy a new commit:

```bash
cd /opt/tradingsys
git pull
sudo systemctl restart tradingsys      # rebuilds the app image
deploy/provision/healthcheck.sh
```

Run `scripts/verify.sh` before pushing anything that will land here. It is the only
verification path and CI runs the same script.

---

## The host, as provisioned

Recorded so that a session that did not create it does not have to infer it.

| | |
|---|---|
| Address | 206.189.147.213 |
| Region | DigitalOcean SGP1, Singapore |
| Image | Ubuntu 24.04 |
| Droplet | 2 vCPU, 4 GB RAM, 80 GB disk |
| Volume | 20 GB, XFS |
| Volume mount | `/mnt/tradingsys_db`, owned by `mnt-tradingsys_db.mount` |
| fstab entry | None, deliberately. See step 1 |

The filesystem is XFS rather than ext4, which is what DigitalOcean's automatic format
produces. It makes no difference to anything here: `bootstrap.sh` no longer formats, and
XFS grows online with `xfs_growfs` exactly as the resize path needs.

**Live venue observation should be run from this host and not from a workstation.** Four
TCP connects from the development machine to the cTrader endpoint measured 211, 231, 219
and 6461 ms, with three outright connection failures across several attempts, which is
the same shape as the 23 DNS resolution failures in the last crypto capture. A
measurement taken from there is a measurement of that link.
