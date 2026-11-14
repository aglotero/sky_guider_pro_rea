"""
iOptron iPolar Profile Manager (Profile.local.txt & Profile.global.txt)
Reverse-engineered from iOptron iPolar.exe v2.92 (0x6884b0, 0x6885c0, 0x6922b0..0x69308d).

Format details from static analysis:
- Key-value pairs stored as `Key=Base64(UTF8(Value))` one per line.
- `CenterX` and `CenterY` are stored as `int(round(value * 10.0))` in tenths of a pixel.
  Note: In iPolar's internal coordinate convention (0x692bf0 & 0x6a1600):
    - Internal `X` is vertical row [0 .. height] (e.g. 0..960 for iPolar v1)
    - Internal `Y` is horizontal column [0 .. width] (e.g. 0..1280 for iPolar v1)
- `CurrentX` and `CurrentY` (Position 1 pole coordinates) are also stored as `int(round(value * 10.0))`.
- `ExposureTime` is an integer index 0..12 into:
  [0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 800, 1000] ms.
"""

from __future__ import annotations
import base64
from dataclasses import dataclass, field
import os
from typing import Dict

DEFAULT_IPOLAR_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\lamas\AppData\Local"),
    "iOptron iPolar",
)

EXPOSURE_MS_TABLE = [0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 800.0, 1000.0]
EXPOSURE_LABELS = [
    "0.2 ms", "0.5 ms", "1 ms", "2 ms", "5 ms", "10 ms",
    "20 ms", "50 ms", "100 ms", "200 ms", "500 ms", "800 ms", "1000 ms"
]


def _b64_decode(val: str) -> str:
    try:
        return base64.b64decode(val.strip()).decode("utf-8")
    except Exception:
        return ""


def _b64_encode(val: str) -> str:
    return base64.b64encode(val.encode("utf-8")).decode("ascii")


@dataclass
class IPolarProfile:
    profile_dir: str = DEFAULT_IPOLAR_DIR
    exposure_index: int = 12  # Default 12 = 1000 ms (or 8 = 100 ms)
    gain: int = 12
    stack_frames: int = 1
    # Note: In iPolar's Profile.local.txt:
    # CenterX is vertical row (0..960), CenterY is horizontal column (0..1280)
    center_row_x: float = 0.0
    center_col_y: float = 0.0
    current_row_x: float = 0.0
    current_col_y: float = 0.0
    latitude_deg: float = -23.483055555555556
    longitude_deg: float = -43.649166666666666
    barometric_pressure_pa: int = 101325
    temperature_k: float = 283.15
    auto_confirm: bool = True
    enable_refraction: bool = True
    night_mode: bool = False
    daylight_saving: bool = False
    language: int = 0
    extra_local: Dict[str, str] = field(default_factory=dict)
    extra_global: Dict[str, str] = field(default_factory=dict)

    @property
    def local_path(self) -> str:
        return os.path.join(self.profile_dir, "Profile.local.txt")

    @property
    def global_path(self) -> str:
        return os.path.join(self.profile_dir, "Profile.global.txt")

    @property
    def dark_frame_path(self) -> str:
        return os.path.join(self.profile_dir, "DarkFrame.bin")

    @property
    def has_valid_center(self) -> bool:
        return self.center_row_x > 1.0 and self.center_col_y > 1.0

    @property
    def temperature_c(self) -> float:
        return self.temperature_k - 273.15

    @temperature_c.setter
    def temperature_c(self, val_c: float) -> None:
        self.temperature_k = val_c + 273.15

    @classmethod
    def load(cls, profile_dir: str = DEFAULT_IPOLAR_DIR) -> "IPolarProfile":
        prof = cls(profile_dir=profile_dir)
        local_raw = cls._read_kv_file(prof.local_path)
        global_raw = cls._read_kv_file(prof.global_path)
        prof.extra_local = dict(local_raw)
        prof.extra_global = dict(global_raw)

        if "ExposureTime" in local_raw:
            try:
                idx = int(local_raw["ExposureTime"])
                prof.exposure_index = max(0, min(12, idx))
            except ValueError:
                pass
        if "CenterX" in local_raw:
            try:
                prof.center_row_x = int(local_raw["CenterX"]) / 10.0
            except ValueError:
                pass
        if "CenterY" in local_raw:
            try:
                prof.center_col_y = int(local_raw["CenterY"]) / 10.0
            except ValueError:
                pass
        if "CurrentX" in local_raw:
            try:
                prof.current_row_x = int(local_raw["CurrentX"]) / 10.0
            except ValueError:
                pass
        if "CurrentY" in local_raw:
            try:
                prof.current_col_y = int(local_raw["CurrentY"]) / 10.0
            except ValueError:
                pass
        if "Latitude" in local_raw:
            try:
                prof.latitude_deg = float(local_raw["Latitude"])
            except ValueError:
                pass
        if "Longitude" in local_raw:
            try:
                prof.longitude_deg = float(local_raw["Longitude"])
            except ValueError:
                pass
        if "BarometricPressure" in local_raw:
            try:
                p = int(float(local_raw["BarometricPressure"]))
                if 10000 <= p <= 140000:
                    prof.barometric_pressure_pa = p
            except ValueError:
                pass
        if "Temperature" in local_raw:
            try:
                t = float(local_raw["Temperature"])
                if 200.0 <= t <= 350.0:
                    prof.temperature_k = t
            except ValueError:
                pass
        if "AutoConfirm" in local_raw:
            prof.auto_confirm = local_raw["AutoConfirm"] == "1"
        if "NightMode" in local_raw:
            prof.night_mode = local_raw["NightMode"] == "1"

        if "Language" in global_raw:
            try:
                prof.language = int(global_raw["Language"])
            except ValueError:
                pass

        return prof

    def save(self) -> None:
        os.makedirs(self.profile_dir, exist_ok=True)
        local_out = dict(self.extra_local)
        local_out["ExposureTime"] = str(int(self.exposure_index))
        local_out["CenterX"] = str(int(round(self.center_row_x * 10.0)))
        local_out["CenterY"] = str(int(round(self.center_col_y * 10.0)))
        local_out["Latitude"] = f"{self.latitude_deg:.15g}"
        local_out["Longitude"] = f"{self.longitude_deg:.15g}"
        local_out["BarometricPressure"] = str(int(self.barometric_pressure_pa))
        local_out["Temperature"] = f"{self.temperature_k:.15g}"
        local_out["CurrentX"] = str(int(round(self.current_row_x * 10.0)))
        local_out["CurrentY"] = str(int(round(self.current_col_y * 10.0)))
        local_out["AutoConfirm"] = "1" if self.auto_confirm else "0"
        local_out["NightMode"] = "1" if self.night_mode else "0"
        self._write_kv_file(self.local_path, local_out)

        global_out = dict(self.extra_global)
        global_out["Language"] = str(int(self.language))
        self._write_kv_file(self.global_path, global_out)

    @staticmethod
    def _read_kv_file(path: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if not os.path.exists(path):
            return result
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    key, b64_val = line.split("=", 1)
                    result[key.strip()] = _b64_decode(b64_val)
        except OSError:
            pass
        return result

    @staticmethod
    def _write_kv_file(path: str, data: Dict[str, str]) -> None:
        lines = [f"{k}={_b64_encode(v)}" for k, v in data.items()]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
