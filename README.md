# iOptron iPolar / SkyGuider Pro Polar Alignment Suite (Windows 11 Fixed Edition)

A clean-room, 100% standalone, hardware-compatible re-implementation of **iOptron iPolar v2.92** (`iOptron iPolar.exe`) built from reverse engineering the shipped NativeAOT binary (`SHA-256: 9cbc332f5aaa4604e5d32a0c41cbb1c86430af9b6feda0fe280cbc4abd638159`).

## Quick Start

```powershell
# Install standalone dependencies (numpy + full opencv-python, no iOptron binary needed)
pip install -r d:\ltisistemas\sky_guider_pro_rea\requirements.txt

# Launch the GUI (auto-detects USB iPolar camera or falls back to Sky Simulator)
python d:\ltisistemas\sky_guider_pro_rea\ipolar_app.py

# Launch directly in Sky Simulator Mode for indoor testing
python d:\ltisistemas\sky_guider_pro_rea\ipolar_app.py --sim

# Launch with Night Vision (Red Theme) enabled for outdoor dark adaptation
python d:\ltisistemas\sky_guider_pro_rea\ipolar_app.py --night

# Run the automated verification suite
python d:\ltisistemas\sky_guider_pro_rea\test_ipolar.py
```

---

## Outdoor Polar Alignment Field Guide

1. **Exposure, Gain & Dark Frame Setup**:
   - Connect your iPolar USB camera and click **Connect Camera**.
   - For outdoor night sky use, set **Exposure** to `500 ms` or `1000 ms` and **Gain** around `8`–`12`. *(When testing indoors during the day or in a lit room, drop **Exposure** to `5 ms` – `20 ms` so the sensor does not saturate at `255`.)*
   - *(Optional)* Place the lens cap over the iPolar and click **Capture Dark (5 frames)** to subtract hot pixels (`DarkFrame.bin`).
2. **2-Position RA Axis Center Calibration**:
   - Point your SkyGuider Pro roughly toward the Celestial Pole, wait for `"SOLVED"` (`4+` matched catalog stars), and click **Confirm Position 1**.
   - Loosen the RA clutch, rotate the camera / RA axis by `30°`–`90°`, and hold steady for ~1 second.
   - With **Auto-Confirm Position 2** checked, the app will automatically lock in and save your **RA Axis Center** once 3 consecutive frames stabilize (or you can click **Confirm Position 2** manually at any time once rotation exceeds `15°`).
3. **Precision Alt / Az Polar Alignment**:
   - Enable **Night Mode (Red)** to preserve your dark adaptation in the field.
   - Adjust your mount's Altitude and Azimuth bolts following the live **ALT** (`▲ UP` / `▼ DOWN`) and **AZ** (`◄ LEFT` / `► RIGHT`) readouts until the **Celestial Pole** bullseye sits inside the **RA Center** crosshair.
   - Once the pole is within ~18 arcminutes (`< 35 px`) of the RA Center, the viewport automatically switches to **`4x` Precision Zoom** (you can also toggle **Zoom Target** manually at any time, even before calibration).

---

## Root-Cause Diagnosis of Bugs in `iOptron iPolar.exe` v2.92

### 1. Why Labels Draw Outside the Window on Windows 11 (`0x68e600..0x68ef55`)
- **Broken High-DPI Scaling Workaround**: In `MainPanel` layout initialization (`RVA 0x68e7c2..0x68ef55`), the binary queries `Screen.Scaling` and checks `if (Screen.Scaling >= 1.24)` (`0x68e7f0: ucomisd xmm6, [0x144e61ad0]`). On any Windows 11 display using `125%`, `150%`, `175%`, or `200%` DPI scaling, it computes:
  $$\Delta = \text{round}\big((\text{buttonConnect.Bounds.Width} + \text{buttonConnect.Bounds.Height} + 5.0) \times \text{Screen.Scaling}\big)$$
  and **adds $\Delta$ (hundreds of logical pixels) directly to the `Margin.Top` / `Canvas.Top` / `Height` of `buttonConnect` (`[rbx+0x3b0]`), `labelStatus` (`[rbx+0x3c0]`), `textBoxHint` (`[rbx+0x3d8]`), and `buttonConfirmPosition` (`[rbx+0x3e0]`)** on top of Avalonia's own DPI scaling, pushing controls off the bottom/right of the window.
- **WinForms-to-Avalonia Fixed Pixel Port**: `v2.92` was mechanically ported from WinForms (`v2.76`) to Avalonia `12.1.0.0` using hardcoded pixel `Margin` offsets (`Thickness(left, top, 0, 0)`) and tiny fixed widths/heights (`Height = 16`, `Width = 10` for `labelX`/`labelY`, `Width = 220` inside a `230 px` right panel), which clip under Windows 11 `Segoe UI` font metrics.

### 2. Why Labels Have Missing / Blank Texts (`0x6a8110..0x6a955e`, `0x68d600..0x68de66`, `0x6ba750..0x6bc100`)
- **`UpdateUIText` Wipes `labelStatus`**: `MainPanel.UpdateUIText` (`RVA 0x6a8110`) overwrites `labelStatus.Content` (`[rsi+0x3b8]`) with the global `statusString` (`[0x145158910 + 0x58]`), which is initialized to `""` before camera connection, erasing the XAML text `"Actions needed"`.
- **Hidden Telemetry Controls**: The constructor (`RVA 0x68d600..0x68de66`) sets `IsVisible = false` on `labelBackgroundBrightness`, `labelFPS`, `labelGain`, `trackBarGain`, and `buttonErase`, leaving blank gaps in the sidebar.
- **Corrupted `MaskedTextBox` Masks in Settings**: `Settings` (`RVA 0x6ba750..0x6bc100`) uses `Avalonia.Controls.MaskedTextBox` with curly Unicode quotes (`#00°00’00”`, `U+2019`/`U+201D`), which fail to format and clip inside `20 px`-high boxes.

### 3. Why Camera Center Calibration Hangs in the Middle (`0x6a1699`, `0x6944c0`, `0x6a3a9a`)
1. **Sub-Pixel 4-Frame Stability Reset (`RVA 0x6a1699..0x6a1738` & `0x6a39fe`)**:
   - Every plate-solved frame checks `(PoleX - PrevPoleX)^2 + (PoleY - PrevPoleY)^2 < 1.0` (`< 1.0 pixel = 30.94 arcsec`). Any single frame with `>= 1.0 px` seeing/wind/mount jitter resets the stability counter `[rbp - 0x88] = 1` and jumps over `0x6944c0`, preventing Position 2 from ever evaluating.
2. **Disjoint Star-Subset Jump Bug in `0x6944c0` (`RVA 0x694855..0x695722`)**:
   - `0x6944c0` intersects perpendicular bisectors of **catalog star IDs recognized in both Position 1 and Position 2**.
   - When the camera rotates in RA and the recognized star subset in Position 2 shares **zero common catalog star IDs** with Position 1 (`edx == 1` at `0x694c47`, only the pole bisector exists), lines `0x694c4c..0x694c84` set `[rcx + 0x140] = 90.0` and execute `jmp 0x140695722`, **jumping right over `0x69570c` (`seta [rdx + 0x24]`)!** Because `canConfirmPos2` (`[rcx + 0x24]`) was cleared to `0` at `0x694618`, it remains `0` forever. Similarly, when only 1 common star ID overlaps (`edx == 2`) without a multi-bisector cluster, `0x694ed9` sets `angle = 0.0`, also leaving `canConfirmPos2 = 0` forever.
3. **Synchronous Cross-Thread `Dispatcher.UIThread.Invoke` Modal Dialog Deadlock (`RVA 0x6a3a9a -> 0x695b40 -> 0x6c9500 -> 0x6ace30`)**:
   - When `Auto Confirm` triggers on the background worker thread (`0x6a3a9a`), `ConfirmPosition2` (`0x695b40`) makes three synchronous blocking `Dispatcher.UIThread.Invoke` calls (`0x6c9500`), the third of which (`0x6ace30`) opens a modal `Window.ShowDialog` (`0x688120`) while holding the camera worker thread.

---

## Key Design Decisions & Hardware Findings

1. **Standalone USB Capture via Full `opencv-python` (`CAP_MSMF`)**:
   - The iPolar v1 camera transmits its `1280×960` raw 8-bit sensor rows packed inside a `640×960` 16-bit (`YUY2`) UVC container (`1,228,800` bytes) and requires `CAP_PROP_CONVERT_RGB = 0` (`0x69a890..0x69ab90`).
   - Full `opencv-python` (not `opencv-python-headless`) is required on Windows because `headless` strips out `opencv_videoio_msmf*.dll`, and OpenCV's DirectShow (`CAP_DSHOW`) backend ignores `CAP_PROP_CONVERT_RGB = 0` and converts the raw stream into a 3-channel `(960, 640, 3)` BGR image.
   - `USBIPolarCamera` probes MediaFoundation (`CAP_MSMF`) and validates the raw buffer dimensions (`(960, 1280)` for v1, `(1096, 1944)` for v2), automatically scanning indices `0..3` if Windows PnP device order differs from MediaFoundation order.
2. **Bayer `GRBG` Demosaicing (`cv2.COLOR_BayerGR2GRAY`) & Anti-Aliased Viewport**:
   - The physical sensor in `iOptron iPolar 1.1` (`AR0130CS`) has a `2×2` `GRBG` Bayer Color Filter Array. Unpacking raw bytes without demosaicing creates a `2×2` checkerboard under continuous lighting.
   - `unpack_ipolar_raw_frame(..., debayer=True)` applies hardware-speed `cv2.cvtColor(img, cv2.COLOR_BayerGR2GRAY)` demosaicing (reducing `2×2` sub-pixel variance from `12.92` to `0.029`) and `_render_canvas` scales the viewport using `cv2.resize(..., cv2.INTER_AREA)`.
3. **Digital ISP Gain Curve for Fixed-Gain iPolar 1.1 Firmware**:
   - The `iOptron iPolar 1.1` UVC firmware locks hardware `CAP_PROP_GAIN (14)` to `64.0` and rejects UVC `SET_CUR` requests for `0..16` (which is why iOptron hid `trackBarGain` in `v2.92`).
   - When `USBIPolarCamera` detects that hardware UVC gain is locked, it applies a real-time logarithmic ISP gain multiplier $\text{factor} = 2^{(\text{gain} - 8) / 4}$ (`Gain = 0` $\rightarrow 0.25\times$, `Gain = 8` $\rightarrow 1.0\times$, `Gain = 16` $\rightarrow 4.0\times$).
4. **Night Vision Mode (Red Theme), Responsive Scrollbar & Anytime `4x` Zoom**:
   - **Night Mode (`--night`)**: Renders the entire UI and live star feed (`P6` monochrome red PPM) in deep red (`#070000`–`#ff4d4d`) with zero green/blue emission.
   - **Auto-Hiding Sidebar Scrollbar**: Automatically appears when the window is resized smaller than the right control panel height and supports mousewheel scrolling.
   - **Anytime `4x` Zoom Target**: Crops a `320×240` (`4x`) precision view around the calibrated RA Axis Center (or around the sensor center `(480, 640)` if not yet calibrated).

---

## Architecture of the Replacement Suite

- [`ipolar_catalog.py`](file:///d:/sky_guider_pro_rea/ipolar_catalog.py): Exact 77-star Northern and 66-star Southern Hemisphere catalogs + IAU precession, 13-term nutation, GMST/GAST, and Bennett atmospheric refraction extracted from `.rdata` (`0x4ac5e00..0x4ac8a28`).
- [`ipolar_camera.py`](file:///d:/sky_guider_pro_rea/ipolar_camera.py): Standalone hardware USB capture for iPolar v1 (`1280×960`, `30.94"/px`) and iPolar v2 (`1944×1096`, `23.93"/px`) using `cv2.VideoCapture` (`CAP_MSMF`), `COLOR_BayerGR2GRAY` demosaicing, ISP gain fallback, and a realistic **Astrometric Sky Simulator**.
- [`ipolar_solver.py`](file:///d:/sky_guider_pro_rea/ipolar_solver.py): Sub-pixel star centroid detector, fast pair/triangle plate solver, `DarkFrame.bin` manager, and the fixed **Dual-Method RA Axis Center Calibrator** (combining rigid-body pole rotation + common-star perpendicular bisectors).
- [`ipolar_profile.py`](file:///d:/sky_guider_pro_rea/ipolar_profile.py): Full read/write compatibility with `%LOCALAPPDATA%\iOptron iPolar\Profile.local.txt` and `Profile.global.txt` (Base64 key-value format).
- [`ipolar_app.py`](file:///d:/sky_guider_pro_rea/ipolar_app.py): Windows 11 Per-Monitor v2 High-DPI native GUI with Night Vision mode, auto-scrolling sidebar, live star overlay, `4x` precision zoom, and Alt/Az knob guidance.
- [`test_ipolar.py`](file:///d:/sky_guider_pro_rea/test_ipolar.py): Automated unit and GUI verification suite (`6` tests covering astrometry, profiles/dark frames, N/S plate solving, disjoint-star calibration, GUI/Night Mode/Scrollbar, and standalone `cv2` raw unpacking + Bayer demosaicing).
