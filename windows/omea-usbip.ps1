# Watchdog that keeps the OMEA camera and sensor-board serial adapter attached
# to WSL. Started at Windows log-in from the Startup folder (omea-usbip.cmd);
# it runs until log-off.
#
# Every $interval seconds it:
#   1. starts WSL if it is not running (e.g. after Windows Update installed a
#      new WSL and shut it down). With systemd and linger, starting WSL also
#      starts the omea service;
#   2. checks each device is visible inside WSL (lsusb). If one has been
#      missing for two checks in a row, it detaches whatever usbipd still
#      thinks is attached (a WSL restart leaves stale "Attached" entries) and
#      attaches it again.
# This replaces `usbipd attach --auto-attach`, whose processes exit when WSL
# shuts down and leave the devices unattached.
#
# The devices must have been shared once from an administrator prompt:
#   usbipd bind --hardware-id 03c3:294f
#   usbipd bind --hardware-id 0403:6015
# after which attaching needs no elevation.

$usbipd = Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"
$devices = @(
    "03c3:294f",    # ZWO ASI294MC Pro
    "0403:6015"     # FTDI RS232 adapter to the Alcor sensor board (COM3)
)
$interval = 30      # seconds between checks
$log = Join-Path $env:LOCALAPPDATA "omea-usbip.log"

function Log($msg) { Add-Content -Path $log -Value "$(Get-Date -Format s) $msg" }

# one watchdog at a time: a second copy (started by hand, say) just exits
$mutex = New-Object System.Threading.Mutex($false, "Local\omea-usbip-watchdog")
if (-not $mutex.WaitOne(0)) { exit }

# usbipd --auto-attach processes left by the old version of this script would
# fight the watchdog over the devices
Get-CimInstance Win32_Process -Filter "Name='usbipd.exe'" |
    Where-Object { $_.CommandLine -like "*--auto-attach*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Log "stopped old auto-attach process $($_.ProcessId)"
    }

function Test-Wsl {
    # also starts WSL when it is not running
    wsl.exe --exec /bin/true 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-InWsl($id) {
    wsl.exe --exec lsusb -d $id 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Repair-Device($id) {
    # detach first: after a WSL restart usbipd still lists the device as
    # "Attached" to the old instance, and attaching again then fails
    & $usbipd detach --hardware-id $id 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    $out = & $usbipd attach --wsl --hardware-id $id 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Log "attached $id"
    } else {
        Log "attach $id failed: $($out.Trim() -replace '\s+', ' ')"
    }
}

Log "watchdog started"
$wslUp = $true
$fresh = $true      # at start-up, or WSL just came back: repair at once
$missing = @{}
foreach ($id in $devices) { $missing[$id] = 0 }

while ($true) {
    if (-not (Test-Wsl)) {
        if ($wslUp) { Log "WSL not responding; retrying" }
        $wslUp = $false
        Start-Sleep -Seconds $interval
        continue
    }
    if (-not $wslUp) {
        Log "WSL up"
        $fresh = $true
    }
    $wslUp = $true

    # Skywatch holds both devices and makes every attach fail as "Device busy".
    # Windows reopens it at log-in if it was open at shutdown ("restartable apps").
    $sky = Get-Process Skywatch -ErrorAction SilentlyContinue
    if ($sky) {
        Stop-Process -InputObject $sky -Force
        Log "stopped Skywatch (pid $($sky.Id)) to free the devices"
    }

    foreach ($id in $devices) {
        if (Test-InWsl $id) {
            $missing[$id] = 0
            continue
        }
        $missing[$id]++
        # right after start-up or a WSL restart, repair at once; otherwise wait
        # for a second miss, so a momentary drop (a USB reset) is not treated
        # as a lost device
        if ($fresh -or $missing[$id] -ge 2) {
            Log "$id not visible in WSL; re-attaching"
            Repair-Device $id
            $missing[$id] = 0
        }
    }
    $fresh = $false
    Start-Sleep -Seconds $interval
}
