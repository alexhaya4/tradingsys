#!/usr/bin/env bash
#
# Regenerate the cTrader Open API protobuf modules from the vendored schema.
#
# The schema is vendored rather than fetched at build time, and the generated modules
# are committed, so a build needs no protoc and no network. This script is the only
# path that produces them: a transcribed protoc invocation drifts from the one that
# was actually run, and generated code whose command is unknown cannot be regenerated
# with confidence when upstream changes.
#
# Upstream: https://github.com/spotware/openapi-proto-messages
# Pinned commit: 3fd8bddfbe0cfc2ecfda079623dc4e498af11e66 (2025-11-13), MIT licensed.
# That commit is four ahead of upstream tag 91, carrying payload removals and a typo
# fix in the model messages. A commit SHA is pinned rather than a tag because a tag
# can be moved and a SHA cannot.
#
# Usage:
#   scripts/generate_ctrader_messages.sh            verify checksums, then generate
#   scripts/generate_ctrader_messages.sh --fetch    re-download the schema at the
#                                                   pinned commit first, then generate
#   scripts/generate_ctrader_messages.sh --help     this message

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

UPSTREAM_REPO=spotware/openapi-proto-messages
PINNED_COMMIT=3fd8bddfbe0cfc2ecfda079623dc4e498af11e66
MESSAGES_DIR=src/tradingsys/venues/ctrader/messages
PACKAGE=tradingsys.venues.ctrader.messages

# sha256 of each vendored file as downloaded at PINNED_COMMIT. Checked before every
# generation so that a local edit to vendored code is caught here rather than
# discovered later as an unexplained difference from upstream.
declare -A EXPECTED_SHA256=(
    [OpenApiCommonMessages.proto]=9816cd24b340dcc4eb28548eb4dd16735995d2a61889337591e5c4d8021652a2
    [OpenApiCommonModelMessages.proto]=b95d7df670a7e890a53ec08f676198ace7bb0a074a4b07ff0b493c4be00a0dea
    [OpenApiMessages.proto]=a84df9b528e69a494e197d48e21d6291b3d76db31396663a8188721eef9fcf35
    [OpenApiModelMessages.proto]=56338dcac45a149227678b7c23637d64f4e2b607f6649af82394ce5c0957fedf
)

FETCH=false

log() { printf '%s\n' "$*"; }
fail() {
    printf 'generate_ctrader_messages: %s\n' "$*" >&2
    exit 1
}

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

fetch_schema() {
    command -v gh >/dev/null 2>&1 ||
        fail "gh is required for --fetch; it authenticates the GitHub API call"
    local name
    for name in "${!EXPECTED_SHA256[@]}"; do
        gh api "repos/${UPSTREAM_REPO}/contents/${name}?ref=${PINNED_COMMIT}" --jq '.content' |
            base64 -d >"${MESSAGES_DIR}/${name}"
        log "fetched ${name} at ${PINNED_COMMIT:0:12}"
    done
    gh api "repos/${UPSTREAM_REPO}/contents/LICENSE?ref=${PINNED_COMMIT}" --jq '.content' |
        base64 -d >"${MESSAGES_DIR}/LICENSE"
}

verify_checksums() {
    local name actual mismatched=()
    for name in "${!EXPECTED_SHA256[@]}"; do
        [[ -f "${MESSAGES_DIR}/${name}" ]] || fail "vendored schema file is missing: ${name}"
        actual=$(sha256sum "${MESSAGES_DIR}/${name}" | cut -d' ' -f1)
        if [[ "$actual" != "${EXPECTED_SHA256[$name]}" ]]; then
            mismatched+=("${name}: expected ${EXPECTED_SHA256[$name]}, found ${actual}")
        fi
    done
    if ((${#mismatched[@]})); then
        fail "$(
            printf 'the vendored schema does not match the pinned commit:\n'
            printf '  %s\n' "${mismatched[@]}"
            printf '\nEither restore it with --fetch, or, if upstream is being moved on\n'
            printf 'purpose, update PINNED_COMMIT and these checksums together and say so\n'
            printf 'in docs/DECISIONS.md.\n'
        )"
    fi
    log "vendored schema matches the pinned commit ${PINNED_COMMIT:0:12}"
}

generate() {
    uv run python -m grpc_tools.protoc \
        --proto_path="$MESSAGES_DIR" \
        --python_out="$MESSAGES_DIR" \
        --mypy_out="$MESSAGES_DIR" \
        OpenApiCommonMessages.proto \
        OpenApiCommonModelMessages.proto \
        OpenApiMessages.proto \
        OpenApiModelMessages.proto
    log "generated modules and stubs"
}

qualify_imports() {
    # The vendored .proto files import each other by bare filename, which is how
    # upstream ships them and is not ours to change: keeping them byte identical is
    # what makes the checksum check above meaningful. protoc therefore emits bare
    # top-level imports, which cannot resolve from inside a package.
    #
    # Rewriting them to package qualified imports is deterministic and is part of
    # generation rather than a manual step afterwards, so the committed output is
    # always reproducible from this script alone.
    local file rewritten=0
    for file in "$MESSAGES_DIR"/*_pb2.py "$MESSAGES_DIR"/*_pb2.pyi; do
        if grep -qE '^import [A-Za-z]+_pb2 as ' "$file"; then
            sed -i -E "s/^import ([A-Za-z]+_pb2) as /from ${PACKAGE} import \\1 as /" "$file"
            rewritten=$((rewritten + 1))
        fi
    done
    log "qualified cross module imports in ${rewritten} generated files"
}

check_it_imports() {
    # Generated protobuf code can be produced by a protoc whose runtime the installed
    # protobuf package does not accept, and that failure appears only at import time.
    # Importing here means this script cannot report success over output that does not
    # load.
    uv run python -c "
from ${PACKAGE} import OpenApiCommonMessages_pb2, OpenApiMessages_pb2

envelope = OpenApiCommonMessages_pb2.ProtoMessage()
envelope.payloadType = 51
assert OpenApiMessages_pb2.ProtoOAApplicationAuthReq is not None
print('generated modules import and construct cleanly')
"
}

main() {
    while (($#)); do
        case "$1" in
            --fetch) FETCH=true ;;
            -h | --help)
                usage
                return 0
                ;;
            *) fail "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done

    [[ -d "$MESSAGES_DIR" ]] || fail "${MESSAGES_DIR} does not exist"

    if [[ "$FETCH" == true ]]; then
        fetch_schema
    fi
    verify_checksums
    generate
    qualify_imports
    check_it_imports

    log "done. Commit the regenerated modules and stubs together with the schema."
}

main "$@"
