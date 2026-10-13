# Connecting the camera to Linux

The OMEA 8C has two links to the host, both USB:

| Link | Windows device | Linux device | Used for |
|---|---|---|---|
| ZWO ASI294MC Pro | `USB\VID_03C3&PID_294F` | libusb (via libASICamera2) | images, sensor cooling |
| FTDI FT231X RS232 adapter | `USB Serial Port (COM3)`, `VID_0403&PID_6015` | `/dev/ttyUSB0` | temperature/humidity, dome heater, focus, iris |

Only one program can own each device. **Stop Skywatch (and its "Dome heating"
window) before attaching them to Linux.** The dome heater keeps running on its
own without any software: it works from thresholds stored in the camera.

## Under WSL2 (the current Windows host)

WSL2 reaches USB devices through [usbipd-win](https://github.com/dorssel/usbipd-win).
The WSL kernel here (6.18) already has `usbip`, `vhci-hcd` and `ftdi_sio`.

On Windows, once, as Administrator:

```powershell
winget install usbipd
usbipd bind --hardware-id 03c3:294f          # ZWO camera
usbipd bind --hardware-id 0403:6015          # FTDI serial adapter
```

Binding persists; after it, attaching needs no elevation. Attaching is kept up
by [windows/omea-usbip.ps1](../windows/omea-usbip.ps1), which waits for WSL and
then runs `usbipd attach --wsl --hardware-id <id> --auto-attach` for each device
in a hidden window (`--auto-attach` re-attaches after a replug or WSL restart,
but only while that process runs). On this host:

* `C:\Users\skycam\omea\omea-usbip.ps1` is the script; it logs to
  `%LOCALAPPDATA%\omea-usbip.log`.
* `windows/omea-usbip.cmd` is in the Startup folder
  (`shell:startup`) next to the "Ubuntu" shortcut that starts WSL at log-in.
* The "Skywatch" startup shortcut was moved to
  `C:\Users\skycam\omea\disabled-startup\`; put it back to return to Skywatch.

To install on another host, copy `omea-usbip.ps1` (with CRLF line endings) to
`%USERPROFILE%\omea\` and `omea-usbip.cmd` to `shell:startup`.

In WSL:

```bash
lsusb                                        # 03c3:294f ZWO, 0403:6015 FTDI
sudo usermod -aG dialout $USER               # /dev/ttyUSB0
# the ASI SDK needs a usbfs buffer larger than one 23 MB frame (default 16 MB);
# without it every exposure fails
echo 'w /sys/module/usbcore/parameters/usbfs_memory_mb - - - - 256' | \
    sudo tee /etc/tmpfiles.d/usbfs.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/usbfs.conf
loginctl enable-linger $USER                 # run the user service from WSL boot
```

The camera's device node is world-writable under WSL, so `lib/99-asi.rules` is
only needed on a native Linux machine.

### USB/IP caveat

The camera is a USB 3 device and a full RAW16 frame is 23 MB. USB/IP carries
it over the Hyper-V virtual network, so expect readout to take longer than on
bare metal (Skywatch reports ~1.9 s). At one frame every 15-35 s that is fine,
but if `omea snap` shows timeouts or corrupt frames, try a lower
`[camera] usb_bandwidth`, or run on a native Linux machine instead. The software
does not care which.

## First light checklist

1. `omea camera-info` lists the camera and its controls.
2. `omea board` prints the firmware (expect 2.20, camera type "OMEA 8"),
   temperatures and humidity matching what Skywatch last showed.
3. `omea iris 16`, then `omea iris 2.8`: you should hear the iris.
4. `omea focus` homes the focus and drives it to 1320 steps.
5. On a clear afternoon, take a frame and compare it with the last Skywatch
   frame (same Sun, terrain fixed):

   ```bash
   omea iris 16
   omea snap --exposure 0.005 --gain 0 --fratio 16 -o new.fits.bz2 --raw new_raw.npy
   omea check-geometry new.fits.bz2 /mnt/d/skycam/<day>/<last Skywatch frame>.fits.bz2
   ```

   It must say `OK: same orientation, no shift`. It also prints the bias: it
   should be ~1930 ADU (Skywatch at offset 30). If the bias comes out at about
   a quarter of that, the SDK is returning 14-bit values unshifted; set
   `[image] adu_scale = 4.0`.
6. `omea run --no-redis` for a while, watching http://localhost:8081/.
7. After the first night, run `alcor_process_night` on it as usual and compare
   the sky-brightness and zeropoint-driven numbers (`ext_*` on a clear night
   should sit near the recent baseline). The demosaic weights are inferred, not
   copied from Skywatch, so colour terms may shift by a few hundredths of a
   magnitude; if they do, that is a new `ALCOR_ZEROPOINTS` epoch in skycam_utils.
