# What Skywatch did, and how we know

Skywatch 4.3.15 (build 142, 2024-03-19) ran the OMEA 8C at the MMTO from 2023 to
2026. Its source is not available; everything here was recovered from its
registry settings, logs, output files and Alcor's Linux sample code, on
2026-10-10. omea reproduces it unless noted.

## Settings (HKCU\Software\SkyWatch)

| Setting | Value | Meaning |
|---|---|---|
| Camera\DoBin2x2, Debayer, Pattern | Yes, Yes, 4 | 2x2 binned colour output |
| Camera\MX / MY | Yes / No | mirror in x only |
| Camera\MaxExpo, MaxGain, MinGain | 20, 250, 0 | auto-exposure limits |
| Camera\ROI_Compute | 2 | meter in the fisheye circle (cx 696, cy 698, r 596) |
| Camera\TimerSec | 10 | idle seconds between frames |
| Camera\RequiredCCDTemperature, Heater\CCD_CoolingAUTO | -5, Yes | cool to -5 C, at night only |
| Camera\Fratio, DaySettings | 160, Yes | f/16 by day, f/2.8 at night |
| ZWO_ASI_Euda\PosFoc\PosFocRef_ADU | 1320 | focus position after homing |
| Interface\SunHeightNightDay | -0.1047 rad | day/night switch at Sun -6 deg |
| SaveData | `D:\skycam\`, FITS+bz2, JPEG q98, Unwrap JPEG | output |
| Webpublish | FTP to localhost `/var/www/html/skycam`, every 30 s | latest images |
| Heater\COM, RH_Factor, RH_Offset | 3, 157.2327, 0.1515 | RS232 port and humidity calibration |
| TelescopePos | `D:\skycam\mmt_position.txt` | MMT pointing overlay |
| Site | lat 0.553037 rad, long 1.935294 rad W, 2600 m | |

## Frame formation

From the session log: the full 4144 x 2822 sensor is read as RAW16
(`ASIGetDataAfterExp sizeX=4144 sizeY=2822`), then `ROI X1=701 Y1=1 X2=3544
Y2=2822`, "debayer", "Bin2x2", "X Mirror". The result is a (3, 1411, 1422)
int16 FITS cube.

How the debayer and binning combine was inferred from the pixels:

* **Per-channel, no colour mixing.** In a star-free patch the noise correlation
  between channels is ~0.01.
* **Bilinear demosaic, then a 2x2 mean.** That model's correlations between
  neighbouring pixels match Skywatch's: R 0.30 vs 0.34 measured, G 0.16 vs 0.15,
  B 0.30 vs 0.32. In a linear model with no cross-channel terms, nothing else
  fits.
* **The scale is the 2x2 mean of RAW16 (equivalently, the sum of four 14-bit
  values), clipped at 32767.** Saturated pixels pile up at exactly 32767, with a
  smooth histogram below it, so this is int16 clipping, not a sensor ceiling.
  The bias of ~1930 ADU at offset 30 fits too. Skywatch therefore threw away
  the top half of the RAW16 range. omea keeps the clip so the saturation
  thresholds in skycam_utils stay valid. The first hardware frame must confirm
  the scale (docs/hardware.md, step 5).

## Timing

* `DATE` = UT when readout finished = exposure start + exposure + ~1.9 s.
* `DATE-OBS` = **local** exposure start, despite its "UT" comment.
* File name = `DATE` in local time, truncated to the second.
* Day directories run local noon to noon, named after the first day.
* Cadence: ~15 s by day, ~35 s at night (20 s exposure + readout and
  processing + the 10 s timer).

skycam_utils takes frame times from the file name, so omea keeps all four.

## Exposure control (and a problem it caused)

Skywatch's auto-exposure raises exposure to 20 s, then raises gain to 250.
Under a bright sky at night it went back the other way: it held ~19.3-20 s and
**lowered the gain**. Across 2026-09-01..10-09 the night gain fell below 250 on
nine nights, as low as 60 at full Moon (09-26/27). Scattered drops also showed
on moonless nights (09-22/23), probably cloud lit from below.

ZWO gain is in 0.1 dB, so gain 170 vs 250 is a factor of 10^(80/200) = 2.5 in
ADU, about 1 mag. skycam_utils normalises sky brightness and photometry by
`EXPOSURE` only, so **archived moonlit frames are miscalibrated by up to
~1 mag** unless `GAIN_ELE` is also taken into account. omea instead holds the
gain at 250 all night and varies only the exposure, which skycam_utils already
handles.

## RS232 board (firmware 2.20, type 0x15 "OMEA 8")

115200 8N1. Request `1D n cmd args.. sum`, reply `1F n data.. sum`; `n` counts
the bytes after it, and `sum` is the low byte of the sum of all preceding
bytes. Commands (from `envi/Main.pas`):

| Code | Command | Arguments / reply |
|---|---|---|
| 9C | version | reply: major, minor, camera type |
| 9D | get data | reply bytes 0-1 T ext, 2-3 RH ext, 4-5 T int, 6 heater on, 7 auto, 8 power %, 13-14 RH int, 15-16 T heater ring |
| 9E | set config | T threshold (2), RH threshold (2), auto (1), power (1); reply CA |
| 9F | get config | T threshold (2), RH threshold (2) |
| A0 | heater on/off | 1 byte |
| A4 | iris | `FF 00` = fully open, then `77 code` (f/2.8=0 ... f/16=44 ... f/22=52) |
| A5 | focus | `14 hi lo`: signed relative steps; home = -2500 |
| A6 | extended data | only with the external weather module (not fitted here) |

Sensor words are the sum of 16 10-bit samples of a 0-5 V signal:
T = (V - 0.5) x 100, RH = (word/16384 - 0.1515) x 157.23.

At start-up Skywatch opened the iris, homed the focus, drove it to 1320, then
set the day iris. It switched the iris at Sun -6 deg, and switched cooling (ramped)
at Sun 0 deg.

## What omea does not reproduce

* Dark-frame subtraction and hot-pixel lists: both disabled in this
  installation. skycam_utils repairs bad pixels from its own masks.
* Star counting (`STARCNT` was always 0) and Skywatch's "magnitude" display
  mode (disabled). skycam_utils photometry replaces both.
* AVI creation (disabled), e-mail alarms (disabled), the secondary FTP (disabled).
* The FTP upload to localhost: omea writes the latest-image files directly.
