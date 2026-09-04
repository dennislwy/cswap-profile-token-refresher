# cswap-token-refresher

Cycle through every [`cswap`](https://github.com/realiti4/claude-swap)-managed Claude profile, make a
tiny `claude` API call on each one whose refresh token is close to expiring, then
restore the profile you started on.

Useful as a scheduled job so that no profile's refresh token goes stale from
disuse.

## What it does

1. Reads the profile list and the currently active profile from
   `cswap list --json`.
2. For each profile: `cswap switch <n>`, then read Claude's `.credentials.json`
   (location below) and look at `claudeAiOauth.refreshTokenExpiresAt`:
   - **already expired** — print an error and move on to the next profile.
   - **more than 4 days out** (`--within-days` / `-WithinDays`) — nothing to do,
     reported as `OK (fresh)`.
   - **within 4 days** — run `claude -p "Reply exactly: Claude Code is OK"`, then
     re-read `.credentials.json` to confirm. If `refreshTokenExpiresAt` moved
     forward the token was rotated (`OK (refreshed, ...)`); if it did not, the
     access token was still valid so no refresh was due, which is also fine
     (`OK (no refresh needed, ...)`). Only a failed `claude` call counts as a
     failure.
3. Restores the originally-active profile — always, including on error or Ctrl-C.

Note: a `claude` call only triggers an OAuth refresh when the current access
token has already expired; the script cannot force a rotation on its own. It
keeps the refresh token exercised so it does not lapse from disuse.

A failure on one profile is logged and the run continues with the next. The
script exits `0` only if every profile succeeded **and** the original profile was
restored; otherwise `1`.

Credentials file:

- Windows: `%USERPROFILE%\.claude\.credentials.json`
- Linux / macOS: `~/.claude/.credentials.json`

## Requirements

- [`cswap`](https://github.com/realiti4/claude-swap) (claude-swap) and `claude`
  on `PATH`. `cswap` manages the Claude profiles this script cycles through; see
  its repo for installation.

## Usage

```sh
# Windows / Linux / macOS
python ./cswap-token-refresher.py
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File "./cswap-token-refresher.ps1"
```

The two scripts take equivalent options and produce the same output.

### Options

| Python flag            | PowerShell parameter | Default                            | Meaning                                                     |
| ---------------------- | -------------------- | ---------------------------------- | ---------------------------------------------------------- |
| `--within-days`        | `-WithinDays`        | `4`                                | Refresh when the token expires within N days.             |
| `--prompt`             | `-Prompt`            | `Reply exactly: Claude Code is OK` | Prompt passed to `claude -p`.                             |
| `--timeout`            | `-TimeoutSeconds`    | `30`                               | Per-profile timeout for the `claude` call, in seconds.   |
| `--log-dir`            | `-LogDir`            | _(off)_                            | Also write this run's output to a dated file here.       |
| `--log-retention-days` | `-LogRetentionDays`  | `14`                               | Delete log files older than this many days before a run. |

### Logging

With `--log-dir` / `-LogDir` set, each run is copied to
`<dir>/cswap-refresher_YYYY-MM-DD_HHMMSS.log` (one file per run) as well as the
console. At the start of every run, log files in that directory older than
`--log-retention-days` / `-LogRetentionDays` (default 14) are deleted. Without
the option nothing is written to disk. `setup.ps1` (below) turns this on by
default, pointing at a `logs/` folder in the repo.

## Scheduling

Run it once a day so no profile's refresh token lapses. The script is quiet on a
healthy run and exits non-zero if anything failed, so keeping the output around
is enough to notice problems — either redirect it (examples below) or use the
built-in `--log-dir` / `-LogDir` option (see [Logging](#logging)).

In every example below, replace `/path/to` (or `C:\path\to`) with the directory
you cloned this repo into. `cron` and Task Scheduler both run with a minimal
environment, so make sure `cswap` and `claude` resolve — either they are in a
system-wide location already on `PATH`, or you add their directory explicitly
(shown below).

### Linux / macOS (cron)

Edit your crontab:

```sh
crontab -e
```

Add one line — this runs every day at 09:00 and appends output to a log:

```cron
0 9 * * * /usr/bin/python3 /path/to/cswap-token-refresher/cswap-token-refresher.py >> /path/to/cswap-refresher.log 2>&1
```

If `cswap` / `claude` are not on the cron `PATH` (common when installed under
`~/.local/bin`, `~/.npm-global/bin`, a Node version manager, etc.), set `PATH`
at the top of the crontab or inline:

```cron
PATH=/usr/local/bin:/usr/bin:/bin:/home/youruser/.local/bin
0 9 * * * cd /path/to/cswap-token-refresher && /usr/bin/python3 cswap-token-refresher.py >> /path/to/cswap-refresher.log 2>&1
```

Verify with `crontab -l`. On macOS the first run may prompt to grant `cron` (or
your terminal) permission to run in the background — allow it.

### Linux (systemd timer, alternative)

If you prefer systemd, create `~/.config/systemd/user/cswap-refresher.service`:

```ini
[Unit]
Description=Refresh cswap-managed Claude profile tokens

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /path/to/cswap-token-refresher/cswap-token-refresher.py
```

and `~/.config/systemd/user/cswap-refresher.timer`:

```ini
[Unit]
Description=Daily cswap token refresh

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then enable it (and `loginctl enable-linger $USER` so it runs while you are
logged out):

```sh
systemctl --user daemon-reload
systemctl --user enable --now cswap-refresher.timer
systemctl --user list-timers cswap-refresher.timer
journalctl --user -u cswap-refresher.service   # view past runs
```

### Windows (Task Scheduler)

Run `setup.ps1` from a PowerShell prompt (no elevation needed for a per-user
task):

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

It registers — or replaces — a daily task named **cswap token refresher** that:

- runs `cswap-token-refresher.ps1` from this folder at a time you choose
  (prompted; default 4:30 AM),
- catches up as soon as possible after a missed start (machine asleep / off),
- only starts when a network connection is available,
- is allowed to start and keep running on battery power,
- launches hidden — no console window, and
- logs each run to `logs\` in the repo, pruning files older than 14 days.

When it finishes it offers to run the task once so you can confirm it works.

Handy switches: `-At "6:00"` skips the time prompt, `-RunNow` triggers the task
right after registering, `-NoLog` registers without logging,
`-WindowStyle Minimized|Normal` changes the window, `-LogRetentionDays <n>`
changes log retention. `Get-Help .\setup.ps1 -Detailed` has the rest.

The task runs only while you are logged on, which is usually what you want since
`cswap` and `claude` act on your user profile.

Useful follow-ups:

```powershell
Start-ScheduledTask   -TaskName "cswap token refresher"   # run it now
Get-ScheduledTaskInfo -TaskName "cswap token refresher"   # LastRunTime / LastTaskResult (0 = success)
Unregister-ScheduledTask -TaskName "cswap token refresher" -Confirm:$false
```

<details>
<summary>Registering the task by hand instead</summary>

```powershell
$dir = "C:\path\to\cswap-token-refresher"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\cswap-token-refresher.ps1`" -LogDir `"$dir\logs`" -LogRetentionDays 14" `
    -WorkingDirectory $dir

$trigger = New-ScheduledTaskTrigger -Daily -At "4:30AM"

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "cswap token refresher" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Exercise cswap-managed Claude profile refresh tokens daily" `
    -RunLevel Limited
```

Swap in `python` + the `.py` path if you prefer. To run whether or not you are
logged on, add `-User $env:USERNAME` and `-LogonType Password` (you will be
prompted for your password) to `Register-ScheduledTask`. If `cswap` / `claude`
are not on the `PATH` the scheduler sees, point the action at a wrapper that
prepends their folder (`$env:PATH = 'C:\Users\you\AppData\Roaming\npm;' + $env:PATH`)
before invoking the script.
</details>

## Example output

```
==> cswap list --json
Found 3 profile(s): 1 (email1@example.com), 2 (email2@example.com), 3 (email3@example.com)
Original active profile: 2 (email2@example.com)

==> cswap switch 1 (email1@example.com)
==> claude -p "Reply exactly: Claude Code is OK"
Claude Code is OK
Profile 1: OK (no refresh needed, refreshTokenExpiresAt unchanged at 2026-09-09T06:52:26+00:00, 2.1d left)

==> cswap switch 2 (email2@example.com)
Profile 2: OK (fresh, 17.1d left)

==> cswap switch 3 (email3@example.com)
Profile 3: OK (fresh, 23.3d left)

==> restoring original active profile: cswap switch 2 (email2@example.com)

==================== Summary ====================
  Profile 1: OK (no refresh needed, refreshTokenExpiresAt unchanged at 2026-09-09T06:52:26+00:00, 2.1d left), email1@example.com
  Profile 2: OK (fresh, 17.1d left), email2@example.com
  Profile 3: OK (fresh, 23.3d left), email3@example.com
  restore -> Profile 2 (email2@example.com): OK
```

## License  
MIT