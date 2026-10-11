from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from astropy.io import fits

from omea import board, outputs
from omea.camera import RawFrame
from omea.config import Config
from omea.ephem import Site
from omea.fitsout import build_header, write_fits_bz2


@pytest.fixture
def frame():
    start = datetime(2026, 10, 10, 3, 15, 0, 250000, tzinfo=timezone.utc)
    return RawFrame(data=np.zeros((2, 2), np.uint16), start=start,
                    end=start + timedelta(seconds=21.9), exposure=20.0, gain=250,
                    offset=30, sensor_temp=-5.3, cooler_power=41.0,
                    camera="ZWO ASI294MC Pro")


def test_header_keeps_skywatch_conventions(tmp_path, frame):
    cfg = Config()
    site = Site(cfg.site)
    cube = np.full((3, 1411, 1422), 1930, np.int16)
    extra = {"fratio": 2.8, "focus": 1320, "temp_ext": 11.2, "rh_ext": float("nan"),
             "sun_alt": -40.0, "moon_alt": 12.0, "moon_az": 100.0}
    hdr = build_header(cube, frame, site, cfg.image, extra)
    stem = site.stem(frame.end)
    path = tmp_path / f"{stem}.fits.bz2"
    write_fits_bz2(path, cube, hdr)

    with fits.open(path) as hdul:
        h = hdul[0].header
        assert hdul[0].data.shape == (3, 1411, 1422)
        assert hdul[0].data.dtype.kind == "i" and h["BITPIX"] == 16
    # DATE is UT readout time; DATE-OBS is LOCAL exposure start (Skywatch's quirk)
    assert h["DATE"] == "2026-10-10T03:15:22.150000"
    assert h["DATE-OBS"] == "2026-10-09T20:15:00.250000"
    assert stem == "2026_10_09__20_15_22"
    assert (h["X1"], h["X2"], h["Y1"], h["Y2"]) == (701, 3544, 1, 2822)
    assert h["EXPOSURE"] == 20.0 and h["GAIN_ELE"] == 250 and h["OFFSET_E"] == 30
    assert h["SITELAT"] == "+31:41:12:00" and h["SITELONG"] == "-110:53:03:00"
    assert "Iris F2.8" in str(h["COMMENT"])
    assert h["HUMIDITY"] == 0.0                     # NaN never reaches the header
    assert h["DATE-BEG"] == "2026-10-10T03:15:00.250000"


def test_skycam_utils_reads_our_frames(tmp_path, frame):
    """The file name and DATE must give skycam_utils the same frame time."""
    pytest.importorskip("skycam_utils")
    from skycam_utils.alcor.timeutils import _alcor_frame_time, _frame_time
    cfg = Config()
    site = Site(cfg.site)
    cube = np.full((3, 1411, 1422), 1930, np.int16)
    path = tmp_path / f"{site.stem(frame.end)}.fits.bz2"
    write_fits_bz2(path, cube, build_header(cube, frame, site, cfg.image, {}))
    t_name = _alcor_frame_time(path).datetime
    t_date = _frame_time(path).datetime
    assert abs((t_name - t_date).total_seconds()) < 1.0


def test_day_directory_runs_noon_to_noon():
    site = Site(Config().site)
    # MST = UT - 7: 18:59:59 UT is 11:59:59 local, still the previous directory
    assert site.day_dir(datetime(2026, 10, 10, 18, 59, 59, tzinfo=timezone.utc)) == "2026-10-09"
    assert site.day_dir(datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)) == "2026-10-10"
    # 11:59 local on the 11th is still the 10th's directory; noon starts the 11th
    assert site.day_dir(datetime(2026, 10, 11, 18, 59, tzinfo=timezone.utc)) == "2026-10-10"
    assert site.day_dir(datetime(2026, 10, 11, 19, 0, tzinfo=timezone.utc)) == "2026-10-11"


def test_weather_line_matches_skywatch():
    site = Site(Config().site)
    local = site.local(datetime(2026, 10, 11, 1, 16, 54, tzinfo=timezone.utc))
    d = board.BoardData(time=0, temp_ext=10.9, rh_ext=89.2, temp_int=16.2, rh_int=46.4,
                        temp_hot=12.3, heater_on=False, auto_heat=True, heater_power=50,
                        dew_point=9.2)
    line = outputs.weather_line(local, d, -5.3)
    # a real line from DataOut_Weather.csv, 2026-10-10 6:16:54 PM
    assert line == "46305.76174;10/10/2026 6:16:54 PM;10.9;16.2;12.3;-5.3;89;46;FALSE;-1.00;-1.0;-1;\r\n"
