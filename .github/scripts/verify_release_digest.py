"""Cross-check the ACR-resolved deploy digest against the release body.

Used by `_deploy-release.yaml`'s digest-resolution step (see ADR-023): the
image identity deployed to an environment is resolved from Azure Container
Registry (ACR), keyed by the release tag, not read out of the release body.
That leaves the registry tag itself -- which the CI identity can push -- as
the one surface controlling what deploys. This script closes the second
surface: it requires the release body's own digest to agree with the
registry digest, so forging a deploy needs both GitHub release-edit access
*and* ACR push access, not either alone. See #173 for the vulnerability this
guards against.

The release body arrives on **stdin**, never as an argv value or a shell
interpolation -- it is attacker-controlled Markdown.

Exit codes:
  0  the digests agree (prints ``digest=<registry digest>``)
  2  fail closed -- no usable, agreeing digest (never deploy)
"""

from __future__ import annotations

import argparse
import re
import sys

# IGNORECASE so an uppercase `SHA256:` cannot hide a second digest from the
# check. The trailing negative lookahead stops a run of 65+ hex characters
# from being truncated into a valid-looking 64-character digest.
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}(?![0-9a-f])", re.IGNORECASE)


def body_digests(body: str) -> list[str]:
    """Return the distinct sha256 digests in ``body``, lowercased, first-seen order."""
    seen: list[str] = []
    for match in _DIGEST_RE.findall(body):
        digest = match.lower()
        if digest not in seen:
            seen.append(digest)
    return seen


def verify(body: str, registry_digest: str) -> str:
    """Return ``registry_digest`` if it is well-formed and the body agrees with it.

    Raises ``ValueError`` when: ``registry_digest`` is not a well-formed
    ``sha256:<64 hex>`` value; the body has no digest; the body has more than
    one *distinct* digest; or the body's single digest differs from
    ``registry_digest``. Each message names the remedy.
    """
    normalized = registry_digest.strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        raise ValueError(
            f"registry digest {registry_digest!r} is not a well-formed sha256 digest "
            "-- was the tag ever pushed to ACR by release.yaml?"
        )

    digests = body_digests(body)
    if not digests:
        raise ValueError(
            "release body has no image digest -- was this release ever built? "
            "release.yaml writes the digest into the release body when the draft "
            "is created."
        )
    if len(digests) > 1:
        raise ValueError(
            f"release body has {len(digests)} distinct image digests {digests!r} "
            "-- inspect the release body for a prepended or edited digest and "
            "re-cut the release if it is not trustworthy."
        )
    if digests[0] != normalized:
        raise ValueError(
            f"release body digest {digests[0]} does not match the registry "
            f"digest {normalized} -- the release body was edited after the "
            "release was cut, or the ACR tag was re-pushed. Re-cut the release."
        )
    return normalized


def main() -> int:
    """Parse CLI args, read the body from stdin, print the verdict, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag", required=True, help="release tag being deployed (for messages only)"
    )
    parser.add_argument(
        "--registry-digest", required=True, help="digest resolved from ACR for this tag"
    )
    args = parser.parse_args()

    body = sys.stdin.read()
    try:
        digest = verify(body, args.registry_digest)
    except ValueError as exc:
        raise ValueError(f"{args.tag}: {exc}") from exc
    print(f"digest={digest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as exc:
        # Fail closed: an unverified digest must never reach `az webapp config
        # container set`. Never echo the release body here -- it is
        # attacker-controlled Markdown; only the validated `digest=` line above
        # may reach $GITHUB_OUTPUT.
        print(f"::error::digest verification failed: {exc}", file=sys.stderr)
        sys.exit(2)
