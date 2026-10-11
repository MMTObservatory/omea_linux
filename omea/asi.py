"""
Minimal ctypes binding to ZWO's libASICamera2 (structures follow ASICamera2.h
from SDK 1.37). Only what a single-camera, snapshot-mode controller needs.
"""

import ctypes
from ctypes import (POINTER, byref, c_char, c_char_p, c_double, c_float, c_int,
                    c_long, c_ubyte)
from pathlib import Path

# ASI_CONTROL_TYPE
GAIN = 0
EXPOSURE = 1           # microseconds
GAMMA = 2
WB_R = 3
WB_B = 4
OFFSET = 5
BANDWIDTHOVERLOAD = 6
OVERCLOCK = 7
TEMPERATURE = 8        # 10 x degC
FLIP = 9
HIGH_SPEED_MODE = 14
COOLER_POWER_PERC = 15
TARGET_TEMP = 16       # degC
COOLER_ON = 17
FAN_ON = 19
ANTI_DEW_HEATER = 21

# ASI_IMG_TYPE
IMG_RAW8 = 0
IMG_RGB24 = 1
IMG_RAW16 = 2

# ASI_EXPOSURE_STATUS
EXP_IDLE = 0
EXP_WORKING = 1
EXP_SUCCESS = 2
EXP_FAILED = 3

BAYER_NAMES = {0: "RGGB", 1: "BGGR", 2: "GRBG", 3: "GBRG"}

ERRORS = [
    "SUCCESS", "INVALID_INDEX", "INVALID_ID", "INVALID_CONTROL_TYPE",
    "CAMERA_CLOSED", "CAMERA_REMOVED", "INVALID_PATH", "INVALID_FILEFORMAT",
    "INVALID_SIZE", "INVALID_IMGTYPE", "OUTOF_BOUNDARY", "TIMEOUT",
    "INVALID_SEQUENCE", "BUFFER_TOO_SMALL", "VIDEO_MODE_ACTIVE",
    "EXPOSURE_IN_PROGRESS", "GENERAL_ERROR", "INVALID_MODE",
    "GPS_NOT_SUPPORTED", "GPS_VER_ERR", "GPS_FPGA_ERR",
    "GPS_PARAM_OUT_OF_RANGE", "GPS_DATA_INVALID",
]


class ASIError(RuntimeError):
    def __init__(self, func, code):
        name = ERRORS[code] if 0 <= code < len(ERRORS) else str(code)
        super().__init__(f"{func} failed: ASI_ERROR_{name}")
        self.code = code


class CameraInfo(ctypes.Structure):
    _fields_ = [
        ("Name", c_char * 64),
        ("CameraID", c_int),
        ("MaxHeight", c_long),
        ("MaxWidth", c_long),
        ("IsColorCam", c_int),
        ("BayerPattern", c_int),
        ("SupportedBins", c_int * 16),
        ("SupportedVideoFormat", c_int * 8),
        ("PixelSize", c_double),
        ("MechanicalShutter", c_int),
        ("ST4Port", c_int),
        ("IsCoolerCam", c_int),
        ("IsUSB3Host", c_int),
        ("IsUSB3Camera", c_int),
        ("ElecPerADU", c_float),
        ("BitDepth", c_int),
        ("IsTriggerCam", c_int),
        ("Unused", c_char * 16),
    ]


class ControlCaps(ctypes.Structure):
    _fields_ = [
        ("Name", c_char * 64),
        ("Description", c_char * 128),
        ("MaxValue", c_long),
        ("MinValue", c_long),
        ("DefaultValue", c_long),
        ("IsAutoSupported", c_int),
        ("IsWritable", c_int),
        ("ControlType", c_int),
        ("Unused", c_char * 32),
    ]


class ASISerial(ctypes.Structure):
    _fields_ = [("id", c_ubyte * 8)]


def default_library_path():
    lib = Path(__file__).resolve().parent.parent / "lib"
    found = sorted(lib.glob("libASICamera2.so*"))
    return str(found[-1]) if found else "libASICamera2.so"


class ASILibrary:
    """Thin wrapper that raises :class:`ASIError` on any non-zero return."""

    def __init__(self, path=""):
        self.lib = ctypes.CDLL(path or default_library_path())
        lib = self.lib
        lib.ASIGetNumOfConnectedCameras.restype = c_int
        lib.ASIGetCameraProperty.argtypes = [POINTER(CameraInfo), c_int]
        lib.ASIOpenCamera.argtypes = [c_int]
        lib.ASIInitCamera.argtypes = [c_int]
        lib.ASICloseCamera.argtypes = [c_int]
        lib.ASIGetNumOfControls.argtypes = [c_int, POINTER(c_int)]
        lib.ASIGetControlCaps.argtypes = [c_int, c_int, POINTER(ControlCaps)]
        lib.ASIGetControlValue.argtypes = [c_int, c_int, POINTER(c_long), POINTER(c_int)]
        lib.ASISetControlValue.argtypes = [c_int, c_int, c_long, c_int]
        lib.ASISetROIFormat.argtypes = [c_int, c_int, c_int, c_int, c_int]
        lib.ASISetStartPos.argtypes = [c_int, c_int, c_int]
        lib.ASIStartExposure.argtypes = [c_int, c_int]
        lib.ASIStopExposure.argtypes = [c_int]
        lib.ASIGetExpStatus.argtypes = [c_int, POINTER(c_int)]
        lib.ASIGetDataAfterExp.argtypes = [c_int, POINTER(c_ubyte), c_long]
        lib.ASIGetSerialNumber.argtypes = [c_int, POINTER(ASISerial)]
        lib.ASIGetSDKVersion.restype = c_char_p

    def _check(self, func, *args):
        code = getattr(self.lib, func)(*args)
        if code != 0:
            raise ASIError(func, code)

    def sdk_version(self):
        return self.lib.ASIGetSDKVersion().decode()

    def num_cameras(self):
        return self.lib.ASIGetNumOfConnectedCameras()

    def camera_info(self, index):
        info = CameraInfo()
        self._check("ASIGetCameraProperty", byref(info), index)
        return info

    def open(self, camera_id):
        self._check("ASIOpenCamera", camera_id)
        self._check("ASIInitCamera", camera_id)

    def close(self, camera_id):
        self._check("ASICloseCamera", camera_id)

    def controls(self, camera_id):
        n = c_int()
        self._check("ASIGetNumOfControls", camera_id, byref(n))
        caps = {}
        for i in range(n.value):
            c = ControlCaps()
            self._check("ASIGetControlCaps", camera_id, i, byref(c))
            caps[c.ControlType] = c
        return caps

    def get(self, camera_id, control):
        value, auto = c_long(), c_int()
        self._check("ASIGetControlValue", camera_id, control, byref(value), byref(auto))
        return value.value

    def set(self, camera_id, control, value, auto=False):
        self._check("ASISetControlValue", camera_id, control, int(value), int(auto))

    def set_roi(self, camera_id, width, height, binning=1, img_type=IMG_RAW16):
        self._check("ASISetROIFormat", camera_id, width, height, binning, img_type)
        self._check("ASISetStartPos", camera_id, 0, 0)

    def start_exposure(self, camera_id):
        self._check("ASIStartExposure", camera_id, 0)

    def stop_exposure(self, camera_id):
        self._check("ASIStopExposure", camera_id)

    def exposure_status(self, camera_id):
        status = c_int()
        self._check("ASIGetExpStatus", camera_id, byref(status))
        return status.value

    def read(self, camera_id, buffer):
        """Read a finished exposure into a C-contiguous numpy ``buffer``."""
        ptr = buffer.ctypes.data_as(POINTER(c_ubyte))
        self._check("ASIGetDataAfterExp", camera_id, ptr, buffer.nbytes)

    def serial_number(self, camera_id):
        sn = ASISerial()
        try:
            self._check("ASIGetSerialNumber", camera_id, byref(sn))
        except ASIError:
            return ""
        return bytes(sn.id).hex()
