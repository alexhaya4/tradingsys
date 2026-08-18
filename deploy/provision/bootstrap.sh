#!/usr/bin/env bash
#
# Idempotent host setup. Run it as the service user from the repository root, as many
# times as you like: it converges the host to the state this repository describes rather
# than applying a one way sequence of steps.
#
#     sudo -u tradingsys deploy/provision/bootstrap.sh --volume-name tradingsys-data
#
# What it does, in order: verifies the block storage volume exists, formats it only if
# it has never been formatted, mounts it permanently, creates the data directories,
# checks that .env is present and has no placeholder values left in it, installs the
# systemd unit, and starts the stack.
#
# WHAT BREAKS IF THIS CHANGES: the systemd unit and docker-compose.prod.yml both expect
# TRADINGSYS_DATA_ROOT to be the mount point and expect ${TRADINGSYS_DATA_ROOT}/postgres
# to exist and be owned by the container's postgres uid. Changing the layout here means
# changing both of those.

set -euo pipefail

SERVICE_USER="${SERVICE_USER:-tradingsys}"
DATA_ROOT="${TRADINGSYS_DATA_ROOT:-/mnt/tradingsys_data}"
VOLUME_NAME=""
SKIP_START=0

usage() {
    cat <<'USAGE'
Usage: bootstrap.sh --volume-name NAME [options]

Required:
  --volume-name NAME    DigitalOcean block storage volume name, exactly as created.
                        The device appears at /dev/disk/by-id/scsi-0DO_Volume_NAME.

Options:
  --data-root PATH      Mount point for the volume. Default: /mnt/tradingsys_data
  --service-user NAME   Unix user that owns the repository and runs the stack.
                        Default: tradingsys
  --skip-start          Converge the host but do not start the stack.
  -h, --help            This message.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --volume-name)  VOLUME_NAME="${2:?--volume-name needs a value}"; shift 2 ;;
        --data-root)    DATA_ROOT="${2:?--data-root needs a value}"; shift 2 ;;
        --service-user) SERVICE_USER="${2:?--service-user needs a value}"; shift 2 ;;
        --skip-start)   SKIP_START=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$VOLUME_NAME" ]]; then
    echo "error: --volume-name is required. It is the name of the DigitalOcean block" >&2
    echo "storage volume, and there is no default because guessing it would mount the" >&2
    echo "wrong disk or none at all." >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEVICE="/dev/disk/by-id/scsi-0DO_Volume_${VOLUME_NAME}"

step() { printf '\n== %s\n' "$1"; }

# ---------------------------------------------------------------------------
step "checking the block storage volume"
# ---------------------------------------------------------------------------
if [[ ! -e "$DEVICE" ]]; then
    echo "error: no block device at $DEVICE" >&2
    echo "The volume must be created and attached to this droplet before bootstrap" >&2
    echo "runs. Available DigitalOcean volumes on this host:" >&2
    ls -1 /dev/disk/by-id/ 2>/dev/null | grep '^scsi-0DO_Volume_' >&2 || echo "  (none)" >&2
    exit 1
fi
echo "found $DEVICE"

# ---------------------------------------------------------------------------
step "formatting the volume, only if it has never been formatted"
# ---------------------------------------------------------------------------
# blkid prints the filesystem type and exits non-zero when there is none. An unformatted
# volume is formatted once; a formatted one is never touched again, because reformatting
# a volume that already holds the database would destroy every recorded tick and there
# is no confirmation prompt in an unattended script that could save us from it.
if FS_TYPE="$(sudo blkid -o value -s TYPE "$DEVICE" 2>/dev/null)"; then
    echo "already formatted as ${FS_TYPE}, leaving it alone"
else
    echo "unformatted, creating ext4"
    sudo mkfs.ext4 -F -L tradingsys "$DEVICE"
fi

# ---------------------------------------------------------------------------
step "mounting at ${DATA_ROOT}"
# ---------------------------------------------------------------------------
sudo mkdir -p "$DATA_ROOT"
# discard passes TRIM through to the volume; noatime removes a write per read, which on
# a database volume is a write we never read back.
FSTAB_LINE="${DEVICE} ${DATA_ROOT} ext4 defaults,nofail,discard,noatime 0 2"
if grep -qsF "$DEVICE" /etc/fstab; then
    echo "already in /etc/fstab"
else
    echo "$FSTAB_LINE" | sudo tee -a /etc/fstab >/dev/null
    echo "added to /etc/fstab"
fi
sudo systemctl daemon-reload
mountpoint -q "$DATA_ROOT" || sudo mount "$DATA_ROOT"
mountpoint -q "$DATA_ROOT" || { echo "error: ${DATA_ROOT} did not mount" >&2; exit 1; }
df -h "$DATA_ROOT" | tail -1

# ---------------------------------------------------------------------------
step "creating the data directories"
# ---------------------------------------------------------------------------
sudo mkdir -p "${DATA_ROOT}/postgres" "${DATA_ROOT}/captures"
# The postgres image runs as uid 999. The bind mount carries host ownership straight
# through, so without this the container cannot write its own data directory.
sudo chown 999:999 "${DATA_ROOT}/postgres"
sudo chown "${SERVICE_USER}:${SERVICE_USER}" "${DATA_ROOT}/captures"
echo "postgres data: ${DATA_ROOT}/postgres"
echo "capture output: ${DATA_ROOT}/captures"

# ---------------------------------------------------------------------------
step "checking secrets"
# ---------------------------------------------------------------------------
if [[ ! -f "${REPO_ROOT}/.env" ]]; then
    echo "error: ${REPO_ROOT}/.env does not exist." >&2
    echo "Copy .env.example to .env and fill in every required value. It is gitignored" >&2
    echo "and is the only place secrets live on this host." >&2
    exit 1
fi
chmod 600 "${REPO_ROOT}/.env"

MISSING=()
for required in POSTGRES_PASSWORD TRADINGSYS_DATABASE__PASSWORD GRAFANA_PASSWORD; do
    value="$(grep -E "^${required}=" "${REPO_ROOT}/.env" | head -1 | cut -d= -f2- || true)"
    if [[ -z "$value" || "$value" == "change-me" ]]; then
        MISSING+=("$required")
    fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "error: these values in .env are absent or still the placeholder:" >&2
    printf '  %s\n' "${MISSING[@]}" >&2
    echo "A stack started with a placeholder password is a stack with a known password." >&2
    exit 1
fi
echo "required secrets present"

# ---------------------------------------------------------------------------
step "installing the systemd unit"
# ---------------------------------------------------------------------------
sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__DATA_ROOT__|${DATA_ROOT}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    "${REPO_ROOT}/deploy/provision/tradingsys.service" \
    | sudo tee /etc/systemd/system/tradingsys.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable tradingsys.service
echo "enabled, so the stack returns after a reboot"

# ---------------------------------------------------------------------------
step "applying database migrations and starting"
# ---------------------------------------------------------------------------
if [[ "$SKIP_START" -eq 1 ]]; then
    echo "skipped by --skip-start. Start with: sudo systemctl start tradingsys"
    exit 0
fi

sudo systemctl start tradingsys.service
export TRADINGSYS_DATA_ROOT="$DATA_ROOT"
cd "$REPO_ROOT"

# The app refuses to start against an unmigrated database and says so, so migrations run
# before the readiness check rather than after it.
docker compose -f docker-compose.yml -f deploy/provision/docker-compose.prod.yml \
    run --rm app alembic upgrade head

echo
echo "stack status:"
docker compose -f docker-compose.yml -f deploy/provision/docker-compose.prod.yml ps
echo
echo "Bootstrap complete. Verify with:"
echo "  deploy/provision/healthcheck.sh"
