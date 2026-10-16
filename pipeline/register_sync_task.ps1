# Register the Layer 2 sync as a Windows scheduled task.
#
# Why a scheduled SCRIPT and not a scheduled Claude session: after the cloud
# Routines took over scoring and tailoring, everything left on this machine is
# deterministic -- fold sidecars into the DB, re-render with Word to settle the
# one-page gate, rebuild the dashboard, push. An LLM session would spend
# subscription quota re-deciding decisions already made, and could fail in ways a
# script cannot.
#
# Timing is NOT decided here. `pipeline/schedule.py` derives every trigger time
# from the review windows and passes them in with -At, because a schedule split
# across two files drifts: this script hardcoded 7:45am/3:45pm while the live
# task had carried four triggers since 2026-08-23, so re-running it would have
# silently deleted two of them. One owner for the times, one owner for the
# registration mechanics, and they meet at -At.
# StartWhenAvailable means a laptop that was closed picks the run up on wake --
# tailoring already happened in the cloud, so a missed sync delays ingest only.
#
# Usage:  python pipeline\schedule.py --profile home --apply        (normal path)
#         powershell -ExecutionPolicy Bypass -File pipeline\register_sync_task.ps1 -At 07:00,13:00,20:0000
#         powershell -ExecutionPolicy Bypass -File pipeline\register_sync_task.ps1 -Unregister

param(
    [switch]$Unregister,
    [string]$TaskName = "job-search-os Layer 2 sync",
    # Comma-separated laptop-clock times, e.g. 02:00,05:00,09:00. Required to
    # register; -Unregister needs no times and must keep working without it.
    [string]$At = "",
    # One extra one-time trigger today, for a cloud run already in flight.
    # Windows leaves a FIRED one-time trigger in the list as an inert entry --
    # the next --apply clears it, since Register-ScheduledTask -Force replaces
    # the whole trigger set.
    [string]$Once = ""
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$log = Join-Path $repo "pipeline\sync.log"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "unregistered: $TaskName"
    exit 0
}

$python = (Get-Command python).Source
if (-not $python) { throw "python not found on PATH" }

# cmd /c so both streams land in the log; PowerShell's own redirection would
# wrap native stderr in ErrorRecords and muddy it.
$cmd = "/c `"`"$python`" `"$repo\pipeline\sync.py`" >> `"$log`" 2>&1`""
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $repo

# Each slot repeats every 15 min for an hour, and THAT is the retry mechanism.
# Measured 2026-08-19: Windows' -RestartCount does NOT fire on a nonzero action
# exit code -- a probe task exiting 2 recorded LastTaskResult=2 and was never
# restarted, because "the task failed" means it failed to LAUNCH. So a sync that
# defers (exit 2, a file git must rewrite is open in a viewer) would have sat
# until the next 12-hour slot. Repetition fires regardless of exit code.
# Repeats are nearly free: sync.py exits immediately when the previous run in
# this window already succeeded (see RETRY_WINDOW_MIN in sync.py).
function New-RepeatingDaily([string]$at) {
    $t = New-ScheduledTaskTrigger -Daily -At $at
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 15) `
        -RepetitionDuration (New-TimeSpan -Minutes 60)).Repetition
    return $t
}

if (-not $At) {
    throw ("-At is required to register. Trigger times are owned by " +
           "pipeline/schedule.py -- run 'python pipeline/schedule.py " +
           "--profile <p> --apply' rather than calling this script directly.")
}
$times = $At.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$triggers = @($times | ForEach-Object { New-RepeatingDaily $_ })
if ($Once) {
    # No repetition on the catch-up: it exists to fold ONE in-flight cloud run,
    # and a repeat whose predecessor succeeded short-circuits in sync.py anyway.
    $when = [datetime]::Today.Add([timespan]::Parse($Once))
    $triggers += (New-ScheduledTaskTrigger -Once -At $when)
}

# RestartCount/-Interval covers the task failing to LAUNCH only -- it does NOT
# fire on a nonzero exit code (measured; see the trigger comment above). Kept
# because launch failures are real, but the retry that matters is the trigger
# repetition. -RestartCount throws without -RestartInterval.
# RunOnlyIfNetworkAvailable: every run starts with a fetch, so a run with no
# connectivity should defer to the scheduler rather than fail at step 1.
# 60 min: a cap-6 tailor batch means six Word COM page checks, each with a
# 5-min timeout, and a run killed mid-git-operation is worse than a slow one.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Description (
        "Folds cloud score/tailor sidecars into jobs.sqlite, re-renders resumes " +
        "with Word to settle the one-page gate, rebuilds the dashboard, pushes. " +
        "Deterministic: no LLM call, safe to run twice.") -Force | Out-Null

$desc = ($times -join ", ")
if ($Once) { $desc += "  + one-time $Once today" }
Write-Output "registered: $TaskName  ($desc)"
Write-Output "log: $log"
Write-Output ""
Write-Output "verify:  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Output "run now: Start-ScheduledTask -TaskName '$TaskName'"
