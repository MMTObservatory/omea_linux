# Bringing OMEA up on Debian 13

A self-contained handoff for rebuilding the OMEA all-sky camera host
(`new-skycam`) on a fresh Debian 13 (trixie) install, replacing Windows 11 +
WSL2. omea itself does not care which it runs on: everything here is plumbing.

The plan:

* Everything worth keeping from Windows (`C:`) and from the WSL system
  directories is gathered into `/home/skycam`, which is backed up and
  restored onto the new machine before anything is started.
* The 18 TB data drive (`D:`, NTFS) is backed up, reformatted ext4 and mounted
  at **`/data`**; the archive moves from `/mnt/d/skycam` to `/data/skycam`.
* Every WSL-only path and mechanism is mapped to its Debian equivalent
  ([the table below](#path-and-mechanism-map)).

Read [1. Prepare and back up](#1-prepare-and-back-up) before touching the
machine: wiping the NVMe and reformatting the data drive are both one-way.

The host was last surveyed on 2026-11-09 (Ubuntu 24.04 under WSL 3.0.5,
usbipd-win 5.3).

## What the system is

| Piece | Details |
|---|---|
| Machine | Intel NUC11PAHi7, 32 GB, Lexar 1 TB NVMe, Ethernet Intel I225-V (`igc`) |
| Camera | ZWO ASI294MC Pro, USB 3, `03c3:294f` |
| Sensor board | Alcor board behind an FTDI FT231X RS232 adapter, `0403:6015`, `/dev/ttyUSB0`: temperatures, humidity, dome heater, focus, iris |
| Data drive | WD Elements 18 TB USB (`1058:25a3`), today one NTFS partition "Skycam Data"; 9.8 TB used, 6.7 TB free. The archive grows about 32 GB and 13,000 files a day since omea took over, so the free space lasts roughly seven months |
| Network | static 128.196.100.174/24, gateway 128.196.100.1, DNS 128.196.100.19 and 8.8.8.8 |
| omea | `~/omea_linux` ([MMTObservatory/omea_linux](https://github.com/MMTObservatory/omea_linux)), config `~/omea_linux/config.toml` |
| skycam_utils | `~/skycam-utils` ([tepickering/skycam-utils](https://github.com/tepickering/skycam-utils)), installed editable; its `scripts/` run from cron |
| Python | Miniforge in `~/conda` (4.9 GB; conda 26.10), env `skycam`: Python 3.14.10, numpy 2.5.4, astropy 8.0.1, scipy 1.18.1, photutils 3.0.0, matplotlib 3.11.2, scikit-image 0.26.0, pandas 3.0.5, reproject 0.21.0, pillow 13.0.0, pyserial 3.5, redis 8.1.0 |
| cron (user `skycam`) | `latest_movie.sh` every minute (last 150 JPEGs, to `latest_movie.mp4`); `daily_movies.sh` at 13:15 (yesterday's movies, `alcor_process_night`, keograms) |
| Web | lighttpd on 80 and 443 (`/etc/lighttpd/server.pem`; dir-listing, ssl, userdir enabled), `/var/www/html/skycam` (owned by `skycam`) with symlinks `archive` and `movies` into the archive; omea's status page on 8081 |
| Redis | publishes to `redis.mmto.arizona.edu`, no password needed |
| Monitoring | Nagios (elsewhere) warns when frames stop coming in; nothing runs on this host for it |

Not worth carrying over: `pure-ftpd` (Skywatch's localhost FTP, unused),
`postfix` (installed but never configured; cron's mail to it fails, harmlessly),
`wireshark`, `iraf`, `saods9`, `~/wireshark.pcapng`, `~/errors.txt*`, and on
the data drive WD's installers, `$RECYCLE.BIN` and `System Volume Information`.

## Path and mechanism map

| Under Windows + WSL2 | On Debian | Where it is set |
|---|---|---|
| `/mnt/d/skycam` (archive, drvfs) | `/data/skycam` (ext4) | omea's defaults; `/etc/fstab`; `SKYCAM_DATA_DIR` in the crontab for skycam-utils' scripts ([PR #22](https://github.com/tepickering/skycam-utils/pull/22)); the web symlinks |
| `C:\Users\skycam\Documents\skywatch` = `/mnt/c/Users/skycam/Documents/skywatch` | `/home/skycam/Documents/skywatch` | copied into the home directory before the backup |
| Weather log `.../skywatch/Heater/DataOut_Weather.csv` (omea appends to it) | `/home/skycam/Documents/skywatch/Heater/DataOut_Weather.csv` | omea's default |
| `/mnt/d/skycam/mmt_position.txt`, `alcor_extinction_latest.fits` | `/data/skycam/...` | omea's defaults |
| `config.toml`'s "WSL host only" block, which pins the paths above | deleted | `~/omea_linux/config.toml` |
| User service `~/.config/systemd/user/omea.service` + `loginctl enable-linger` | system service `/etc/systemd/system/omea.service`, from `systemd/omea-system.service`; it waits for `/data` | systemd |
| usbipd-win + `windows/omea-usbip.ps1` watchdog in the Startup folder | plain USB; udev rule `lib/99-asi.rules` | `/etc/udev/rules.d/` |
| `/etc/modprobe.d/usbfs.conf` (usbfs 256 MB) | kernel argument `usbcore.usbfs_memory_mb=256` | `/etc/default/grub` |
| `/etc/wsl.conf`, `%USERPROFILE%\.wslconfig` (mirrored networking, no idle shut-down) | nothing; static address in `/etc/network/interfaces` | |
| Windows clock (WSL ~1 s slow) | chrony against `mmt-ntp.mmto.arizona.edu` | `/etc/chrony/chrony.conf` |
| Git credentials from Windows' Git Credential Manager (`.gitconfig`) | `gh auth login` | `~/.gitconfig` |
| Windows Update restarting WSL | nothing; `apt` when you choose | |

The archive's own files hold no absolute paths, so moving it is safe. The
FITS files, `omea_live.csv`, the `*_phot.csv` files and the keograms are all
found relative to the day directory.

## 1. Prepare and back up

Do this on the running system in daytime: the cooler is off, and the dome
heater runs from thresholds stored in the camera with no software at all.

### Code

1. Merge [skycam-utils PR #22](https://github.com/tepickering/skycam-utils/pull/22)
   (`SKYCAM_DATA_DIR`), then pull both repos on the host. Their defaults
   are still the WSL paths, so nothing changes yet:

   ```bash
   git -C ~/skycam-utils pull
   git -C ~/omea_linux pull
   systemctl --user restart omea
   ```

2. Check that nothing is left uncommitted that should be (`git status` in each).
   Leave the untracked `skycam-utils/scripts/position.txt`,
   `skycam_utils/version.py` and `omea_linux/config.toml` in place: the home
   backup carries them.

### Gather everything into the home directory

The home directory is the one backup that gets restored. Copy into it what
lives elsewhere:

```bash
M=~/migration && mkdir -p $M
crontab -l > $M/crontab.txt
~/conda/bin/conda env export -n skycam > $M/skycam-env.yml
tar czf $M/www.tgz -C /var/www/html skycam      # keeps the two symlinks as links
sudo tar czf $M/etc.tgz /etc/lighttpd /etc/udev/rules.d/99-asi.rules \
    /etc/modprobe.d/usbfs.conf /etc/wsl.conf
sudo chown skycam: $M/etc.tgz
cp /mnt/c/Users/skycam/.wslconfig $M/
```

`etc.tgz` holds `/etc/lighttpd/server.pem`, the HTTPS certificate and key:
keep the home backup private.

**The Windows side** (`C:`). `Documents\skywatch` (78 GB: Skywatch's dark
frames, hot-pixel lists, logs and the `Heater` folder with the weather log)
goes to the same place under the Linux home. The WSL disk has room (944 GB
free). omea appends to the weather log, so stop it for the final copy:

```bash
mkdir -p ~/Documents
rsync -a /mnt/c/Users/skycam/Documents/skywatch ~/Documents/      # the bulk, any time
systemctl --user stop omea                                         # then, at the switch:
rsync -a /mnt/c/Users/skycam/Documents/skywatch ~/Documents/      # catch up
```

Look through the rest of `C:\Users\skycam` (Desktop, Downloads, other folders
in Documents) for anything else worth keeping and copy it the same way. If a
return to Skywatch should stay possible, also export the registry key
`HKCU\Software\SkyWatch` (Skywatch's board settings) to `~/migration/`. It
holds a localhost FTP password, so never put that file in git.

**Note the board's heater thresholds** (`omea board`, with the service
stopped). They live in the camera and survive anyway; it's a check for
afterwards.

**Back up `/home/skycam`** with your usual method. It's about 85 GB with
`Documents/skywatch`.
`~/conda` is self-contained and usually runs unchanged on Debian; if it
doesn't, `migration/skycam-env.yml` records what to rebuild.

### The data drive

`/mnt/d` holds `skycam/` (the archive, 9.8 TB) plus WD's installers, Windows
bookkeeping and a 112-byte `bootTel.dat` of unknown origin, which isn't worth
keeping.

1. **Check the remote mirror covers everything.** It is "mostly" a copy, so
   list what it lacks before the reformat. From the host, or from the mirror
   side, with the matching paths:

   ```bash
   # -n: dry run. Lists files missing on the mirror or different in size
   rsync -an --size-only --out-format='%n' /mnt/d/skycam/ MIRROR:/path/to/skycam/ \
       > ~/migration/not-on-mirror.txt
   wc -l ~/migration/not-on-mirror.txt
   ```

   Over drvfs that scan of a few million files takes hours; run it in `tmux`.
   The usual suspects are the newest days, `keograms/`, `movies/` and the
   top-level files. Copy what's missing to the mirror (or another disk) and
   run the check again until it is empty, apart from the day being written.
2. **Stop writing**: `systemctl --user stop omea`, and comment out the
   crontab, so the final sync is complete. Run the check once more.
3. Reformatting happens on Debian (section 3), after the install.

### Last steps on Windows

* **A disk image of `C:`**, if a way back to Windows/Skywatch is wanted: boot
  Clonezilla and save the NVMe to some other disk (~330 GB used, less
  compressed; not to the data drive, which is about to be reformatted).
  Without it, wiping the NVMe can't be undone.
* Shut down. Fast Startup only matters if the NTFS drive will be read on Linux
  before reformatting; it won't, so any shutdown will do.

Expect a dark gap of a few hours for the install, plus however long the
archive takes to come back (see [section 3](#3-the-data-drive)). omea can run
with an empty `/data/skycam` before the restore finishes.

## 2. Install Debian

1. **BIOS** (F2 at boot): set *After Power Failure* to *Power On*, so the NUC
   comes back by itself after an outage. Leave Secure Boot on.
2. **Debian 13 netinst** from USB. **Unplug the data drive** during the install
   so the installer can't touch it. Whole NVMe, ext4, no desktop: select only
   "SSH server" and "standard system utilities". Hostname `new-skycam`, user
   `skycam` (uid 1000). Set no root password, so `skycam` gets sudo. Ethernet
   needs no firmware.
3. **Network**, `/etc/network/interfaces` (the interface name, e.g. `enp86s0`,
   from `ip link`):

   ```
   auto enp86s0
   iface enp86s0 inet static
       address 128.196.100.174/24
       gateway 128.196.100.1
       dns-nameservers 128.196.100.19 8.8.8.8
   ```

   (`dns-nameservers` needs the `resolvconf` package; otherwise write
   `/etc/resolv.conf` by hand.)
4. **Packages**:

   ```bash
   sudo apt install chrony lighttpd openssh-server ffmpeg usbutils git gh \
       build-essential redis-tools bzip2 cron curl rsync tmux emacs-nox resolvconf
   sudo usermod -aG dialout,systemd-journal skycam   # serial port; read logs without sudo
   ```

5. **Clock and time zone.** In `/etc/chrony/chrony.conf` add
   `server mmt-ntp.mmto.arizona.edu iburst prefer` above the `pool` line. Then:

   ```bash
   sudo systemctl restart chrony
   sudo systemctl enable chrony-wait.service    # time-sync.target waits for a real sync
   sudo timedatectl set-timezone America/Phoenix
   chronyc tracking                             # offset: milliseconds
   ```

6. **Restore `/home/skycam`** from the backup, owned by `skycam`
   (`sudo chown -R skycam: /home/skycam`; `chmod 700 ~/.ssh`, `600` on its files).
   Log out and in again, so `dialout` and the restored shell set-up take effect.

## 3. The data drive

**Reformatting erases the drive.** Only go ahead once the mirror check in
section 1 is clean.

```bash
lsblk -o NAME,SIZE,MODEL,FSTYPE,LABEL      # the 16.4T WD drive, e.g. sda; check the model and size
sudo wipefs -a /dev/sdX                    # X: the WD drive, not the NVMe
sudo parted -s /dev/sdX mklabel gpt mkpart skycam-data ext4 0% 100%
sudo mkfs.ext4 -L skycam-data -i 262144 -m 0 /dev/sdX1
```

* `-i 262144` gives one inode per 256 KB, about 70 million. Files average
  2.4 MB, but omea writes 13,000 a day: the common `-T largefile4` (4.5
  million inodes) would run out within a year. `df -i /data` shows usage.
* `-m 0`: no space reserved for root on a pure data disk (the default 5 % is
  900 GB).

Mount it at `/data`:

```bash
sudo mkdir /data
sudo chattr +i /data        # if the drive is missing, nothing can write to the bare mount point
sudo blkid /dev/sdX1        # the UUID
```

`/etc/fstab`:

```
UUID=<from blkid>  /data  ext4  defaults,noatime,nofail,x-systemd.device-timeout=60s  0  2
```

```bash
sudo systemctl daemon-reload && sudo mount /data
sudo mkdir /data/skycam && sudo chown skycam: /data/skycam
```

`nofail` lets the machine boot without the drive. The omea service then
won't start (`RequiresMountsFor=/data`), and omea refuses to run without
`/data/skycam` anyway, so frames stop and Nagios says so. Nothing fills the
NVMe. ext4 gets a normal `fsck` at boot after a power cut, which NTFS couldn't.

**Restore the archive** from the mirror into `/data/skycam/`, e.g.
`rsync -aH --info=progress2 MIRROR:/path/to/skycam/ /data/skycam/` in `tmux`.
At 1 Gb/s, 10 TB takes about a day. Restore the newest days, `keograms/` and
`movies/` first, if the mirror's layout allows; the service can be running
meanwhile.

## 4. USB for the camera

The SDK's udev rule makes the camera readable by everyone, and it raises the
usbfs buffer. That buffer must be larger than one 23 MB RAW16 frame; the
kernel default is 16 MB, and with it **every exposure fails**. Set it as a
kernel argument too, so it's right from boot:

```bash
sudo cp ~/omea_linux/lib/99-asi.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudoedit /etc/default/grub          # add usbcore.usbfs_memory_mb=256 to GRUB_CMDLINE_LINUX_DEFAULT
sudo update-grub
```

After a reboot: `cat /sys/module/usbcore/parameters/usbfs_memory_mb` is 256,
and `lsusb` lists `03c3:294f` (ZWO) and `0403:6015` (FTDI). Plug the camera
into a USB 3 (blue) port. `ls -l /dev/ttyUSB0` should show group `dialout`.

No usbipd, no watchdog: `windows/` is not used on Debian.

## 5. Python, omea and skycam_utils

The restored `~/conda` should just work. Check:

```bash
conda activate skycam
python -c "import omea, skycam_utils; print('ok')"
cd ~/omea_linux && pytest -q            # 51+ tests, all pass
```

If it doesn't work (a missing system library, say), rebuild the environment;
`~/migration/skycam-env.yml` has the versions that ran under WSL:

```bash
mv ~/conda ~/conda.wsl
curl -LO https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh -b -p ~/conda && ~/conda/bin/conda init bash && exec bash
conda create -n skycam python=3.14 && conda activate skycam
pip install -e ~/skycam-utils
pip install -e "$HOME/omea_linux[test,redis]"
cd ~/omea_linux && pytest -q
```

**Map the paths:**

1. `~/omea_linux/config.toml`: delete the block headed `--- WSL host only`
   (its `[output]` and `[science]` sections). omea's defaults are then the
   Debian paths. Check:

   ```bash
   python -c "from omea.config import load_config as l; c=l('config.toml'); \
   print(c.output.data_dir, c.output.weather_csv, c.science.extinction_file)"
   # /data/skycam /home/skycam/Documents/skywatch/Heater/DataOut_Weather.csv /data/skycam/alcor_extinction_latest.fits
   ```

2. The crontab (restored in the home backup only as `~/migration/crontab.txt`):

   ```bash
   { echo 'SKYCAM_DATA_DIR=/data/skycam'; cat ~/migration/crontab.txt; } | crontab -
   crontab -l | head -3
   ```

3. `~/.gitconfig`: delete the `[credential]` section (it points at the Windows
   Git Credential Manager), then `gh auth login`.

**First light** (service not running yet):

```bash
omea -c ~/omea_linux/config.toml camera-info     # lists the camera
omea -c ~/omea_linux/config.toml board           # firmware 2.20, "OMEA 8", sensible temps/RH
omea iris 16 && omea iris 2.8                    # you hear the iris
omea focus 1320                                  # homes, then 1320 steps
omea snap --exposure 0.005 -o /tmp/test.fits.bz2 # a frame; no timeouts
```

[hardware.md](hardware.md#first-light-checklist) has the full checklist,
including the geometry check against an older frame.

## 6. The omea service

On Debian omea is a system service. It starts at boot without a log-in and
waits for `/data` and the clock:

```bash
sudo cp ~/omea_linux/systemd/omea-system.service /etc/systemd/system/omea.service
sudo systemctl daemon-reload
sudo systemctl enable --now omea
journalctl -u omea -f
```

Commands in the README change from `systemctl --user ... omea` to
`sudo systemctl ... omea`. Stop the service before the hardware utilities
(`omea iris`, `omea heater`, ...); it holds the serial port.

## 7. Web

```bash
sudo tar xzf ~/migration/etc.tgz -C / etc/lighttpd
sudo tar xzf ~/migration/www.tgz -C /var/www/html
sudo chown -R skycam: /var/www/html/skycam
cd /var/www/html/skycam
ln -sfn /data/skycam archive && ln -sfn /data/skycam/movies movies    # were /mnt/d/...
sudo systemctl restart lighttpd
```

If lighttpd complains about `mod_openssl`: `sudo apt install lighttpd-mod-openssl`.
omea rewrites `ImageLastFTP_AllSKY*.jpg` there on every frame, and
`latest_movie.sh` rewrites `latest_movie.mp4` every minute.
`aurora_20241011.mp4`, `RH.jpg` and `temp.jpg` are static and come only from
the backup.

## 8. Checks after the switch

1. Nothing left pointing at Windows paths:
   `grep -rn '/mnt/[cd]' ~/omea_linux/config.toml ~/skycam-utils/scripts /etc/fstab; crontab -l | grep /mnt`,
   and `ls -l /var/www/html/skycam`.
2. `journalctl -u omea` shows frames, board readings and, after dark,
   photometry. http://new-skycam:8081/ and
   http://new-skycam/skycam/latest_image.jpg are live. Redis updates:
   `redis-cli -h redis.mmto.arizona.edu get allsky_external_temp`. Frames land
   in `/data/skycam/<day>/`, and the weather log grows in
   `~/Documents/skywatch/Heater/`.
3. `curl -s localhost:8081/api/status`: `"dropped": 0`, and `frames` going up.
4. `chronyc tracking`: offset of a few ms.
5. At dusk: the log shows `cooling on, ramping ...`, then the iris to f/2.8
   and night exposures.
6. The next afternoon: the 13:15 `daily_movies.sh` ran
   (`/data/skycam/movies/<day>/`, `/data/skycam/keograms/<year>/png/`), and
   `latest_movie.mp4` updates every minute.
7. **Reboot test**: `sudo reboot`; everything comes back by itself.
8. **Power-cut test** (pull the plug): the NUC powers on, `/data` mounts
   (after an fsck if needed), and omea starts.
9. Readout should be faster than under USB/IP. Under USB/IP the kernel logged
   a `reset SuperSpeed USB device` once per exposure, which was harmless.
   Natively, compare `dmesg | grep -c reset` against the number of frames; an
   occasional reset is fine, but a rising count with `exposure failed` is not.
10. Tell whoever runs Nagios if the host's name or address changed.

## Known gaps and history worth knowing

* **Disk space.** At about 32 GB a day the drive fills in roughly seven
  months from 2026-11. Plan pruning (the per-frame JPEGs are the bulk that
  `make_movies.sh` already has commented-out `rm` lines for) or a bigger drive.
* **The sensor board isn't reopened on its own.** omea opens the serial board
  only when it (re)opens the camera. If the FTDI adapter alone drops, the
  board stays lost, with `sensor board read failed` warnings, until the
  service restarts: `sudo systemctl restart omea`.
* **Under WSL, Windows was the main hazard.** On 2026-11-06 the camera returned
  `EXP_FAILED` on every exposure until the machine restarted. On 2026-11-09 an
  automatic WSL update shut WSL down for four hours. Native Debian removes both
  layers. Debian doesn't update itself unless `unattended-upgrades` is
  installed; if it is, keep it to security updates (its default) and don't
  let it reboot (`Automatic-Reboot "false"`, the default). A new kernel only
  takes effect at a reboot, so reboot by hand, in daytime.
* The dome-heater logic (`omea/heater.py`) and its tuning are independent of
  the host. Its history is in the git log.

## Clean-up in the code (after the move)

* `docs/hardware.md` and the README describe WSL as the host: rewrite for
  native, and move `windows/` and `systemd/omea.service` (the user service) to
  a historical note.
* skycam-utils' scripts: once no WSL host remains, change the
  `SKYCAM_DATA_DIR` default to `/data/skycam`.
* The drvfs workarounds (reading the extinction FITS without memmap in
  `omea/science.py`) are harmless natively; leave them.

## Going back

If the Clonezilla image of `C:` was taken, restoring it brings back Windows
and WSL as they were, apart from the data drive. The data drive would then be
ext4, which Windows can't read. Either keep the Debian archive somewhere else
or reformat it as NTFS and copy back, and in both cases restore the WSL
`config.toml` block and paths. Without the image: install Windows 11, WSL and
usbipd-win, then follow
[hardware.md](hardware.md#under-wsl2-the-current-windows-host).
