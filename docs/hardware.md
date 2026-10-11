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

On Windows, as Administrator:

```powershell
winget install usbipd
usbipd list                                  # find the BUSIDs of 03c3:294f and 0403:6015
usbipd bind --busid <camera-busid>
usbipd bind --busid <ftdi-busid>
usbipd attach --wsl --busid <camera-busid> --auto-attach
usbipd attach --wsl --busid <ftdi-busid> --auto-attach
```

`--auto-attach` re-attaches after a camera power cycle or replug, but only while
that command runs. Start it at log-in the same way the cyclop host keeps WSL
alive (a script in the Startup folder), or use `usbipd policy` to allow the
devices and attach them from a WSL boot script.

In WSL:

```bash
lsusb                                        # 03c3:294f ZWO, 0403:6015 FTDI
sudo cp lib/99-asi.rules /etc/udev/rules.d/  # camera access without root,
                                             # and a larger usbfs buffer
sudo udevadm control --reload && sudo udevadm trigger
sudo usermod -aG dialout $USER               # /dev/ttyUSB0
```

Raise the USB buffer the ASI SDK needs for full frames (the rule file does this
on attach; check with `cat /sys/module/usbcore/parameters/usbfs_memory_mb`, which
should read 200).

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
