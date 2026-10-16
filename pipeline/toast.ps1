# Desktop notification for the unattended sync (see pipeline/winlock.py).
#
# Three rungs, tried in order, because the sync runs unattended and the whole
# point of this channel is that it still works when something is wrong:
#   1. WinRT toast   -- the real thing, no module install needed
#   2. Forms balloon -- works when WinRT refuses an unregistered AppUserModelID
#   3. msg.exe       -- a blocking window; ugly, but impossible to miss
#
# ErrorActionPreference stays Continue on purpose: this is the alert channel, so
# it degrades to the next rung rather than throwing.
#
# Usage: powershell -NoProfile -File pipeline\toast.ps1 -Title "..." -Body "..."

param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Body
)

# PowerShell itself is a registered AUMID, so WinRT will accept it. A made-up id
# is accepted by the API and then silently never displays -- the failure mode
# this script exists to avoid.
$AppId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

function Escape-Xml([string]$s) {
    return $s.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;")
}

# -- rung 1: WinRT toast ----------------------------------------------------
try {
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

    $xml = @"
<toast><visual><binding template="ToastText02">
<text id="1">$(Escape-Xml $Title)</text>
<text id="2">$(Escape-Xml $Body)</text>
</binding></visual></toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
    exit 0
} catch {
    Write-Output "winrt toast failed: $($_.Exception.Message)"
}

# -- rung 2: NotifyIcon balloon --------------------------------------------
try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Warning
    $icon.BalloonTipTitle = $Title
    $icon.BalloonTipText = $Body
    $icon.Visible = $true
    $icon.ShowBalloonTip(20000)
    Start-Sleep -Seconds 6
    $icon.Dispose()
    exit 0
} catch {
    Write-Output "balloon failed: $($_.Exception.Message)"
}

# -- rung 3: msg.exe --------------------------------------------------------
try {
    $null = & msg.exe * /time:60 "$Title -- $Body"
    if ($LASTEXITCODE -eq 0) { exit 0 }
} catch {
    Write-Output "msg.exe failed: $($_.Exception.Message)"
}

exit 1
