# Attach the OMEA camera and sensor-board serial adapter to WSL at Windows
# login, and keep re-attaching them if they drop (replug, WSL restart).
#
# Run from the Startup folder via omea-usbip.cmd. The devices must have been
# shared once from an administrator prompt:
#   usbipd bind --hardware-id 03c3:294f
#   usbipd bind --hardware-id 0403:6015
# after which attaching needs no elevation.

$usbipd = Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"
$devices = @(
    "03c3:294f",    # ZWO ASI294MC Pro
    "0403:6015"     # FTDI RS232 adapter to the Alcor sensor board (COM3)
)
$log = Join-Path $env:LOCALAPPDATA "omea-usbip.log"

function Log($msg) { Add-Content -Path $log -Value "$(Get-Date -Format s) $msg" }

# usbipd attach --wsl needs a running distribution; WSL itself is started by
# the Ubuntu shortcut in the Startup folder
for ($i = 0; $i -lt 60; $i++) {
    wsl.exe --exec /bin/true 2>$null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
Log "WSL up after $($i * 5) s"

# Skywatch holds both devices and makes every attach fail as "Device busy".
# Windows reopens it at log-in if it was open at shutdown ("restartable apps").
$sky = Get-Process Skywatch -ErrorAction SilentlyContinue
if ($sky) {
    Stop-Process -InputObject $sky -Force
    Log "stopped Skywatch (pid $($sky.Id)) to free the devices"
}

foreach ($id in $devices) {
    # one long-lived process per device; --auto-attach re-attaches after a drop
    Start-Process -FilePath $usbipd -WindowStyle Hidden `
        -ArgumentList "attach", "--wsl", "--hardware-id", $id, "--auto-attach"
    Log "started auto-attach for $id"
}
