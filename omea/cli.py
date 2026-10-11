"""Command line: ``omea run`` and the hardware utilities."""

import argparse
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config

log = logging.getLogger("omea")


def _setup_logging(verbose):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("matplotlib", "PIL"):   # not astropy: it installs its own logger class
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(cfg, args):
    from .clock import SimClock
    from .monitor import Monitor
    from . import web

    clock = None
    if args.simulate:
        data = Path(args.data_dir or "data").resolve()
        cfg.camera.driver = "sim"
        cfg.board.driver = "sim"
        if args.archive:
            cfg.camera.sim_archive = args.archive
        # never touch the production paths from a dry run
        cfg.output.data_dir = str(data)
        cfg.output.latest_dir = str(data / "latest")
        cfg.output.weather_csv = str(data / "DataOut_Weather.csv")
        cfg.science.extinction_file = str(data / "alcor_extinction_latest.fits")
        cfg.redis.enabled = args.redis
        start = (datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
                 if args.start else None)
        clock = SimClock(start, args.speed)
    elif args.data_dir:
        cfg.output.data_dir = args.data_dir
    if args.no_redis:
        cfg.redis.enabled = False
    if args.no_science:
        cfg.science.enabled = False
    if args.no_web:
        cfg.web.enabled = False
    if args.web_port:
        cfg.web.port = args.web_port

    mon = Monitor(cfg, clock=clock)
    signal.signal(signal.SIGTERM, mon.stop)
    signal.signal(signal.SIGINT, mon.stop)
    if cfg.web.enabled:
        web.serve(mon.status, cfg.web.host, cfg.web.port)
    mon.run(max_frames=args.frames)


def cmd_camera_info(cfg, args):
    from .camera import open_camera
    cam = open_camera(cfg.camera, cfg.image)
    try:
        print(cam.describe())
    finally:
        cam.close()


def cmd_snap(cfg, args):
    """One exposure, written exactly as ``run`` writes frames."""
    import numpy as np

    from .camera import open_camera
    from .ephem import Site
    from .fitsout import build_header, write_fits_bz2
    from .imaging import raw_to_cube

    ic = cfg.image
    site = Site(cfg.site)
    cam = open_camera(cfg.camera, ic, tz=site.tz)
    try:
        frame = cam.expose(args.exposure, args.gain)
    finally:
        cam.close()
    cube = raw_to_cube(frame.data, ic.crop_x, ic.crop_y, ic.crop_width, ic.crop_height,
                       ic.bayer, ic.mirror_x, ic.mirror_y, ic.adu_scale, ic.clip)
    out = Path(args.output or f"{site.stem(frame.end)}.fits.bz2")
    extra = {"sun_alt": site.sun_altitude(frame.end), "fratio": args.fratio}
    write_fits_bz2(out, cube, build_header(cube, frame, site, ic, extra))
    if args.raw:
        np.save(args.raw, frame.data)
    print(f"{out}: exposure {frame.exposure:g} s, gain {frame.gain}, sensor {frame.sensor_temp} C")
    print(f"  RAW16 min/median/max {frame.data.min()}/{np.median(frame.data):.0f}/{frame.data.max()}")
    print(f"  cube corner bias R/G/B {np.median(cube[:, :10, :10], axis=(1, 2))}, "
          f"max {cube.max()} (Skywatch: bias ~1930 at gain 250, offset 30)")


def _board(cfg):
    from .board import open_board
    cfg.board.enabled = True
    return open_board(cfg.board)


def cmd_board(cfg, args):
    b = _board(cfg)
    try:
        fw, kind = b.version()
        print(f"firmware {fw}, camera type {kind}")
        d = b.read()
        print(f"outside   {d.temp_ext:6.1f} C  {d.rh_ext:5.1f} %  dew point {d.dew_point:.1f} C")
        print(f"inside    {d.temp_int:6.1f} C  {d.rh_int:5.1f} %")
        print(f"heater    ring {d.temp_hot:.1f} C, {'ON' if d.heater_on else 'off'}, "
              f"automatic {'yes' if d.auto_heat else 'no'}, power {d.heater_power} %")
        t, rh = b.get_config()
        print(f"heater thresholds: below {t:.1f} C and above {rh:.0f} % RH")
    finally:
        b.close()


def cmd_iris(cfg, args):
    b = _board(cfg)
    try:
        b.set_iris(args.fratio)
    finally:
        b.close()


def cmd_focus(cfg, args):
    b = _board(cfg)
    try:
        b.focus_to(args.position if args.position is not None else cfg.mode.focus_position)
    finally:
        b.close()


def cmd_heater(cfg, args):
    b = _board(cfg)
    try:
        t, rh = b.get_config()
        d = b.read()
        temp = args.temp if args.temp is not None else t
        hum = args.rh if args.rh is not None else rh
        power = args.power if args.power is not None else d.heater_power
        auto = not (args.on or args.off)
        b.set_config(temp, hum, auto, power)
        if not auto:
            b.set_heater(args.on)
        print(f"heater: {'automatic' if auto else ('forced on' if args.on else 'off')}, "
              f"below {temp:.1f} C and above {hum:.0f} % RH, power {power} %")
    finally:
        b.close()


def cmd_check_geometry(cfg, args):
    from .verify import compare_frames
    print(compare_frames(args.new, args.reference))


def main(argv=None):
    p = argparse.ArgumentParser(prog="omea", description=__doc__)
    p.add_argument("-c", "--config", help="TOML configuration file")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the camera")
    r.add_argument("--simulate", action="store_true",
                   help="simulated camera and sensor board; output under --data-dir")
    r.add_argument("--archive", help="(simulate) directory of Skywatch frames to replay")
    r.add_argument("--speed", type=float, default=1.0, help="(simulate) time compression")
    r.add_argument("--start", help="(simulate) UT start time, e.g. 2026-10-10T00:30")
    r.add_argument("--redis", action="store_true", help="(simulate) publish to redis anyway")
    r.add_argument("--data-dir", help="output root (default from the config; ./data when simulating)")
    r.add_argument("-n", "--frames", type=int, help="stop after this many frames")
    r.add_argument("--no-redis", action="store_true")
    r.add_argument("--no-science", action="store_true")
    r.add_argument("--no-web", action="store_true")
    r.add_argument("--web-port", type=int)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("camera-info", help="list the camera's properties and controls")
    s.set_defaults(func=cmd_camera_info)

    s = sub.add_parser("snap", help="take one frame and save it in the Skywatch format")
    s.add_argument("--exposure", type=float, default=0.001, help="seconds")
    s.add_argument("--gain", type=int, default=0)
    s.add_argument("--fratio", type=float, default=0.0, help="recorded in the header only")
    s.add_argument("-o", "--output")
    s.add_argument("--raw", help="also save the RAW16 frame as .npy")
    s.set_defaults(func=cmd_snap)

    s = sub.add_parser("board", help="sensor board status")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("iris", help="set the lens iris")
    s.add_argument("fratio", type=float)
    s.set_defaults(func=cmd_iris)

    s = sub.add_parser("focus", help="home the focus and move to a position")
    s.add_argument("position", type=int, nargs="?")
    s.set_defaults(func=cmd_focus)

    s = sub.add_parser("heater", help="dome heater mode and thresholds")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="force on (disables automatic mode)")
    g.add_argument("--off", action="store_true", help="force off (disables automatic mode)")
    s.add_argument("--temp", type=float, help="heat below this outside temperature (C)")
    s.add_argument("--rh", type=float, help="heat above this outside humidity (%%)")
    s.add_argument("--power", type=int, help="heater power (%%)")
    s.set_defaults(func=cmd_heater)

    s = sub.add_parser("check-geometry",
                       help="compare a new frame with a Skywatch frame (flips, shift, levels)")
    s.add_argument("new")
    s.add_argument("reference")
    s.set_defaults(func=cmd_check_geometry)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    from .board import BoardError
    from .camera import CameraError
    try:
        args.func(cfg, args)
    except KeyboardInterrupt:
        return 130
    except (CameraError, BoardError, OSError) as err:
        log.error("%s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
