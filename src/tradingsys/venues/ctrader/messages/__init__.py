"""Vendored cTrader Open API protobuf schema, and the modules generated from it.

**This directory is vendored third party code. Do not edit anything in it by hand.**

Upstream
    https://github.com/spotware/openapi-proto-messages

Pinned commit
    ``3fd8bddfbe0cfc2ecfda079623dc4e498af11e66``, dated 2025-11-13, MIT licensed.
    The licence is vendored beside the schema as ``LICENSE``.

    That commit sits four ahead of upstream's own tag ``91``, carrying payload
    removals and a typo fix in the model messages. A commit SHA is pinned rather
    than a tag because a tag can be moved and a SHA cannot, and vendored code whose
    upstream version is unknown becomes unmaintainable the first time upstream
    changes.

Regeneration
    ``scripts/generate_ctrader_messages.sh``, which is the only path that produces
    these modules. It verifies the vendored ``.proto`` files against the sha256
    digests recorded for the pinned commit before generating anything, so a local
    edit to vendored code is caught there rather than discovered later as an
    unexplained difference from upstream. Pass ``--fetch`` to re-download the schema
    at the pinned commit first.

    To move to a newer upstream, change ``PINNED_COMMIT`` and the digests in that
    script together, run it with ``--fetch``, and record the move in
    ``docs/DECISIONS.md``. Changing one without the other is what the digest check
    exists to prevent.

Why the schema is vendored and the generated modules are committed
    A build then needs neither ``protoc`` nor the network, which is what makes it
    reproducible. The generated modules are type checked under ``mypy --strict``
    like everything else: stubs are produced by ``mypy-protobuf`` rather than the
    generated code being excluded from the type check, because a standard that
    holds except where it is inconvenient is not a standard.

The ``.proto`` files are kept byte identical to upstream. The only transformation
applied to generated output is rewriting protobuf's bare cross module imports to
package qualified ones, because upstream's files import each other by bare
filename and protoc's output cannot otherwise resolve from inside a package. That
rewrite lives in the generation script, not in a manual step, so committed output
is always reproducible from the script alone.
"""

from __future__ import annotations

__all__: list[str] = []
