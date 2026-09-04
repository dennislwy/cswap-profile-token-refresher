#!/usr/bin/env python3
"""Cycle through every cswap-managed Claude profile and refresh stale OAuth tokens.

For each profile: switch to it, inspect Claude's .credentials.json, and if the
refresh token expires within REFRESH_WITHIN_DAYS days, make a tiny `claude` API
call. That call prompts a token refresh only when the access token is already
expired, so this exercises the refresh token but cannot force a rotation on its
own. The originally-active profile is always restored, including on error or
Ctrl-C.

Exit code is 0 only if every profile succeeded and the original profile was
restored; otherwise 1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

REFRESH_WITHIN_DAYS = 4
CLAUDE_PROMPT = "Reply exactly: Claude Code is OK"
CLAUDE_TIMEOUT = 30  # seconds


def credentials_path() -> str:
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~")
    return os.path.join(base, ".claude", ".credentials.json")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"==> {' '.join(cmd)}")
    kwargs.setdefault("check", False)
    if kwargs.get("capture_output"):
        # cswap/claude emit UTF-8 (arrows, box drawing); the Windows locale
        # codec (cp1252) can't decode it, so be explicit and lossless-ish.
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)


def cswap_list() -> dict:
    proc = run(
        ["cswap", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`cswap list --json` failed (exit {proc.returncode}): {(proc.stderr or '').strip()}"
        )
    return json.loads(proc.stdout)


def parse_expiry(value) -> datetime | None:
    """Return refresh-token expiry as an aware UTC datetime, or None if unknown."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: milliseconds vs seconds since epoch.
        seconds = value / 1000.0 if value > 1e12 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def refresh_expiry() -> datetime | None:
    path = credentials_path()
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    oauth = data.get("claudeAiOauth") or {}
    return parse_expiry(oauth.get("refreshTokenExpiresAt"))


def call_claude() -> bool:
    """Make a minimal `claude` API call, which prompts a token refresh *if* the
    access token is expired. Returns True if the call itself succeeded."""
    proc = run(
        ["claude", "-p", f'"{CLAUDE_PROMPT}"'],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
    )
    out = (proc.stdout or "").strip()
    if out:
        print(out)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if err:
            print(err, file=sys.stderr)
        return False
    return True


def process_profile(number: int) -> tuple[bool, str]:
    """Switch to `number`, refresh if needed. Returns (success, message)."""
    switch = run(["cswap", "switch", str(number)], capture_output=True, text=True)
    if switch.returncode != 0:
        return False, f"cswap switch failed: {switch.stderr.strip() or switch.stdout.strip()}"

    try:
        expiry = refresh_expiry()
    except FileNotFoundError:
        return False, f"credentials file not found: {credentials_path()}"
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"could not read credentials: {exc}"

    if expiry is None:
        return False, "refreshTokenExpiresAt missing or unparseable"

    now = datetime.now(timezone.utc)
    days_left = (expiry - now).total_seconds() / 86400.0

    if days_left <= 0:
        return False, f"refresh token already expired ({expiry.isoformat()})"

    if days_left > REFRESH_WITHIN_DAYS:
        return True, f"OK (fresh, {days_left:.1f}d left)"

    try:
        ok = call_claude()
    except subprocess.TimeoutExpired:
        return False, f"`claude` call timed out after {CLAUDE_TIMEOUT}s"

    if not ok:
        return False, "`claude` call failed"

    # Confirm by re-reading the credentials file. A plain `claude` call only
    # rotates the refresh token when the access token was actually expired, so
    # `refreshTokenExpiresAt` moving forward is a positive "it refreshed"
    # signal; it staying put just means no refresh was due (access token still
    # valid) -- not a failure.
    try:
        new_expiry = refresh_expiry()
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"could not re-read credentials after refresh: {exc}"

    if new_expiry is None:
        return False, "refreshTokenExpiresAt missing after refresh"

    advance = (new_expiry - expiry).total_seconds()
    if advance <= 1.0:
        return True, (
            f"OK (no refresh needed, refreshTokenExpiresAt unchanged at "
            f"{new_expiry.isoformat()}, {days_left:.1f}d left)"
        )

    new_days_left = (new_expiry - datetime.now(timezone.utc)).total_seconds() / 86400.0
    return True, (
        f"OK (refreshed, was {days_left:.1f}d left, now {new_days_left:.1f}d left, "
        f"+{advance / 86400.0:.1f}d)"
    )


def main() -> int:
    try:
        listing = cswap_list()
    except (RuntimeError, json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    accounts = listing.get("accounts") or []
    numbers = [acc["number"] for acc in accounts if "number" in acc]
    if not numbers:
        print("error: no profiles found in `cswap list --json`", file=sys.stderr)
        return 1

    original = listing.get("activeAccountNumber")
    if original is None:
        for acc in accounts:
            if acc.get("active"):
                original = acc["number"]
                break

    print(f"Found {len(numbers)} profile(s): {', '.join(str(n) for n in numbers)}")
    print(f"Original active profile: {original}")
    print()

    results: dict[int, tuple[bool, str]] = {}
    restore_msg = "not attempted"
    restore_ok = False

    try:
        for number in numbers:
            success, message = process_profile(number)
            results[number] = (success, message)
            print(f"profile {number}: {message}")
            print()
    finally:
        if original is not None:
            print(f"==> restoring original active profile: cswap switch {original}")
            restore = subprocess.run(
                ["cswap", "switch", str(original)],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if restore.returncode == 0:
                restore_ok = True
                restore_msg = "OK"
            else:
                restore_msg = restore.stderr.strip() or restore.stdout.strip() or "failed"
        else:
            restore_msg = "unknown original profile"

    print()
    print("=" * 20 + " Summary " + "=" * 20)
    all_ok = True
    for number in numbers:
        success, message = results.get(number, (False, "not processed"))
        print(f"  profile {number}: {message}")
        if not success:
            all_ok = False
    print(f"  restore -> profile {original}: {restore_msg}")

    return 0 if (all_ok and restore_ok and len(results) == len(numbers)) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # The finally block above already restored the original profile.
        print("\ninterrupted", file=sys.stderr)
        sys.exit(1)
