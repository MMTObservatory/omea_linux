# Bringing OMEA up on Debian 13

A self-contained handoff for rebuilding the OMEA all-sky camera host
(`new-skycam`) from a fresh Debian 13 (trixie) install, replacing Windows 11 +
WSL2. omea itself does not care which it runs on: everything here is plumbing.

Read [Back up first](#1-back-up-first) before touching the machine: wiping
the NVMe is one-way, and several things that matter are not in git.

The host was last surveyed on 2026-11-09 (Ubuntu 24.04 under WSL 3.0.5,
usbipd-win 5.3).

## What the system is

| Piece | Today (WSL2) | On Debian |
|---|---|---|
| Machine | Intel NUC11PAHi7, 32 GB, Lexar 1 TB NVMe (`C:`), Windows 11 Pro | same hardware; Debian on the NVMe |
| Camera | ZWO ASI294MC Pro, USB 3, `03c3:294f`, through usbipd-win | plain USB; `lib/99-asi.rules` |
| Sensor board | Alcor board behind an FTDI FT231X RS232 adapter, `0403:6015`, `/dev/ttyUSB0` (Windows COM3) | the same `/dev/ttyUSB0`; `dialout` group |
| Data drive | WD Elements 18 TB USB (`1058:25a3`), one NTFS partition "Skycam Data" (`D:`), BitLocker off; 9.8 TB used, 6.7 TB free; `/mnt/d` via drvfs | mounted directly at `/mnt/d` (see [The data drive](#the-data-drive)) |
| Network | WSL `networkingMode=mirrored`: Ethernet (Intel I225-V) static 128.196.100.174/24, gateway 128.196.100.1, DNS 128.196.100.19 and 8.8.8.8 | the same static address on the I225-V (`igc` driver) |
| Start-up | Windows log-in, then the Startup folder starts WSL and `windows/omea-usbip.ps1` (the USB watchdog); `systemd --user` with linger starts `omea` | systemd at boot, a system service |
| omea | `~/omea_linux` ([MMTObservatory/omea_linux](https://github.com/MMTObservatory/omea_linux)), user service `~/.config/systemd/user/omea.service`, config `~/omea_linux/config.toml` | same checkout and config; system service |
| skycam_utils | `~/skycam-utils` ([tepickering/skycam-utils](https://github.com/tepickering/skycam-utils)), installed editable | same |
| Python | Miniforge in `~/conda` (conda 26.10), env `skycam`: Python 3.14.10, numpy 2.5.4, astropy 8.0.1, scipy 1.18.1, photutils 3.0.0, matplotlib 3.11.2, scikit-image 0.26.0, pandas 3.0.5, reproject 0.21.0, pillow 13.0.0, pyserial 3.5, redis 8.1.0 | same |
| cron (user `skycam`) | `latest_movie.sh` every minute (last 150 JPEGs, to `latest_movie.mp4`); `daily_movies.sh` at 13:15 (yesterday's movies, `alcor_process_night`, keograms) | same crontab |
| Web | lighttpd on 80 and 443 (`/etc/lighttpd/server.pem`; dir-listing, ssl, userdir enabled), `/var/www/html/skycam` (owned by `skycam`) with symlinks `archive -> /mnt/d/skycam` and `movies -> /mnt/d/skycam/movies`; omea's status page on 8081 | same packages and files |
| Redis | publishes to `redis.mmto.arizona.edu` with no password (`REDISPW` unset, no `~/.config/omea/env`) | same |
| Clock | follows Windows; ~1 s slow when last checked | chrony against `mmt-ntp.mmto.arizona.edu` |
| Weather log | omea appends `DataOut_Weather.csv` to Skywatch's old path, `C:\Users\skycam\Documents\skywatch\Heater\` (192 MB, history back to 2024), because `config.toml` doesn't set `weather_csv` and that is the default | move it to `/mnt/d/skycam/` |
| Git push | HTTPS remotes, credentials from Windows' Git Credential Manager | `gh auth login` (or SSH remotes) |
| Monitoring | Nagios (elsewhere) warns when frames stop coming in; nothing runs on this host for it | unchanged if the hostname and address stay the same |

Not worth carrying over: `pure-ftpd` (Skywatch's localhost FTP, unused),
`postfix` (installed but never configured; cron's mail to it fails, harmlessly),
`wireshark`, `iraf`, `saods9`, `~/wireshark.pcapng`, `~/errors.txt*`.

## 1. Back up first

Do this on the running system, onto the data drive, in daytime (the cooler is
off then, and the dome heater runs from thresholds stored in the camera with no
software at all).

1. **Files not in git.** Both repos are pushed; check with `git status` in each.
   The rest:

   ```bash
   B=/mnt/d/migration-backup && mkdir -p $B
   cp ~/omea_linux/config.toml $B/
   crontab -l > $B/crontab.txt
   tar czf $B/home-extras.tgz -C ~ .ssh public_html .config/systemd .gitconfig \
       .condarc .tmux.conf .emacs.d .ZWO .bash_history
   cp ~/skycam-utils/scripts/position.txt ~/skycam-utils/skycam_utils/version.py $B/
   tar czf $B/www.tgz -C /var/www/html skycam      # keeps the two symlinks as links
   sudo tar czf $B/etc.tgz /etc/lighttpd /etc/udev/rules.d/99-asi.rules \
       /etc/modprobe.d/usbfs.conf /etc/wsl.conf
   ~/conda/bin/conda env export -n skycam > $B/skycam-env.yml
   cp /mnt/c/Users/skycam/Documents/skywatch/Heater/DataOut_Weather.csv $B/
   ```

   `/etc/lighttpd/server.pem` is the HTTPS certificate and key, readable only
   by root; it is in `etc.tgz`.
2. **The whole WSL distro**, as a fallback (about 13 GB), from PowerShell:
   `wsl --export Ubuntu D:\migration-backup\wsl-ubuntu.tar`. Stop the service
   first (`systemctl --user stop omea`) so it isn't writing.
3. **Windows files.** `C:\Users\skycam\Documents\skywatch` (78 GB: Skywatch's
   dark frames, hot-pixel lists, logs and the `Heater` data) is the only copy;
   copy it to `D:\migration-backup\skywatch\`, with anything else wanted from
   `C:\Users`. If a return to Skywatch should stay possible, also export the
   registry key `HKCU\Software\SkyWatch` (Skywatch's board settings). It holds
   a localhost FTP password, so keep that file local and out of git.
4. **A disk image of `C:`**, if a way back to Windows/Skywatch is wanted: boot
   Clonezilla and save the NVMe to the data drive (~330 GB used, less
   compressed). Without it, wiping the NVMe can't be undone.
5. **Note the board's heater thresholds** (`omea board`, with the service
   stopped). They live in the camera and survive anyway; it's a check for
   afterwards.
6. **Shut Windows down fully**, so the NTFS volume is clean. Fast Startup is on
   (`HiberbootEnabled = 1`) and leaves volumes hibernated, which Linux won't
   write to. From an admin prompt: `powercfg /h off`, then `shutdown /s /t 0`.
   Not the Start-menu shutdown.

Expect a dark gap of a few hours.

## The data drive

`D:` is NTFS, which Linux reads and writes with either **ntfs3** (in the
kernel; fast) or **ntfs-3g** (FUSE; slower, more conservative). Either is fine
for this load: a 10-25 MB frame every 15-35 s. What NTFS costs on Linux:

* **No real fsck.** `ntfsfix` only resets the journal and clears the dirty
  flag; a real repair needs Windows' `chkdsk`.
* **An unclean unmount** (power cut, USB drop) **marks the volume dirty**, and
  it then won't mount read-write until `sudo ntfsfix -d /dev/sdX1`. On an
  unattended machine, that means a power cut can leave the drive missing until
  someone logs in. The omea service then won't start (it needs the mount, see
  below), and Nagios will say so.
* Ownership and permissions are fixed at mount time (`uid`, `gid`, `umask`).

**Recommendation: keep NTFS for the move, and plan ext4 as a second step.**
Reformatting first means copying 10 TB elsewhere. Later: buy a second drive
of 18 TB or more, `mkfs.ext4 -L skycam-data -T largefile4`, `rsync -aH` the
archive onto it and run on that, keeping the NTFS drive as an offline backup
readable anywhere. The archive has no other copy today, so that second drive
is worth buying anyway.

Mount it at `/mnt/d`, as now, and nothing else changes: `config.toml`, the
movie scripts (they hard-code `/mnt/d/skycam`), the web symlinks and
skycam_utils' scripts all keep working.

```bash
sudo blkid                     # the partition, e.g. /dev/sda1: LABEL="Skycam Data" UUID="..." TYPE="ntfs"
sudo mkdir -p /mnt/d
sudo chattr +i /mnt/d          # nothing can write to the bare mount point
```

`/etc/fstab`:

```
UUID=<from blkid>  /mnt/d  ntfs3  uid=1000,gid=1000,umask=022,noatime,nofail,x-systemd.device-timeout=60s  0  0
```

(For ntfs-3g: type `ntfs-3g`, same options plus `big_writes`.) `chattr +i`
matters: if the drive isn't mounted, omea would otherwise create
`/mnt/d/skycam/<day>` on the NVMe and fill it silently.

## 2. Install Debian

1. **BIOS** (F2 at boot): set *After Power Failure* to *Power On*, so the NUC
   comes back by itself after an outage. Leave Secure Boot on.
2. **Debian 13 netinst** from USB. **Unplug the data drive** during the install
   so the installer can't touch it. Whole NVMe, ext4, no desktop: select only
   "SSH server" and "standard system utilities". Hostname `new-skycam`, user
   `skycam`; the first user gets uid 1000, which the fstab line assumes. Set
   no root password, so `skycam` gets sudo. Ethernet needs no firmware; the
   AX201 Wi-Fi (unused) would need `firmware-iwlwifi`.
3. **Network**, `/etc/network/interfaces` (the interface name, e.g. `enp86s0`,
   from `ip link`):

   ```
   auto enp86s0
   iface enp86s0 inet static
       address 128.196.100.174/24
       gateway 128.196.100.1
       dns-nameservers 128.196.100.19 8.8.8.8
   ```

   For `dns-nameservers` to take effect, install `resolvconf`, or write
   `/etc/resolv.conf` by hand.
4. **Packages**:

   ```bash
   sudo apt install chrony lighttpd openssh-server ffmpeg ntfs-3g usbutils git gh \
       build-essential redis-tools bzip2 cron curl tmux emacs-nox resolvconf
   sudo usermod -aG dialout,systemd-journal skycam      # serial port; read logs without sudo
   ```

   (`dialout` takes effect at the next log-in. The service sets its own group
   list, so it doesn't need that.)
5. **Clock and time zone.** In `/etc/chrony/chrony.conf` add
   `server mmt-ntp.mmto.arizona.edu iburst prefer` above the `pool` line. Then:

   ```bash
   sudo systemctl restart chrony
   sudo systemctl enable chrony-wait.service    # time-sync.target waits for a real sync
   sudo timedatectl set-timezone America/Phoenix
   chronyc tracking                             # offset: milliseconds
   ```

6. **SSH**: restore `~/.ssh` from `home-extras.tgz` (mode 700, files 600).
7. **The data drive**: plug it in and mount it as in [The data
   drive](#the-data-drive). Then `ls /mnt/d/skycam` lists the day directories,
   and `/mnt/d/migration-backup` holds the backup.

## 3. USB for the camera

The SDK's udev rule makes the camera readable by everyone, and it raises the
usbfs buffer. That buffer must be larger than one 23 MB RAW16 frame; the
kernel default is 16 MB, and with it **every exposure fails**. Set it as a
kernel argument too, so it is right from boot:

```bash
# the rule is lib/99-asi.rules in omea_linux; the backup has the installed copy
sudo tar xzf /mnt/d/migration-backup/etc.tgz -C / etc/udev/rules.d/99-asi.rules
sudo udevadm control --reload
sudoedit /etc/default/grub          # add usbcore.usbfs_memory_mb=256 to GRUB_CMDLINE_LINUX_DEFAULT
sudo update-grub
```

After a reboot: `cat /sys/module/usbcore/parameters/usbfs_memory_mb` is 256,
and `lsusb` lists `03c3:294f` (ZWO) and `0403:6015` (FTDI). Plug the camera
into a USB 3 (blue) port. `ls -l /dev/ttyUSB0` should show group `dialout`.

There is no usbipd, no Windows watchdog and no `omea-usbip.ps1` on Debian.

## 4. Python, omea and skycam_utils

```bash
# Miniforge into ~/conda
curl -LO https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh -b -p ~/conda
~/conda/bin/conda init bash && exec bash

conda create -n skycam python=3.14
conda activate skycam
git clone https://github.com/tepickering/skycam-utils.git ~/skycam-utils
git clone https://github.com/MMTObservatory/omea_linux.git ~/omea_linux
pip install -e ~/skycam-utils
pip install -e "$HOME/omea_linux[test,redis]"
cd ~/omea_linux && pytest            # 51+ tests, all pass
```

`skycam-env.yml` in the backup records the versions that ran under WSL. If a
newer release breaks something, pin back to those.

Restore:

```bash
B=/mnt/d/migration-backup
cp $B/config.toml ~/omea_linux/
cp $B/position.txt ~/skycam-utils/scripts/
cp $B/version.py ~/skycam-utils/skycam_utils/
tar xzf $B/home-extras.tgz -C ~ public_html .gitconfig .condarc .tmux.conf .emacs.d .ZWO
crontab $B/crontab.txt
```

Then **fix `.gitconfig`**: its `credential.helper` points at the Windows Git
Credential Manager. Remove that line and run `gh auth login` (HTTPS), or switch
the remotes to SSH.

**The weather log.** Move it to the data drive and point omea at it, because
the default path is on `C:`:

```bash
cp $B/DataOut_Weather.csv /mnt/d/skycam/
```

In `~/omea_linux/config.toml` add:

```toml
[output]
weather_csv = "/mnt/d/skycam/DataOut_Weather.csv"
```

Nothing else in `config.toml` refers to Windows. As of this writing it sets
only `[redis] enabled = true` and `[board] heater_rh = 60.0`. Everything else
is the defaults in `omea/config.py`, which reproduce Skywatch's setup.

**First light** (service not running yet):

```bash
omea -c ~/omea_linux/config.toml camera-info     # lists the camera
omea -c ~/omea_linux/config.toml board           # firmware 2.20, "OMEA 8", sensible temps/RH
omea iris 16 && omea iris 2.8                    # you hear the iris
omea focus 1320                                  # homes, then 1320 steps
omea snap --exposure 0.005 -o /tmp/test.fits.bz2 # a frame; no timeouts
```

[hardware.md](hardware.md#first-light-checklist) has the full checklist,
including the geometry check against a Skywatch frame.

## 5. The omea service

Under WSL omea is a user service, because that is what starts with WSL. On
Debian, make it a system service: it starts at boot without a log-in, and it
can wait for the data drive and the clock. Create `/etc/systemd/system/omea.service`:

```ini
[Unit]
Description=OMEA all-sky camera (replaces Alcor Skywatch)
After=network-online.target time-sync.target
Wants=network-online.target
RequiresMountsFor=/mnt/d

[Service]
Type=simple
User=skycam
Group=skycam
SupplementaryGroups=dialout
WorkingDirectory=/home/skycam/omea_linux
# REDISPW=... here if redis ever needs a password
EnvironmentFile=-/home/skycam/.config/omea/env
ExecStart=/home/skycam/conda/envs/skycam/bin/omea -c /home/skycam/omea_linux/config.toml run
Restart=on-failure
RestartSec=30
# the cooler is switched off on a clean stop; give a 20 s exposure time to end
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now omea
journalctl -u omea -f
```

Commands from the README change from `systemctl --user ... omea` to
`sudo systemctl ... omea`. Stop the service before the hardware utilities
(`omea iris`, `omea heater`, ...); it holds the serial port.

## 6. Web

```bash
sudo tar xzf /mnt/d/migration-backup/etc.tgz -C / etc/lighttpd
sudo tar xzf /mnt/d/migration-backup/www.tgz -C /var/www/html
sudo chown -R skycam:skycam /var/www/html/skycam
sudo systemctl restart lighttpd
```

`etc.tgz` restores `lighttpd.conf`, `server.pem` and the `conf-enabled` links
(10-dir-listing, 10-ssl, 10-userdir, 99-unconfigured). If Debian's lighttpd
complains about `mod_openssl`, `sudo apt install lighttpd-mod-openssl`. omea
rewrites `ImageLastFTP_AllSKY*.jpg` there on every frame, and `latest_movie.sh`
rewrites `latest_movie.mp4` every minute. `aurora_20241011.mp4`, `RH.jpg` and
`temp.jpg` are static and come only from the backup.

## 7. Checks after the switch

1. `journalctl -u omea` shows frames, board readings and, after dark,
   photometry. http://new-skycam:8081/ and
   http://new-skycam/skycam/latest_image.jpg are live. Redis updates:
   `redis-cli -h redis.mmto.arizona.edu get allsky_external_temp`.
2. `curl -s localhost:8081/api/status`: `"dropped": 0`, and `frames` going up.
3. `chronyc tracking`: offset of a few ms.
4. At dusk: the log shows `cooling on, ramping ...`, then the iris to f/2.8
   and night exposures.
5. The next afternoon: the 13:15 `daily_movies.sh` ran (`/mnt/d/skycam/movies/<day>/`,
   `keograms/<year>/png/`), and `latest_movie.mp4` updates every minute.
6. **Reboot test**: `sudo reboot`; everything comes back by itself.
7. **Power-cut test** (pull the plug): the NUC powers on; check
   `mount | grep /mnt/d` is read-write. If the volume came back dirty, that is
   the NTFS cost: `sudo ntfsfix -d /dev/sdX1 && sudo mount /mnt/d && sudo systemctl start omea`.
8. Readout should be faster than under USB/IP. Under USB/IP the kernel logged
   a `reset SuperSpeed USB device` once per exposure, which was harmless.
   Natively, compare `dmesg | grep -c reset` against the number of frames; an
   occasional reset is fine, but a rising count with `exposure failed` is not.
9. Tell whoever runs Nagios if the host's name or address changed.

## Known gaps and history worth knowing

* **The sensor board isn't reopened on its own.** omea opens the serial board
  only when it (re)opens the camera. If the FTDI adapter alone drops (a replug
  or USB hiccup), the board stays lost, with `sensor board read failed`
  warnings, until the service restarts: `sudo systemctl restart omea`.
* **Under WSL, Windows Update was the main hazard.** On 2026-11-06 the camera
  returned `EXP_FAILED` on every exposure until the machine restarted. On
  2026-11-09 an automatic WSL update shut WSL down for four hours. Native
  Debian removes both layers. Debian doesn't update itself unless
  `unattended-upgrades` is installed; if it is, keep it to security updates
  (its default) and don't let it reboot (`Automatic-Reboot "false"`, the
  default). A new kernel only takes effect at a reboot, so reboot by hand, in
  daytime.
* The dome-heater logic (`omea/heater.py`) and its tuning are independent of
  the host. Its history is in the git log.

## Clean-up in the code (after the move)

* `docs/hardware.md` and the README describe WSL as the host: rewrite for
  native, and move `windows/` to a historical note.
* `omea/config.py`'s default `weather_csv` points at `C:`: change it to
  `/mnt/d/skycam/DataOut_Weather.csv`, as `config.example.toml` has.
* `systemd/omea.service`: ship the system-service version above.
* The drvfs workarounds (reading the extinction FITS without memmap in
  `omea/science.py`) are harmless natively; leave them.

## Going back

If the Clonezilla image of `C:` was taken, restoring it brings back Windows,
WSL and this setup exactly. With only `wsl-ubuntu.tar`: install Windows 11,
WSL and usbipd-win, `wsl --import Ubuntu C:\WSL\Ubuntu D:\migration-backup\wsl-ubuntu.tar`,
then follow [hardware.md](hardware.md#under-wsl2-the-current-windows-host).
