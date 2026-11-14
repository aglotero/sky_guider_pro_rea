"""
iOptron iPolar / SkyGuider Pro Polar Alignment Suite (Windows 11 High-DPI Native Replacement)
Re-implements iOptron iPolar v2.92 without the Windows 11 Avalonia DPI layout bugs,
missing label texts, or calibration state-machine hangs.

Features:
- Native USB iPolar / iPolar 2 hardware capture via cvextern.dll + built-in Astrometric Sky Simulator
- 77-star Northern & 66-star Southern Hemisphere plate solver with precession, nutation, and Bennett refraction
- Non-hanging 2-position RA Axis Center Calibration (works even with 0 overlapping star IDs between Pos 1 & Pos 2)
- Full compatibility with %LOCALAPPDATA%\\iOptron iPolar\\Profile.local.txt and DarkFrame.bin
"""

from __future__ import annotations
from collections import deque
import ctypes
import math
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Deque, Optional, Tuple
import numpy as np

from ipolar_catalog import PLATE_SCALE_IPOLAR_V1
from ipolar_profile import DEFAULT_IPOLAR_DIR, EXPOSURE_LABELS, IPolarProfile
from ipolar_camera import (
    HAS_CV2,
    SimulatedIPolarCamera,
    USBIPolarCamera,
    cv2,
    enumerate_ipolar_devices,
)
from ipolar_solver import (
    CenterCalibrator,
    DarkFrameManager,
    IPolarPlateSolver,
    PlateSolveResult,
    extract_star_centroids,
)


def enable_win11_dpi_awareness() -> None:
    """Enable Per-Monitor v2 DPI awareness on Windows 11 without broken manual pixel scaling."""
    if sys.platform == "win32":
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def format_arcmin_arcsec(px_dist: float, plate_scale_arcsec: float) -> str:
    total_arcsec = abs(px_dist) * plate_scale_arcsec
    deg = int(total_arcsec // 3600)
    rem = total_arcsec % 3600
    arcmin = int(rem // 60)
    arcsec = rem % 60
    if deg > 0:
        return f"{deg}° {arcmin:02d}' {arcsec:04.1f}\""
    return f"{arcmin:02d}' {arcsec:04.1f}\""


class SettingsDialog(tk.Toplevel):
    """Clean, DPI-safe Settings & Site Configuration dialog (replaces broken MaskedTextBox dialog)."""

    def __init__(self, parent: "IPolarApp", profile: IPolarProfile) -> None:
        super().__init__(parent.root)
        self.parent_app = parent
        self.profile = profile
        self.title("iPolar Settings & Site Location")
        self.configure(bg="#070000" if parent.var_night_mode.get() else "#181a20")
        self.resizable(False, False)
        self.transient(parent.root)
        self.grab_set()

        pad = {"padx": 12, "pady": 6}
        main = ttk.Frame(self, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            main,
            text="Site Location & Atmospheric Refraction",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(main, text="Latitude (° N+, S-):").grid(row=1, column=0, sticky="w", **pad)
        self.var_lat = tk.StringVar(value=f"{profile.latitude_deg:.6f}")
        ttk.Entry(main, textvariable=self.var_lat, width=22).grid(row=1, column=1, sticky="ew", **pad)

        ttk.Label(main, text="Longitude (° E+, W-):").grid(row=2, column=0, sticky="w", **pad)
        self.var_lon = tk.StringVar(value=f"{profile.longitude_deg:.6f}")
        ttk.Entry(main, textvariable=self.var_lon, width=22).grid(row=2, column=1, sticky="ew", **pad)

        ttk.Label(main, text="Barometric Pressure (Pa):").grid(row=3, column=0, sticky="w", **pad)
        self.var_press = tk.StringVar(value=str(int(profile.barometric_pressure_pa)))
        ttk.Entry(main, textvariable=self.var_press, width=22).grid(row=3, column=1, sticky="ew", **pad)

        ttk.Label(main, text="Temperature (°C):").grid(row=4, column=0, sticky="w", **pad)
        self.var_temp_c = tk.StringVar(value=f"{profile.temperature_c:.1f}")
        ttk.Entry(main, textvariable=self.var_temp_c, width=22).grid(row=4, column=1, sticky="ew", **pad)

        self.var_refract = tk.BooleanVar(value=profile.enable_refraction)
        ttk.Checkbutton(
            main,
            text="Enable Atmospheric Refraction (5° < |Lat| <= 85°)",
            variable=self.var_refract,
        ).grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        ttk.Separator(main, orient=tk.HORIZONTAL).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=10
        )

        ttk.Label(
            main,
            text="RA Axis Center on Sensor (Internal Row X, Col Y)",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(main, text="CenterX (Vertical Row 0..960):").grid(row=8, column=0, sticky="w", **pad)
        self.var_cx = tk.StringVar(value=f"{profile.center_row_x:.1f}")
        ttk.Entry(main, textvariable=self.var_cx, width=22).grid(row=8, column=1, sticky="ew", **pad)

        ttk.Label(main, text="CenterY (Horizontal Col 0..1280):").grid(row=9, column=0, sticky="w", **pad)
        self.var_cy = tk.StringVar(value=f"{profile.center_col_y:.1f}")
        ttk.Entry(main, textvariable=self.var_cy, width=22).grid(row=9, column=1, sticky="ew", **pad)

        btn_row = ttk.Frame(main)
        btn_row.grid(row=10, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_row, text="Save & Apply", command=self._on_save).pack(side=tk.RIGHT)

    def _on_save(self) -> None:
        try:
            lat = float(self.var_lat.get().strip())
            lon = float(self.var_lon.get().strip())
            press = int(float(self.var_press.get().strip()))
            temp_c = float(self.var_temp_c.get().strip())
            cx = float(self.var_cx.get().strip())
            cy = float(self.var_cy.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter valid numeric values.", parent=self)
            return

        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            messagebox.showerror(
                "Invalid Coordinates",
                "Latitude must be in [-90, 90] and Longitude in [-180, 180].",
                parent=self,
            )
            return

        self.profile.latitude_deg = lat
        self.profile.longitude_deg = lon
        self.profile.barometric_pressure_pa = max(10000, min(140000, press))
        self.profile.temperature_c = max(-60.0, min(60.0, temp_c))
        self.profile.enable_refraction = bool(self.var_refract.get())
        self.profile.center_row_x = max(0.0, min(1096.0, cx))
        self.profile.center_col_y = max(0.0, min(1944.0, cy))
        self.profile.save()
        self.parent_app.on_settings_updated()
        self.destroy()


class IPolarApp:
    """Main Windows 11 High-DPI iOptron iPolar Replacement GUI."""

    def __init__(self, root: tk.Tk, force_simulator: bool = False, force_night: bool = False) -> None:
        self.root = root
        self.root.title("iOptron iPolar / SkyGuider Pro Polar Alignment (Win11 Fixed Edition)")
        self.root.geometry("1260x820")
        self.root.minsize(720, 420)
        self.root.configure(bg="#111318")

        self.profile = IPolarProfile.load(DEFAULT_IPOLAR_DIR)
        if force_night:
            self.profile.night_mode = True
        self.dark_mgr = DarkFrameManager(self.profile.dark_frame_path)
        self.dark_mgr.load(960, 1280)

        self.solver = IPolarPlateSolver(
            latitude_deg=self.profile.latitude_deg,
            longitude_deg=self.profile.longitude_deg,
            pressure_pa=self.profile.barometric_pressure_pa,
            temperature_k=self.profile.temperature_k,
            enable_refraction=self.profile.enable_refraction,
        )

        self.calibrator = CenterCalibrator()
        self.calibrator.reset(
            has_saved_center=self.profile.has_valid_center,
            center_row=self.profile.center_row_x,
            center_col=self.profile.center_col_y,
        )

        self.usb_camera: Optional[USBIPolarCamera] = None
        self.sim_camera = SimulatedIPolarCamera(
            latitude_deg=self.profile.latitude_deg,
            longitude_deg=self.profile.longitude_deg,
            pressure_pa=self.profile.barometric_pressure_pa,
            temperature_k=self.profile.temperature_k,
        )
        if self.profile.has_valid_center:
            self.sim_camera.true_center_row = self.profile.center_row_x
            self.sim_camera.true_center_col = self.profile.center_col_y

        self.is_connected = False
        self.use_simulator = tk.BooleanVar(value=force_simulator)
        self.var_device_idx = tk.IntVar(value=0)
        self.var_exp_label = tk.StringVar(
            value=EXPOSURE_LABELS[max(0, min(12, self.profile.exposure_index))]
        )
        self.var_gain = tk.IntVar(value=self.profile.gain)
        self.var_stack = tk.IntVar(value=1)
        self.var_raw_image = tk.BooleanVar(value=False)
        self.var_auto_confirm = tk.BooleanVar(value=self.profile.auto_confirm)
        self.var_zoom_target = tk.BooleanVar(value=False)
        self.var_night_mode = tk.BooleanVar(value=self.profile.night_mode)
        self._has_usb_cam = False

        self.capturing_dark = False
        self.frame_stack: Deque[np.ndarray] = deque(maxlen=8)
        self.last_frame: Optional[np.ndarray] = None
        self.last_solve = PlateSolveResult()
        self._photo_image: Optional[tk.PhotoImage] = None
        self._fps_times: Deque[float] = deque(maxlen=20)

        self._setup_styles(self.var_night_mode.get())
        self._build_ui()

        # Detect USB iPolar cameras on startup
        found = enumerate_ipolar_devices()
        if found:
            idx, name, _ver = found[0]
            self._has_usb_cam = True
            self.var_device_idx.set(idx)
            self._usb_cam_text = f"USB Detected: {name} (#{idx})"
        else:
            self._has_usb_cam = False
            self._usb_cam_text = "No USB iPolar found (Simulator ready)"
            self.use_simulator.set(True)

        self._apply_theme_colors()
        self._refresh_dark_status()
        self._update_info_labels()

        self._schedule_loop()

    def _setup_styles(self, night_mode: bool = False) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        if night_mode:
            bg_dark = "#070000"
            bg_card = "#120202"
            fg_text = "#ff4d4d"
            fg_header = "#ff3333"
            fg_status = "#ff6666"
            border_col = "#4a0e0e"
            btn_bg = "#260505"
            btn_active = "#3d0808"
            btn_dis_bg = "#140202"
            btn_dis_fg = "#661a1a"
            accent = "#6e0b0b"
            accent_active = "#8f1111"
            accent_dis = "#290404"
            accent_fg = "#ffcccc"
            field_bg = "#0a0000"
        else:
            bg_dark = "#181a20"
            bg_card = "#20242d"
            fg_text = "#e6edf3"
            fg_header = "#79c0ff"
            fg_status = "#58a6ff"
            border_col = "#30363d"
            btn_bg = "#2d333b"
            btn_active = "#3b434d"
            btn_dis_bg = "#1c2026"
            btn_dis_fg = "#6e7681"
            accent = "#2f81f7"
            accent_active = "#388bfd"
            accent_dis = "#26354a"
            accent_fg = "#ffffff"
            field_bg = "#111318"

        style.configure(".", background=bg_dark, foreground=fg_text, font=("Segoe UI", 9))
        style.configure("TFrame", background=bg_dark)
        style.configure("Card.TFrame", background=bg_card)
        style.configure(
            "TLabelframe",
            background=bg_card,
            foreground=fg_header,
            bordercolor=border_col,
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            background=bg_card,
            foreground=fg_header,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure("TLabel", background=bg_card, foreground=fg_text)
        style.configure("Header.TLabel", background=bg_dark, foreground=fg_header, font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", background=bg_card, foreground=fg_status, font=("Segoe UI", 9, "bold"))
        style.configure("TCheckbutton", background=bg_card, foreground=fg_text)
        style.map("TCheckbutton", background=[("active", bg_card)], foreground=[("active", fg_header)])
        style.configure(
            "TButton",
            background=btn_bg,
            foreground=fg_text,
            bordercolor=border_col,
            padding=(8, 5),
            font=("Segoe UI", 9),
        )
        style.map(
            "TButton",
            background=[("active", btn_active), ("disabled", btn_dis_bg)],
            foreground=[("disabled", btn_dis_fg)],
        )
        style.configure(
            "Accent.TButton",
            background=accent,
            foreground=accent_fg,
            bordercolor=border_col,
            font=("Segoe UI", 9, "bold"),
            padding=(8, 6),
        )
        style.map(
            "Accent.TButton",
            background=[("active", accent_active), ("disabled", accent_dis)],
            foreground=[("disabled", btn_dis_fg)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=field_bg,
            background=btn_bg,
            foreground=fg_text,
            arrowcolor=fg_text,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", field_bg)],
            selectbackground=[("readonly", btn_active)],
            selectforeground=[("readonly", fg_text)],
            foreground=[("readonly", fg_text)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=field_bg,
            background=btn_bg,
            foreground=fg_text,
            arrowcolor=fg_text,
        )
        style.configure(
            "TEntry",
            fieldbackground=field_bg,
            foreground=fg_text,
        )
        style.configure(
            "Horizontal.TScale",
            background=bg_card,
            troughcolor=field_bg,
        )
        style.configure(
            "Vertical.TScrollbar",
            background=btn_bg,
            troughcolor=bg_dark,
            bordercolor=border_col,
            arrowcolor=fg_text,
        )

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(0, weight=1)

        # Left pane: Video & Astrometric Overlay Canvas
        left_frame = ttk.Frame(self.root, padding=(10, 10, 4, 10))
        left_frame.grid(row=0, column=0, sticky="nsew")
        left_frame.rowconfigure(0, weight=1)
        left_frame.rowconfigure(1, weight=0)
        left_frame.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            left_frame,
            bg="#090b0e",
            highlightthickness=1,
            highlightbackground="#30363d",
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")

        # Bottom Telemetry & Alignment Guidance Bar under the video feed
        guidance_bar = ttk.LabelFrame(left_frame, text="Polar Alignment Live Guidance", padding=(12, 6))
        guidance_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for c in range(4):
            guidance_bar.columnconfigure(c, weight=1)

        self.lbl_pole_coords = ttk.Label(guidance_bar, text="Celestial Pole: --", font=("Consolas", 10))
        self.lbl_pole_coords.grid(row=0, column=0, sticky="w", padx=6)

        self.lbl_center_coords = ttk.Label(guidance_bar, text="RA Axis Center: --", font=("Consolas", 10))
        self.lbl_center_coords.grid(row=0, column=1, sticky="w", padx=6)

        self.lbl_alt_guide = ttk.Label(
            guidance_bar, text="ALT: --", font=("Segoe UI", 10, "bold"), foreground="#7ee787"
        )
        self.lbl_alt_guide.grid(row=0, column=2, sticky="w", padx=6)

        self.lbl_az_guide = ttk.Label(
            guidance_bar, text="AZ: --", font=("Segoe UI", 10, "bold"), foreground="#7ee787"
        )
        self.lbl_az_guide.grid(row=0, column=3, sticky="w", padx=6)

        # Right pane: Scrollable Responsive Control Sidebar
        self.right_container = ttk.Frame(self.root, padding=(4, 10, 6, 10))
        self.right_container.grid(row=0, column=1, sticky="ns")
        self.right_container.rowconfigure(0, weight=1)
        self.right_container.columnconfigure(0, weight=1)

        self.sidebar_canvas = tk.Canvas(
            self.right_container,
            bg="#181a20",
            highlightthickness=0,
            bd=0,
            width=345,
        )
        self.sidebar_canvas.grid(row=0, column=0, sticky="ns")

        self.sidebar_scrollbar = ttk.Scrollbar(
            self.right_container,
            orient=tk.VERTICAL,
            command=self.sidebar_canvas.yview,
        )
        self.sidebar_canvas.configure(yscrollcommand=self.sidebar_scrollbar.set)

        self.right_outer = ttk.Frame(self.sidebar_canvas, padding=(2, 0, 6, 4))
        self._sidebar_win_id = self.sidebar_canvas.create_window(
            (0, 0), window=self.right_outer, anchor="nw"
        )
        right_outer = self.right_outer

        self.right_outer.bind("<Configure>", self._on_sidebar_configure)
        self.sidebar_canvas.bind("<Configure>", self._on_sidebar_configure)
        self.right_container.bind("<Enter>", self._bind_sidebar_mousewheel)
        self.right_container.bind("<Leave>", self._unbind_sidebar_mousewheel)

        # 1. Camera & Exposure Controls
        grp_cam = ttk.LabelFrame(right_outer, text="1. Camera & Capture", padding=10)
        grp_cam.pack(fill=tk.X, pady=(0, 8))
        grp_cam.columnconfigure(1, weight=1)

        self.lbl_cam_detected = ttk.Label(grp_cam, text="Scanning USB...", font=("Segoe UI", 8))
        self.lbl_cam_detected.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        ttk.Checkbutton(
            grp_cam,
            text="Night Mode (Red)",
            variable=self.var_night_mode,
            command=self._on_night_mode_toggle,
        ).grid(row=0, column=2, sticky="e", pady=(0, 4))

        ttk.Checkbutton(
            grp_cam,
            text="Use Built-in Sky Simulator",
            variable=self.use_simulator,
            command=self._on_mode_toggle,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=2)

        idx_frame = ttk.Frame(grp_cam, style="Card.TFrame")
        idx_frame.grid(row=1, column=2, sticky="e")
        ttk.Label(idx_frame, text="USB#:").pack(side=tk.LEFT)
        ttk.Spinbox(idx_frame, from_=0, to=8, textvariable=self.var_device_idx, width=3).pack(side=tk.LEFT)

        self.btn_connect = ttk.Button(
            grp_cam, text="Connect Camera", style="Accent.TButton", command=self.toggle_connect
        )
        self.btn_connect.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 6), padx=(0, 4))

        ttk.Button(grp_cam, text="Settings...", command=self.open_settings).grid(
            row=2, column=2, sticky="ew", pady=(6, 6)
        )

        ttk.Label(grp_cam, text="Exposure:").grid(row=3, column=0, sticky="w", pady=2)
        self.cmb_exp = ttk.Combobox(
            grp_cam,
            textvariable=self.var_exp_label,
            values=EXPOSURE_LABELS,
            state="readonly",
            width=12,
        )
        self.cmb_exp.grid(row=3, column=1, sticky="ew", pady=2, padx=4)
        self.cmb_exp.bind("<<ComboboxSelected>>", self._on_exposure_changed)

        ttk.Checkbutton(
            grp_cam, text="RAW Image", variable=self.var_raw_image
        ).grid(row=3, column=2, sticky="e", pady=2)

        ttk.Label(grp_cam, text="Gain:").grid(row=4, column=0, sticky="w", pady=2)
        self.lbl_gain_val = ttk.Label(grp_cam, text=f"{self.profile.gain}")
        self.lbl_gain_val.grid(row=4, column=2, sticky="e")
        self.scale_gain = ttk.Scale(
            grp_cam,
            from_=0,
            to=24,
            orient=tk.HORIZONTAL,
        )
        self.scale_gain.set(self.profile.gain)
        self.scale_gain.configure(command=self._on_gain_changed)
        self.scale_gain.grid(row=4, column=1, sticky="ew", pady=2, padx=4)

        ttk.Label(grp_cam, text="Stack:").grid(row=5, column=0, sticky="w", pady=2)
        cmb_stack = ttk.Combobox(
            grp_cam,
            textvariable=self.var_stack,
            values=[1, 2, 4, 8],
            state="readonly",
            width=6,
        )
        cmb_stack.grid(row=5, column=1, sticky="w", pady=2, padx=4)

        ttk.Checkbutton(
            grp_cam, text="Zoom Target", variable=self.var_zoom_target
        ).grid(row=5, column=2, sticky="e", pady=2)

        # Telemetry labels (always visible, never blank!)
        tel_frame = ttk.Frame(grp_cam, style="Card.TFrame")
        tel_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        tel_frame.columnconfigure(0, weight=1)
        tel_frame.columnconfigure(1, weight=1)
        self.lbl_fps = ttk.Label(tel_frame, text="FPS: 0.0  |  Stars: 0 (Matched: 0)")
        self.lbl_fps.grid(row=0, column=0, columnspan=2, sticky="w")
        self.lbl_bg = ttk.Label(tel_frame, text="Background Brightness: 0.0  |  RMS: --")
        self.lbl_bg.grid(row=1, column=0, columnspan=2, sticky="w")

        # 2. Dark Frame Section
        grp_dark = ttk.LabelFrame(right_outer, text="2. Dark Frame Subtraction", padding=10)
        grp_dark.pack(fill=tk.X, pady=(0, 8))
        grp_dark.columnconfigure(0, weight=1)
        grp_dark.columnconfigure(1, weight=1)

        self.lbl_dark_status = ttk.Label(grp_dark, text="Dark Frame: Checking...")
        self.lbl_dark_status.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.btn_take_dark = ttk.Button(
            grp_dark, text="Take Dark Frame (5f)", command=self.start_dark_capture
        )
        self.btn_take_dark.grid(row=1, column=0, sticky="ew", padx=(0, 4))

        self.btn_erase_dark = ttk.Button(
            grp_dark, text="Erase Dark Frame", command=self.erase_dark_frame
        )
        self.btn_erase_dark.grid(row=1, column=1, sticky="ew")

        # 3. RA Axis Center Calibration (Non-hanging!)
        grp_cal = ttk.LabelFrame(right_outer, text="3. Camera Center Calibration", padding=10)
        grp_cal.pack(fill=tk.X, pady=(0, 8))
        grp_cal.columnconfigure(0, weight=1)
        grp_cal.columnconfigure(1, weight=1)

        self.lbl_cal_state = ttk.Label(
            grp_cal,
            text=self.calibrator.status.message,
            style="Status.TLabel",
            wraplength=300,
        )
        self.lbl_cal_state.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.lbl_cal_rot = ttk.Label(
            grp_cal,
            text="RA Rotation: 0.0°  |  Stability: 0/3 frames",
            font=("Consolas", 9),
        )
        self.lbl_cal_rot.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Checkbutton(
            grp_cal,
            text="Auto-Confirm Position 2 when Rotation >= 30°",
            variable=self.var_auto_confirm,
            command=self._on_autoconfirm_changed,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.btn_confirm_pos = ttk.Button(
            grp_cal,
            text="Confirm Position 1",
            style="Accent.TButton",
            command=self.on_confirm_position_clicked,
        )
        self.btn_confirm_pos.grid(row=3, column=0, sticky="ew", padx=(0, 4))

        self.btn_restart_cal = ttk.Button(
            grp_cal,
            text="Recalibrate Center",
            command=self.on_restart_calibration,
        )
        self.btn_restart_cal.grid(row=3, column=1, sticky="ew")

        # 4. Site Info Summary
        grp_info = ttk.LabelFrame(right_outer, text="4. Site & Astrometry Info", padding=10)
        grp_info.pack(fill=tk.X, pady=(0, 8))
        self.lbl_site_info = ttk.Label(grp_info, text="", justify=tk.LEFT, font=("Consolas", 9))
        self.lbl_site_info.pack(anchor="w")

        # 5. Interactive Sky Simulator Controls (for indoor testing & calibration verification)
        self.grp_sim = ttk.LabelFrame(
            right_outer, text="5. Sky Simulator Mount Controls", padding=10
        )
        self.grp_sim.pack(fill=tk.X)
        self.grp_sim.columnconfigure(1, weight=1)

        ttk.Label(self.grp_sim, text="RA Axis Angle (°):").grid(row=0, column=0, sticky="w", pady=2)
        self.lbl_sim_ra = ttk.Label(self.grp_sim, text="0.0°", width=7)
        self.lbl_sim_ra.grid(row=0, column=2, sticky="e")
        self.scale_sim_ra = ttk.Scale(
            self.grp_sim,
            from_=-180.0,
            to=180.0,
            orient=tk.HORIZONTAL,
        )
        self.scale_sim_ra.set(0.0)
        self.scale_sim_ra.grid(row=0, column=1, sticky="ew", padx=4)

        ttk.Label(self.grp_sim, text="Alt Knob Err (px):").grid(row=1, column=0, sticky="w", pady=2)
        self.lbl_sim_alt = ttk.Label(self.grp_sim, text="-65.0", width=7)
        self.lbl_sim_alt.grid(row=1, column=2, sticky="e")
        self.scale_sim_alt = ttk.Scale(
            self.grp_sim,
            from_=-150.0,
            to=150.0,
            orient=tk.HORIZONTAL,
        )
        self.scale_sim_alt.set(self.sim_camera.pole_offset_alt_px)
        self.scale_sim_alt.grid(row=1, column=1, sticky="ew", padx=4)

        ttk.Label(self.grp_sim, text="Az Knob Err (px):").grid(row=2, column=0, sticky="w", pady=2)
        self.lbl_sim_az = ttk.Label(self.grp_sim, text="+85.0", width=7)
        self.lbl_sim_az.grid(row=2, column=2, sticky="e")
        self.scale_sim_az = ttk.Scale(
            self.grp_sim,
            from_=-150.0,
            to=150.0,
            orient=tk.HORIZONTAL,
        )
        self.scale_sim_az.set(self.sim_camera.pole_offset_az_px)
        self.scale_sim_az.grid(row=2, column=1, sticky="ew", padx=4)

        self.scale_sim_ra.configure(command=self._on_sim_slider_changed)
        self.scale_sim_alt.configure(command=self._on_sim_slider_changed)
        self.scale_sim_az.configure(command=self._on_sim_slider_changed)

        sim_btns = ttk.Frame(self.grp_sim, style="Card.TFrame")
        sim_btns.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(
            sim_btns,
            text="Rotate RA +60° (Test Pos 2)",
            command=lambda: self._nudge_sim_ra(60.0),
        ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        ttk.Button(
            sim_btns,
            text="Align Pole (Zero Error)",
            command=self._zero_sim_alignment_error,
        ).pack(side=tk.LEFT, expand=True, fill=tk.X)

        self._update_calibration_buttons()

    def _on_sidebar_configure(self, _event: object = None) -> None:
        """Update right sidebar scrollregion and auto-show/hide the vertical scrollbar."""
        req_w = max(340, self.right_outer.winfo_reqwidth())
        req_h = self.right_outer.winfo_reqheight()
        canvas_h = self.sidebar_canvas.winfo_height()

        if self.sidebar_canvas.winfo_width() != req_w:
            self.sidebar_canvas.configure(width=req_w)
        self.sidebar_canvas.itemconfigure(self._sidebar_win_id, width=req_w)
        self.sidebar_canvas.configure(scrollregion=(0, 0, req_w, req_h))

        # Show vertical scrollbar only when window height is smaller than sidebar content
        if canvas_h > 10 and req_h > canvas_h:
            if not self.sidebar_scrollbar.winfo_ismapped():
                self.sidebar_scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            if self.sidebar_scrollbar.winfo_ismapped():
                self.sidebar_scrollbar.grid_remove()
            self.sidebar_canvas.yview_moveto(0.0)

    def _bind_sidebar_mousewheel(self, _event: object = None) -> None:
        self.root.bind_all("<MouseWheel>", self._on_sidebar_mousewheel)

    def _unbind_sidebar_mousewheel(self, _event: object = None) -> None:
        self.root.unbind_all("<MouseWheel>")

    def _on_sidebar_mousewheel(self, event: tk.Event) -> None:
        # Only scroll when content exceeds visible canvas height
        if self.right_outer.winfo_reqheight() <= self.sidebar_canvas.winfo_height():
            return
        # Avoid hijacking mousewheel while adjusting a Combobox or Scale
        widget_cls = event.widget.winfo_class() if hasattr(event, "widget") and event.widget else ""
        if widget_cls in ("TCombobox", "TScale", "TSpinbox"):
            return
        steps = -1 * int(event.delta / 120) if event.delta else 0
        if steps != 0:
            self.sidebar_canvas.yview_scroll(steps, "units")

    def _on_mode_toggle(self) -> None:
        if self.is_connected:
            self.toggle_connect()

    def _on_night_mode_toggle(self) -> None:
        nm = bool(self.var_night_mode.get())
        self.profile.night_mode = nm
        self.profile.save()
        self._apply_theme_colors()
        self._refresh_dark_status()
        self._render_canvas()

    def _apply_theme_colors(self) -> None:
        nm = bool(self.var_night_mode.get())
        self._setup_styles(nm)
        if nm:
            self.root.configure(bg="#070000")
            self.canvas.configure(bg="#040000", highlightbackground="#4a0e0e")
            self.sidebar_canvas.configure(bg="#070000")
            self.lbl_cam_detected.configure(
                text=getattr(self, "_usb_cam_text", ""),
                foreground="#ff4d4d" if self._has_usb_cam else "#cc3333",
            )
            self.lbl_alt_guide.configure(foreground="#ff4d4d")
            self.lbl_az_guide.configure(foreground="#ff4d4d")
        else:
            self.root.configure(bg="#111318")
            self.canvas.configure(bg="#090b0e", highlightbackground="#30363d")
            self.sidebar_canvas.configure(bg="#181a20")
            self.lbl_cam_detected.configure(
                text=getattr(self, "_usb_cam_text", ""),
                foreground="#5ce1e6" if self._has_usb_cam else "#f5b041",
            )
            self.lbl_alt_guide.configure(foreground="#7ee787")
            self.lbl_az_guide.configure(foreground="#7ee787")

    def _on_exposure_changed(self, _event: object = None) -> None:
        label = self.var_exp_label.get()
        if label in EXPOSURE_LABELS:
            idx = EXPOSURE_LABELS.index(label)
            self.profile.exposure_index = idx
            self.profile.save()
            if self.usb_camera and self.usb_camera.is_connected:
                self.usb_camera.set_exposure_and_gain(idx, self.profile.gain)
            self.sim_camera.set_exposure_and_gain(idx, self.profile.gain)

    def _on_gain_changed(self, val_str: str) -> None:
        g = int(round(float(val_str)))
        self.profile.gain = g
        self.lbl_gain_val.configure(text=str(g))
        if self.usb_camera and self.usb_camera.is_connected:
            self.usb_camera.set_exposure_and_gain(self.profile.exposure_index, g)
        self.sim_camera.set_exposure_and_gain(self.profile.exposure_index, g)

    def _on_autoconfirm_changed(self) -> None:
        self.profile.auto_confirm = bool(self.var_auto_confirm.get())
        self.profile.save()

    def _on_sim_slider_changed(self, _val: str = "") -> None:
        ra = float(self.scale_sim_ra.get())
        alt = float(self.scale_sim_alt.get())
        az = float(self.scale_sim_az.get())
        self.sim_camera.ra_mount_angle_deg = ra
        self.sim_camera.pole_offset_alt_px = alt
        self.sim_camera.pole_offset_az_px = az
        self.lbl_sim_ra.configure(text=f"{ra:+.1f}°")
        self.lbl_sim_alt.configure(text=f"{alt:+.1f}")
        self.lbl_sim_az.configure(text=f"{az:+.1f}")

    def _nudge_sim_ra(self, delta_deg: float) -> None:
        new_ra = ((float(self.scale_sim_ra.get()) + delta_deg + 180.0) % 360.0) - 180.0
        self.scale_sim_ra.set(new_ra)
        self._on_sim_slider_changed()

    def _zero_sim_alignment_error(self) -> None:
        self.scale_sim_alt.set(0.0)
        self.scale_sim_az.set(0.0)
        self._on_sim_slider_changed()

    def _refresh_dark_status(self) -> None:
        nm = bool(self.var_night_mode.get())
        if self.dark_mgr.dark_frame is not None:
            mean_val = float(np.mean(self.dark_mgr.dark_frame))
            self.lbl_dark_status.configure(
                text=f"Dark Frame Loaded (1280x960, avg={mean_val:.1f})",
                foreground="#ff4d4d" if nm else "#7ee787",
            )
            self.btn_erase_dark.state(["!disabled"])
        else:
            self.lbl_dark_status.configure(
                text="No Dark Frame (Using auto background floor)",
                foreground="#cc3333" if nm else "#f5b041",
            )
            self.btn_erase_dark.state(["disabled"])

    def _update_info_labels(self) -> None:
        hemi = "Southern (66 stars)" if self.profile.latitude_deg < 0 else "Northern (77 stars)"
        refr = "ON" if self.profile.enable_refraction else "OFF"
        txt = (
            f"Hemisphere : {hemi}\n"
            f"Latitude   : {self.profile.latitude_deg:+.5f}°\n"
            f"Longitude  : {self.profile.longitude_deg:+.5f}°\n"
            f"Atm Refr.  : {refr} ({self.profile.barometric_pressure_pa} Pa, {self.profile.temperature_c:.1f}°C)\n"
            f"RA Center  : ({self.profile.center_row_x:.1f}, {self.profile.center_col_y:.1f})"
        )
        self.lbl_site_info.configure(text=txt)

    def on_settings_updated(self) -> None:
        self.solver.latitude_deg = self.profile.latitude_deg
        self.solver.longitude_deg = self.profile.longitude_deg
        self.solver.pressure_pa = self.profile.barometric_pressure_pa
        self.solver.temperature_k = self.profile.temperature_k
        self.solver.enable_refraction = self.profile.enable_refraction
        self.solver.update_catalog()

        self.sim_camera.latitude_deg = self.profile.latitude_deg
        self.sim_camera.longitude_deg = self.profile.longitude_deg
        self.sim_camera.pressure_pa = self.profile.barometric_pressure_pa
        self.sim_camera.temperature_k = self.profile.temperature_k
        self.sim_camera.update_astrometry()

        if self.profile.has_valid_center:
            self.calibrator.reset(
                has_saved_center=True,
                center_row=self.profile.center_row_x,
                center_col=self.profile.center_col_y,
            )
        self._update_info_labels()
        self._update_calibration_buttons()

    def open_settings(self) -> None:
        SettingsDialog(self, self.profile)

    def toggle_connect(self) -> None:
        if self.is_connected:
            self.is_connected = False
            if self.usb_camera:
                self.usb_camera.close()
                self.usb_camera = None
            self.sim_camera.close()
            self.btn_connect.configure(text="Connect Camera")
            self.lbl_cal_state.configure(text="Camera Disconnected.")
            return

        self.frame_stack.clear()
        if self.use_simulator.get():
            self.sim_camera.connect(self.profile.exposure_index, self.profile.gain)
            self.is_connected = True
            self.btn_connect.configure(text="Disconnect (Sim)")
        else:
            idx = int(self.var_device_idx.get())
            usb = USBIPolarCamera(device_index=idx, version=1)
            if not usb.connect(self.profile.exposure_index, self.profile.gain):
                messagebox.showwarning(
                    "USB iPolar Not Found",
                    f"Could not open USB camera at index {idx} via DirectShow/MediaFoundation.\n\n"
                    "Switching to Built-in Sky Simulator so you can test calibration and alignment.",
                )
                self.use_simulator.set(True)
                self.sim_camera.connect(self.profile.exposure_index, self.profile.gain)
                self.is_connected = True
                self.btn_connect.configure(text="Disconnect (Sim)")
            else:
                self.usb_camera = usb
                self.is_connected = True
                self.btn_connect.configure(text="Disconnect USB")

        self._update_calibration_buttons()

    def start_dark_capture(self) -> None:
        if not self.is_connected:
            messagebox.showinfo("Connect Camera", "Please connect the camera first.")
            return
        self.capturing_dark = True
        self.dark_mgr._accum.clear()
        nm = bool(self.var_night_mode.get())
        self.lbl_dark_status.configure(
            text="Capturing Dark Frame (0/5)...",
            foreground="#ff6666" if nm else "#58a6ff",
        )

    def erase_dark_frame(self) -> None:
        self.dark_mgr.erase()
        self._refresh_dark_status()

    def on_restart_calibration(self) -> None:
        self.calibrator.start_calibration()
        self._update_calibration_buttons()

    def on_confirm_position_clicked(self) -> None:
        st = self.calibrator.status
        if st.state in (0, 1, 3):
            if self.calibrator.confirm_position_1(self.last_solve):
                self.profile.current_row_x = self.last_solve.pole_row_x
                self.profile.current_col_y = self.last_solve.pole_col_y
                self.profile.save()
        elif st.state == 2:
            center = self.calibrator.confirm_position_2()
            if center is not None:
                self.profile.center_row_x, self.profile.center_col_y = center
                self.profile.save()
                self._update_info_labels()
        self._update_calibration_buttons()

    def _update_calibration_buttons(self) -> None:
        st = self.calibrator.status
        self.lbl_cal_state.configure(text=st.message)
        self.lbl_cal_rot.configure(
            text=f"RA Rotation: {st.current_rotation_deg:5.1f}°  |  Stability: {st.stable_count}/3"
        )
        if st.state == 1:
            self.btn_confirm_pos.configure(text="Confirm Position 1")
            if self.is_connected and self.last_solve.solved:
                self.btn_confirm_pos.state(["!disabled"])
            else:
                self.btn_confirm_pos.state(["disabled"])
        elif st.state == 2:
            self.btn_confirm_pos.configure(text="Confirm Position 2")
            if st.can_confirm_pos2 or (st.candidate_center is not None and st.current_rotation_deg >= 15.0):
                self.btn_confirm_pos.state(["!disabled"])
            else:
                self.btn_confirm_pos.state(["disabled"])
        else:
            self.btn_confirm_pos.configure(text="Confirm Position 1")
            if self.is_connected and self.last_solve.solved:
                self.btn_confirm_pos.state(["!disabled"])
            else:
                self.btn_confirm_pos.state(["disabled"])

    def _schedule_loop(self) -> None:
        self._tick()
        self.root.after(60, self._schedule_loop)

    def _tick(self) -> None:
        if not self.is_connected:
            self._render_canvas()
            return

        t_now = time.perf_counter()
        self._fps_times.append(t_now)
        fps = 0.0
        if len(self._fps_times) >= 2:
            dt = self._fps_times[-1] - self._fps_times[0]
            if dt > 1e-6:
                fps = (len(self._fps_times) - 1) / dt

        raw_frame = (
            self.sim_camera.read_frame()
            if self.use_simulator.get()
            else (self.usb_camera.read_frame() if self.usb_camera else None)
        )
        if raw_frame is None:
            return

        if self.capturing_dark:
            cnt, done = self.dark_mgr.add_capture_frame(raw_frame)
            self.lbl_dark_status.configure(text=f"Capturing Dark Frame ({cnt}/5)...")
            if done:
                self.capturing_dark = False
                self._refresh_dark_status()
            self.last_frame = raw_frame
            self._render_canvas()
            return

        # Rolling frame stacking
        stack_n = max(1, int(self.var_stack.get()))
        self.frame_stack.append(raw_frame.astype(np.float32))
        while len(self.frame_stack) > stack_n:
            self.frame_stack.popleft()

        if len(self.frame_stack) == 1:
            stacked = raw_frame
        else:
            stacked = np.clip(np.mean(self.frame_stack, axis=0), 0, 255).astype(np.uint8)

        self.last_frame = stacked

        # Extract star centroids & solve plate
        centroids, bg_mean = extract_star_centroids(stacked, self.dark_mgr.dark_frame)
        solve = self.solver.solve_centroids(centroids)
        solve.background_mean = bg_mean
        self.last_solve = solve

        # Update calibration state machine
        if self.calibrator.status.state == 2 and solve.solved:
            st = self.calibrator.update_frame(solve)
            if st.can_confirm_pos2 and self.var_auto_confirm.get():
                center = self.calibrator.confirm_position_2()
                if center is not None:
                    self.profile.center_row_x, self.profile.center_col_y = center
                    self.profile.save()
                    self._update_info_labels()

        self._update_calibration_buttons()

        # Update telemetry labels
        rms_str = f"{solve.rms_residual_px:.2f} px" if solve.solved else "--"
        self.lbl_fps.configure(
            text=f"FPS: {fps:4.1f}  |  Stars: {len(centroids)} (Matched: {len(solve.matched_stars)})"
        )
        self.lbl_bg.configure(
            text=f"Background: {bg_mean:5.1f}  |  Solve RMS: {rms_str}"
        )

        # Update live alignment guidance
        nm = bool(self.var_night_mode.get())
        if solve.solved:
            self.lbl_pole_coords.configure(
                text=f"Celestial Pole: ({solve.pole_row_x:6.1f}, {solve.pole_col_y:6.1f})"
            )
            if self.profile.has_valid_center:
                cr = self.profile.center_row_x
                cc = self.profile.center_col_y
                self.lbl_center_coords.configure(text=f"RA Axis Center: ({cr:6.1f}, {cc:6.1f})")
                d_row = solve.pole_row_x - cr
                d_col = solve.pole_col_y - cc
                alt_dir = "▲ UP" if d_row < 0 else "▼ DOWN"
                az_dir = "◄ LEFT" if d_col < 0 else "► RIGHT"
                alt_txt = f"ALT: {alt_dir} {format_arcmin_arcsec(d_row, solve.plate_scale_arcsec)}"
                az_txt = f"AZ: {az_dir} {format_arcmin_arcsec(d_col, solve.plate_scale_arcsec)}"
                tot_px = math.hypot(d_row, d_col)
                if nm:
                    color = "#ff6666" if tot_px < 3.0 else ("#ff3333" if tot_px < 20.0 else "#cc1f1f")
                else:
                    color = "#7ee787" if tot_px < 3.0 else ("#f5b041" if tot_px < 20.0 else "#ff7b72")
                self.lbl_alt_guide.configure(text=alt_txt, foreground=color)
                self.lbl_az_guide.configure(text=az_txt, foreground=color)
            else:
                self.lbl_center_coords.configure(text="RA Axis Center: Uncalibrated")
                warn_col = "#cc3333" if nm else "#f5b041"
                self.lbl_alt_guide.configure(text="ALT: Calibrate Center First", foreground=warn_col)
                self.lbl_az_guide.configure(text="AZ: Calibrate Center First", foreground=warn_col)
        else:
            self.lbl_pole_coords.configure(text="Celestial Pole: Searching (<4 stars)...")

        self._render_canvas()

    def _render_canvas(self) -> None:
        cw = max(100, self.canvas.winfo_width())
        ch = max(100, self.canvas.winfo_height())
        self.canvas.delete("all")
        nm = bool(self.var_night_mode.get())

        if not self.is_connected or self.last_frame is None:
            self.canvas.create_text(
                cw // 2,
                ch // 2 - 15,
                text="iOptron iPolar / SkyGuider Pro Polar Scope",
                fill="#992626" if nm else "#8b949e",
                font=("Segoe UI", 16, "bold"),
            )
            self.canvas.create_text(
                cw // 2,
                ch // 2 + 20,
                text="Click 'Connect Camera' on the right to start live plate solving & polar alignment.",
                fill="#ff4d4d" if nm else "#58a6ff",
                font=("Segoe UI", 11),
            )
            return

        frame = self.last_frame
        fh, fw = frame.shape

        # Compute aspect-preserving viewport transform
        # Also support Zoom Target mode centered on the RA Axis Center when close to alignment
        zoom_active = bool(self.var_zoom_target.get())
        if (
            not zoom_active
            and self.profile.has_valid_center
            and self.last_solve.solved
            and self.calibrator.status.state == 3
        ):
            dist_to_center = math.hypot(
                self.last_solve.pole_row_x - self.profile.center_row_x,
                self.last_solve.pole_col_y - self.profile.center_col_y,
            )
            if dist_to_center < 35.0:
                zoom_active = True

        if zoom_active and self.profile.has_valid_center:
            # Crop a 320x240 region around (center_row_x, center_col_y) for 4x precision view
            crop_h, crop_w = 240, 320
            r0 = int(round(self.profile.center_row_x)) - crop_h // 2
            c0 = int(round(self.profile.center_col_y)) - crop_w // 2
            r0 = max(0, min(fh - crop_h, r0))
            c0 = max(0, min(fw - crop_w, c0))
            sub_img = frame[r0 : r0 + crop_h, c0 : c0 + crop_w]
            view_h, view_w = crop_h, crop_w
        else:
            r0, c0 = 0, 0
            sub_img = frame
            view_h, view_w = fh, fw

        scale = min(cw / view_w, ch / view_h)
        disp_w = max(64, int(view_w * scale))
        disp_h = max(48, int(view_h * scale))
        off_x = (cw - disp_w) // 2
        off_y = (ch - disp_h) // 2

        # Render image if RAW Image is checked or as stretched astrometric background
        if self.var_raw_image.get():
            vis = sub_img
        else:
            bg = float(np.percentile(sub_img, 55))
            if bg > 35.0 and len(self.last_solve.matched_stars) == 0:
                # Indoor / twilight continuous-tone scene: preserve full dynamic range without clipping darks
                lo, hi = np.percentile(sub_img, [2.0, 99.5])
                span = max(1.0, float(hi - lo))
                vis = np.clip((sub_img.astype(np.float32) - float(lo)) * (255.0 / span), 0, 255).astype(np.uint8)
            else:
                # Dark night-sky star preview (subtract dark/background floor, boost stars)
                vis = np.clip((sub_img.astype(np.float32) - bg) * 3.2, 0, 255).astype(np.uint8)

        # Area/bilinear resize via OpenCV (eliminates scaling moire/checkerboard), with numpy fallback
        if HAS_CV2 and cv2 is not None:
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            resized = cv2.resize(vis, (disp_w, disp_h), interpolation=interp)
        else:
            y_idx = (np.arange(disp_h) * (view_h / disp_h)).astype(np.int32)
            x_idx = (np.arange(disp_w) * (view_w / disp_w)).astype(np.int32)
            resized = vis[y_idx[:, None], x_idx[None, :]]
        if nm:
            # Monochrome Red PPM (P6) so stars and sky glow purely in dark-adapted red
            rgb = np.zeros((disp_h, disp_w, 3), dtype=np.uint8)
            rgb[:, :, 0] = resized
            ppm_header = f"P6\n{disp_w} {disp_h}\n255\n".encode("ascii")
            self._photo_image = tk.PhotoImage(data=ppm_header + rgb.tobytes())
        else:
            pgm_header = f"P5\n{disp_w} {disp_h}\n255\n".encode("ascii")
            self._photo_image = tk.PhotoImage(data=pgm_header + resized.tobytes())
        self.canvas.create_image(off_x, off_y, anchor=tk.NW, image=self._photo_image)

        def to_canvas(row_x: float, col_y: float) -> Tuple[float, float]:
            cx_px = off_x + (col_y - c0) * scale
            cy_px = off_y + (row_x - r0) * scale
            return cx_px, cy_px

        # Draw detected stars and catalog-matched stars
        col_matched = "#ff4d4d" if nm else "#39c5bb"
        col_detected = "#801a1a" if nm else "#d29922"
        matched_set = {d_i: cat_id for d_i, _, _, cat_id in self.last_solve.matched_stars}
        for idx, (sr, sc, _flux) in enumerate(self.last_solve.detected_stars):
            px, py = to_canvas(sr, sc)
            if off_x <= px <= off_x + disp_w and off_y <= py <= off_y + disp_h:
                if idx in matched_set:
                    self.canvas.create_oval(
                        px - 6, py - 6, px + 6, py + 6, outline=col_matched, width=1.5
                    )
                else:
                    self.canvas.create_oval(
                        px - 4, py - 4, px + 4, py + 4, outline=col_detected, width=1.0
                    )

        # Draw Position 1 Pole if in calibration step 2
        if self.calibrator.status.state == 2 and self.calibrator.status.pos1_pole is not None:
            p1r, p1c = self.calibrator.status.pos1_pole
            p1x, p1y = to_canvas(p1r, p1c)
            p1_col = "#cc2929" if nm else "#a371f7"
            self.canvas.create_oval(p1x - 8, p1y - 8, p1x + 8, p1y + 8, outline=p1_col, width=2)
            self.canvas.create_text(
                p1x + 12, p1y - 10, text="Pos 1 Pole", fill=p1_col, anchor="w", font=("Segoe UI", 9, "bold")
            )

        # Draw RA Axis Center Crosshair (+ Concentric Alignment Rings)
        center_rc: Optional[Tuple[float, float]] = None
        if self.calibrator.status.state == 2 and self.calibrator.status.candidate_center is not None:
            center_rc = self.calibrator.status.candidate_center
        elif self.profile.has_valid_center:
            center_rc = (self.profile.center_row_x, self.profile.center_col_y)

        if center_rc is not None:
            cr, cc = center_rc
            cx_p, cy_p = to_canvas(cr, cc)
            # Draw 5 arcmin (~9.7 px) and 30 arcmin (~58.2 px) reference rings
            rings = ((5.0, "#991111"), (30.0, "#660a0a")) if nm else ((5.0, "#2ea043"), (30.0, "#238636"))
            for arcmin, col_ring in rings:
                rad_canvas = (arcmin * 60.0 / self.last_solve.plate_scale_arcsec) * scale
                if 6.0 <= rad_canvas <= max(cw, ch):
                    self.canvas.create_oval(
                        cx_p - rad_canvas,
                        cy_p - rad_canvas,
                        cx_p + rad_canvas,
                        cy_p + rad_canvas,
                        outline=col_ring,
                        dash=(3, 3),
                    )
            # Crosshair
            cross_col = "#ff3333" if nm else "#3fb950"
            arm = 16
            self.canvas.create_line(cx_p - arm, cy_p, cx_p + arm, cy_p, fill=cross_col, width=2)
            self.canvas.create_line(cx_p, cy_p - arm, cx_p, cy_p + arm, fill=cross_col, width=2)
            self.canvas.create_text(
                cx_p + 10,
                cy_p + 12,
                text="RA Center",
                fill=cross_col,
                anchor="nw",
                font=("Segoe UI", 9, "bold"),
            )

        # Draw Solved Celestial Pole (Target Circle + Vector to RA Center)
        if self.last_solve.solved:
            pr = self.last_solve.pole_row_x
            pc = self.last_solve.pole_col_y
            px_p, py_p = to_canvas(pr, pc)

            if center_rc is not None:
                cx_p, cy_p = to_canvas(center_rc[0], center_rc[1])
                self.canvas.create_line(
                    px_p, py_p, cx_p, cy_p, fill="#cc2222" if nm else "#ff7b72", width=1.5, dash=(4, 2)
                )

            # Clamp indicator to viewport border with arrow if Pole is off-screen
            clamped_x = max(off_x + 14, min(off_x + disp_w - 14, px_p))
            clamped_y = max(off_y + 14, min(off_y + disp_h - 14, py_p))
            is_offscreen = (clamped_x != px_p) or (clamped_y != py_p)

            r_pole = max(9.0, 9.0 * scale) if zoom_active else 11.0
            pole_outline = ("#ff6666" if not is_offscreen else "#cc3333") if nm else ("#ff4444" if not is_offscreen else "#ffa657")
            self.canvas.create_oval(
                clamped_x - r_pole,
                clamped_y - r_pole,
                clamped_x + r_pole,
                clamped_y + r_pole,
                outline=pole_outline,
                width=2.5,
            )
            self.canvas.create_oval(
                clamped_x - 2,
                clamped_y - 2,
                clamped_x + 2,
                clamped_y + 2,
                fill="#ff4444",
                outline="#ff4444",
            )
            lbl_pole = "Celestial Pole (Off-Screen)" if is_offscreen else "Celestial Pole"
            self.canvas.create_text(
                clamped_x + r_pole + 5,
                clamped_y - 8,
                text=lbl_pole,
                fill="#ff6666" if nm else "#ff7b72",
                anchor="w",
                font=("Segoe UI", 9, "bold"),
            )

        # Top-left HUD overlay on canvas
        mode_txt = "SIMULATOR MODE" if self.use_simulator.get() else "USB HARDWARE MODE"
        zoom_txt = " [4x ZOOM]" if zoom_active else ""
        night_txt = " [NIGHT VISION]" if nm else ""
        self.canvas.create_text(
            off_x + 12,
            off_y + 12,
            anchor="nw",
            text=f"{mode_txt}{zoom_txt}{night_txt}  |  Sky Angle: {self.last_solve.camera_angle_deg:5.1f}°  |  Scale: {self.last_solve.plate_scale_arcsec:.2f}\"/px",
            fill="#ff4d4d" if nm else "#58a6ff",
            font=("Consolas", 10, "bold"),
        )


def main() -> None:
    enable_win11_dpi_awareness()
    force_sim = "--sim" in sys.argv
    force_night = "--night" in sys.argv
    root = tk.Tk()
    _app = IPolarApp(root, force_simulator=force_sim, force_night=force_night)
    root.mainloop()


if __name__ == "__main__":
    main()
