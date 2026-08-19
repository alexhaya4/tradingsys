#!/usr/bin/env bash
#
# Idempotent host setup. Run it as the service user from the repository root, as many
# times as you like: it converges the host to the state this repository describes rather
# than applying a one way sequence of steps.
#
#     sudo -u tradingsys deploy/provision/bootstrap.sh --volume-name tradingsys-data
#
# What it does, in order: adopts the mount the platform already created, creates the
# data directories, checks that .env is present and has no placeholder values left in
# it, installs the systemd unit, and starts the stack.
#
# It does not format and it does not mount. Whatever already owns the mount keeps
# owning it, and this script verifies and adopts, refusing to proceed when the volume is
# not mounted rather than mounting it a second way.
#
# Two arrangements are both valid and which one you get depends on when the volume was
# attached. Attached during droplet creation, DigitalOcean writes a real .mount unit and
# no fstab entry. Attached afterwards, the operator adds fstab and systemd generates the
# unit from it. The script distinguishes them by SourcePath rather than by the presence
# of both, because a generated unit and the fstab line it came from are one definition,
# and warning about that pair is a false positive that trains an operator to ignore the
# warning. A real unit file plus an fstab entry is genuinely two definitions, and a typo
# in either is how a volume silently fails to mount while df reports the root disk.
#
# Removing mkfs from this script also removes the only line in it that could destroy
# recorded history.
#
# WHAT BREAKS IF THIS CHANGES: the systemd unit and docker-compose.prod.yml both expect
# TRADINGSYS_DATA_ROOT to be the mount point and expect ${TRADINGSYS_DATA_ROOT}/postgres
# to exist and be owned by the container's postgres uid. Changing the layout here means
# changing both of those.

set -euo pipefail

SERVICE_USER="${SERVICE_USER:-tradingsys}"
DATA_ROOT="${TRADINGSYS_DATA_ROOT:-/mnt/tradingsys_db}"
SKIP_START=0

usage() {
    cat <<'USAGE'
Usage: bootstrap.sh [options]

Options:
  --data-root PATH      Where the platform mounted the volume. DigitalOcean's automatic
                        format and mount uses /mnt/<volume-name>.
                        Default: /mnt/tradingsys_db
  --service-user NAME   Unix user that owns the repository and runs the stack.
                        Default: tradingsys
  --skip-start          Converge the host but do not start the stack.
  -h, --help            This message.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-root)    DATA_ROOT="${2:?--data-root needs a value}"; shift 2 ;;
        --service-user) SERVICE_USER="${2:?--service-user needs a value}"; shift 2 ;;
        --skip-start)   SKIP_START=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

step() { printf '\n== %s\n' "$1"; }

# ---------------------------------------------------------------------------
step "adopting the volume the platform mounted"
# ---------------------------------------------------------------------------
if ! mountpoint -q "$DATA_ROOT"; then
    echo "error: ${DATA_ROOT} is not a mount point." >&2
    echo >&2
    echo "This script adopts the mount rather than creating one, because the platform's" >&2
    echo "automatic format and mount already owns it through a systemd .mount unit and a" >&2
    echo "second definition for the same device is ambiguous rather than redundant." >&2
    echo >&2
    echo "Mounts currently under /mnt:" >&2
    findmnt --noheadings --output TARGET,SOURCE,FSTYPE --submounts /mnt 2>/dev/null >&2 || echo "  (none)" >&2
    echo >&2
    echo "Attached DigitalOcean volumes on this host:" >&2
    ls -1 /dev/disk/by-id/ 2>/dev/null | grep '^scsi-0DO_Volume_' >&2 || echo "  (none)" >&2
    echo >&2
    echo "Pass --data-root with the path the platform used, which is /mnt/<volume-name>," >&2
    echo "or attach the volume with the automatic format and mount option." >&2
    exit 1
fi

MOUNT_UNIT="$(systemd-escape --path --suffix=mount "$DATA_ROOT")"
MOUNT_SOURCE="$(systemctl show "$MOUNT_UNIT" -p SourcePath --value 2>/dev/null || true)"
echo "mounted: $(findmnt --noheadings --output SOURCE,FSTYPE,SIZE --target "$DATA_ROOT")"

# Two valid arrangements produce a working mount, and only one of them is a problem.
#
# Attached during droplet creation: DigitalOcean's automatic format and mount writes a
# real .mount unit and no fstab entry. SourcePath is then empty.
#
# Attached afterwards: the platform writes nothing, an operator adds /etc/fstab, and
# systemd generates the .mount unit from it. SourcePath is then /etc/fstab.
#
# Only the first arrangement plus an fstab entry is two definitions of one device, which
# is the ambiguity worth warning about. A generated unit and the fstab line it came from
# are one definition and its generated form, and warning about that pair is a false
# positive that trains an operator to ignore the warning.
if [[ "$MOUNT_SOURCE" == "/etc/fstab" ]]; then
    echo "owned by ${MOUNT_UNIT}, generated by systemd from /etc/fstab"
elif [[ -n "$MOUNT_SOURCE" ]]; then
    echo "owned by ${MOUNT_UNIT}, generated from ${MOUNT_SOURCE}"
else
    echo "owned by ${MOUNT_UNIT}, a unit file rather than a generated one"
    if grep -qsE "[[:space:]]${DATA_ROOT}[[:space:]]" /etc/fstab; then
        # Reported rather than removed: deleting someone's fstab line unattended is
        # worse than telling them it is there.
        echo >&2
        echo "warning: ${MOUNT_UNIT} is a real unit file and /etc/fstab also has an" >&2
        echo "entry for ${DATA_ROOT}. That is two definitions of one device, and a typo" >&2
        echo "in the device name in either is how a volume silently fails to mount while" >&2
        echo "df reports the root disk. Review it:" >&2
        grep -nE "[[:space:]]${DATA_ROOT}[[:space:]]" /etc/fstab >&2
        echo >&2
    fi
fi

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
