"""
iOptron iPolar Astrometric Catalog and Apparent Place Engine
Reverse-engineered from iOptron iPolar.exe v2.92 (.rdata 0x4ac5e00..0x4ac8a28, .text 0x69c770..0x69ccc0 & 0x6b2c20..0x6b60e0).

Contains:
- STARS_NORTH: 77 Northern Hemisphere circumpolar stars [RA_J2000_deg, Dec_J2000_deg, pmRA_mas_yr, pmDec_mas_yr, parallax_mas]
- STARS_SOUTH: 66 Southern Hemisphere circumpolar stars [RA_J2000_deg, Dec_J2000_deg, pmRA_mas_yr, pmDec_mas_yr, parallax_mas]
- IAU 1976/1980 Precession, Nutation (63-term series), Aberration, GMST/GAST, and Bennett Atmospheric Refraction.
"""

from __future__ import annotations
import math
from datetime import datetime, timezone
import numpy as np

# Plate scales extracted from MainPanel_DoWork (0x69a140)
PLATE_SCALE_IPOLAR_V1 = 30.939720709172285  # arcsec / pixel (1280 x 960)
PLATE_SCALE_IPOLAR_V2 = 23.926717413917513  # arcsec / pixel (1944 x 1096)

GMST_POLY = np.array([0.014506, 4612.156534, 1.3915817, -4.4e-07, -2.9956e-05, -3.68e-08], dtype=np.float64)

OBLIQUITY_POLY = np.array([84381.406, -46.836769, -0.0001831, 0.0020034, -5.76e-07, -4.34e-08], dtype=np.float64)

PRECESSION_ZETA = np.array([-2.650545, 2306.083227, 0.2988499, 0.01801828, -5.971e-06, -3.173e-07], dtype=np.float64)

PRECESSION_Z = np.array([-2.650545, 2306.077181, 1.0927348, 0.01826837, -2.8596e-05, -2.904e-07], dtype=np.float64)

PRECESSION_THETA = np.array([0.0, 2004.191903, -0.4294934, -0.04182264, -7.089e-06, -1.274e-07], dtype=np.float64)

DELAUNAY_POLY = np.array([[485868.249036, 1717915923.2178, 31.8792, 0.051635, -0.0002447], [1287104.793048, 129596581.0481, -0.5532, 0.000136, -1.149e-05], [335779.526232, 1739527262.8478, -12.7512, -0.001037, 4.17e-06], [1072260.703692, 1602961601.209, -6.3706, 0.006593, -3.169e-05], [450160.398036, -6962890.5431, 7.4722, 0.007702, -5.939e-05]], dtype=np.float64)

NUTATION_MULT = np.array([[0, 0, 0, 0, 1], [0, 0, 2, -2, 2], [0, 0, 2, 0, 2], [0, 0, 0, 0, 2], [0, 1, 0, 0, 0], [0, 1, 2, -2, 2], [1, 0, 0, 0, 0], [0, 0, 2, 0, 1], [1, 0, 2, 0, 2], [0, -1, 2, -2, 2], [0, 0, 2, -2, 1], [-1, 0, 2, 0, 2], [-1, 0, 0, 2, 0]], dtype=np.float64)

NUTATION_DPSI = np.array([-17.209210755999997, -1.3170906, -0.2276413, 0.2074554, 0.1475877, -0.0516821, 0.0711159, -0.0387298, -0.0301461, 0.0215829, 0.0128227, 0.0123457, 0.0156994], dtype=np.float64)

NUTATION_DEPS = np.array([9.2052331, 0.5730336, 0.0978459, -0.0897492, 0.0073871, 0.0224386, -0.000675, 0.0200728, 0.0129025, -0.0095929, -0.0068982, -0.0053311, -0.0001235], dtype=np.float64)

STARS_NORTH = np.array([[37.9522807, 89.2641023, 44.5, -11.9, 7.2918], [354.83582335, 77.63310085, -62.997, 171.457, 73.8307], [236.01506631, 77.79448406, 18.778, -2.398, 9.6733], [251.4932693, 82.03726856, 19.92, 1.287, 10.2446], [263.05488068, 86.58669235, 9.643, 55.05, 18.3212], [17.19230008, 86.25704527, 81.002, -11.413, 11.562], [302.22251416, 77.71152693, 12.044, 26.199, 10.0861], [144.27156593, 81.32630956, -16.794, -17.605, 3.7178], [105.01821579, 76.97735084, 73.763, -14.325, 17.1931], [343.60834238, 84.34627888, 99.721, 24.185, 8.9232], [341.87198938, 83.15403209, 24.943, 47.532, 10.2152], [212.2115789, 77.54765336, -29.183, 36.968, 3.9586], [112.76849622, 82.411288, -5.249, -42.174, 6.4908], [267.36333854, 76.96394409, 36.54, 247.809, 31.896], [287.29214337, 76.55998954, 52.87, -119.092, 36.4059], [232.8529386, 77.34939449, -44.662, 6.979, 4.5525], [183.04998476, 77.61632754, 10.23, 19.555, 29.7942], [115.12391239, 87.01996308, -38.212, -33.317, 6.0649], [333.29764715, 86.10817338, 65.037, 41.679, 12.3008], [157.76675104, 82.558738, -65.395, 12.982, 43.9944], [31.28324472, 77.28106401, 153.63, -91.619, 25.8042], [337.47082038, 78.82419695, 3.633, -20.722, 13.5856], [50.08362002, 77.73447432, 66.298, -65.6, 12.6573], [307.06171156, 81.42277735, 32.926, 16.026, 7.4482], [311.88863982, 80.55209987, -28.586, -36.985, 7.8052], [157.41721725, 84.25183773, -145.145, -21.494, 23.049], [67.49970595, 83.34081946, -44.91, 103.44, 10.8695], [246.42741304, 78.96431111, -112.499, 105.346, 24.1725], [18.07234418, 79.67394245, 94.584, -5.011, 12.3915], [13.72370554, 83.70733256, 59.337, -18.233, 12.1577], [338.06728609, 76.22639538, -16.98, -10.599, 4.1245], [222.59095385, 82.51097703, 177.19, -223.948, 22.8454], [53.0870705, 84.91045257, 68.642, -138.793, 9.1464], [240.88028563, 76.79403076, -17.656, 20.38, 5.9276], [310.64779638, 82.5312465, 29.675, 21.994, 9.1598], [277.43737293, 77.5470687, 0.499, 0.225, 7.1268], [262.70278847, 86.96808923, 55.493, 10.139, 20.4635], [201.73360917, 78.64400099, -140.984, 30.426, 11.194], [336.6764017, 78.78566355, -17.942, -48.098, 15.9906], [62.51733253, 86.62576395, 144.011, -87.282, 23.4701], [259.90510916, 80.13640333, 18.649, 0.793, 3.4977], [100.12061065, 77.99575818, 14.386, -6.075, 4.5573], [316.37235133, 78.1265164, 19.68, 28.987, 7.1426], [41.94904856, 81.44817201, 5.664, -70.85, 6.7929], [205.66198895, 78.0646376, -65.088, 46.199, 9.676], [205.59762115, 82.75223121, 29.433, -43.441, 8.6659], [342.76350862, 85.37421793, 14.38, 112.179, 7.643], [32.35446565, 81.29597218, -34.605, 6.674, 14.112], [250.77733443, 77.51485507, 56.53, 205.683, 27.7419], [321.20757505, 80.52477268, 42.55, -12.001, 8.4184], [290.418323, 79.60269654, 12.379, -31.644, 9.6794], [307.36668637, 81.09222832, 66.243, 221.381, 15.5367], [20.94490627, 78.72607458, -4.459, 3.984, 0.6255], [318.33983998, 81.23098505, -3.225, 3.979, 5.7771], [276.03922847, 83.17529231, 17.046, -23.907, 6.0233], [307.26315862, 83.62533989, 22.925, -17.056, 10.599], [7.73600868, 77.01925663, 339.878, -34.322, 26.0335], [183.83155013, 87.70034406, -27.122, 52.324, 20.6262], [181.11399553, 85.58752137, -55.702, 90.612, 12.9911], [184.22854492, 86.43577528, 211.298, -10.363, 24.9712], [180.07575259, 80.8530182, -64.821, -38.699, 3.1601], [137.80214952, 80.82884181, -21.108, -16.058, 13.2776], [20.08043128, 77.57107772, -19.574, 91.173, 7.6161], [267.54221682, 78.30657855, 3.791, 9.679, 3.8078], [249.46919594, 78.91886082, -24.271, 44.167, 8.1178], [9.94301612, 82.49409695, -116.849, 77.511, 30.2391], [124.22427252, 84.05756311, -8.682, -19.076, 5.2337], [247.65961105, 77.44769529, -104.121, 273.802, 9.035], [23.47755172, 89.01549618, 65.194, -33.246, 9.7893], [64.46555843, 80.53583194, -11.071, 10.429, 4.2746], [6.78132518, 80.05213484, 22.812, -1.66, 6.118], [71.50380912, 76.61046128, 75.668, -135.502, 30.5881], [259.22889448, 89.03768906, -23.989, -3.463, 3.7084], [290.39787486, 76.55959793, -5.816, 0.19, 2.0394], [320.58833036, 77.09013831, -15.043, -27.632, 7.0652], [359.11767501, 83.19114684, 50.433, 6.836, 11.0074], [159.00768227, 80.49461465, -17.792, -3.608, 6.4446]], dtype=np.float64)

STARS_SOUTH = np.array([[6.43779778, -77.25424528, 2219.6, 325.4, 134.07], [325.37027917, -77.39108534, 68.656, -250.044, 51.5172], [248.36003794, -78.89748476, -126.122, -78.699, 21.7674], [221.96534811, -79.04482391, -7.395, -15.726, 7.5519], [124.63357098, -76.91926305, 111.956, 108.276, 51.9434], [341.51301186, -81.38162228, -51.441, 1.789, 23.0989], [184.58582847, -79.31218506, -39.691, 10.609, 11.0311], [158.86627999, -78.60774021, -37.202, 10.049, 8.7044], [250.76379418, -77.51896105, -280.588, -355.704, 21.8518], [161.44489218, -80.54016105, -37.394, 7.254, 9.1857], [216.72638044, -83.66794455, -95.711, -13.557, 10.9922], [125.15801502, -77.48430454, -130.06, 39.585, 21.4168], [0.39765345, -77.06648634, -60.071, -175.489, 15.6889], [215.5964153, -80.10901967, -8.683, -13.542, 6.9644], [143.47145195, -80.94119967, -34.818, 13.028, 5.6572], [316.1796768, -77.02536059, 12.813, -369.325, 22.5215], [181.19219177, -76.51886577, -77.805, 45.837, 6.6724], [342.59555966, -80.12389925, 19.803, -12.518, 6.3259], [37.92160685, -79.10962463, 119.53, -49.875, 11.5131], [141.03470479, -80.78629641, -138.93, 135.014, 17.3878], [2.50835033, -82.22413409, -22.706, -20.267, 12.5955], [283.69265177, -87.60642759, -36.519, -135.219, 12.6815], [130.33064041, -78.96324723, -29.776, 26.6, 10.0442], [134.16403088, -85.66300606, -117.914, 33.972, 20.8838], [104.1435895, -79.42019753, -2.172, -1.176, 8.2527], [317.20148643, -88.95647909, 26.671, 5.612, 11.0917], [334.4600697, -77.51149578, -37.125, 13.832, 25.7375], [150.18164804, -82.21453619, -26.639, 31.129, 7.9879], [146.58752618, -76.77636133, 83.279, -56.443, 17.2608], [48.99251486, -77.38818549, 113.165, 62.542, 34.119], [205.22600224, -85.78614261, -86.743, -22.664, 11.7704], [225.46235188, -83.22742122, 19.184, 52.693, 8.5143], [24.36843802, -84.76950449, 34.976, 25.807, 9.5998], [52.49514064, -78.35196685, -12.07, -27.055, 9.6674], [226.19521881, -83.03838671, -8.278, -17.46, 2.3037], [126.07879585, -80.91325148, -150.502, 223.82, 12.0038], [65.24226619, -81.57938173, 10.145, 125.315, 18.4707], [211.33116943, -76.79690262, -83.823, -34.694, 9.2261], [337.9040141, -85.96698835, -37.508, 61.259, 9.5762], [353.33190014, -77.3853286, 16.304, 0.833, 32.0826], [24.48579905, -82.97447622, 122.832, 119.547, 37.4167], [198.57200446, -78.44747268, -9.199, -4.315, 1.5225], [160.46425882, -79.78326202, -16.601, 5.878, 3.1445], [131.4791468, -79.50402497, -26.758, 80.86, 5.5004], [255.24439575, -86.36429754, 8.285, 0.264, 5.2241], [262.86434576, -80.85932143, -3.926, -43.585, 4.2033], [277.33253402, -80.23297898, -22.516, -63.128, 9.2261], [354.60110885, -76.86965144, 83.598, -24.866, 6.123], [347.10152872, -79.48084203, 92.75, -35.708, 13.2818], [69.590382, -77.65605319, -6.99, -11.182, 6.0733], [306.22968221, -83.3105926, 23.254, 6.338, 13.221], [82.55754936, -84.78491589, -6.403, 43.052, 8.1301], [28.96282279, -78.34831413, 103.101, 53.896, 24.4803], [324.73658985, -79.44269895, 71.055, -26.577, 15.3305], [338.86170116, -78.77161673, 53.928, -7.234, 3.5101], [199.7007093, -79.97611743, -17.646, -23.204, 7.8483], [220.49838008, -77.01150447, -21.728, -11.033, 5.1002], [323.4781463, -82.68309691, 22.35, -2.455, 3.43], [330.4689808, -77.66256792, -11.999, -5.972, 9.7784], [158.85286039, -76.30895458, -17.982, 6.318, 4.4931], [186.40898555, -86.15064623, -4.423, -7.936, 2.956], [281.95417841, -77.8676105, -26.099, 148.708, 23.0186], [270.39221096, -85.21545491, -2.747, -146.27, 12.8623], [232.06787783, -88.13335262, -88.328, -80.185, 15.3453], [299.00766261, -81.34984493, 20.414, -1.162, 4.8898], [341.3805556, -88.81844891, 22.193, -35.866, 9.6463]], dtype=np.float64)


def datetime_to_jd(dt: datetime | None = None) -> float:
    """Convert UTC datetime to Julian Date (matches 0x6b2f60)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    year = dt.year
    month = dt.month
    day = dt.day + (dt.hour + (dt.minute + (dt.second + dt.microsecond / 1e6) / 60.0) / 60.0) / 24.0
    if month <= 2:
        year -= 1
        month += 12
    a = int(year // 100)
    b = 2 - a + (a // 4)
    return int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + b - 1524.5


def eval_poly(coeffs: np.ndarray, t: float) -> float:
    """Evaluate polynomial c[0] + c[1]*t + c[2]*t^2 + ... (matches 0x6b3220)."""
    res = 0.0
    for c in reversed(coeffs):
        res = res * t + float(c)
    return res


def nutation_and_obliquity(jd: float) -> tuple[float, float, float]:
    """
    Compute nutation in longitude dpsi (deg), nutation in obliquity deps (deg),
    and true obliquity eps (deg) using the 13-term IAU series at 0x6b5c40.
    """
    t = (jd - 2451545.0) / 36525.0
    d_args = np.array([(eval_poly(DELAUNAY_POLY[i], t) / 3600.0) % 360.0 for i in range(5)], dtype=np.float64)
    d_rad = np.deg2rad(d_args)
    arg = NUTATION_MULT @ d_rad
    dpsi_arcsec = float(np.sum(NUTATION_DPSI * np.sin(arg)))
    deps_arcsec = float(np.sum(NUTATION_DEPS * np.cos(arg)))
    dpsi_deg = dpsi_arcsec / 3600.0
    deps_deg = deps_arcsec / 3600.0
    eps0_arcsec = eval_poly(OBLIQUITY_POLY, t)
    eps_deg = eps0_arcsec / 3600.0 + deps_deg
    return dpsi_deg, deps_deg, eps_deg


def apparent_sidereal_time_deg(jd: float) -> float:
    """Compute Greenwich Apparent Sidereal Time in degrees [0, 360) (matches 0x6b4ab0)."""
    t = (jd - 2451545.0) / 36525.0
    gmst_deg = (
        GMST_POLY[0]
        + 360.98564736629 * (jd - 2451545.0)
        + GMST_POLY[2] * (t ** 2)
        + GMST_POLY[3] * (t ** 3)
    ) % 360.0
    dpsi_deg, _, eps_deg = nutation_and_obliquity(jd)
    gast_deg = (gmst_deg + dpsi_deg * math.cos(math.radians(eps_deg))) % 360.0
    return float(gast_deg)


def atmospheric_refraction_deg(true_alt_deg: float, pressure_pa: float = 101325.0, temp_k: float = 283.15) -> float:
    """
    Bennett atmospheric refraction in degrees (matches 0x6b5d80).
    Returns delta_alt >= 0 to add to true altitude (if true_alt_deg > -1.0).
    """
    if true_alt_deg < -1.0 or true_alt_deg >= 89.9:
        return 0.0
    angle_deg = true_alt_deg + 7.31 / (true_alt_deg + 4.4)
    r_arcmin = 1.0 / math.tan(math.radians(angle_deg))
    r_arcmin -= 0.06 * math.sin(math.radians(14.7 * r_arcmin + 13.0))
    scale = (pressure_pa / 101000.0) * (283.0 / max(temp_k, 150.0))
    return (r_arcmin * scale) / 60.0


def equatorial_to_horizontal(
    ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, gast_deg: float
) -> tuple[float, float]:
    """
    Convert (RA, Dec) to (Azimuth, Altitude) in degrees (matches 0x6b2c20).
    Azimuth convention in iPolar: measured from South (0 = South, +90 = West, 180 = North).
    """
    h_rad = math.radians((gast_deg + lon_deg - ra_deg) % 360.0)
    lat_rad = math.radians(lat_deg)
    dec_rad = math.radians(dec_deg)

    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    sin_dec = math.sin(dec_rad)
    cos_dec = math.cos(dec_rad)
    cos_h = math.cos(h_rad)
    sin_h = math.sin(h_rad)

    y = sin_h
    x = cos_h * sin_lat - math.tan(dec_rad) * cos_lat
    az_deg = math.degrees(math.atan2(y, x)) % 360.0

    sin_alt = max(-1.0, min(1.0, sin_lat * sin_dec + cos_lat * cos_dec * cos_h))
    alt_deg = math.degrees(math.asin(sin_alt))
    return az_deg, alt_deg


def horizontal_to_equatorial(
    az_deg: float, alt_deg: float, lat_deg: float, lon_deg: float, gast_deg: float
) -> tuple[float, float]:
    """
    Convert (Azimuth, Altitude) back to (RA, Dec) in degrees (matches 0x6b2dd0).
    """
    az_rad = math.radians(az_deg)
    alt_rad = math.radians(alt_deg)
    lat_rad = math.radians(lat_deg)

    sin_alt = math.sin(alt_rad)
    cos_alt = math.cos(alt_rad)
    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    cos_az = math.cos(az_rad)
    sin_az = math.sin(az_rad)

    h_rad = math.atan2(sin_az, cos_az * sin_lat + math.tan(alt_rad) * cos_lat)
    ra_deg = (gast_deg + lon_deg - math.degrees(h_rad)) % 360.0

    sin_dec = max(-1.0, min(1.0, sin_lat * sin_alt - cos_lat * cos_alt * cos_az))
    dec_deg = math.degrees(math.asin(sin_dec))
    return ra_deg, dec_deg


def compute_catalog_apparent(
    latitude_deg: float,
    longitude_deg: float,
    pressure_pa: float = 101325.0,
    temperature_k: float = 283.15,
    enable_refraction: bool = True,
    dt: datetime | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """
    Compute apparent (and optionally refracted) RA, Dec in degrees for the hemisphere catalog
    matching iPolar's 0x69c770 routine.
    Returns:
      (ra_deg_array, dec_deg_array, gast_deg)
    """
    jd = datetime_to_jd(dt)
    t = (jd - 2451545.0) / 36525.0
    years = t * 100.0

    raw = STARS_NORTH if latitude_deg >= 0.0 else STARS_SOUTH
    n = raw.shape[0]

    zeta_rad = math.radians(eval_poly(PRECESSION_ZETA, t) / 3600.0)
    z_rad = math.radians(eval_poly(PRECESSION_Z, t) / 3600.0)
    theta_rad = math.radians(eval_poly(PRECESSION_THETA, t) / 3600.0)

    dpsi_deg, deps_deg, eps_deg = nutation_and_obliquity(jd)
    gast_deg = apparent_sidereal_time_deg(jd)

    ra_out = np.zeros(n, dtype=np.float64)
    dec_out = np.zeros(n, dtype=np.float64)

    for i in range(n):
        ra0_deg, dec0_deg, pm_ra_mas, pm_dec_mas, _parallax_mas = raw[i]
        dec_pm_deg = dec0_deg + (pm_dec_mas * years) / 3600000.0
        cos_dec0 = max(1e-6, math.cos(math.radians(dec0_deg)))
        ra_pm_deg = ra0_deg + (pm_ra_mas * years) / (3600000.0 * cos_dec0)

        ra_r = math.radians(ra_pm_deg)
        dec_r = math.radians(dec_pm_deg)
        a = math.cos(dec_r) * math.sin(ra_r + zeta_rad)
        b = math.cos(theta_rad) * math.cos(dec_r) * math.cos(ra_r + zeta_rad) - math.sin(theta_rad) * math.sin(dec_r)
        c = math.sin(theta_rad) * math.cos(dec_r) * math.cos(ra_r + zeta_rad) + math.cos(theta_rad) * math.sin(dec_r)

        ra_prec = (math.degrees(math.atan2(a, b) + z_rad)) % 360.0
        dec_prec = math.degrees(math.asin(max(-1.0, min(1.0, c))))

        eps_r = math.radians(eps_deg)
        ra_pr = math.radians(ra_prec)
        dec_pr = math.radians(dec_prec)
        tan_dec = math.tan(dec_pr)
        dra_nut = (
            (math.cos(eps_r) + math.sin(eps_r) * math.sin(ra_pr) * tan_dec) * dpsi_deg
            - (math.cos(ra_pr) * tan_dec) * deps_deg
        )
        ddec_nut = (math.sin(eps_r) * math.cos(ra_pr)) * dpsi_deg + math.sin(ra_pr) * deps_deg

        ra_app = (ra_prec + dra_nut) % 360.0
        dec_app = max(-89.9999, min(89.9999, dec_prec + ddec_nut))

        if enable_refraction and 5.0 < abs(latitude_deg) <= 85.0:
            az_deg, alt_deg = equatorial_to_horizontal(ra_app, dec_app, latitude_deg, longitude_deg, gast_deg)
            r_deg = atmospheric_refraction_deg(alt_deg, pressure_pa, temperature_k)
            ra_app, dec_app = horizontal_to_equatorial(az_deg, alt_deg + r_deg, latitude_deg, longitude_deg, gast_deg)

        ra_out[i] = ra_app
        dec_out[i] = dec_app

    return ra_out, dec_out, gast_deg


def compute_pole_refraction_offset_deg(
    latitude_deg: float,
    pressure_pa: float = 101325.0,
    temperature_k: float = 283.15,
    enable_refraction: bool = True,
) -> float:
    """
    Compute atmospheric refraction lift of the Celestial Pole in degrees (matches 0x69ca60).
    At latitude |lat| in (5, 85], the pole is at true altitude |lat| and is lifted toward the zenith
    by atmospheric_refraction_deg(|lat|).
    """
    abs_lat = abs(latitude_deg)
    if not enable_refraction or abs_lat <= 5.0 or abs_lat > 85.0:
        return 0.0
    return atmospheric_refraction_deg(abs_lat, pressure_pa, temperature_k)
