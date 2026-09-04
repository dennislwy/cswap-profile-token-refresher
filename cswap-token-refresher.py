#!/usr/bin/env python3
"""Cycle through every cswap-managed Claude profile and refresh stale OAuth tokens.

For each profile: switch to it, inspect Claude's .credentials.json, and if the
refresh token expires within --within-days days, make a tiny `claude` API call.
That call prompts a token refresh only when the access token is already expired,
so this exercises the refresh token but cannot force a rotation on its own. The
originally-active profile is always restored, including on error or Ctrl-C.

Exit code is 0 only if every profile succeeded and the original profile was
restored; otherwise 1.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

REFRESH_WITHIN_DAYS = 4
CLAUDE_PROMPT = "Reply exactly: Claude Code is OK"
CLAUDE_TIMEOUT = 30  # seconds
LOG_RETENTION_DAYS = 14

# Profile number -> email, populated from `cswap list --json`. Display only.
EMAIL_BY_NUMBER: dict[int, str] = {}


def credentials_path() -> str:
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~")
    return os.path.join(base, ".claude", ".credentials.json")


def fmt_profile(number) -> str:
    """`3 (someone@example.com)` when the email is known, else just `3`."""
    email = EMAIL_BY_NUMBER.get(number)
    return f"{number} ({email})" if email else f"{number}"


def run(cmd: list[str], *, echo: bool = True, **kwargs) -> subprocess.CompletedProcess:
    if echo:
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
    print(f"==> cswap switch {fmt_profile(number)}")
    switch = run(
        ["cswap", "switch", str(number)],
        echo=False,
        capture_output=True,
        text=True,
    )
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

    EMAIL_BY_NUMBER.clear()
    EMAIL_BY_NUMBER.update(
        {acc["number"]: acc["email"] for acc in accounts if acc.get("number") is not None and acc.get("email")}
    )

    original = listing.get("activeAccountNumber")
    if original is None:
        for acc in accounts:
            if acc.get("active"):
                original = acc["number"]
                break

    orig_display = fmt_profile(original) if original is not None else "unknown"
    print(f"Found {len(numbers)} profile(s): {', '.join(fmt_profile(n) for n in numbers)}")
    print(f"Original active profile: {orig_display}")
    print()

    results: dict[int, tuple[bool, str]] = {}
    restore_msg = "not attempted"
    restore_ok = False

    try:
        for number in numbers:
            success, message = process_profile(number)
            results[number] = (success, message)
            print(f"Profile {number}: {message}")
            print()
    finally:
        if original is not None:
            print(
                f"==> restoring original active profile: cswap switch {fmt_profile(original)}"
            )
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
        suffix = f", {EMAIL_BY_NUMBER[number]}" if number in EMAIL_BY_NUMBER else ""
        print(f"  Profile {number}: {message}{suffix}")
        if not success:
            all_ok = False
    print(f"  restore -> Profile {orig_display}: {restore_msg}")

    return 0 if (all_ok and restore_ok and len(results) == len(numbers)) else 1


# --- Optional run log -------------------------------------------------------


class _Tee:
    """Write everything to the real stream and to a log file."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        self._stream.write(data)
        self._fh.write(data)
        return len(data)

    def flush(self):
        self._stream.flush()
        self._fh.flush()

    def isatty(self):
        return getattr(self._stream, "isatty", lambda: False)()


def start_log(log_dir: str, retention_days: int):
    """Create log_dir, prune logs older than retention_days, and start teeing
    stdout/stderr to a dated file. Returns the open file handle, or None."""
    try:
        os.makedirs(log_dir, exist_ok=True)
        if retention_days > 0:
            cutoff = time.time() - retention_days * 86400
            for path in glob.glob(os.path.join(log_dir, "cswap-refresher_*.log")):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
        name = datetime.now().strftime("cswap-refresher_%Y-%m-%d_%H%M%S.log")
        fh = open(os.path.join(log_dir, name), "w", encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not start log in {log_dir!r}: {exc}", file=sys.stderr)
        return None

    fh.write(
        f"# cswap-token-refresher transcript start: "
        f"{datetime.now().isoformat(timespec='seconds')}\n"
    )
    fh.flush()
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)
    return fh


def stop_log(fh, code) -> None:
    try:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        fh.write(
            f"# cswap-token-refresher transcript end: "
            f"{datetime.now().isoformat(timespec='seconds')} (exit {code})\n"
        )
        fh.close()
    except OSError:
        pass


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--within-days",
        type=int,
        default=REFRESH_WITHIN_DAYS,
        help="Refresh when the token expires within N days (default: %(default)s).",
    )
    p.add_argument(
        "--prompt",
        default=CLAUDE_PROMPT,
        help="Prompt passed to `claude -p`.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=CLAUDE_TIMEOUT,
        help="Per-profile timeout for the `claude` call, in seconds (default: %(default)s).",
    )
    p.add_argument(
        "--log-dir",
        help="Tee this run's output to a dated file in this directory (created if "
        "needed) and delete files older than --log-retention-days.",
    )
    p.add_argument(
        "--log-retention-days",
        type=int,
        default=LOG_RETENTION_DAYS,
        help="Delete log files older than this many days (default: %(default)s).",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    REFRESH_WITHIN_DAYS = args.within_days
    CLAUDE_PROMPT = args.prompt
    CLAUDE_TIMEOUT = args.timeout

    log_fh = start_log(args.log_dir, args.log_retention_days) if args.log_dir else None

    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        # The finally block inside main() already restored the original profile.
        print("\ninterrupted", file=sys.stderr)
        code = 1
    finally:
        if log_fh is not None:
            stop_log(log_fh, code)

    sys.exit(code)
