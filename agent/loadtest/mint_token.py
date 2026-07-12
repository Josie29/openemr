#!/usr/bin/env python3
#
# mint_token.py — mint a fresh 1-hour SMART access token for the load test.
#
# Why this exists: /chat in the deployed (http-mode) agent requires an
# `Authorization: Bearer <token>` SMART patient-scoped access token. Access
# tokens live ~1 hour and are freely reusable across concurrent requests, so
# the load harness mints ONE here and reuses it for the whole run. The refresh
# token, by contrast, is single-use and rotates on every grant — so we write
# the rotated pair back into prod.bru to keep the Bruno collection green (same
# contract as agent/scripts/mint-fhir-token.sh, but with no OpenEMR password or
# `railway ssh` needed: a plain refresh-token grant is all the load test wants).
#
# Usage:
#   python agent/loadtest/mint_token.py            # prints the access token to stdout
#   python agent/loadtest/mint_token.py --no-write  # do not persist the rotated pair
#
# All diagnostics go to stderr so stdout is the bare token (safe for
# `TOKEN=$(python mint_token.py)`).

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# prod.bru lives beside the agent's Bruno collection; resolve relative to this file
# so the script works regardless of the caller's working directory. It is gitignored
# (secrets), so a git worktree checkout won't have it — set LOADTEST_PROD_BRU to point
# at the primary checkout's copy in that case.
PROD_BRU = Path(
    os.environ.get(
        "LOADTEST_PROD_BRU",
        Path(__file__).resolve().parent.parent / "api-collection" / "environments" / "prod.bru",
    )
)

# Required OAuth vars we expect to parse out of prod.bru.
REQUIRED_VARS = ("oauth_url", "client_id", "client_secret", "refresh_token")


def parse_bru_vars(path: Path) -> dict[str, str]:
    """Parse the ``vars { key: value }`` block of a Bruno environment file.

    Bruno env files use a simple ``key: value`` line format inside a ``vars``
    block. This is a deliberately small parser — just enough to pull the OAuth
    credentials — not a general Bruno grammar.

    Args:
        path: Path to the ``prod.bru`` environment file.

    Returns:
        Mapping of variable name to its (possibly empty) string value.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Bruno env file not found: {path}")

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        # Match "  key: value" (value optional); ignore braces and blank lines.
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def refresh_grant(
    oauth_url: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, str]:
    """Exchange a refresh token for a fresh access/refresh token pair.

    Performs the OAuth2 ``refresh_token`` grant. The server rotates the refresh
    token on each successful grant, so the returned ``refresh_token`` differs
    from the one supplied.

    Args:
        oauth_url: The token endpoint URL.
        client_id: OAuth client id.
        client_secret: OAuth client secret.
        refresh_token: The current (single-use) refresh token.

    Returns:
        The parsed token response, including ``access_token`` and the rotated
        ``refresh_token``.

    Raises:
        RuntimeError: If the endpoint returns a non-200 status or a body that
            is missing an ``access_token``.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        oauth_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(
            f"token endpoint returned HTTP {exc.code}. The committed refresh "
            f"token may be stale — re-run agent/scripts/mint-fhir-token.sh to "
            f"re-seed it. Endpoint said: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach token endpoint {oauth_url}: {exc.reason}") from exc

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("token response did not contain an access_token")
    return payload


def write_back_pair(path: Path, access_token: str, refresh_token: str) -> None:
    """Persist the rotated token pair into a Bruno env file in place.

    Rewrites the ``access_token`` and ``refresh_token`` lines so the collection
    (and any later run) keeps working after the refresh token rotates.

    Args:
        path: Path to the ``prod.bru`` environment file.
        access_token: The freshly minted access token.
        refresh_token: The rotated refresh token.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        if re.match(r"^\s*refresh_token\s*:", line):
            rewritten.append(f"  refresh_token: {refresh_token}")
        elif re.match(r"^\s*access_token\s*:", line):
            rewritten.append(f"  access_token: {access_token}")
        else:
            rewritten.append(line)
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def main() -> int:
    """Mint an access token and print it to stdout.

    Returns:
        Process exit code: 0 on success, 1 on any handled failure.
    """
    parser = argparse.ArgumentParser(description="Mint a 1-hour SMART access token for the load test.")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Do not persist the rotated token pair back into prod.bru.",
    )
    args = parser.parse_args()

    try:
        env = parse_bru_vars(PROD_BRU)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    missing = [key for key in REQUIRED_VARS if not env.get(key)]
    if missing:
        print(
            f"error: prod.bru is missing required var(s): {', '.join(missing)}. "
            f"Populate it (see agent/api-collection/README.md) first.",
            file=sys.stderr,
        )
        return 1

    print("Minting a fresh access token via refresh-token grant...", file=sys.stderr)
    try:
        tokens = refresh_grant(
            env["oauth_url"],
            env["client_id"],
            env["client_secret"],
            env["refresh_token"],
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    access_token = tokens["access_token"]
    rotated_refresh = tokens.get("refresh_token", "")

    if not args.no_write and rotated_refresh:
        write_back_pair(PROD_BRU, access_token, rotated_refresh)
        print(f"  rotated pair written back to {PROD_BRU.name}", file=sys.stderr)

    print(f"  access token ready ({len(access_token)} chars, ~1h TTL)", file=sys.stderr)
    # Bare token on stdout so callers can capture it directly.
    print(access_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
