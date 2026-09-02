# cswap-profile-token-refresher

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
   - **more than `REFRESH_WITHIN_DAYS` (4) days out** — nothing to do, reported as
     `OK (fresh)`.
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
python ./cswap-profile-token-refresher.py
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File "./cswap-profile-token-refresher.ps1"
```

### Configuration

| Python constant       | PowerShell parameter | Default                            | Meaning                                       |
| --------------------- | -------------------- | ---------------------------------- | --------------------------------------------- |
| `REFRESH_WITHIN_DAYS` | `-WithinDays`        | `4`                                | Refresh when the token expires within N days. |
| `CLAUDE_PROMPT`       | `-Prompt`            | `Reply exactly: Claude Code is OK` | Prompt passed to `claude -p`.                 |
| `CLAUDE_TIMEOUT`      | `-TimeoutSeconds`    | `30`                               | Per-profile timeout for the `claude` call.    |

## Scheduling

Run it once a day so no profile's refresh token lapses. The script is quiet on a
healthy run and exits non-zero if anything failed, so pointing the output at a
log file is enough to notice problems.

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
0 9 * * * /usr/bin/python3 /path/to/cswap-profile-token-refresher/cswap-profile-token-refresher.py >> /path/to/cswap-refresher.log 2>&1
```

If `cswap` / `claude` are not on the cron `PATH` (common when installed under
`~/.local/bin`, `~/.npm-global/bin`, a Node version manager, etc.), set `PATH`
at the top of the crontab or inline:

```cron
PATH=/usr/local/bin:/usr/bin:/bin:/home/youruser/.local/bin
0 9 * * * cd /path/to/cswap-profile-token-refresher && /usr/bin/python3 cswap-profile-token-refresher.py >> /path/to/cswap-refresher.log 2>&1
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
ExecStart=/usr/bin/python3 /path/to/cswap-profile-token-refresher/cswap-profile-token-refresher.py
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

Register a daily task from an elevated PowerShell prompt. This uses the
PowerShell script; swap in `python` + the `.py` path if you prefer.

```powershell
$dir = "C:\path\to\cswap-profile-token-refresher"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$dir\cswap-profile-token-refresher.ps1`"" `
    -WorkingDirectory $dir

$trigger = New-ScheduledTaskTrigger -Daily -At 9am

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName "cswap token refresher" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Exercise cswap-managed Claude profile refresh tokens daily" `
    -RunLevel Limited
```

By default the task runs only while you are logged on, which is usually what you
want since `cswap` and `claude` act on your user profile. To run whether or not
you are logged on, add `-User $env:USERNAME` and pass `-LogonType Password` (you
will be prompted for your password) to `Register-ScheduledTask`.

Useful follow-ups:

```powershell
Start-ScheduledTask   -TaskName "cswap token refresher"   # run it now
Get-ScheduledTaskInfo -TaskName "cswap token refresher"   # LastRunTime / LastTaskResult (0 = success)
Unregister-ScheduledTask -TaskName "cswap token refresher" -Confirm:$false
```

To capture output, point the action at a wrapper that redirects, e.g.
`-Argument "-NoProfile -ExecutionPolicy Bypass -Command `"& '$dir\cswap-profile-token-refresher.ps1' *>&1 | Out-File -Append '$dir\cswap-refresher.log'`""`.

If `cswap` / `claude` are not on the system `PATH` seen by the scheduler,
prepend their folder inside such a wrapper (`$env:PATH = 'C:\Users\you\AppData\Roaming\npm;' + $env:PATH`)
before invoking the script.

## Example output

```
==> cswap list --json
Found 3 profile(s): 1, 2, 3
Original active profile: 2

==> cswap switch 1
==> claude -p "Reply exactly: Claude Code is OK"
Claude Code is OK
profile 1: OK (no refresh needed, refreshTokenExpiresAt unchanged at 2026-09-09T06:52:26+00:00, 2.1d left)

==> cswap switch 2
profile 2: OK (fresh, 17.1d left)

==> cswap switch 3
profile 3: OK (fresh, 23.3d left)

==> restoring original active profile: cswap switch 2

==================== Summary ====================
  profile 1: OK (no refresh needed, refreshTokenExpiresAt unchanged at 2026-09-09T06:52:26+00:00, 2.1d left)
  profile 2: OK (fresh, 17.1d left)
  profile 3: OK (fresh, 23.3d left)
  restore -> profile 2: OK
```

## License  
MIT