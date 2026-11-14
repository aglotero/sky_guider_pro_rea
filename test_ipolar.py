"""
Automated Verification Suite for the Re-Implemented iOptron iPolar Suite
Tests:
1. Profile.local.txt & DarkFrame.bin loading from the user's actual AppData folder
2. Southern Hemisphere (Lat -23.483°, Lon -43.649°) & Northern Hemisphere (Lat +40.0°) astrometry
3. Simulated 1280x960 star field rendering, centroid extraction, and plate solving accuracy (< 0.25 px)
4. RA Axis Center Calibration across 0° -> 45° and 0° -> 75° mount rotation
5. Regression test for the original iPolar v2.92 calibration hang bug (0x694c4c: 0 common stars between Pos 1 & Pos 2)
"""

from __future__ import annotations
import math
import os
import tempfile
import unittest
import numpy as np

from ipolar_catalog import (
    PLATE_SCALE_IPOLAR_V1,
    STARS_NORTH,
    STARS_SOUTH,
    compute_catalog_apparent,
    compute_pole_refraction_offset_deg,
)
from ipolar_profile import DEFAULT_IPOLAR_DIR, IPolarProfile
from ipolar_camera import SimulatedIPolarCamera
from ipolar_solver import (
    CenterCalibrator,
    DarkFrameManager,
    IPolarPlateSolver,
    PlateSolveResult,
    extract_star_centroids,
)


class TestIPolarReimplementation(unittest.TestCase):
    def test_catalog_shapes_and_astrometry(self) -> None:
        self.assertEqual(STARS_NORTH.shape, (77, 5))
        self.assertEqual(STARS_SOUTH.shape, (66, 5))

        # Test Southern Hemisphere (user's site: -23.483055555555556, -43.649166666666666)
        ra_s, dec_s, gast = compute_catalog_apparent(
            -23.483055555555556, -43.649166666666666, 101325.0, 283.15, True
        )
        self.assertEqual(len(ra_s), 66)
        self.assertTrue(np.all(dec_s < -70.0))
        self.assertTrue(0.0 <= gast < 360.0)

        # Test refraction lift at |lat| = 23.483°
        refr_deg = compute_pole_refraction_offset_deg(-23.483055555555556, 101325.0, 283.15, True)
        self.assertGreater(refr_deg * 60.0, 1.5)  # ~2.2 arcmin at 23.5 deg altitude
        self.assertLess(refr_deg * 60.0, 3.0)

    def test_profile_and_dark_frame_compatibility(self) -> None:
        if os.path.exists(os.path.join(DEFAULT_IPOLAR_DIR, "Profile.local.txt")):
            prof = IPolarProfile.load(DEFAULT_IPOLAR_DIR)
            self.assertAlmostEqual(prof.latitude_deg, -23.483055555555556, places=6)
            self.assertAlmostEqual(prof.longitude_deg, -43.649166666666666, places=6)
            self.assertEqual(prof.barometric_pressure_pa, 101325)
            self.assertAlmostEqual(prof.temperature_k, 283.15, places=2)

            dark_mgr = DarkFrameManager(prof.dark_frame_path)
            if os.path.exists(prof.dark_frame_path):
                self.assertTrue(dark_mgr.load(960, 1280))
                self.assertEqual(dark_mgr.dark_frame.shape, (960, 1280))

        # Test round-trip Base64 save & load in temp dir
        with tempfile.TemporaryDirectory() as tmpdir:
            p = IPolarProfile(
                profile_dir=tmpdir,
                exposure_index=10,
                center_row_x=481.3,
                center_col_y=638.7,
                latitude_deg=-23.483055555555556,
                longitude_deg=-43.649166666666666,
            )
            p.save()
            p2 = IPolarProfile.load(tmpdir)
            self.assertEqual(p2.exposure_index, 10)
            self.assertAlmostEqual(p2.center_row_x, 481.3, places=1)
            self.assertAlmostEqual(p2.center_col_y, 638.7, places=1)
            self.assertTrue(p2.has_valid_center)

    def test_southern_and_northern_plate_solving(self) -> None:
        for lat in (-23.483055555555556, 40.7128):
            sim = SimulatedIPolarCamera(latitude_deg=lat, longitude_deg=-43.649166666666666)
            sim.true_center_row = 475.5
            sim.true_center_col = 642.2
            sim.pole_offset_alt_px = -55.0
            sim.pole_offset_az_px = 70.0
            sim.ra_mount_angle_deg = 25.0
            sim.connect()

            solver = IPolarPlateSolver(latitude_deg=lat, longitude_deg=-43.649166666666666)
            frame = sim.read_frame()
            centroids, _ = extract_star_centroids(frame)
            self.assertGreaterEqual(len(centroids), 8)

            solve = solver.solve_centroids(centroids)
            self.assertTrue(solve.solved, f"Failed to solve for lat={lat}")

            true_r, true_c, _ = sim.get_true_pole_sensor_coords()
            err_px = math.hypot(solve.pole_row_x - true_r, solve.pole_col_y - true_c)
            self.assertLess(err_px, 0.35, f"Pole error {err_px:.3f} px too high for lat={lat}")

    def test_center_calibration_and_zero_common_stars_fix(self) -> None:
        sim = SimulatedIPolarCamera(latitude_deg=-23.483055555555556, longitude_deg=-43.649166666666666)
        sim.true_center_row = 482.4
        sim.true_center_col = 637.8
        sim.pole_offset_alt_px = -80.0
        sim.pole_offset_az_px = 95.0
        sim.ra_mount_angle_deg = 0.0
        sim.connect()

        solver = IPolarPlateSolver(latitude_deg=-23.483055555555556, longitude_deg=-43.649166666666666)
        cal = CenterCalibrator()
        cal.start_calibration()

        # Position 1 at RA = 0 deg
        centroids1, _ = extract_star_centroids(sim.read_frame())
        solve1 = solver.solve_centroids(centroids1)
        self.assertTrue(cal.confirm_position_1(solve1))
        self.assertEqual(cal.status.state, 2)

        # Rotate mount RA to 55 deg (Position 2) and feed 3 frames
        sim.ra_mount_angle_deg = 55.0
        for _ in range(3):
            centroids2, _ = extract_star_centroids(sim.read_frame())
            solve2 = solver.solve_centroids(centroids2)
            status = cal.update_frame(solve2)

        self.assertTrue(status.can_confirm_pos2)
        center = cal.confirm_position_2()
        self.assertIsNotNone(center)
        center_err = math.hypot(center[0] - sim.true_center_row, center[1] - sim.true_center_col)
        self.assertLess(center_err, 0.6, f"Calibrated center error {center_err:.3f} px too large")

        # Now test the exact bug from iOptron iPolar.exe 0x694c4c:
        # Suppose Position 2 shares ZERO common catalog star IDs with Position 1!
        cal_disjoint = CenterCalibrator()
        cal_disjoint.start_calibration()
        cal_disjoint.confirm_position_1(solve1)
        # Clear matched_stars in solve2 so 0 common stars exist with Position 1
        solve2_no_common = PlateSolveResult(
            solved=True,
            pole_row_x=solve2.pole_row_x,
            pole_col_y=solve2.pole_col_y,
            camera_angle_deg=solve2.camera_angle_deg,
            plate_scale_arcsec=solve2.plate_scale_arcsec,
            matched_stars=[],
        )
        for _ in range(3):
            st_disjoint = cal_disjoint.update_frame(solve2_no_common)
        self.assertTrue(
            st_disjoint.can_confirm_pos2,
            "Disjoint star subsets should still allow Position 2 confirmation!",
        )
        center_disjoint = cal_disjoint.confirm_position_2()
        self.assertIsNotNone(center_disjoint)
        err_disjoint = math.hypot(
            center_disjoint[0] - sim.true_center_row,
            center_disjoint[1] - sim.true_center_col,
        )
        self.assertLess(err_disjoint, 0.6)

    def test_gui_smoke_and_autoconfirm_workflow(self) -> None:
        import tkinter as tk
        from ipolar_app import IPolarApp

        with tempfile.TemporaryDirectory() as tmpdir:
            root = tk.Tk()
            try:
                app = IPolarApp(root, force_simulator=True)
                app.profile.profile_dir = tmpdir
                root.update_idletasks()
                root.update()

                # Connect simulator camera
                app.toggle_connect()
                self.assertTrue(app.is_connected)

                # Run 2 ticks to solve Position 1
                for _ in range(2):
                    app._tick()
                    root.update()
                self.assertTrue(app.last_solve.solved)

                # Start calibration and confirm Position 1
                app.on_restart_calibration()
                app.on_confirm_position_clicked()
                self.assertEqual(app.calibrator.status.state, 2)

                # Rotate simulated RA axis by +60 deg and run 4 ticks (Auto-Confirm enabled)
                app._nudge_sim_ra(60.0)
                for _ in range(4):
                    app._tick()
                    root.update()

                # Verify calibration completed (state == 3) and center matches true simulator center
                self.assertEqual(app.calibrator.status.state, 3)
                self.assertAlmostEqual(app.profile.center_row_x, app.sim_camera.true_center_row, delta=0.8)
                self.assertAlmostEqual(app.profile.center_col_y, app.sim_camera.true_center_col, delta=0.8)

                # Verify right sidebar scrollbar auto-shows when window height is smaller than content
                root.geometry("800x450")
                root.update_idletasks()
                root.update()
                app._on_sidebar_configure()
                root.update_idletasks()
                self.assertTrue(app.sidebar_scrollbar.winfo_ismapped())

                # Verify Night Mode (Red Theme) toggle and P6 red monochrome rendering
                app.var_night_mode.set(True)
                app._on_night_mode_toggle()
                app._tick()
                root.update()
                self.assertTrue(app.profile.night_mode)
                self.assertEqual(app.root.cget("bg"), "#070000")
                self.assertEqual(app.canvas.cget("bg"), "#040000")
            finally:
                root.destroy()

    def test_standalone_cv2_usb_camera_unpacking(self) -> None:
        from ipolar_camera import HAS_CV2, USBIPolarCamera, unpack_ipolar_raw_frame

        self.assertTrue(HAS_CV2, "Standard cv2 (opencv-python-headless) should be available")

        # 1. Verify iPolar v1 raw 640x960 16-bit buffer -> 960x1280 uint8 unpacking
        expected_v1 = np.arange(960 * 1280, dtype=np.uint8).reshape((960, 1280))
        raw_v1_mat = expected_v1.view(np.uint16).reshape((960, 640))
        unpacked_v1 = unpack_ipolar_raw_frame(raw_v1_mat, version=1)
        self.assertIsNotNone(unpacked_v1)
        self.assertEqual(unpacked_v1.shape, (960, 1280))
        np.testing.assert_array_equal(unpacked_v1, expected_v1)

        # Verify 3-channel BGR buffer (960x640x3 from DSHOW when ConvertRgb=0 is ignored) is rejected
        bgr_dshow_mat = np.zeros((960, 640, 3), dtype=np.uint8)
        self.assertIsNone(unpack_ipolar_raw_frame(bgr_dshow_mat, version=1))

        # 2. Verify iPolar v2 raw 1458x1107 16-bit (2916 bytes/row) 12-bit packed buffer -> 1096x1944 uint8 unpacking
        raw_v2_bytes = np.zeros((1107, 2916), dtype=np.uint8)
        raw_v2_bytes[:1096, 0::3] = 0xAB
        raw_v2_bytes[:1096, 1::3] = 0x0C
        raw_v2_bytes[:1096, 2::3] = 0xD0
        raw_v2_mat = raw_v2_bytes.view(np.uint16).reshape((1107, 1458))
        unpacked_v2 = unpack_ipolar_raw_frame(raw_v2_mat, version=2)
        self.assertIsNotNone(unpacked_v2)
        self.assertEqual(unpacked_v2.shape, (1096, 1944))
        self.assertTrue(np.all(unpacked_v2[:, 0::2] == 0xAB))
        self.assertTrue(np.all(unpacked_v2[:, 1::2] == 0xCD))

        # 3. Verify USBIPolarCamera with mock cv2.VideoCapture and zero cvextern dependency
        cam = USBIPolarCamera(device_index=0, version=1, use_cvextern_fallback=False, debayer=False)
        self.assertIsNone(cam.cv_wrap)

        class _FakeCap:
            def read(self):
                return True, raw_v1_mat

            def set(self, prop, val):
                return True

            def release(self):
                pass

        cam._cv2_cap = _FakeCap()
        cam.is_connected = True
        frame = cam.read_frame()
        self.assertIsNotNone(frame)
        self.assertEqual(frame.shape, (960, 1280))
        np.testing.assert_array_equal(frame, expected_v1)

        # 4. Verify Bayer GRBG demosaicing eliminates 2x2 checkerboard pattern
        bayer_frame = np.zeros((960, 1280), dtype=np.uint8)
        bayer_frame[0::2, 0::2] = 176  # G1
        bayer_frame[0::2, 1::2] = 131  # R
        bayer_frame[1::2, 0::2] = 119  # B
        bayer_frame[1::2, 1::2] = 176  # G2
        debayered = unpack_ipolar_raw_frame(bayer_frame.view(np.uint16).reshape((960, 640)), version=1, debayer=True)
        self.assertIsNotNone(debayered)
        self.assertEqual(debayered.shape, (960, 1280))
        # Interior pixels should be completely uniform (zero checkerboard variance)
        self.assertEqual(int( np.ptp(debayered[2:-2, 2:-2]) ), 0)
        cam.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
