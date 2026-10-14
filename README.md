# omea

Linux replacement for Alcor System's Windows-only Skywatch software, written for
the MMTO's OMEA 8C all-sky camera (a ZWO ASI294MC Pro behind a fisheye, with an
Alcor sensor board for temperature, humidity, the dome heater, focus and iris).
Modeled on [cyclop_linux](https://github.com/MMTObservatory/cyclop_linux), and
built on [skycam_utils](../skycam-utils) for all geometry, photometry and
visualization.

## What it does

* **Acquisition.** Takes one exposure after another: auto-exposure by day, and
  at night a fixed gain of 250 with up to 20 s exposures. The iris switches
  between f/16 and f/2.8 at Sun -6 deg. Sensor cooling to -5 C is on while
  the Sun is down. At start-up it homes the focus and drives it to 1320 steps.
* **Skywatch-compatible frames.** Each RAW16 frame becomes the same
  (3, 1411, 1422) int16 cube Skywatch saved: same crop, demosaic, 2x2 binning,
  mirror and clipping, with the same FITS header, file names and noon-to-noon
  directories (`/mnt/d/skycam/YYYY-MM-DD/YYYY_MM_DD__HH_MM_SS.fits.bz2`).
  Every skycam_utils calibration (WCS epochs, bad-pixel and horizon masks,
  zeropoints) stays valid. [docs/skywatch.md](docs/skywatch.md) explains how
  the format was recovered.
* **Display products.** The annotated all-sky JPEG, the `Unwrap_` panorama and
  the 24 h keogram, drawn from skycam_utils' calibrated WCS rather than a
  hand-set circle, so they are distortion-corrected. Star, planet and
  MMT-pointing overlays come from the same WCS. Latest copies go to
  `/var/www/html/skycam/ImageLastFTP_AllSKY*.jpg`, as before.
* **Real-time science.** Each night frame (Sun < -12 deg) is passed to
  skycam_utils in a worker process. It writes star photometry to
  `<frame>_phot.csv`, which `alcor_process_night` reuses rather than
  re-measures. It also measures calibrated sky brightness in 5 deg cones, and
  rebuilds the cloud-extinction map from the newest 10 frames as a running
  average (single-frame star magnitudes scatter by ~0.3 mag). Results go to
  `<day>/omea_live.csv` and to redis.
* **Redis.** These keys are floats, or `nan` when there is no measurement. All
  of them are set to `nan` when the night ends.

  | Key | Meaning |
  |---|---|
  | `allsky_mv_zenith` | surface brightness at the zenith, V mag/arcsec² (5 deg cone median) |
  | `allsky_mv_tucson` | over Tucson, cone at az 0, alt 15 |
  | `allsky_mv_nogales` | over Nogales, cone at az 190, alt 15 |
  | `allsky_mv_target` | at the MMT pointing (mid-exposure; `nan` when parked) |
  | `allsky_extinction_median` | median extinction across the sky, V mag. Undetected stars count as the most extinguished; when over half are lost, the largest measured value is published as a floor |
  | `allsky_extinction_lost_fraction` | share of the map's stars not detected at all; near 1 means overcast |
  | `allsky_extinction_target` | extinction interpolated from the map at the MMT pointing; `nan` where the map has no stars nearby |

  The pointing comes from the mount's redis keys, as in skycam-utils'
  `mmt_extinction_redis.py`, which this replaces.
* **Sensors.** Polls the RS232 board every 30 s and publishes the keys
  skycam-utils' `tcs_logger.py` cron job did (`allsky_external_temp`,
  `allsky_internal_temp`, `allsky_hotside_temp`, `allsky_ccd_temp`,
  `allsky_external_rh`, `allsky_internal_rh`), which it replaces, plus
  `allsky_dewpoint` (C) and `allsky_heater` (0/1). It also appends the
  Skywatch-format `DataOut_Weather.csv`.
* **Web status** at `http://<host>:8081/`: the latest image, panorama and
  keogram, camera, sky and enclosure state, the night's plots, and the log.
  Read-only.

## Install

```bash
conda activate skycam                 # the env that has skycam_utils
pip install -e ".[test,redis]"
pytest
```

The ZWO SDK library (`lib/libASICamera2.so.1.37`, from ZWO's Linux SDK in this
directory) is found automatically. See [docs/hardware.md](docs/hardware.md) for
attaching the camera and serial adapter to Linux (usbipd under WSL2), and the
first-light checklist.

## Use

```bash
cp config.example.toml config.toml    # then edit; empty sections keep Skywatch's values
omea -c config.toml camera-info
omea -c config.toml board             # firmware, temperatures, heater thresholds
omea -c config.toml run               # the real thing

# a dry run: replays a night of Skywatch frames through the whole pipeline,
# 30x faster, writing only under ./data (no redis)
omea run --simulate --archive /mnt/d/skycam/2026-10-09 --start 2026-10-10T01:20 --speed 30

# hardware utilities: they need the board/camera, so stop the service first
# (systemctl --user stop omea); thresholds and power are kept by the board
omea iris 2.8
omea focus 1320
omea heater --temp 7 --rh 90 --power 50       # automatic mode, thresholds
omea snap --exposure 0.005 -o test.fits.bz2
omea check-geometry test.fits.bz2 <a Skywatch frame of the same scene>
```

## Running as a service

The same arrangement as cyclop: a systemd user service under WSL2
(`systemd=true` and `vmIdleTimeout=-1` are already set on this host).

```bash
mkdir -p ~/.config/systemd/user
cp systemd/omea.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now omea
journalctl --user -u omea -f
```

A clean stop switches the sensor cooler off. The dome heater is autonomous and
keeps working without the software.

## Changes from Skywatch

* **Night gain is fixed.** Skywatch lowered the gain under moonlight, which
  skycam_utils does not correct for; omea shortens the exposure instead. See
  docs/skywatch.md.
* The FITS header adds keywords and changes none: `DATE-BEG`/`DATE-END` (UT),
  `FRATIO`, `FOCUSSTP`, `COOLPWR`, `SUNALT`, `MOONALT`, and the board's
  temperatures and humidity, which Skywatch left at 0.
* Products are written directly with atomic renames, not FTP'd to localhost.
