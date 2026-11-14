"""
iOptron iPolar USB Camera Driver & Realistic Sky Simulator
Reverse-engineered from iOptron iPolar.exe v2.92:
- DirectShow / MediaFoundation camera discovery: 0x67ee60..0x67fe00
- Emgu.CV / cvextern.dll P/Invoke wrappers: 0x460000..0x463100
- MainPanel_DoWork capture & raw frame unpacking: 0x69a140..0x69b350
"""

from __future__ import annotations
import ctypes
import math
import os
import subprocess
import tempfile
import time
from typing import Any, List, Optional, Tuple
import numpy as np

try:
    import cv2  # type: ignore

    HAS_CV2 = True
except ImportError:
    cv2 = None  # type: ignore
    HAS_CV2 = False

from ipolar_catalog import (
    PLATE_SCALE_IPOLAR_V1,
    PLATE_SCALE_IPOLAR_V2,
    compute_catalog_apparent,
    compute_pole_refraction_offset_deg,
)

# Exact offsets and sizes of native DLLs embedded in iOptron iPolar.exe (.rdata section)
EMBEDDED_DLLS = {
    "concrt140.dll": (0x1A8D73C, 324208),
    "msvcp140.dll": (0x1ADC9AC, 557728),
    "vcruntime140.dll": (0x1B64C4C, 124544),
    "vcruntime140_1.dll": (0x1B832CC, 49792),
    "libusb-1.0.dll": (0x1B8F54C, 148992),
    "cvextern.dll": (0x1BB3B4C, 47993832),
}

DEFAULT_EXE_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\lamas\AppData\Local"),
    "iOptron iPolar",
    "iOptron iPolar.exe",
)

# OpenCV VideoCapture properties (from 0x69a880..0x69ad50)
CAP_ANY = 0
CAP_DSHOW = 700
CAP_MSMF = 1400

CAP_PROP_FRAME_WIDTH = 3
CAP_PROP_FRAME_HEIGHT = 4
CAP_PROP_GAIN = 14
CAP_PROP_EXPOSURE = 15
CAP_PROP_CONVERT_RGB = 16


class _CvSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int)]


class CvExternWrapper:
    """Loads cvextern.dll from %TEMP%\\iOptron iPolar (or extracts it from iOptron iPolar.exe)."""

    def __init__(self, exe_path: str = DEFAULT_EXE_PATH) -> None:
        self.dll_dir = os.path.join(tempfile.gettempdir(), "iOptron iPolar")
        self.exe_path = exe_path
        self.cv: Optional[ctypes.CDLL] = None
        self._load_library()

    def _ensure_extracted(self) -> str:
        os.makedirs(self.dll_dir, exist_ok=True)
        cv_path = os.path.join(self.dll_dir, "cvextern.dll")
        if os.path.exists(cv_path) and os.path.getsize(cv_path) > 40_000_000:
            return cv_path
        if os.path.exists(self.exe_path):
            with open(self.exe_path, "rb") as f:
                for name, (offset, size) in EMBEDDED_DLLS.items():
                    out_file = os.path.join(self.dll_dir, name)
                    if not os.path.exists(out_file) or os.path.getsize(out_file) != size:
                        f.seek(offset)
                        data = f.read(size)
                        with open(out_file, "wb") as out_f:
                            out_f.write(data)
        return cv_path

    def _load_library(self) -> None:
        cv_path = self._ensure_extracted()
        if not os.path.exists(cv_path):
            return
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(self.dll_dir)
        self.cv = ctypes.CDLL(cv_path)

        # Configure ctypes signatures matching Emgu.CV P/Invoke (0x460000..0x463100)
        self.cv.cveVideoCaptureCreateFromDevice.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self.cv.cveVideoCaptureCreateFromDevice.restype = ctypes.c_void_p

        self.cv.cveVideoCaptureRelease.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.cv.cveVideoCaptureRelease.restype = None

        self.cv.cveVideoCaptureIsOpened.argtypes = [ctypes.c_void_p]
        self.cv.cveVideoCaptureIsOpened.restype = ctypes.c_bool

        self.cv.cveVideoCaptureSet.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_double]
        self.cv.cveVideoCaptureSet.restype = ctypes.c_bool

        self.cv.cveVideoCaptureGet.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.cv.cveVideoCaptureGet.restype = ctypes.c_double

        self.cv.cveVideoCaptureReadToMat.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.cv.cveVideoCaptureReadToMat.restype = ctypes.c_bool

        self.cv.cveMatCreate.argtypes = []
        self.cv.cveMatCreate.restype = ctypes.c_void_p

        self.cv.cveMatRelease.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.cv.cveMatRelease.restype = None

        self.cv.cveMatGetDataPointer.argtypes = [ctypes.c_void_p]
        self.cv.cveMatGetDataPointer.restype = ctypes.c_void_p

        self.cv.cveMatGetSize.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CvSize)]
        self.cv.cveMatGetSize.restype = None

        self.cv.cveMatGetStep.argtypes = [ctypes.c_void_p]
        self.cv.cveMatGetStep.restype = ctypes.c_int


def enumerate_ipolar_devices() -> List[Tuple[int, str, int]]:
    """
    Search connected USB video devices for 'iPolar' (version 1) or 'iPolar 2' (version 2).
    Returns list of (device_index, friendly_name, ipolar_version).
    """
    devices: List[Tuple[int, str, int]] = []
    try:
        # Query Windows PnP camera / image devices
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -in @('Camera','Image') -or $_.Name -match 'iPolar' } | Select-Object -ExpandProperty Name",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0)
        if res.returncode == 0:
            lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
            for idx, name in enumerate(lines):
                if "ipolar 2" in name.lower():
                    devices.append((idx, name, 2))
                elif "ipolar" in name.lower():
                    devices.append((idx, name, 1))
    except Exception:
        pass
    return devices


def unpack_ipolar_raw_frame(
    raw_bytes: np.ndarray,
    version: int,
    fallback_height: int = 0,
    fallback_step: int = 0,
    fallback_width: int = 0,
    debayer: bool = False,
) -> Optional[np.ndarray]:
    """
    Unpacks raw USB UVC frame bytes from iPolar v1 or v2 (with CAP_PROP_CONVERT_RGB=0):
    - v1 (0x69ab90): 640x960 16-bit raw (1,228,800 bytes) -> 960x1280 uint8 grayscale
    - v2 (0x69af90): 1458x1107 12-bit packed raw (3 bytes -> 2 pixels) -> 1096x1944 uint8 grayscale
    When `debayer=True`, converts the sensor's raw 2x2 Bayer CFA pattern (GRBG) into smooth grayscale.
    """
    # Guard against 3-channel BGR frames (e.g. 960x640x3 = 1,843,200 bytes from DSHOW when ConvertRgb=0 is ignored)
    if raw_bytes.ndim == 3 and raw_bytes.shape[2] == 3:
        return None

    raw_1d = np.ascontiguousarray(raw_bytes).view(np.uint8).ravel()
    total_bytes = int(raw_1d.size)

    img: Optional[np.ndarray] = None
    if version == 1 and total_bytes == 1280 * 960:
        # 0x69ab90: 640x960 YUY2/16-bit raw with ConvertRgb=0 is 1,228,800 bytes = 960x1280 uint8
        img = raw_1d.reshape((960, 1280)).copy()
    elif version == 2 and total_bytes >= 2916 * 1096:
        # 0x69af90: 12-bit packed raw in 1458x1107 16-bit UVC container (2916 bytes/row = 972*3 -> 1944 pixels/row)
        raw_rows = raw_1d[: 2916 * 1096].reshape((1096, 972, 3))
        out = np.empty((1096, 972, 2), dtype=np.uint8)
        out[:, :, 0] = raw_rows[:, :, 0]
        out[:, :, 1] = ((raw_rows[:, :, 1] & 0x0F) << 4) | (raw_rows[:, :, 2] >> 4)
        img = out.reshape((1096, 1944))
    elif fallback_height > 0 and fallback_step > 0 and fallback_width > 0 and total_bytes >= fallback_height * fallback_step:
        arr = raw_1d[: fallback_height * fallback_step].reshape((fallback_height, fallback_step))[:, :fallback_width]
        img = arr.copy()

    if img is not None and debayer:
        if HAS_CV2 and cv2 is not None:
            img = cv2.cvtColor(img, cv2.COLOR_BayerGR2GRAY)
    return img


class USBIPolarCamera:
    """
    Hardware USB camera interface for iOptron iPolar / iPolar 2.
    Uses standard OpenCV (`cv2.VideoCapture` from `opencv-python` with `CAP_MSMF`)
    as the primary standalone backend (zero dependency on `iOptron iPolar.exe`), with
    `CvExternWrapper` (`cvextern.dll`) kept only as an optional fallback if `cv2` is not installed.
    Implements exact frame unpacking from 0x69a880..0x69b350.
    """

    def __init__(
        self,
        device_index: int = 0,
        version: int = 1,
        use_cvextern_fallback: bool = True,
        debayer: bool = True,
    ) -> None:
        self.device_index = device_index
        self.version = version
        self.use_cvextern_fallback = use_cvextern_fallback
        self.debayer = debayer
        self._cv2_cap: Optional[Any] = None
        self.cv_wrap: Optional[CvExternWrapper] = None
        self.cap_ptr: Optional[ctypes.c_void_p] = None
        self.mat_ptr: Optional[ctypes.c_void_p] = None
        self.is_connected = False
        self.width = 1280 if version == 1 else 1944
        self.height = 960 if version == 1 else 1096
        self.plate_scale = PLATE_SCALE_IPOLAR_V1 if version == 1 else PLATE_SCALE_IPOLAR_V2
        self._current_exp_idx = -1
        self._current_gain = -1

    def _configure_cv2_cap(self, cap: Any) -> None:
        if self.version == 1:
            # 0x69a890..0x69a8de: height=960, width=640, convert_rgb=0
            cap.set(CAP_PROP_FRAME_HEIGHT, 960.0)
            cap.set(CAP_PROP_FRAME_WIDTH, 640.0)
            cap.set(CAP_PROP_CONVERT_RGB, 0.0)
            self.width = 1280
            self.height = 960
            self.plate_scale = PLATE_SCALE_IPOLAR_V1
        else:
            # 0x69acf0..0x69ad50: height=1107, width=1458, convert_rgb=0
            cap.set(CAP_PROP_FRAME_HEIGHT, 1107.0)
            cap.set(CAP_PROP_FRAME_WIDTH, 1458.0)
            cap.set(CAP_PROP_CONVERT_RGB, 0.0)
            self.width = 1944
            self.height = 1096
            self.plate_scale = PLATE_SCALE_IPOLAR_V2

    def connect(self, exposure_index: int = 12, gain: int = 12) -> bool:
        self._current_exp_idx = -1
        self._current_gain = -1

        # Candidate device indices (prefer self.device_index first, then scan 0..3 in case PnP order != MSMF order)
        candidate_indices = [self.device_index] + [i for i in range(4) if i != self.device_index]

        # 1. Primary Standalone Backend: Standard Python OpenCV (cv2.VideoCapture with CAP_MSMF)
        # Note: CAP_MSMF is required because CAP_DSHOW ignores CAP_PROP_CONVERT_RGB=0 and converts 640x960 YUY2 to 3-ch BGR.
        if HAS_CV2 and cv2 is not None:
            for idx in candidate_indices:
                try:
                    cap = cv2.VideoCapture(idx, CAP_MSMF)
                    if cap is not None and cap.isOpened():
                        self._configure_cv2_cap(cap)
                        ok, probe_mat = cap.read()
                        if ok and probe_mat is not None:
                            unpacked = unpack_ipolar_raw_frame(probe_mat, self.version)
                            if unpacked is not None and unpacked.shape == (self.height, self.width):
                                self.device_index = idx
                                self._cv2_cap = cap
                                self.set_exposure_and_gain(exposure_index, gain)
                                self.is_connected = True
                                return True
                        cap.release()
                    elif cap is not None:
                        cap.release()
                except Exception:
                    pass

        # 2. Optional Fallback Backend: cvextern.dll (only if cv2 is unavailable)
        if not self.use_cvextern_fallback:
            return False
        if self.cv_wrap is None:
            self.cv_wrap = CvExternWrapper()
        if self.cv_wrap.cv is None:
            return False
        cv = self.cv_wrap.cv

        # Try MSMF (1400) first as iPolar v2.92 does at 0x69a853, then DSHOW (700)
        for api in (CAP_MSMF, CAP_DSHOW, CAP_ANY):
            ptr = cv.cveVideoCaptureCreateFromDevice(self.device_index, api, None)
            if ptr and cv.cveVideoCaptureIsOpened(ptr):
                self.cap_ptr = ctypes.c_void_p(ptr)
                break
            elif ptr:
                p = ctypes.c_void_p(ptr)
                cv.cveVideoCaptureRelease(ctypes.byref(p))

        if not self.cap_ptr:
            return False

        self.mat_ptr = ctypes.c_void_p(cv.cveMatCreate())
        if self.version == 1:
            # 0x69a890..0x69a8de: height=960, width=640, convert_rgb=0
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_FRAME_HEIGHT, 960.0)
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_FRAME_WIDTH, 640.0)
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_CONVERT_RGB, 0.0)
            self.width = 1280
            self.height = 960
            self.plate_scale = PLATE_SCALE_IPOLAR_V1
        else:
            # 0x69acf0..0x69ad50: height=1107, width=1458, convert_rgb=0
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_FRAME_HEIGHT, 1107.0)
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_FRAME_WIDTH, 1458.0)
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_CONVERT_RGB, 0.0)
            self.width = 1944
            self.height = 1096
            self.plate_scale = PLATE_SCALE_IPOLAR_V2

        self.set_exposure_and_gain(exposure_index, gain)
        self.is_connected = True
        return True

    def set_exposure_and_gain(self, exposure_index: int, gain: int) -> None:
        if self._cv2_cap is not None:
            if exposure_index != self._current_exp_idx:
                # 0x69aab6: double exp_val = (double)(exposure_index - 13)
                self._cv2_cap.set(CAP_PROP_EXPOSURE, float(exposure_index - 13))
                self._current_exp_idx = exposure_index
            if gain != self._current_gain:
                self._cv2_cap.set(CAP_PROP_GAIN, float(gain))
                self._current_gain = gain
            return

        if not self.cap_ptr or self.cv_wrap is None or self.cv_wrap.cv is None:
            return
        cv = self.cv_wrap.cv
        if exposure_index != self._current_exp_idx:
            # 0x69aab6: double exp_val = (double)(exposure_index - 13)
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_EXPOSURE, float(exposure_index - 13))
            self._current_exp_idx = exposure_index
        if gain != self._current_gain:
            cv.cveVideoCaptureSet(self.cap_ptr, CAP_PROP_GAIN, float(gain))
            self._current_gain = gain

    def read_frame(self) -> Optional[np.ndarray]:
        if not self.is_connected:
            return None

        # 1. Primary Standalone Backend: cv2.VideoCapture
        if self._cv2_cap is not None:
            ok, mat = self._cv2_cap.read()
            if not ok or mat is None:
                return None
            h = mat.shape[0] if mat.ndim >= 2 else 0
            w = mat.shape[1] if mat.ndim >= 2 else 0
            step = int(mat.nbytes // h) if h > 0 else 0
            return unpack_ipolar_raw_frame(mat, self.version, h, step, w, debayer=self.debayer)

        # 2. Fallback Backend: cvextern.dll
        if not self.cap_ptr or not self.mat_ptr or self.cv_wrap is None or self.cv_wrap.cv is None:
            return None
        cv = self.cv_wrap.cv
        ok = cv.cveVideoCaptureReadToMat(self.cap_ptr, self.mat_ptr)
        if not ok:
            return None

        sz = _CvSize()
        cv.cveMatGetSize(self.mat_ptr, ctypes.byref(sz))
        step = cv.cveMatGetStep(self.mat_ptr)
        data_ptr = cv.cveMatGetDataPointer(self.mat_ptr)
        if not data_ptr or sz.width <= 0 or sz.height <= 0:
            return None

        total_bytes = step * sz.height
        raw_bytes = np.ctypeslib.as_array(
            ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint8)),
            shape=(total_bytes,),
        )
        return unpack_ipolar_raw_frame(raw_bytes, self.version, sz.height, step, sz.width, debayer=self.debayer)

    def close(self) -> None:
        self.is_connected = False
        if self._cv2_cap is not None:
            try:
                self._cv2_cap.release()
            except Exception:
                pass
            self._cv2_cap = None
        if self.cv_wrap is not None and self.cv_wrap.cv is not None:
            if self.mat_ptr:
                self.cv_wrap.cv.cveMatRelease(ctypes.byref(self.mat_ptr))
                self.mat_ptr = None
            if self.cap_ptr:
                self.cv_wrap.cv.cveVideoCaptureRelease(ctypes.byref(self.cap_ptr))
                self.cap_ptr = None


class SimulatedIPolarCamera:
    """
    Astrometrically accurate Sky Simulator for iOptron iPolar (1280x960, 30.94"/px).
    Simulates:
    - Exact 66 Southern or 77 Northern circumpolar catalog stars at the user's latitude/longitude
    - Mount RA Axis Center (default row=480.0, col=640.0)
    - Mount Polar Alignment error (Alt/Az offset of the Celestial Pole from the RA axis)
    - Mount RA rotation angle (rotating the mount in RA rotates the sky AND swings the pole around the RA axis center!)
    """

    def __init__(
        self,
        latitude_deg: float = -23.483055555555556,
        longitude_deg: float = -43.649166666666666,
        pressure_pa: float = 101325.0,
        temperature_k: float = 283.15,
    ) -> None:
        self.width = 1280
        self.height = 960
        self.plate_scale = PLATE_SCALE_IPOLAR_V1
        self.is_connected = False

        # True physical camera RA rotation center on the sensor (row_x, col_y)
        self.true_center_row = 480.0
        self.true_center_col = 640.0

        # Polar alignment error in pixels from the RA axis (in world/sky frame before mount rotation, or mount Alt/Az frame)
        # Wait: physically on a SkyGuider Pro, the iPolar camera is bolted to the RA shaft!
        # When the mount is misaligned from the Celestial Pole by (alt_err_px, az_err_px) in the stationary ground frame:
        # As the RA shaft (and camera) rotates by angle `ra_mount_angle_deg`, the stationary Celestial Pole
        # traces a circle on the rotating camera sensor centered at `(true_center_row, true_center_col)`!
        self.pole_offset_alt_px = -65.0   # ~33.5 arcmin vertical misalignment
        self.pole_offset_az_px = 85.0     # ~43.8 arcmin horizontal misalignment
        self.ra_mount_angle_deg = 0.0     # Mount RA rotation angle in degrees

        self.latitude_deg = latitude_deg
        self.longitude_deg = longitude_deg
        self.pressure_pa = pressure_pa
        self.temperature_k = temperature_k
        self.exposure_index = 12
        self.gain = 12

        self._cat_ra: Optional[np.ndarray] = None
        self._cat_dec: Optional[np.ndarray] = None
        self._gast_deg: float = 0.0
        self._rng = np.random.default_rng(42)
        self.update_astrometry()

    def update_astrometry(self) -> None:
        self._cat_ra, self._cat_dec, self._gast_deg = compute_catalog_apparent(
            self.latitude_deg,
            self.longitude_deg,
            self.pressure_pa,
            self.temperature_k,
            enable_refraction=True,
        )

    def connect(self, exposure_index: int = 12, gain: int = 12) -> bool:
        self.exposure_index = exposure_index
        self.gain = gain
        self.update_astrometry()
        self.is_connected = True
        return True

    def set_exposure_and_gain(self, exposure_index: int, gain: int) -> None:
        self.exposure_index = exposure_index
        self.gain = gain

    def get_true_pole_sensor_coords(self) -> Tuple[float, float, float]:
        """
        Return the exact (pole_row_x, pole_col_y, camera_sky_angle_rad) on the sensor
        for the current mount RA rotation angle `ra_mount_angle_deg` and Alt/Az alignment error.
        """
        rot_rad = math.radians(self.ra_mount_angle_deg)
        cos_r = math.cos(rot_rad)
        sin_r = math.sin(rot_rad)

        # The stationary Celestial Pole is at (pole_offset_alt_px, pole_offset_az_px) in the ground frame
        # relative to the mount's RA axis. Rotating the camera by +rot_rad rotates the sensor coordinates
        # of the pole around (true_center_row, true_center_col):
        d_row = self.pole_offset_alt_px * cos_r - self.pole_offset_az_px * sin_r
        d_col = self.pole_offset_alt_px * sin_r + self.pole_offset_az_px * cos_r

        pole_row = self.true_center_row + d_row
        pole_col = self.true_center_col + d_col
        return pole_row, pole_col, rot_rad

    def read_frame(self) -> np.ndarray:
        if self._cat_ra is None or self._cat_dec is None:
            self.update_astrometry()

        pole_row, pole_col, rot_rad = self.get_true_pole_sensor_coords()
        scale_deg = self.plate_scale / 3600.0

        # Background sky brightness + subtle read noise
        bg_level = 14.0 + 0.5 * self.gain
        frame = self._rng.normal(bg_level, 2.2, size=(self.height, self.width)).clip(0, 255).astype(np.float32)

        # Refracted pole shift: in iPolar (0x69ca60), the solver matches stars using their apparent refracted
        # coordinates and then shifts the true pole to the refracted pole. Here we place stars relative to
        # the refracted Celestial Pole so the plate solver recovers (pole_row, pole_col) directly.
        hemi_sign = 1.0 if self.latitude_deg >= 0.0 else -1.0

        # Standard tangent-plane projection around the Celestial Pole
        # Hour angle / RA orientation on the sensor rotates by rot_rad
        ra_rad = np.deg2rad(self._cat_ra) + rot_rad
        r_Search_px = (90.0 - np.abs(self._cat_dec)) / scale_deg

        # Star positions on sensor (row, col)
        star_rows = pole_row - r_Search_px * np.cos(ra_rad) * hemi_sign
        star_cols = pole_col + r_Search_px * np.sin(ra_rad)

        # Render Gaussian PSF for each star inside the sensor bounds
        exp_factor = min(1.8, 0.35 + 0.12 * self.exposure_index)
        yy, xx = np.ogrid[-4:5, -4:5]
        psf = np.exp(-(yy * yy + xx * xx) / (2.0 * (1.35 ** 2))).astype(np.float32)

        for idx in range(len(star_rows)):
            sr = float(star_rows[idx])
            sc = float(star_cols[idx])
            ir = int(round(sr))
            ic = int(round(sc))
            if 5 <= ir < self.height - 5 and 5 <= ic < self.width - 5:
                # Sub-pixel shifted Gaussian
                dr = sr - ir
                dc = sc - ic
                sub_psf = np.exp(-((yy - dr) ** 2 + (xx - dc) ** 2) / (2.0 * (1.38 ** 2))).astype(np.float32)
                peak = (210.0 - (idx % 7) * 14.0) * exp_factor
                patch = frame[ir - 4 : ir + 5, ic - 4 : ic + 5]
                np.add(patch, sub_psf * peak, out=patch)

        return np.clip(frame, 0, 255).astype(np.uint8)

    def close(self) -> None:
        self.is_connected = False
