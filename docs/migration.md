# Moving from WSL2 to native Debian

How to move the OMEA host (`new-skycam`, an Intel NUC11PAHi7) from Windows 11
+ WSL2 (Ubuntu 24.04) to Debian running directly on the hardware. omea itself
does not care which: the changes are in the plumbing around it. The data drive
is the one part that needs a decision; see [The data drive](#the-data-drive).

## What the host runs now

Surveyed on 2026-10-17.

| Piece | Now (WSL2) | Native Debian |
|---|---|---|
| Machine | NUC11PAHi7, 32 GB, Lexar 1 TB NVMe (`C:`), Windows 11 Pro | same hardware; Debian on the NVMe |
| Data drive | WD Elements 18 TB USB, GPT, one NTFS partition "Skycam Data" (`D:`), BitLocker off, 10 TB used / 8.1 TB free; seen as `/mnt/d` through drvfs (9p) | mounted directly; keep it at `/mnt/d` (see below) |
| Camera, serial board | usbipd-win on Windows attaches them to WSL (`windows/omea-usbip.ps1` in the Startup folder) | plain USB; udev rule and `dialout` group |
| Start-up | Windows log-in starts WSL, then `systemd --user` (linger) starts `omea` | systemd at boot |
| omea | `~/.config/systemd/user/omea.service`, conda env `~/conda/envs/skycam` (Python 3.14), `~/omea_linux`, `~/skycam-utils` (editable) | same, or a system service (recommended, see below) |
| cron (user `skycam`) | `latest_movie.sh` every minute; `daily_movies.sh` 13:15 (movies, `alcor_process_night`, keograms) | same crontab |
| Web | lighttpd on 80 and 443 (`/etc/lighttpd/server.pem`, `mod_userdir` for `~/public_html`), `/var/www/html/skycam` with symlinks `archive -> /mnt/d/skycam` and `movies -> /mnt/d/skycam/movies`; omea's status page on 8081 | same packages and files |
| Network | WSL `networkingMode=mirrored` shares Windows' interface: Ethernet (Intel I225-V) static 128.196.100.174/24, gateway 128.196.100.1, DNS 128.196.100.19 and 8.8.8.8 | the same static address on the I225-V (`igc` driver) |
| Clock | WSL follows the Windows clock; Windows Time is stopped (manual start) and the WSL clock is **1.05 s slow** (vs mmt-ntp, pool.ntp.org and time.google.com) | chrony against `mmt-ntp.mmto.arizona.edu` |
| SSH | `ssh.socket`, `~/.ssh/authorized_keys` | openssh-server, same keys |
| Weather log | `DataOut_Weather.csv` on `C:` (Skywatch's path, 187 MB of history back to 2024) | move it to the data drive |

The Windows side also holds Skywatch's own files
(`C:\Users\skycam\Documents\skywatch`, 78 GB: dark frames, hot-pixel lists,
logs, the `Heater` data). Nothing reads them now, but they are the only copy.

## The data drive

`D:` is NTFS. Linux has two NTFS drivers:

* **ntfs3**, in the kernel since 5.15 (Debian 13's kernel has it). Fast, low
  CPU. Its record is shakier than ntfs-3g's (corruption reports in its first
  years) and it is thinly maintained.
* **ntfs-3g**, a FUSE driver, mature and conservative, but slower and heavier
  on CPU. Fine for this load (a 10-25 MB frame every 15-35 s).

What NTFS costs on Linux either way:

* **No real fsck.** `ntfsfix` only resets the journal and clears the dirty
  flag; repairing the file system needs Windows' `chkdsk`. Keep a Windows
  machine (or a Windows To Go stick) to hand for that.
* **An unclean unmount (power cut, USB drop) marks the volume dirty**, and both
  drivers then refuse to mount it read-write until `ntfsfix -d` is run (ntfs3
  has a `force` option; don't put it in fstab). On an unattended machine that
  means the drive comes back read-only or not at all after a power cut, until
  someone logs in. Watch for this after every outage.
* **Windows Fast Startup** (it is on: `HiberbootEnabled = 1`) leaves volumes in
  a hibernated state that Linux won't write to. The last Windows shutdown before
  the switch must be a full one (see the checklist).
* Ownership and permissions are fixed at mount time (`uid`, `gid`, `umask`);
  that suits a single-user data drive.

**Recommendation: keep NTFS for the move, and plan ext4 as a second step.**
Reformatting now means first copying 10 TB somewhere, which turns a one-day
migration into a week, and the archive should not be moved without a second
copy anyway. So:

1. Migrate with the drive as it is, mounted with ntfs3 (ntfs-3g as the fallback
   if ntfs3 misbehaves; switching is an fstab edit).
2. Buy a second drive of 18 TB or more, format it ext4, `rsync -aH` the archive
   onto it, and run on that. The NTFS drive becomes the offline backup, readable
   by any Windows or Mac machine. Does the archive have a copy anywhere else
   today? If not, this second drive is worth buying regardless.

ext4 on 18 TB needs nothing special (`mkfs.ext4 -L skycam-data -T largefile4
/dev/sdX1` gives a sensible inode count for 1-10 MB files; drop `-T largefile4`
if many small files, e.g. the `*_phot.csv`, are expected; check `df -i` on a
test).

### Mount it at `/mnt/d`

Mounting at the same path means nothing else changes: `config.toml`, the movie
scripts (they hard-code `/mnt/d/skycam`), the web symlinks and skycam_utils'
scripts all keep working.

```bash
sudo blkid                              # find the partition, e.g. UUID="0A1B2C3D4E5F6789" TYPE="ntfs"
sudo mkdir -p /mnt/d
sudo chattr +i /mnt/d                   # nothing can write to the bare mount point
```

`/etc/fstab`:

```
UUID=<from blkid>  /mnt/d  ntfs3  uid=1000,gid=1000,umask=022,noatime,nofail,x-systemd.device-timeout=60s  0  0
```

(For ntfs-3g: type `ntfs-3g` and the same options, plus `big_writes`.) After a
dirty shutdown: `sudo ntfsfix -d /dev/sdX1 && sudo mount /mnt/d`.

`chattr +i` matters: if the drive is not mounted, omea would otherwise create
`/mnt/d/skycam/<day>` on the NVMe and fill it without complaint. With the bare
mount point immutable, writes fail and the service logs it.

## Before the switch (on the running system)

1. **Code and config.** `omea_linux` and `skycam-utils` are pushed, but these
   are not in git: `~/omea_linux/config.toml`, the crontab (`crontab -l`),
   `~/.ssh`, `~/public_html`, `/etc/lighttpd` (incl. `server.pem`),
   `/var/www/html/skycam` (only `aurora_20241011.mp4`, `RH.jpg`, `temp.jpg`
   and the symlinks are not regenerated), and skycam-utils' untracked
   `scripts/position.txt` and `skycam_utils/version.py`. Copy them to the data
   drive:

   ```bash
   B=/mnt/d/migration-backup && mkdir -p $B
   cp ~/omea_linux/config.toml $B/
   crontab -l > $B/crontab.txt
   tar czf $B/home-extras.tgz -C ~ .ssh public_html .config/systemd .gitconfig
   sudo tar czf $B/etc.tgz /etc/lighttpd /etc/udev/rules.d/99-asi.rules /etc/modprobe.d/usbfs.conf
   tar czf $B/www.tgz -C /var/www/html skycam     # keeps the two symlinks as links
   cp ~/skycam-utils/scripts/position.txt ~/skycam-utils/skycam_utils/version.py $B/
   ```

2. **The whole WSL distro**, as a fallback (13 GB), from PowerShell:
   `wsl --export Ubuntu D:\migration-backup\wsl-ubuntu.tar`.
3. **Windows files.** Copy `C:\Users\skycam\Documents\skywatch` (78 GB) to
   `D:\migration-backup\skywatch\`, and anything else wanted from `C:\Users`.
   Note the SkyWatch registry key (`HKCU\Software\SkyWatch`) holds the board
   settings Skywatch used; export it there too if a return to Skywatch should
   stay possible (it contains a localhost FTP password; keep the file local).
4. **A disk image of `C:`** if a way back to Windows/Skywatch is wanted: boot
   Clonezilla and save the NVMe to the data drive (~330 GB used, less
   compressed). Without it, wiping the NVMe is one-way.
5. **Note the board's heater thresholds** (`omea board`); they live in the
   camera and survive anyway, but it's a useful check afterwards.
6. **Shut Windows down fully**, so the NTFS volume is clean:
   `shutdown /s /t 0` from an admin prompt (or turn Fast Startup off first:
   `powercfg /h off`). Not the Start-menu shutdown.

Expect a dark gap of a few hours. Do it in daytime; the dome heater runs from
its stored thresholds with no software, and the cooler is off in daylight.

## Install

1. **BIOS** (F2 at boot): set *After Power Failure* (in the power settings) to *Power On*, so
   the NUC comes back by itself after an outage. Leave Secure Boot on; Debian
   boots with it.
2. **Debian 13 (trixie)**, netinst from USB: unplug the data drive during the
   install so the installer can't touch it. Use the whole NVMe, ext4, no
   desktop (SSH server + standard utilities). Hostname `new-skycam`, user
   `skycam` (uid 1000, which the fstab line and file ownership assume). The
   AX201 Wi-Fi needs `firmware-iwlwifi`; Ethernet needs nothing.
3. **Network**, `/etc/network/interfaces` (the interface name, e.g. `enp86s0`,
   from `ip link`):

   ```
   auto enp86s0
   iface enp86s0 inet static
       address 128.196.100.174/24
       gateway 128.196.100.1
       dns-nameservers 128.196.100.19 8.8.8.8
   ```

4. **Packages**:

   ```bash
   sudo apt install chrony lighttpd lighttpd-mod-openssl openssh-server ffmpeg \
       ntfs-3g usbutils git build-essential redis-tools bzip2 cron
   sudo usermod -aG dialout skycam
   ```

5. **Clock**: in `/etc/chrony/chrony.conf` replace the pool line with
   `server mmt-ntp.mmto.arizona.edu iburst prefer` (keep a `pool` line as a
   backup), then `chronyc tracking` should show an offset of milliseconds. Set
   `sudo timedatectl set-timezone America/Phoenix`.
6. **The data drive**: plug it in and mount it as in
   [Mount it at /mnt/d](#mount-it-at-mntd). `ls /mnt/d/skycam` should list the
   day directories.
7. **USB for the camera**: the udev rule from the SDK, which also raises the
   usbfs buffer the 23 MB frames need. The modprobe.d option used under WSL
   only works if usbcore is a module; a kernel argument works either way:

   ```bash
   sudo cp ~/omea_linux/lib/99-asi.rules /etc/udev/rules.d/
   sudo udevadm control --reload
   # and add usbcore.usbfs_memory_mb=256 to GRUB_CMDLINE_LINUX_DEFAULT
   # in /etc/default/grub, then: sudo update-grub
   ```

   After plugging the camera in (or a reboot):
   `cat /sys/module/usbcore/parameters/usbfs_memory_mb` should be 200 or more,
   and `lsusb` should list `03c3:294f` and `0403:6015`.
8. **Python**: install Miniforge to `~/conda`, then

   ```bash
   conda create -n skycam python=3.14
   conda activate skycam
   git clone git@github.com:tepickering/skycam-utils.git ~/skycam-utils
   git clone git@github.com:MMTObservatory/omea_linux.git ~/omea_linux
   pip install -e ~/skycam-utils
   pip install -e ~/omea_linux"[test,redis]"
   cd ~/omea_linux && pytest
   ```

   Restore `config.toml`, skycam-utils' `scripts/position.txt` and
   `skycam_utils/version.py`, `~/.ssh` (mode 700/600), `~/public_html` and the
   crontab (`crontab /mnt/d/migration-backup/crontab.txt`).
9. **Config changes** in `config.toml`: the weather log moves off `C:`.
   Copy the history over first:

   ```bash
   cp /mnt/d/migration-backup/skywatch/Heater/DataOut_Weather.csv /mnt/d/skycam/
   ```

   ```toml
   [output]
   weather_csv = "/mnt/d/skycam/DataOut_Weather.csv"
   ```

   Nothing else in it refers to Windows.
10. **Web**: restore `/etc/lighttpd` (including `server.pem` and the enabled
    `conf-enabled` links: dir-listing, ssl, userdir) and
    `/var/www/html/skycam` with its two symlinks into `/mnt/d/skycam`;
    `sudo systemctl restart lighttpd`.
11. **The omea service.** Under WSL it is a user service because that is what
    starts with WSL. Natively a system service is better: it starts at boot
    without a log-in or linger, and it can wait for the data drive.
    `/etc/systemd/system/omea.service`:

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
    WorkingDirectory=/home/skycam/omea_linux
    EnvironmentFile=-/home/skycam/.config/omea/env
    ExecStart=/home/skycam/conda/envs/skycam/bin/omea -c /home/skycam/omea_linux/config.toml run
    Restart=on-failure
    RestartSec=30
    TimeoutStopSec=60

    [Install]
    WantedBy=multi-user.target
    ```

    `sudo systemctl enable --now omea`; logs with `journalctl -u omea -f`. Add
    `skycam` to the `systemd-journal` group to read them without sudo. (The
    user service from `systemd/omea.service` with `loginctl enable-linger`
    still works if preferred, but it can't depend on the mount.)
    `systemctl enable chrony-wait.service` makes `time-sync.target` wait for a
    real sync, so frames never carry an unsynced time.

## Checks after the switch

1. The [first-light checklist](hardware.md#first-light-checklist), steps 1-4.
2. `omea run` in the service: the log shows frames, the board readings and,
   at night, photometry; http://new-skycam:8081/ and
   http://new-skycam/skycam/latest_image.jpg are live; the redis keys update
   (`redis-cli -h redis.mmto.arizona.edu get allsky_external_temp`).
3. `chronyc tracking`: offset under a few ms. Under WSL it was ~1 s.
4. The next morning: the 13:15 `daily_movies.sh` ran (movies and keograms
   for the night), `latest_movie.mp4` updates every minute.
5. **Reboot test**: `sudo reboot`, and check everything comes back on its own,
   then a **power-cut test** (pull the plug): the NUC powers on, and see
   whether the data drive mounted read-write (`mount | grep /mnt/d`). If it is
   dirty, that is the NTFS cost described above; `ntfsfix -d` and remount.
6. Readout should be faster than under USB/IP; compare frame cadence in
   `omea_live.csv` with a WSL night.

## Clean-up in the code (after the move)

* `docs/hardware.md` and the README describe WSL as the host; rewrite for
  native, and move `windows/` to a historical note.
* `omea/config.py`'s default `weather_csv` points at `C:`; change it to
  `/mnt/d/skycam/DataOut_Weather.csv` (as `config.example.toml` already has).
* `systemd/omea.service`: ship the system-service version above.
* The drvfs workarounds (reading the extinction FITS without memmap in
  `omea/science.py`) are harmless natively; leave them.
