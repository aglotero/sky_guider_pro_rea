"""
iOptron iPolar Star Detector, Astrometric Plate Solver & Fixed RA Center Calibrator
Reverse-engineered from iOptron iPolar.exe v2.92:
- Blob / centroid detection: 0x696f20 & 0x69b800..0x69c700
- Star pattern triangle/pair plate solver: 0x69cd92..0x69ea84
- Position 1 & Position 2 perpendicular-bisector center calibration: 0x694450..0x696b00
  (with fixes for the 0x694c4c zero-common-star jump bug, 0x694ed9 single-star failure,
   and 0x6a1699 sub-pixel jitter lockup).
"""

from __future__ import annotations
from dataclasses import dataclass, field
import math
import os
from typing import Dict, List, Optional, Tuple
import numpy as np

from ipolar_catalog import (
    PLATE_SCALE_IPOLAR_V1,
    compute_catalog_apparent,
)


@dataclass
class PlateSolveResult:
    solved: bool = False
    pole_row_x: float = 0.0          # Vertical row coordinate (0 .. height)
    pole_col_y: float = 0.0          # Horizontal col coordinate (0 .. width)
    camera_angle_deg: float = 0.0    # Orientation angle of sky on sensor (degrees)
    plate_scale_arcsec: float = PLATE_SCALE_IPOLAR_V1
    detected_stars: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.float64))
    # List of (detected_idx, row_x, col_y, catalog_id)
    matched_stars: List[Tuple[int, float, float, int]] = field(default_factory=list)
    background_mean: float = 0.0
    rms_residual_px: float = 0.0


@dataclass
class CalibrationStatus:
    state: int = 0                   # 0=Uncalibrated/Idle, 1=Waiting Pos 1, 2=Waiting Pos 2, 3=Calibrated
    pos1_pole: Optional[Tuple[float, float]] = None
    pos1_angle_deg: float = 0.0
    pos1_stars: Dict[int, Tuple[float, float]] = field(default_factory=dict)
    current_rotation_deg: float = 0.0
    candidate_center: Optional[Tuple[float, float]] = None
    can_confirm_pos2: bool = False
    stable_count: int = 0
    message: str = "Uncalibrated - Click 'Calibrate Center' to begin"


def extract_star_centroids(
    frame: np.ndarray,
    dark_frame: Optional[np.ndarray] = None,
    max_stars: int = 29,
) -> Tuple[np.ndarray, float]:
    """
    Detect up to `max_stars` (29 in iPolar v2.92 at 0x69c5b0) star centroids from an 8-bit grayscale frame.
    Returns (centroids_array [N, 3] of [row_x, col_y, flux], background_mean).
    """
    img = frame.astype(np.float32)
    if dark_frame is not None and dark_frame.shape == frame.shape:
        img = np.maximum(0.0, img - dark_frame.astype(np.float32))

    bg_mean = float(np.mean(img))
    bg_std = float(np.std(img))
    threshold = max(bg_mean + max(4.5 * bg_std, 18.0), bg_mean + 15.0)

    # Zero out border (5 px margin) and pixels below threshold
    sub = np.maximum(0.0, img - threshold)
    sub[:5, :] = 0.0
    sub[-5:, :] = 0.0
    sub[:, :5] = 0.0
    sub[:, -5:] = 0.0

    # Fast 5x5 local peak detection using numpy slicing
    active_rows, active_cols = np.nonzero(sub > 0.0)
    if len(active_rows) == 0:
        return np.zeros((0, 3), dtype=np.float64), bg_mean

    # Keep pixels that are >= their 3x3 neighborhood AND strictly > at least one neighbor (rejects single hot pixels)
    vals = sub[active_rows, active_cols]
    n_up = sub[active_rows - 1, active_cols]
    n_dn = sub[active_rows + 1, active_cols]
    n_lt = sub[active_rows, active_cols - 1]
    n_rt = sub[active_rows, active_cols + 1]

    # Hot-pixel filter (0x69c180: blob_size > 1): require at least one 4-neighbor > 0
    has_neighbor = (n_up > 0) | (n_dn > 0) | (n_lt > 0) | (n_rt > 0)
    is_peak = (
        has_neighbor
        & (vals >= n_up)
        & (vals >= n_dn)
        & (vals >= n_lt)
        & (vals >= n_rt)
        & (vals >= sub[active_rows - 1, active_cols - 1])
        & (vals >= sub[active_rows - 1, active_cols + 1])
        & (vals >= sub[active_rows + 1, active_cols - 1])
        & (vals >= sub[active_rows + 1, active_cols + 1])
    )

    peak_rows = active_rows[is_peak]
    peak_cols = active_cols[is_peak]
    peak_vals = vals[is_peak]

    if len(peak_rows) == 0:
        return np.zeros((0, 3), dtype=np.float64), bg_mean

    # Sort peaks brightest first and apply non-maximum suppression within radius 6 px
    order = np.argsort(-peak_vals)
    peak_rows = peak_rows[order]
    peak_cols = peak_cols[order]

    centroids: List[Tuple[float, float, float]] = []
    yy, xx = np.mgrid[-3:4, -3:4]

    for r, c in zip(peak_rows, peak_cols):
        r_i = int(r)
        c_i = int(c)
        # Check distance to already accepted centroids
        too_close = False
        for cr, cc, _ in centroids:
            if (r_i - cr) ** 2 + (c_i - cc) ** 2 < 49.0:
                too_close = True
                break
        if too_close:
            continue

        # Compute intensity-weighted centroid in a 7x7 window (matches 0x696f20 moment sum)
        patch = np.maximum(0.0, img[r_i - 3 : r_i + 4, c_i - 3 : c_i + 4] - bg_mean)
        flux = float(np.sum(patch))
        if flux <= 25.0:
            continue
        dr = float(np.sum(patch * yy)) / flux
        dc = float(np.sum(patch * xx)) / flux
        centroids.append((r_i + dr, c_i + dc, flux))
        if len(centroids) >= max_stars:
            break

    if not centroids:
        return np.zeros((0, 3), dtype=np.float64), bg_mean

    return np.array(centroids, dtype=np.float64), bg_mean


class IPolarPlateSolver:
    """
    Astrometric Plate Solver matching iOptron iPolar's 77-star North / 66-star South catalog.
    """

    def __init__(
        self,
        latitude_deg: float = -23.483055555555556,
        longitude_deg: float = -43.649166666666666,
        pressure_pa: float = 101325.0,
        temperature_k: float = 283.15,
        enable_refraction: bool = True,
        nominal_plate_scale: float = PLATE_SCALE_IPOLAR_V1,
    ) -> None:
        self.latitude_deg = latitude_deg
        self.longitude_deg = longitude_deg
        self.pressure_pa = pressure_pa
        self.temperature_k = temperature_k
        self.enable_refraction = enable_refraction
        self.nominal_plate_scale = nominal_plate_scale

        # Precomputed catalog tangent-plane coordinates (Pole at (0, 0), in nominal pixel units)
        self.cat_xy: np.ndarray = np.zeros((0, 2), dtype=np.float64)
        self.cat_pair_i: np.ndarray = np.zeros(0, dtype=np.int32)
        self.cat_pair_j: np.ndarray = np.zeros(0, dtype=np.int32)
        self.cat_pair_dist: np.ndarray = np.zeros(0, dtype=np.float64)
        self.cat_pair_vec: np.ndarray = np.zeros((0, 2), dtype=np.float64)
        self.update_catalog()

    def update_catalog(self) -> None:
        ra_deg, dec_deg, _ = compute_catalog_apparent(
            self.latitude_deg,
            self.longitude_deg,
            self.pressure_pa,
            self.temperature_k,
            self.enable_refraction,
        )
        scale_deg = self.nominal_plate_scale / 3600.0
        hemi_sign = 1.0 if self.latitude_deg >= 0.0 else -1.0
        ra_rad = np.deg2rad(ra_deg)
        r_px = (90.0 - np.abs(dec_deg)) / scale_deg

        # In camera sensor frame at rotation=0:
        # row_offset = -r_px * cos(ra) * hemi_sign
        # col_offset = +r_px * sin(ra)
        cat_rows = -r_px * np.cos(ra_rad) * hemi_sign
        cat_cols = r_px * np.sin(ra_rad)
        self.cat_xy = np.column_stack([cat_rows, cat_cols])

        # Precompute catalog pairs within sensor diagonal (60 px .. 1450 px)
        n = len(self.cat_xy)
        ii, jj = np.triu_indices(n, k=1)
        vecs = self.cat_xy[jj] - self.cat_xy[ii]
        dists = np.hypot(vecs[:, 0], vecs[:, 1])
        valid = (dists >= 45.0) & (dists <= 1480.0)

        order = np.argsort(dists[valid])
        self.cat_pair_i = ii[valid][order]
        self.cat_pair_j = jj[valid][order]
        self.cat_pair_dist = dists[valid][order]
        self.cat_pair_vec = vecs[valid][order]

    def solve_centroids(self, detected_stars: np.ndarray) -> PlateSolveResult:
        """
        Match detected star centroids (N, 3) against the hemisphere catalog and trilaterate
        the Celestial Pole (pole_row_x, pole_col_y) and camera orientation angle.
        """
        res = PlateSolveResult(
            solved=False,
            plate_scale_arcsec=self.nominal_plate_scale,
            detected_stars=detected_stars,
        )
        m = len(detected_stars)
        if m < 4 or len(self.cat_xy) == 0:
            return res

        # Use top 18 brightest stars for rapid hypothesis generation, verify against all m stars
        k_hyp = min(m, 18)
        det_xy = detected_stars[:, :2]

        best_inliers: List[Tuple[int, int]] = []
        best_pole = (0.0, 0.0)
        best_angle_rad = 0.0
        best_scale = 1.0

        # Tolerance from 0x69d126: 3% scale tolerance -> [0.975, 1.025]
        for i in range(k_hyp):
            for j in range(i + 1, k_hyp):
                v_img = det_xy[j] - det_xy[i]
                d_img = float(math.hypot(v_img[0], v_img[1]))
                if d_img < 60.0 or d_img > 1450.0:
                    continue

                # Binary search in sorted catalog distances within +-2.2%
                d_min = d_img * 0.978
                d_max = d_img * 1.022
                idx_start = int(np.searchsorted(self.cat_pair_dist, d_min, side="left"))
                idx_end = int(np.searchsorted(self.cat_pair_dist, d_max, side="right"))
                if idx_start >= idx_end:
                    continue

                ang_img = math.atan2(v_img[1], v_img[0])
                c_indices = range(idx_start, idx_end)

                for c_idx in c_indices:
                    ca = int(self.cat_pair_i[c_idx])
                    cb = int(self.cat_pair_j[c_idx])
                    v_cat = self.cat_pair_vec[c_idx]
                    d_cat = float(self.cat_pair_dist[c_idx])
                    scale = d_img / d_cat

                    # Test both orientations: (i -> ca, j -> cb) and (i -> cb, j -> ca)
                    for cat_first, cat_second, sign in ((ca, cb, 1.0), (cb, ca, -1.0)):
                        ang_cat = math.atan2(sign * v_cat[1], sign * v_cat[0])
                        # Rotation from catalog frame to image frame:
                        # In our convention:
                        # d_row = d_row_cat * cos(rot) - d_col_cat * sin(rot)
                        # d_col = d_row_cat * sin(rot) + d_col_cat * cos(rot)
                        # Which is a complex multiplication (row + i*col)_img = (row + i*col)_cat * exp(i * rot)!
                        rot = ang_img - ang_cat
                        cos_r = math.cos(rot) * scale
                        sin_r = math.sin(rot) * scale

                        # Implied pole position on sensor: det_xy[i] - R(rot)*scale * cat_xy[cat_first]
                        cr0, cc0 = self.cat_xy[cat_first]
                        pole_r = float(det_xy[i, 0] - (cr0 * cos_r - cc0 * sin_r))
                        pole_c = float(det_xy[i, 1] - (cr0 * sin_r + cc0 * cos_r))

                        # Reject implausible pole positions far outside sensor (+-1500 px)
                        if abs(pole_r - 480.0) > 1800.0 or abs(pole_c - 640.0) > 1800.0:
                            continue

                        # Transform catalog stars to image coordinates and count matches within 4.0 px
                        proj_r = pole_r + self.cat_xy[:, 0] * cos_r - self.cat_xy[:, 1] * sin_r
                        proj_c = pole_c + self.cat_xy[:, 0] * sin_r + self.cat_xy[:, 1] * cos_r

                        # Compute squared distances from each detected star to nearest projected catalog star
                        dr = det_xy[:, 0:1] - proj_r[None, :]
                        dc = det_xy[:, 1:2] - proj_c[None, :]
                        dist_sq = dr * dr + dc * dc
                        min_cat_idx = np.argmin(dist_sq, axis=1)
                        min_dist_sq = dist_sq[np.arange(m), min_cat_idx]

                        inlier_mask = min_dist_sq < 16.0  # 4.0 px radius
                        n_inliers = int(np.count_nonzero(inlier_mask))
                        if n_inliers > len(best_inliers):
                            inliers = [
                                (int(d_i), int(min_cat_idx[d_i]))
                                for d_i in np.nonzero(inlier_mask)[0]
                            ]
                            # Ensure unique catalog star IDs
                            unique_cats = {c_id: d_id for d_id, c_id in inliers}
                            if len(unique_cats) > len(best_inliers):
                                best_inliers = [(d_id, c_id) for c_id, d_id in unique_cats.items()]
                                best_pole = (pole_r, pole_c)
                                best_angle_rad = rot
                                best_scale = scale

                if len(best_inliers) >= max(8, m - 2):
                    break
            if len(best_inliers) >= max(8, m - 2):
                break

        # Minimum 4 matched catalog stars required (matches 0x6a1670: solved_stars >= 4)
        if len(best_inliers) < 4:
            return res

        # Least-squares similarity transform refinement over all inlier pairs:
        # det_xy = pole + scale * R(rot) @ cat_xy
        det_pts = np.array([det_xy[d_i] for d_i, _ in best_inliers], dtype=np.float64)
        cat_pts = np.array([self.cat_xy[c_i] for _, c_i in best_inliers], dtype=np.float64)

        det_mean = np.mean(det_pts, axis=0)
        cat_mean = np.mean(cat_pts, axis=0)
        dp = det_pts - det_mean
        cp = cat_pts - cat_mean

        # Complex least squares: z_det = alpha * z_cat where z = row + i * col
        denom = float(np.sum(cp[:, 0] ** 2 + cp[:, 1] ** 2))
        if denom > 1e-6:
            re_a = float(np.sum(dp[:, 0] * cp[:, 0] + dp[:, 1] * cp[:, 1])) / denom
            im_a = float(np.sum(dp[:, 1] * cp[:, 0] - dp[:, 0] * cp[:, 1])) / denom
            best_scale = math.hypot(re_a, im_a)
            best_angle_rad = math.atan2(im_a, re_a)
            pole_r = float(det_mean[0] - (cat_mean[0] * re_a - cat_mean[1] * im_a))
            pole_c = float(det_mean[1] - (cat_mean[0] * im_a + cat_mean[1] * re_a))
            best_pole = (pole_r, pole_c)

        # Compute RMS residual
        cos_r = math.cos(best_angle_rad) * best_scale
        sin_r = math.sin(best_angle_rad) * best_scale
        pred_r = best_pole[0] + cat_pts[:, 0] * cos_r - cat_pts[:, 1] * sin_r
        pred_c = best_pole[1] + cat_pts[:, 0] * sin_r + cat_pts[:, 1] * cos_r
        residuals = np.hypot(det_pts[:, 0] - pred_r, det_pts[:, 1] - pred_c)

        res.solved = True
        res.pole_row_x = best_pole[0]
        res.pole_col_y = best_pole[1]
        res.camera_angle_deg = math.degrees(best_angle_rad) % 360.0
        res.plate_scale_arcsec = self.nominal_plate_scale / best_scale
        res.rms_residual_px = float(np.sqrt(np.mean(residuals ** 2)))
        res.matched_stars = [
            (d_i, float(det_xy[d_i, 0]), float(det_xy[d_i, 1]), c_i)
            for d_i, c_i in best_inliers
        ]
        return res


class CenterCalibrator:
    """
    Fixed RA Axis Center Calibration Engine.
    Replaces the buggy state machine at 0x6944c0 / 0x6a1699 / 0x6a3a47 in iOptron iPolar.exe:
    - Uses a 4-frame rolling window with 2.8 px tolerance (instead of < 1.0 px hard reset).
    - Computes the RA rotation center (CenterX, CenterY) from Position 1 and Position 2 using BOTH:
      1. Exact rigid-body sky rotation (Pole chord + camera sky angle delta_theta = theta_2 - theta_1),
         which works even when ZERO common catalog star IDs overlap between Position 1 and Position 2!
      2. Perpendicular-bisector intersections of all common matched catalog stars (matching 0x694450),
         fused via median consensus.
    - Never blocks or deadlocks the UI thread when Auto-Confirm triggers.
    """

    def __init__(self, stability_threshold_px: float = 2.8, min_rotation_deg: float = 30.0) -> None:
        self.stability_threshold_px = stability_threshold_px
        self.min_rotation_deg = min_rotation_deg
        self.status = CalibrationStatus()
        self._recent_solves: List[PlateSolveResult] = []

    def reset(self, has_saved_center: bool = False, center_row: float = 0.0, center_col: float = 0.0) -> None:
        self._recent_solves.clear()
        if has_saved_center and center_row > 1.0 and center_col > 1.0:
            self.status = CalibrationStatus(
                state=3,
                candidate_center=(center_row, center_col),
                message=f"Center Calibrated: ({center_row:.1f}, {center_col:.1f})",
            )
        else:
            self.status = CalibrationStatus(
                state=1,
                message="Step 1: Point mount near pole & click 'Confirm Position 1'",
            )

    def start_calibration(self) -> None:
        self._recent_solves.clear()
        self.status = CalibrationStatus(
            state=1,
            message="Step 1: Point mount near pole & click 'Confirm Position 1'",
        )

    def confirm_position_1(self, solve: PlateSolveResult) -> bool:
        if not solve.solved:
            self.status.message = "Cannot confirm Position 1: Plate solve not locked yet."
            return False
        self.status.pos1_pole = (solve.pole_row_x, solve.pole_col_y)
        self.status.pos1_angle_deg = solve.camera_angle_deg
        self.status.pos1_stars = {
            cat_id: (r, c) for _, r, c, cat_id in solve.matched_stars
        }
        self.status.state = 2
        self.status.current_rotation_deg = 0.0
        self.status.can_confirm_pos2 = False
        self.status.stable_count = 0
        self._recent_solves.clear()
        self.status.message = "Position 1 saved! Rotate RA axis > 30° until stable."
        return True

    @staticmethod
    def _angle_diff_deg(a2: float, a1: float) -> float:
        """Signed shortest rotation angle from a1 to a2 in (-180, 180]."""
        d = (a2 - a1 + 180.0) % 360.0 - 180.0
        return d

    @staticmethod
    def _intersect_perp_bisectors(
        p1_a: Tuple[float, float],
        p2_a: Tuple[float, float],
        p1_b: Tuple[float, float],
        p2_b: Tuple[float, float],
    ) -> Optional[Tuple[float, float]]:
        """
        Intersect perpendicular bisector of segment (p1_a -> p2_a) with perpendicular bisector
        of segment (p1_b -> p2_b) (matches 0x694450).
        """
        dx1 = p2_a[0] - p1_a[0]
        dy1 = p2_a[1] - p1_a[1]
        mx1 = 0.5 * (p1_a[0] + p2_a[0])
        my1 = 0.5 * (p1_a[1] + p2_a[1])

        dx2 = p2_b[0] - p1_b[0]
        dy2 = p2_b[1] - p1_b[1]
        mx2 = 0.5 * (p1_b[0] + p2_b[0])
        my2 = 0.5 * (p1_b[1] + p2_b[1])

        # Line 1: dx1 * x + dy1 * y = dx1 * mx1 + dy1 * my1
        # Line 2: dx2 * x + dy2 * y = dx2 * mx2 + dy2 * my2
        det = dx1 * dy2 - dy1 * dx2
        if abs(det) < 1e-5:
            return None
        c1 = dx1 * mx1 + dy1 * my1
        c2 = dx2 * mx2 + dy2 * my2
        cx = (c1 * dy2 - dy1 * c2) / det
        cy = (dx1 * c2 - c1 * dx2) / det
        return (cx, cy)

    def compute_center_from_pos2(self, solve2: PlateSolveResult) -> Tuple[Optional[Tuple[float, float]], float]:
        """
        Compute RA rotation center (center_row_x, center_col_y) and rotation angle (degrees)
        between Position 1 and current Position 2 solve.
        Works whether 0, 1, or many common catalog stars overlap!
        """
        if not solve2.solved or self.status.pos1_pole is None:
            return None, 0.0

        d_theta_deg = self._angle_diff_deg(solve2.camera_angle_deg, self.status.pos1_angle_deg)
        abs_rot_deg = abs(d_theta_deg)
        if abs_rot_deg < 3.0:
            return None, abs_rot_deg

        p1 = self.status.pos1_pole
        p2 = (solve2.pole_row_x, solve2.pole_col_y)

        # Method 1: Exact rigid-body isometry from Pole (p1 -> p2) + sky rotation angle d_theta_deg
        # Since p2 - C = R(d_theta) * (p1 - C):
        # Midpoint M = 0.5*(p1 + p2), chord D = p2 - p1
        # C = M + (1 / (2 * tan(d_theta / 2))) * [-D_col, +D_row]
        d_theta_rad = math.radians(d_theta_deg)
        half_tan = math.tan(0.5 * d_theta_rad)
        candidates: List[Tuple[float, float]] = []

        if abs(half_tan) > 1e-4:
            mx = 0.5 * (p1[0] + p2[0])
            my = 0.5 * (p1[1] + p2[1])
            dx = p2[0] - p1[0]
            dy = p2[1] - p1[1]
            cot_half = 0.5 / half_tan
            cx_rigid = mx - dy * cot_half
            cy_rigid = my + dx * cot_half
            candidates.append((cx_rigid, cy_rigid))

        # Method 2: Perpendicular bisectors of common catalog stars + Pole chord (0x6944c0)
        common_segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = [(p1, p2)]
        for _, r2, c2, cat_id in solve2.matched_stars:
            if cat_id in self.status.pos1_stars:
                p1_s = self.status.pos1_stars[cat_id]
                p2_s = (r2, c2)
                if math.hypot(p2_s[0] - p1_s[0], p2_s[1] - p1_s[1]) > 5.0:
                    common_segments.append((p1_s, p2_s))

        if len(common_segments) >= 2:
            bisector_pts: List[Tuple[float, float]] = []
            for i in range(len(common_segments)):
                for j in range(i + 1, len(common_segments)):
                    pt = self._intersect_perp_bisectors(
                        common_segments[i][0],
                        common_segments[i][1],
                        common_segments[j][0],
                        common_segments[j][1],
                    )
                    if pt is not None and -500.0 <= pt[0] <= 1500.0 and -500.0 <= pt[1] <= 1800.0:
                        bisector_pts.append(pt)
            if bisector_pts:
                med_r = float(np.median([p[0] for p in bisector_pts]))
                med_c = float(np.median([p[1] for p in bisector_pts]))
                candidates.append((med_r, med_c))

        if not candidates:
            return None, abs_rot_deg

        # Combine rigid-body pole center and bisector median center
        final_r = float(np.mean([c[0] for c in candidates]))
        final_c = float(np.mean([c[1] for c in candidates]))
        return (final_r, final_c), abs_rot_deg

    def update_frame(self, solve: PlateSolveResult) -> CalibrationStatus:
        """
        Process a new plate-solved frame during Position 2 calibration.
        Updates live rotation angle, stability count, and candidate center.
        """
        if self.status.state != 2 or not solve.solved:
            return self.status

        center, rot_deg = self.compute_center_from_pos2(solve)
        self.status.current_rotation_deg = rot_deg
        if center is not None:
            self.status.candidate_center = center

        # Rolling stability check (fixes 0x6a1699 <1.0px hard reset bug)
        if self._recent_solves:
            prev = self._recent_solves[-1]
            step_dist = math.hypot(solve.pole_row_x - prev.pole_row_x, solve.pole_col_y - prev.pole_col_y)
            if step_dist <= self.stability_threshold_px:
                self._recent_solves.append(solve)
                if len(self._recent_solves) > 4:
                    self._recent_solves.pop(0)
            else:
                self._recent_solves = [solve]
        else:
            self._recent_solves = [solve]

        self.status.stable_count = len(self._recent_solves)
        self.status.can_confirm_pos2 = (
            center is not None
            and rot_deg >= self.min_rotation_deg
            and self.status.stable_count >= 3
        )

        if rot_deg < self.min_rotation_deg:
            self.status.message = (
                f"Rotate RA axis: {rot_deg:.1f}° / {self.min_rotation_deg:.0f}° "
                f"(Need {self.min_rotation_deg - rot_deg:.1f}° more)"
            )
        elif not self.status.can_confirm_pos2:
            self.status.message = (
                f"Rotation {rot_deg:.1f}° OK! Stabilizing mount ({self.status.stable_count}/3 frames)..."
            )
        else:
            self.status.message = (
                f"Ready! Rotation {rot_deg:.1f}°, Center=({center[0]:.1f}, {center[1]:.1f})"
            )
        return self.status

    def confirm_position_2(self) -> Optional[Tuple[float, float]]:
        if self.status.candidate_center is None:
            return None
        center = self.status.candidate_center
        self.status.state = 3
        self.status.can_confirm_pos2 = False
        self.status.message = f"Center Calibrated: ({center[0]:.1f}, {center[1]:.1f})"
        return center


class DarkFrameManager:
    """Loads, captures, and saves 1280x960 (or 1944x1096) raw 8-bit DarkFrame.bin."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.dark_frame: Optional[np.ndarray] = None
        self._accum: List[np.ndarray] = []
        self.target_frames = 5

    def load(self, height: int = 960, width: int = 1280) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            expected = height * width
            if os.path.getsize(self.path) == expected:
                data = np.fromfile(self.path, dtype=np.uint8)
                self.dark_frame = data.reshape((height, width))
                return True
        except OSError:
            pass
        return False

    def erase(self) -> None:
        self.dark_frame = None
        self._accum.clear()
        if os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    def add_capture_frame(self, frame: np.ndarray) -> Tuple[int, bool]:
        """Add a frame during dark frame capture. Returns (count, finished)."""
        self._accum.append(frame.astype(np.float32))
        count = len(self._accum)
        if count >= self.target_frames:
            avg = np.mean(self._accum, axis=0)
            self.dark_frame = np.clip(avg, 0, 255).astype(np.uint8)
            self._accum.clear()
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                self.dark_frame.tofile(self.path)
            except OSError:
                pass
            return count, True
        return count, False
