"""Decide whether a release tag is the greatest semver among all releases.

Used by the release-publish auto-deploy guard (``_is-latest-release.yaml``):
auto-deploy (staging + docs) runs only when the just-published release is the
newest version that exists, so publishing an older draft to finalize it does
*not* trigger a deploy.

The comparison is semver-aware via ``packaging.version`` (not ``sort -V``), so a
pre-release ranks below its final release (``v1.0.0-rc.1`` < ``v1.0.0``). The
caller feeds *all* release tags including drafts, so a newer unpublished draft
correctly blocks an older publish.

Stray non-semver tags in the release list (a hand-cut ``nightly`` or ``latest``
tag) are **ignored** when finding the greatest version, so one unparseable tag
does not brick every publish. The guard still fails closed when the answer is
genuinely undecidable: the *published* tag itself is unparseable, the published
tag is absent from the list, or no parseable release exists at all.

Exit codes:
  0  answer determined (prints ``is_greatest=<true|false>`` and ``latest=<tag>``)
  2  fail closed — the latest release could not be determined (never deploy)
"""

from __future__ import annotations

import argparse
import sys

from packaging.version import InvalidVersion, Version


def _parse(tag: str) -> Version:
    """Parse a release tag into a comparable version, tolerating a leading ``v``.

    Raises ``ValueError`` if the tag is not a valid version. For the *published*
    tag this becomes a fail-closed exit; stray non-semver tags in the release
    list are filtered out by ``greatest_tag`` before they reach here.
    """
    try:
        return Version(tag.removeprefix("v"))
    except InvalidVersion as exc:
        raise ValueError(f"unparseable version tag: {tag!r}") from exc


def _parseable(all_tags: list[str]) -> list[str]:
    """Return the subset of ``all_tags`` that parse as a valid version.

    Non-semver tags (e.g. a hand-cut ``nightly``) are dropped so a single stray
    tag cannot make every comparison fail closed.
    """
    parseable = []
    for tag in all_tags:
        try:
            _parse(tag)
        except ValueError:
            continue
        parseable.append(tag)
    return parseable


def greatest_tag(all_tags: list[str]) -> str:
    """Return the parseable tag with the greatest semver in ``all_tags``.

    Stray non-semver tags are ignored. Raises ``ValueError`` only if *no* tag in
    the list is parseable (nothing to compare -> fail closed).
    """
    parseable = _parseable(all_tags)
    if not parseable:
        raise ValueError(f"no parseable release tags among {all_tags!r}")
    return max(parseable, key=_parse)


def is_latest(tag: str, all_tags: list[str]) -> bool:
    """Return True iff ``tag`` is the greatest semver in ``all_tags``.

    Stray non-semver tags in ``all_tags`` are ignored. Raises ``ValueError``
    (caller fails closed) if ``tag`` is absent from ``all_tags``, if ``tag``
    itself is unparseable, or if no parseable release exists to compare against.
    """
    if tag not in all_tags:
        raise ValueError(f"{tag!r} is not among the known releases {all_tags!r}")
    return _parse(tag) == _parse(greatest_tag(all_tags))


def main() -> int:
    """Parse CLI args, print the guard verdict, and return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="the just-published release tag")
    parser.add_argument(
        "--all-tags",
        required=True,
        help="comma-separated list of all release tags (drafts included)",
    )
    args = parser.parse_args()

    all_tags = [t for t in args.all_tags.split(",") if t]
    # Surface (don't silently drop) any non-semver tags we exclude from the
    # comparison, so an unexpected junk tag is visible in the run log.
    ignored = [t for t in all_tags if t not in _parseable(all_tags)]
    if ignored:
        print(
            f"::warning::ignoring non-semver release tags: {', '.join(ignored)}",
            file=sys.stderr,
        )
    latest = greatest_tag(all_tags)
    verdict = is_latest(args.tag, all_tags)
    print(f"is_greatest={'true' if verdict else 'false'}")
    print(f"latest={latest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as exc:
        # Fail closed: the guard could not determine the latest release, so the
        # workflow must not deploy. A non-zero exit fails the guard job red.
        print(
            f"::error::guard could not determine latest release: {exc}", file=sys.stderr
        )
        sys.exit(2)
