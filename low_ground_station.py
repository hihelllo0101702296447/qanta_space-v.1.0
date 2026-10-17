#!/usr/bin/env python3
"""
================================================================================
 GROUND STATION  -  single-file, keyless, offline-math satellite ground station
================================================================================
 INSTALL (works in a venv OR system Python; the script also auto-installs
 anything missing on first run):

     pip install fastapi "uvicorn[standard]" sgp4 numpy requests
     pip install pyrtlsdr          # OPTIONAL - only for a real RTL-SDR dongle
                                   # (also needs the librtlsdr system library)

 RUN:   python ground_station.py            ->  http://127.0.0.1:8000
        HOST=0.0.0.0 PORT=9000 python ground_station.py   (LAN access)

 NO API KEYS. Data comes only from plain files / public pages:
   * TLE text files ........ celestrak.org/NORAD/elements/*.txt
   * Space weather ......... services.swpc.noaa.gov/text/*.txt (+ static json)
   * Launches .............. spaceflightnow.com launch-schedule page (scraped)
   * News .................. NASA / SpaceflightNow RSS
   * APOD .................. apod.nasa.gov page (scraped)
   * Mars raw images ....... mars.nasa.gov public raw-image feed
   * Weather imagery ....... NOAA STAR CDN / NOAA SWPC / NASA SDO direct JPGs
   * Orbit math ............ local sgp4 + numpy (works fully offline)
 Every network feature has a graceful fallback; the script never crashes.
================================================================================
"""
import importlib, importlib.util, site, subprocess, sys


def _bootstrap():
    need = {"fastapi": "fastapi", "uvicorn": "uvicorn[standard]", "websockets": "websockets",
            "sgp4": "sgp4", "numpy": "numpy", "requests": "requests"}
    miss = [p for m, p in need.items() if importlib.util.find_spec(m) is None]
    if not miss:
        return
    base = [sys.executable, "-m", "pip", "install", "-q"]
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    for extra in ([[]] if in_venv else [[], ["--user"], ["--break-system-packages"]]):
        if subprocess.call(base + extra + miss) == 0:
            try:
                sys.path.append(site.getusersitepackages())
            except Exception:
                pass
            importlib.invalidate_caches()
            return
    sys.exit("Auto-install failed. Run: pip install " + " ".join(miss))


_bootstrap()

import asyncio, json, math, os, re, tempfile, threading, time, random
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
import numpy as np
import requests
from sgp4.api import Satrec, SatrecArray
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

RtlSdr, HAS_RTL, RTL_ERR = None, False, ""


def _import_rtl():
    global RtlSdr, HAS_RTL, RTL_ERR
    for k in [k for k in sys.modules if k.startswith("rtlsdr")]:
        del sys.modules[k]
    try:
        from rtlsdr import RtlSdr as _R
        RtlSdr, HAS_RTL, RTL_ERR = _R, True, ""
    except Exception as e:  # ImportError, OSError (missing librtlsdr), etc.
        RtlSdr, HAS_RTL, RTL_ERR = None, False, f"{e.__class__.__name__}: {e}"


def _shim_missing_rtlsdr_symbols():
    """Fedora's librtlsdr lacks newer symbols (rtlsdr_set_dithering...) that pyrtlsdr>=0.3.x binds at import.
    Return harmless stubs for any missing rtlsdr_* function so the import succeeds."""
    import ctypes
    if getattr(ctypes.CDLL, "_gs_shim", False):
        return
    orig = ctypes.CDLL.__getattr__

    def ga(self, name):
        try:
            return orig(self, name)
        except AttributeError:
            if name.startswith("rtlsdr_"):
                return lambda *a, **k: -1
            raise
    ctypes.CDLL.__getattr__, ctypes.CDLL._gs_shim = ga, True


_import_rtl()
if not HAS_RTL and "undefined symbol" in RTL_ERR:
    _shim_missing_rtlsdr_symbols()
    _import_rtl()
if not HAS_RTL and "pkg_resources" in RTL_ERR:      # old pyrtlsdr needs setuptools' pkg_resources
    for _x in ([], ["--user"], ["--break-system-packages"]):
        if subprocess.call([sys.executable, "-m", "pip", "install", "-q", "setuptools<81"] + _x) == 0:
            break
    importlib.invalidate_caches()
    _import_rtl()


# ----------------------------------------------------------------------------
# Constants & orbital math (all local, no network)
# ----------------------------------------------------------------------------
A_KM = 6378.137
F_EARTH = 1 / 298.257223563
E2 = F_EARTH * (2 - F_EARTH)
OMEGA = 7.2921150e-5          # Earth rotation rad/s
C_KMS = 299792.458
UA = {"User-Agent": "Mozilla/5.0 (GroundStation/1.0)"}


def split_jd(ts):
    ts = np.atleast_1d(np.asarray(ts, dtype=float))
    return np.floor(ts / 86400.0) + 2440587.5, (ts % 86400.0) / 86400.0


def gmst(jd, fr):
    T = (jd - 2451545.0 + fr) / 36525.0
    s = 67310.54841 + (876600 * 3600 + 8640184.812866) * T + 0.093104 * T ** 2 - 6.2e-6 * T ** 3
    return np.deg2rad((s / 240.0) % 360.0)


def teme2ecef(r, th):
    c, s = np.cos(th), np.sin(th)
    return np.stack([r[..., 0] * c + r[..., 1] * s, -r[..., 0] * s + r[..., 1] * c, r[..., 2]], -1)


def ecef2geo(p):
    x, y, z = p[..., 0], p[..., 1], p[..., 2]
    lon, pr = np.arctan2(y, x), np.hypot(x, y)
    lat = np.arctan2(z, pr * (1 - E2))
    for _ in range(5):
        N = A_KM / np.sqrt(1 - E2 * np.sin(lat) ** 2)
        h = pr / np.cos(lat) - N
        lat = np.arctan2(z, pr * (1 - E2 * N / (N + h)))
    N = A_KM / np.sqrt(1 - E2 * np.sin(lat) ** 2)
    return np.degrees(lat), np.degrees(lon), pr / np.cos(lat) - N


def geo2ecef(lat, lon, alt_km):
    la, lo = math.radians(lat), math.radians(lon)
    N = A_KM / math.sqrt(1 - E2 * math.sin(la) ** 2)
    return np.array([(N + alt_km) * math.cos(la) * math.cos(lo),
                     (N + alt_km) * math.cos(la) * math.sin(lo),
                     (N * (1 - E2) + alt_km) * math.sin(la)])


def look(r, v, jd, fr, obs):
    """Geodetic subpoint, az/el/range and range-rate for TEME state vectors r,v (N,3)."""
    with np.errstate(all="ignore"):
        th = np.broadcast_to(gmst(jd, fr), r.shape[:-1])
        lat, lon, alt = ecef2geo(teme2ecef(r, th))
        o = geo2ecef(obs["lat"], obs["lon"], obs["alt"] / 1000.0)
        d = teme2ecef(r, th) - o
        φ, λ = math.radians(obs["lat"]), math.radians(obs["lon"])
        e = -math.sin(λ) * d[..., 0] + math.cos(λ) * d[..., 1]
        n = -math.sin(φ) * math.cos(λ) * d[..., 0] - math.sin(φ) * math.sin(λ) * d[..., 1] + math.cos(φ) * d[..., 2]
        u = math.cos(φ) * math.cos(λ) * d[..., 0] + math.cos(φ) * math.sin(λ) * d[..., 1] + math.sin(φ) * d[..., 2]
        rng = np.sqrt(e * e + n * n + u * u)
        az, el = np.degrees(np.arctan2(e, n)) % 360, np.degrees(np.arcsin(u / rng))
        c, s = np.cos(th), np.sin(th)
        ot = np.stack([o[0] * c - o[1] * s, o[0] * s + o[1] * c, np.zeros_like(th) + o[2]], -1)
        vo = OMEGA * np.stack([-ot[..., 1], ot[..., 0], np.zeros_like(th)], -1)
        dt = r - ot
        rr = np.sum(dt * (v - vo), -1) / np.linalg.norm(dt, axis=-1)
    return dict(lat=lat, lon=lon, alt=alt, az=az, el=el, rng=rng, rr=rr)


def predict_passes(sat, obs, hours=48, step=20, min_el=5.0):
    """Vectorised pass prediction. Each pass carries a thinned az/el/range-rate profile."""
    ts = time.time() + np.arange(0, hours * 3600, step)
    jd, fr = split_jd(ts)
    _, r, v = SatrecArray([sat]).sgp4(jd, fr)
    g = look(r[0], v[0], jd, fr, obs)
    el = np.nan_to_num(g["el"], nan=-90.0)
    up = (el > 0).astype(int)
    idx = np.flatnonzero(np.diff(np.r_[0, up, 0]))
    out = []
    for a, b in zip(idx[::2], idx[1::2]):
        if el[a:b].max() < min_el:
            continue
        samp = np.linspace(a, b - 1, min(60, b - a)).astype(int)
        out.append(dict(aos=float(ts[a]), los=float(ts[b - 1]), max_el=round(float(el[a:b].max()), 1),
                        az_aos=round(float(g["az"][a]), 0), az_los=round(float(g["az"][b - 1]), 0),
                        prof=[[round(float(ts[i] - ts[a]), 0), round(float(g["az"][i]), 1),
                               round(float(el[i]), 1), round(float(g["rr"][i]), 4)] for i in samp]))
        if len(out) >= 10:
            break
    return out


def sun_subpoint(t):
    jd = t / 86400 + 2440587.5
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 4e-7 * n)
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    j, f = split_jd(t)
    return dec, (math.degrees(ra - float(gmst(j, f)[0])) + 180) % 360 - 180


# ----------------------------------------------------------------------------
# TLE catalog (raw .txt downloads, disk cache, embedded offline fallback)
# ----------------------------------------------------------------------------
CEL = "https://celestrak.org/NORAD/elements/"
TLE_GROUPS = {"stations": "stations.txt", "isro": "", "weather": "weather.txt", "amateur": "amateur.txt",
              "cubesat": "cubesat.txt", "visual": "visual.txt", "gps-ops": "gps-ops.txt", "resource": "resource.txt",
              "science": "science.txt", "geo": "geo.txt", "starlink": "starlink.txt"}
ALIASES = {"isro": "active"}      # virtual group: CelesTrak 'active' list filtered by name
ISRO_RX = re.compile(r"INSAT|GSAT|CARTOSAT|RISAT|RESOURCESAT|OCEANSAT|IRNSS|NAVIC|NVS-|ASTROSAT|SCATSAT|EOS-|EMISAT|"
                     r"IMS-1|HAMSAT|NISAR|CMS-0|SARAL|MEGHA|YOUTHSAT|KALPANA|HYSIS|AZAADISAT|PSLV|GSLV|LVM3|TDS-0|INS-", re.I)
OFFLINE_TLE = """ISS (ZARYA) [OFFLINE-STALE]
1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927
2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537
VANGUARD 1 [OFFLINE-STALE]
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667
"""
CACHE_DIR = os.path.join(tempfile.gettempdir(), "gs_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def http(url, timeout=12, binary=False):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.content if binary else r.text


class Catalog:
    def __init__(self):
        self.group, self.names, self.sats, self.arr, self.source = "stations", [], [], None, ""

    @staticmethod
    def urls(group):
        f = TLE_GROUPS.get(group) or group + ".txt"
        u = [f"{CEL}gp.php?GROUP={group}&FORMAT=tle", CEL + f, "https://celestrak.com/NORAD/elements/" + f]
        if group in ("stations", "amateur", "cubesat", "science"):
            u.append("https://www.amsat.org/tle/current/nasabare.txt")      # AMSAT raw mirror
        if group == "stations":
            u.append("https://live.ariss.org/iss.txt")                       # ARISS ISS-only mirror
        return u

    def load(self, group):
        real = ALIASES.get(group, group)
        fn = os.path.join(CACHE_DIR, f"tle_{real}.txt")
        text, src = None, ""
        if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 6 * 3600:
            text, src = open(fn).read(), "cache"
        if text is None:
            errs = []
            for url in self.urls(real):
                try:
                    t = http(url, timeout=25 if real == "active" else 12)
                    if "<html" in t[:300].lower() or "\n1 " not in t.replace("\r", ""):
                        raise ValueError("not TLE data")
                    text, src = t, url.split("/")[2]
                    open(fn, "w").write(t)
                    break
                except Exception as e:
                    errs.append(f"{url.split('/')[2]}: {str(e)[:70]}")
            if text is None:
                elog("TLE download failed -> " + " | ".join(errs[:4]))
                if os.path.exists(fn):
                    text, src = open(fn).read(), "stale-cache"
                elif self.sats:
                    elog(f"Keeping previously loaded group [{self.group}]")
                    return
                else:
                    text, src = OFFLINE_TLE, "OFFLINE"
        flt = ISRO_RX if group == "isro" else None
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        names, sats, i = [], [], 0
        while i < len(lines) - 1 and len(sats) < 800:
            if lines[i].startswith("1 ") and lines[i + 1].startswith("2 "):          # 2-line sets (no name)
                nm, l1, l2, step = f"NORAD {lines[i][2:7].strip()}", lines[i], lines[i + 1], 2
            elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
                nm, l1, l2, step = re.sub(r"^0 ", "", lines[i].strip()), lines[i + 1], lines[i + 2], 3
            else:
                i += 1
                continue
            i += step
            if flt and (not flt.search(nm) or " DEB" in nm):
                continue
            try:
                sats.append(Satrec.twoline2rv(l1, l2))
                names.append(nm)
            except Exception:
                pass
        if not sats:
            if self.sats:
                elog(f"No satellites found for [{group}] - keeping [{self.group}]")
                return
            return self.load_offline()
        self.group, self.names, self.sats, self.source = group, names, sats, src
        self.arr = SatrecArray(sats)
        elog(f"Loaded {len(sats)} satellites [{group}] from {src}")

    def load_offline(self):
        lines = OFFLINE_TLE.splitlines()
        self.names = [lines[0], lines[3]]
        self.sats = [Satrec.twoline2rv(lines[1], lines[2]), Satrec.twoline2rv(lines[4], lines[5])]
        self.arr, self.source = SatrecArray(self.sats), "OFFLINE"

    def default_index(self):
        for pref in ("ISS (ZARYA)", "CARTOSAT", "INSAT-3DR", "NOAA 19"):
            for i, n in enumerate(self.names):
                if pref in n:
                    return i
        return 0


# ----------------------------------------------------------------------------
# SDR: real RTL-SDR via pyrtlsdr, or physically-plausible simulation
# ----------------------------------------------------------------------------
class SDR:
    NB = 256

    def __init__(self):
        self.lock = threading.Lock()
        self.center, self.rate, self.gain = 145.8e6, 2.4e6, "auto"
        self.dev, self.mode = None, "SIM"
        self.beacon_f, self.beacon_up = 145.8e6, False
        self.win = np.hanning(2048)
        if HAS_RTL:
            try:
                d = RtlSdr()
                d.sample_rate, d.center_freq, d.gain = self.rate, self.center, "auto"
                self.dev, self.mode = d, "RTL-SDR"
            except Exception as e:
                elog(f"RTL-SDR not available ({str(e)[:60]}) - simulation mode")
        else:
            hint = " -> run: sudo dnf install rtl-sdr" if re.search(r"cannot open|Error loading|No such file|not find", RTL_ERR, re.I) else ""
            elog(f"pyrtlsdr failed to import [{RTL_ERR[:140]}]{hint} - SDR simulation mode")

    def configure(self, center=None, rate=None, gain=None):
        with self.lock:
            if center is not None:
                self.center = float(center)
            if rate is not None:
                self.rate = float(rate)
            if gain is not None:
                self.gain = "auto" if gain == "auto" else float(gain)
            if self.dev:
                try:
                    self.dev.sample_rate, self.dev.center_freq, self.dev.gain = self.rate, self.center, self.gain
                except Exception as e:
                    elog(f"SDR config error: {e}")

    def row(self):
        with self.lock:
            db = None
            if self.dev:
                try:
                    x = self.dev.read_samples(16384)[:16384].reshape(8, 2048) * self.win
                    P = (np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2).mean(0) / 2048 ** 2
                    db = 10 * np.log10(P.reshape(self.NB, -1).mean(1) + 1e-12)
                except Exception as e:
                    elog(f"SDR read failed ({e.__class__.__name__}) -> simulation")
                    self.dev, self.mode = None, "SIM"
            if db is None:
                db = self._sim()
            center, rate, mode = self.center, self.rate, self.mode
        noise = float(np.median(db))
        pk = int(np.argmax(db))
        binw = rate / self.NB
        peaks = [i for i in range(1, self.NB - 1) if db[i] > noise + 8 and db[i] >= db[i - 1] and db[i] >= db[i + 1]]
        peaks = sorted(peaks, key=lambda i: -db[i])[:4]
        f0 = center - rate / 2
        stats = dict(noise=round(noise, 1), peak_f=round((f0 + (pk + .5) * binw) / 1e6, 4), peak_db=round(float(db[pk]), 1),
                     snr=round(float(db[pk]) - noise, 1), obw=round(float((db > noise + 6).sum()) * binw / 1e3, 1),
                     carriers=[round((f0 + (i + .5) * binw) / 1e6, 4) for i in peaks], mode=mode)
        return dict(type="spec", bins=np.round(db, 1).tolist(), f0=f0 / 1e6, f1=(center + rate / 2) / 1e6, stats=stats)

    def _sim(self):
        t = time.time()
        f = np.linspace(-.5, .5, self.NB) * self.rate + self.center
        binw = self.rate / self.NB
        lin = 10 ** ((-85 + np.random.randn(self.NB) * 1.2) / 10)

        def add(freq, pw, bw):
            nonlocal lin
            bw = max(bw, binw * .6)
            lin = lin + 10 ** (pw / 10) * np.exp(-0.5 * ((f - freq) / bw) ** 2)
        add(98.3e6, -50, 60e3); add(100.1e6, -55, 60e3); add(162.55e6, -58, 6e3)
        add(137.1e6, -57 + 3 * math.sin(t / 3), 15e3); add(137.62e6, -60, 15e3)
        add(145.5e6, -52 if int(t / 4) % 3 == 0 else -90, 5e3)
        add(145.825e6, -62 + 6 * (random.random() > .93), 1.5e3)
        add(433.92e6, -70 + 10 * (random.random() > .8), 3e3)
        if self.beacon_up:
            add(self.beacon_f, -48 + 4 * math.sin(t * 2), 1.2e3)
        return 10 * np.log10(lin)


# ----------------------------------------------------------------------------
# Public data feeds (scraped / plain-text / RSS) with TTL cache + mock fallback
# ----------------------------------------------------------------------------
def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def xcls(f):
    for L, th in (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7)):
        if f >= th:
            return f"{L}{f / th:.1f}"
    return f"A{f / 1e-8:.1f}"


def feed_wx():
    t = http("https://services.swpc.noaa.gov/text/wwv.txt")
    d = dict(kp=None, sfi=None, a=None, xray=None, forecast=[])
    for key, rx in (("sfi", r"Solar flux (\d+)"), ("a", r"A-index (\d+)"), ("kp", r"K-index at \d+ UTC on [\w ]+ was (\d+(?:\.\d+)?)")):
        m = re.search(rx, t)
        d[key] = float(m.group(1)) if m else None
    try:
        t3 = http("https://services.swpc.noaa.gov/text/3-day-forecast.txt").split("Kp index breakdown")[-1]
        rows = [re.findall(r"\b(\d\.\d{2})\b", l) for l in t3.splitlines() if re.match(r"\d\d-\d\dUT", l.strip())]
        rows = [r for r in rows if len(r) >= 3][:8]
        d["forecast"] = [float(rows[s][day]) for day in range(3) for s in range(len(rows))]
    except Exception:
        pass
    try:
        j = json.loads(http("https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json"))
        d["xray"] = xcls([x for x in j if x.get("energy") == "0.1-0.8nm"][-1]["flux"])
    except Exception:
        pass
    if d["kp"] is None and d["sfi"] is None:
        raise ValueError("no data")
    return d


def feed_launches():
    h = http("https://spaceflightnow.com/launch-schedule/")
    items = re.findall(r'class="launchdate">(.*?)</[^>]+>\s*<[^>]*class="mission">(.*?)</', h, re.S)
    items = [dict(date=strip_tags(a), name=strip_tags(b)) for a, b in items][:10]
    if not items:
        raise ValueError("no launches parsed")
    return dict(items=items)


def feed_news():
    for url in ("https://www.nasa.gov/feed/", "https://spaceflightnow.com/feed/"):
        try:
            root = ET.fromstring(http(url).encode("utf-8"))
            items = [dict(title=i.findtext("title"), link=i.findtext("link"), date=(i.findtext("pubDate") or "")[:16])
                     for i in root.iter("item")][:8]
            if items:
                return dict(items=items)
        except Exception:
            continue
    raise ValueError("no news")


def _apod_page(url):
    h = http(url)
    img = re.search(r'<img[^>]+src="(image/[^"]+)"', h, re.I) or re.search(r'<a href="(image/[^"]+\.(?:jpg|jpeg|png|gif))"', h, re.I)
    if not img:
        return None
    title = next((strip_tags(t) for t in re.findall(r"<b>\s*([^<]{3,150}?)\s*</b>", h) if "Astronomy" not in t), "APOD")
    ex = re.search(r"Explanation:\s*</b>(.*?)<p>", h, re.S | re.I)
    return dict(title=title, img="https://apod.nasa.gov/apod/" + img.group(1), text=strip_tags(ex.group(1))[:420] if ex else "")


def feed_apod():
    base = "https://apod.nasa.gov/apod/"
    d = _apod_page(base + "astropix.html")
    if d is None:      # today's APOD is a video/interactive -> walk back through the archive
        for link in re.findall(r'<a href="(ap\d{6}\.html)"', http(base + "archivepics.html"))[:6]:
            d = _apod_page(base + link)
            if d:
                d["title"] += " (recent)"
                break
    if d is None:
        raise ValueError("no APOD image found")
    return d


def feed_mars():
    for cat in ("msl", "mars2020"):
        try:
            raw = http(f"https://mars.nasa.gov/rss/api/?feed=raw_images&category={cat}&feedtype=json&num=12&page=0&order=sol+desc", timeout=15)
        except Exception:
            continue
        urls = list(dict.fromkeys(re.findall(r'https?:[^"\s\\]+?\.(?:jpg|jpeg|png)', raw.replace("\\/", "/"))))
        urls = [u for u in urls if "thumb" not in u.lower()] or urls
        if urls:
            return dict(items=[dict(url=u, label=cat.upper() + " · " + u.rsplit("/", 1)[-1][:30]) for u in urls[:8]])
    raise ValueError("Mars feed returned no image URLs")


def rss_items(url, n=8):
    root = ET.fromstring(http(url).encode("utf-8"))
    return [dict(title=i.findtext("title"), link=i.findtext("link"), date=(i.findtext("pubDate") or "")[:16])
            for i in root.iter("item")][:n]


def feed_isro():
    items = rss_items("https://news.google.com/rss/search?q=ISRO+when:14d&hl=en-IN&gl=IN&ceid=IN:en", 10)
    if not items:
        raise ValueError("no ISRO news")
    return dict(items=items)


def feed_insat():
    """ISRO MOSDAC public datacasting XML: latest INSAT-3DR imager preview JPGs."""
    raw = http("https://www.mosdac.gov.in/3drimager.xml", timeout=20)
    rx = re.compile(r"https?://[^<\s\"]*?3RIMG_(\d{2}[A-Za-z]{3}\d{4})_(\d{4})_([A-Za-z0-9_]+)\.jpg")
    best = {}
    for m in rx.finditer(raw):
        try:
            ts = time.strptime(m.group(1) + m.group(2), "%d%b%Y%H%M")
        except ValueError:
            continue
        if m.group(3) not in best or ts > best[m.group(3)][0]:
            best[m.group(3)] = (ts, m.group(0))
    items = [dict(url=u, label=f"{prod} · {time.strftime('%d %b %H:%MZ', ts)}")
             for prod, (ts, u) in sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)][:6]
    if not items:
        raise ValueError("no INSAT previews")
    return dict(items=items)


FEEDS = {
    "wx": (600, feed_wx, lambda: dict(kp=2.0, sfi=140, a=6, xray="B4.2", forecast=[2, 2, 3, 3, 2, 2, 1, 2] * 3)),
    "launches": (3600, feed_launches, lambda: dict(items=[dict(date="--", name="Launch schedule unreachable (offline demo data)")])),
    "news": (900, feed_news, lambda: dict(items=[dict(title="News feed unreachable - check connection", link="#", date="")])),
    "apod": (21600, feed_apod, lambda: dict(title="APOD unavailable", img="", text="Feed unreachable or today's APOD is a video.")),
    "mars": (3600, feed_mars, lambda: dict(items=[])),
    "isro": (900, feed_isro, lambda: dict(items=[dict(title="ISRO news unreachable - check connection", link="#", date="")])),
    "insat": (900, feed_insat, lambda: dict(items=[])),
}
FEED_CACHE = {}


def get_feed(name):
    ttl, fn, mock = FEEDS[name]
    c = FEED_CACHE.get(name)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        d = fn(); d["mock"] = False
    except Exception as e:
        d = dict(c[1], stale=True) if c else dict(mock(), mock=True)
        elog(f"Feed '{name}' fallback: {str(e)[:70]}")
    FEED_CACHE[name] = (time.time(), d)
    return d


# ----------------------------------------------------------------------------
# Application state, websocket hub, background loops
# ----------------------------------------------------------------------------
LOGQ, CLIENTS = [], set()
ST = dict(obs=dict(lat=12.9716, lon=77.5946, alt=900.0), sel=0, freq=145.8e6, auto=False, was_up=False,
          track=None, track_key=None, passes_msg=None, alerted=0, tlm_next=0)
CAT, RADIO = Catalog(), None


def elog(msg):
    LOGQ.append(f"{time.strftime('%H:%M:%S', time.gmtime())}Z  {msg}")
    print("[GS]", msg)


async def broadcast(m):
    if not CLIENTS:
        return
    s, dead = json.dumps(m, separators=(",", ":")), []
    for w in list(CLIENTS):
        try:
            await w.send_text(s)
        except Exception:
            dead.append(w)
    for w in dead:
        CLIENTS.discard(w)


def init_msg():
    return dict(type="init", groups=list(TLE_GROUPS), group=CAT.group, names=CAT.names, sel=ST["sel"], obs=ST["obs"],
                freq=ST["freq"], src=CAT.source, sdr=dict(mode=RADIO.mode, center=RADIO.center, rate=RADIO.rate))


async def refresh_passes():
    try:
        i = ST["sel"]
        ps = await asyncio.to_thread(predict_passes, CAT.sats[i], ST["obs"])
        ST["passes_msg"] = dict(type="passes", name=CAT.names[i], passes=ps, made=time.time())
        await broadcast(ST["passes_msg"])
    except Exception as e:
        elog(f"Pass prediction error: {e}")


def build_tick():
    t = time.time()
    jd, fr = split_jd([t])
    _, r, v = CAT.arr.sgp4(jd, fr)
    r, v = r[:, 0, :], v[:, 0, :]
    g = look(r, v, jd, fr, ST["obs"])
    ok = np.isfinite(g["lat"])
    pos = np.stack([np.where(ok, g["lat"], 999).round(2), np.where(ok, g["lon"], 999).round(2),
                    ((g["el"] > 0) & ok).astype(float)], 1).tolist()
    i = ST["sel"]
    sel, track, foot = None, None, 0
    if ok[i]:
        rr, el = float(g["rr"][i]), float(g["el"][i])
        dop = -ST["freq"] * rr / C_KMS
        sel = dict(name=CAT.names[i], lat=round(float(g["lat"][i]), 3), lon=round(float(g["lon"][i]), 3),
                   alt=round(float(g["alt"][i]), 1), az=round(float(g["az"][i]), 1), el=round(el, 1),
                   rng=round(float(g["rng"][i]), 1), spd=round(float(np.linalg.norm(v[i])), 3), dop=round(dop, 1))
        foot = round(math.degrees(math.acos(A_KM / (A_KM + float(g["alt"][i])))), 2)
        key = (i, int(t // 30))
        if key != ST["track_key"]:
            ts = t + np.arange(0, 5700, 60)
            j2, f2 = split_jd(ts)
            _, r2, _ = SatrecArray([CAT.sats[i]]).sgp4(j2, f2)
            la, lo, _ = ecef2geo(teme2ecef(r2[0], gmst(j2, f2)))
            track = [[round(float(a), 1), round(float(b), 1)] for a, b in zip(la, lo) if np.isfinite(a)]
            ST["track_key"] = key
        up = el > 0
        if up != ST["was_up"]:
            elog(f"{'AOS' if up else 'LOS'} {CAT.names[i]}  (el {el:.1f}°)")
            ST["was_up"] = up
        RADIO.beacon_f, RADIO.beacon_up = ST["freq"] + dop, up
        if ST["auto"] and abs(RADIO.center - RADIO.beacon_f) > 500:
            RADIO.configure(center=RADIO.beacon_f if up else ST["freq"])
    dec, slon = sun_subpoint(t)
    age = ((t / 86400 + 2440587.5) - 2451550.1) % 29.530588853
    return dict(type="tick", t=t, sun=[round(dec, 2), round(slon, 2)], moon=[round(age, 1), round((1 - math.cos(2 * math.pi * age / 29.5306)) / 2, 2)],
                pos=pos, sel=sel, track=track, foot=foot, n=len(pos))


async def retry_tle():
    await asyncio.to_thread(CAT.load, CAT.group)
    if CAT.source != "OFFLINE":
        ST["sel"], ST["track_key"] = CAT.default_index(), None
        await broadcast(init_msg()); await refresh_passes()


async def tick_loop():
    while True:
        try:
            if CLIENTS:
                await broadcast(build_tick())
            while LOGQ:
                await broadcast(dict(type="log", msg=LOGQ.pop(0)))
            if CAT.source == "OFFLINE" and time.time() > ST.setdefault("retry", time.time() + 120):
                ST["retry"] = time.time() + 120
                asyncio.create_task(retry_tle())
            pm = ST["passes_msg"]
            if not pm or time.time() - pm["made"] > 900:
                ST["passes_msg"] = dict(pm or {}, made=time.time())
                asyncio.create_task(refresh_passes())
        except Exception as e:
            print("[GS] tick error:", e)
        await asyncio.sleep(1)


async def spec_loop():
    while True:
        try:
            if CLIENTS:
                await broadcast(await asyncio.to_thread(RADIO.row))
                if RADIO.mode == "SIM" and RADIO.beacon_up and time.time() > ST["tlm_next"]:
                    ST["tlm_next"] = time.time() + 5
                    await broadcast(dict(type="tlm", line=f"[SIM] {CAT.names[ST['sel']][:14]}>BEACON "
                                         f"vbat={7.3 + random.random() * .6:.2f}V temp={18 + random.random() * 9:.1f}C "
                                         f"sol={random.randint(60, 99)}% rssi={-95 + random.randint(0, 20)}dBm"))
        except Exception as e:
            print("[GS] spec error:", e)
        await asyncio.sleep(0.2)


async def handle(m):
    c = m.get("cmd")
    if c == "select":
        ST["sel"], ST["track_key"] = max(0, min(int(m["idx"]), len(CAT.sats) - 1)), None
        await refresh_passes()
    elif c == "group" and m.get("group") in TLE_GROUPS:
        await asyncio.to_thread(CAT.load, m["group"])
        ST["sel"], ST["track_key"] = CAT.default_index(), None
        await broadcast(init_msg()); await refresh_passes()
    elif c == "observer":
        ST["obs"] = dict(lat=float(m["lat"]), lon=float(m["lon"]), alt=float(m.get("alt", 0)))
        elog(f"Observer set {ST['obs']['lat']:.3f}, {ST['obs']['lon']:.3f}")
        await refresh_passes()
    elif c == "freq":
        ST["freq"] = float(m["hz"])
    elif c == "sdr":
        ST["auto"] = bool(m.get("auto"))
        RADIO.configure(center=m.get("center"), rate=m.get("rate"), gain=m.get("gain"))


@asynccontextmanager
async def lifespan(app):
    global RADIO
    await asyncio.to_thread(CAT.load, "stations")
    if not CAT.sats:
        CAT.load_offline()
    ST["sel"] = CAT.default_index()
    RADIO = await asyncio.to_thread(SDR)
    tasks = [asyncio.create_task(tick_loop()), asyncio.create_task(spec_loop()), asyncio.create_task(refresh_passes())]
    yield
    for t in tasks:
        t.cancel()
    if RADIO and RADIO.dev:
        try:
            RADIO.dev.close()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/data/{name}")
async def data(name: str):
    if name not in FEEDS:
        return JSONResponse({"error": "unknown feed"}, status_code=404)
    return JSONResponse(await asyncio.to_thread(get_feed, name))


@app.websocket("/ws")
async def ws_ep(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    try:
        await ws.send_text(json.dumps(init_msg()))
        if ST["passes_msg"] and ST["passes_msg"].get("passes") is not None:
            await ws.send_text(json.dumps(ST["passes_msg"]))
        while True:
            try:
                await handle(json.loads(await ws.receive_text()))
            except WebSocketDisconnect:
                raise
            except Exception as e:
                elog(f"Bad command: {e}")
    except Exception:
        pass
    finally:
        CLIENTS.discard(ws)


# ----------------------------------------------------------------------------
# Dashboard (Tailwind + Chart.js via CDN)
# ----------------------------------------------------------------------------
HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GROUND STATION</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
body{background:#05080f;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.card{background:#0a111d;border:1px solid #1b2a42;border-radius:.5rem;padding:.7rem;box-shadow:0 0 18px #0ea5e90d inset}
.t{font-size:.65rem;letter-spacing:.18em;color:#2dd4bf;text-transform:uppercase;margin-bottom:.5rem;display:flex;justify-content:space-between}
.k{font-size:.6rem;color:#64748b}.v{color:#e2e8f0;font-size:.95rem}
input,select,button{background:#0f1a2b;border:1px solid #24364f;border-radius:.3rem;padding:.2rem .4rem;color:#e2e8f0;font-size:.75rem;max-width:100%}
button:hover{background:#16314f}canvas{width:100%;display:block}
.pill{font-size:.65rem;padding:.1rem .5rem;border-radius:9999px;border:1px solid #24364f}
tr.sel{background:#12304a}td,th{padding:.15rem .3rem;text-align:left}
a{color:#7dd3fc}a:hover{text-decoration:underline}
</style></head>
<body class="text-slate-300 text-sm">
<header class="flex flex-wrap items-center gap-3 px-4 py-2 border-b border-slate-800 bg-[#070c15]">
 <div class="text-teal-300 font-bold tracking-[.3em]">◉ GROUND STATION</div>
 <span id="utc" class="v"></span><span id="loc" class="k"></span>
 <div class="ml-auto flex gap-2 items-center">
  <label class="k"><input type="checkbox" id="beep"> AOS beep</label>
  <span id="pWS" class="pill text-red-400">WS OFF</span><span id="pSDR" class="pill">SDR ?</span><span id="pTLE" class="pill">TLE ?</span>
 </div></header>

<main class="grid grid-cols-1 lg:grid-cols-12 gap-3 p-3">
 <section class="card lg:col-span-8"><div class="t"><span>World track · day/night</span><span id="nsat" class="k"></span></div><canvas id="map"></canvas></section>

 <section class="card lg:col-span-4">
  <div class="t"><span>Tracking</span><span id="moon" class="k"></span></div>
  <div class="flex gap-2 mb-2"><input id="satIn" list="satList" placeholder="search satellite…" class="flex-1"><datalist id="satList"></datalist>
   <select id="grp"></select></div>
  <div id="satName" class="text-amber-300 font-bold mb-2">—</div>
  <div class="grid grid-cols-4 gap-2">
   <div><div class="k">LAT</div><div class="v" id="rLat">–</div></div><div><div class="k">LON</div><div class="v" id="rLon">–</div></div>
   <div><div class="k">ALT km</div><div class="v" id="rAlt">–</div></div><div><div class="k">SPD km/s</div><div class="v" id="rSpd">–</div></div>
   <div><div class="k">AZ</div><div class="v" id="rAz">–</div></div><div><div class="k">EL</div><div class="v" id="rEl">–</div></div>
   <div><div class="k">RANGE km</div><div class="v" id="rRng">–</div></div><div><div class="k">DOPPLER Hz</div><div class="v text-teal-300" id="rDop">–</div></div>
  </div>
  <div id="cd" class="mt-2 text-center text-lg text-emerald-300">—</div>
  <div class="grid grid-cols-4 gap-1 mt-2 items-end">
   <div><div class="k">OBS LAT</div><input id="oLat" class="w-full"></div><div><div class="k">LON</div><input id="oLon" class="w-full"></div>
   <div><div class="k">ALT m</div><input id="oAlt" class="w-full"></div><div><button id="oGo" class="w-full">SET</button></div>
   <div class="col-span-2"><div class="k">DOWNLINK MHz</div><input id="fq" class="w-full" value="145.800"></div>
   <div class="col-span-2"><button id="gps" class="w-full">⌖ Use my location</button></div>
  </div></section>

 <section class="card lg:col-span-8"><div class="t"><span>Spectrum · waterfall</span><span id="fr" class="k"></span></div>
  <div style="height:150px"><canvas id="spec"></canvas></div>
  <canvas id="wf" width="256" height="140" style="height:200px;image-rendering:pixelated;margin-top:4px"></canvas></section>

 <section class="card lg:col-span-4"><div class="t"><span>SDR control</span><span id="sdrMode" class="k"></span></div>
  <div class="grid grid-cols-2 gap-2">
   <div><div class="k">CENTER MHz</div><input id="sC" class="w-full" value="145.800"></div>
   <div><div class="k">RATE MS/s</div><select id="sR" class="w-full"><option>0.25</option><option>1.024</option><option>1.4</option><option>2.048</option><option selected>2.4</option></select></div>
   <div><div class="k">GAIN dB (blank=auto)</div><input id="sG" class="w-full"></div>
   <div class="flex items-end gap-2"><button id="sGo" class="flex-1">APPLY</button></div>
  </div>
  <label class="k block mt-2"><input type="checkbox" id="auto"> Auto-tune to satellite (Doppler corrected)</label>
  <div class="t mt-3"><span>Telemetry processor</span></div>
  <div class="grid grid-cols-3 gap-2 text-center">
   <div><div class="k">NOISE dB</div><div class="v" id="tN">–</div></div><div><div class="k">PEAK MHz</div><div class="v" id="tP">–</div></div><div><div class="k">SNR dB</div><div class="v" id="tS">–</div></div>
   <div><div class="k">OBW kHz</div><div class="v" id="tO">–</div></div><div class="col-span-2"><div class="k">CARRIERS MHz</div><div class="v text-xs" id="tC">–</div></div></div>
  <div id="tlm" class="mt-2 text-xs text-emerald-300 h-16 overflow-y-auto"></div></section>

 <section class="card lg:col-span-4"><div class="t"><span>Passes (local time)</span><span id="pn" class="k"></span></div>
  <table class="w-full text-xs"><thead class="k"><tr><th>AOS</th><th>DUR</th><th>MAX°</th><th>AZ</th></tr></thead><tbody id="pt"></tbody></table></section>
 <section class="card lg:col-span-4"><div class="t"><span>Doppler curve · kHz</span></div><div style="height:230px"><canvas id="dopC"></canvas></div></section>
 <section class="card lg:col-span-4"><div class="t"><span>Sky view</span></div><canvas id="polar" width="300" height="300" style="max-width:300px;margin:auto"></canvas></section>

 <section class="card lg:col-span-4"><div class="t"><span>Space weather (NOAA SWPC)</span><span id="wxs" class="k"></span></div>
  <div class="grid grid-cols-4 gap-2 text-center"><div><div class="k">Kp</div><div class="v" id="wKp">–</div></div><div><div class="k">SFI</div><div class="v" id="wS">–</div></div>
  <div><div class="k">A-idx</div><div class="v" id="wA">–</div></div><div><div class="k">X-RAY</div><div class="v" id="wX">–</div></div></div>
  <div class="k mt-2">3-day Kp forecast</div><div style="height:110px"><canvas id="kpC"></canvas></div></section>
 <section class="card lg:col-span-4"><div class="t"><span>Launch schedule</span></div><ul id="launch" class="space-y-1 text-xs"></ul></section>
 <section class="card lg:col-span-4"><div class="t"><span>Space news (RSS)</span></div><ul id="news" class="space-y-1 text-xs"></ul></section>

 <section class="card lg:col-span-4"><div class="t"><span>Weather · Sun · Aurora</span>
  <select id="imSel"><option value="https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/678x678.jpg">GOES-19 East GeoColor</option>
  <option value="https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/13/678x678.jpg">GOES-19 IR (Band 13)</option>
  <option value="https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/02/678x678.jpg">GOES-19 Visible (Band 02)</option>
  <option value="https://cdn.star.nesdis.noaa.gov/GOES18/ABI/FD/GEOCOLOR/678x678.jpg">GOES-18 West GeoColor</option>
  <option value="https://cdn.star.nesdis.noaa.gov/GOES19/ABI/CONUS/GEOCOLOR/1250x750.jpg">GOES-19 CONUS</option>
  <option value="https://services.swpc.noaa.gov/images/animations/ovation/north/latest.jpg">Aurora North</option>
  <option value="https://services.swpc.noaa.gov/images/animations/ovation/south/latest.jpg">Aurora South</option>
  <option value="https://sdo.gsfc.nasa.gov/assets/img/latest/latest_512_0304.jpg">Sun 304Å (SDO)</option>
  <option value="https://sdo.gsfc.nasa.gov/assets/img/latest/latest_512_0193.jpg">Sun 193Å (SDO)</option>
  <option value="https://sdo.gsfc.nasa.gov/assets/img/latest/latest_512_HMIIC.jpg">Sun HMI (SDO)</option></select></div>
  <img id="im" class="w-full rounded" alt="imagery"><div id="imE" class="k hidden">Image unavailable.</div></section>
 <section class="card lg:col-span-4"><div class="t"><span>Astronomy Picture of the Day</span></div>
  <img id="apImg" class="w-full rounded mb-1 hidden" alt=""><div id="apT" class="text-amber-300 text-xs"></div><div id="apX" class="k"></div></section>
 <section class="card lg:col-span-4"><div class="t"><span>Mars · Curiosity raw</span></div><div id="mars" class="grid grid-cols-2 gap-1"></div></section>

 <section class="card lg:col-span-6"><div class="t"><span>ISRO · INSAT-3DR imager (MOSDAC)</span><span id="inS" class="k"></span></div><div id="insat" class="grid grid-cols-3 gap-1"></div></section>
 <section class="card lg:col-span-6"><div class="t"><span>ISRO news</span></div><ul id="isroN" class="space-y-1 text-xs"></ul></section>

 <section class="card lg:col-span-8"><div class="t"><span>Live video</span>
  <span class="flex gap-1 flex-wrap justify-end"><select id="vSel">
   <option data-ch="UCLA_DiR1FfKNvjuUpBHmylQ" data-open="https://www.youtube.com/@NASA/live">NASA</option>
   <option data-ch="UCtI0Hodo5o5dUb67FeUjDeA" data-open="https://www.youtube.com/@SpaceX/live">SpaceX</option>
   <option data-ch="UCIBaDdAbGlFDeS33shmlD0A" data-open="https://www.youtube.com/@esa/live">ESA</option>
   <option data-open="https://www.youtube.com/results?search_query=ISRO+live&sp=EgJAAQ%3D%3D">ISRO (find live)</option></select>
   <button id="vGo">▶ Embed</button><a id="vOpen" target="_blank" rel="noopener" class="pill">↗ Open on YouTube</a></span></div>
  <div class="flex gap-1 mb-1"><input id="vUrl" class="flex-1" placeholder="…or paste any YouTube link / video ID"><button id="vPaste">Play</button></div>
  <div class="aspect-video bg-black rounded"><iframe id="vid" class="w-full h-full rounded" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen allow="autoplay; encrypted-media; picture-in-picture"></iframe></div>
  <div class="k mt-1">If the player says “unavailable” or Error 153, that stream isn’t embeddable (or isn’t live now): use ↗ Open on YouTube.</div></section>
 <section class="card lg:col-span-4"><div class="t"><span>Mission log</span></div><div id="log" class="text-xs h-64 overflow-y-auto space-y-0.5 text-slate-400"></div></section>
</main>

<script>
const $=id=>document.getElementById(id), pad=n=>String(n).padStart(2,'0'), F=(x,d=1)=>x==null?'–':Number(x).toFixed(d);
const hms=t=>{const d=new Date(t*1000);return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds())};
let ws,S={names:[],sel:0,pos:[],track:[],selv:null,sun:[0,0],foot:0,obs:{lat:0,lon:0},freq:145.8e6,passes:[],pi:0,spec:null};
const send=o=>ws&&ws.readyState===1&&ws.send(JSON.stringify(o));
const H={};
function log(m){const d=document.createElement('div');d.textContent=m;$('log').prepend(d);while($('log').children.length>80)$('log').lastChild.remove()}

/* ---------- websocket ---------- */
function connect(){
 ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
 ws.onopen=()=>{$('pWS').textContent='LIVE';$('pWS').className='pill text-emerald-400'};
 ws.onclose=()=>{$('pWS').textContent='WS OFF';$('pWS').className='pill text-red-400';setTimeout(connect,2000)};
 ws.onmessage=e=>{const m=JSON.parse(e.data);H[m.type]&&H[m.type](m)};
}
H.init=m=>{S.names=m.names;S.sel=m.sel;S.obs=m.obs;S.freq=m.freq;
 $('grp').innerHTML=m.groups.map(g=>`<option ${g===m.group?'selected':''}>${g}</option>`).join('');
 $('satList').innerHTML=m.names.map(n=>`<option value="${n.replace(/"/g,'')}">`).join('');
 $('oLat').value=m.obs.lat;$('oLon').value=m.obs.lon;$('oAlt').value=m.obs.alt;$('fq').value=(m.freq/1e6).toFixed(3);
 $('loc').textContent=`OBS ${F(m.obs.lat,2)}, ${F(m.obs.lon,2)}`;
 $('pSDR').textContent='SDR '+m.sdr.mode;$('pSDR').className='pill '+(m.sdr.mode==='SIM'?'text-amber-300':'text-emerald-400');
 $('pTLE').textContent='TLE '+m.src;$('pTLE').className='pill '+(/OFFLINE|stale/i.test(m.src)?'text-amber-300':'text-emerald-400');
 $('sdrMode').textContent=m.sdr.mode;$('satIn').value=m.names[m.sel]||''};
H.log=m=>log(m.msg);
H.tlm=m=>{const d=document.createElement('div');d.textContent=m.line;$('tlm').prepend(d)};
H.passes=m=>{S.passes=m.passes||[];S.pi=0;$('pn').textContent=m.name||'';renderPasses();drawDoppler();drawPolar()};
H.tick=m=>{S.pos=m.pos;S.sun=m.sun;S.selv=m.sel;S.foot=m.foot;if(m.track)S.track=m.track;
 $('nsat').textContent=m.n+' objects';$('moon').textContent=`☾ ${Math.round(m.moon[1]*100)}% · day ${m.moon[0]}`;
 const s=m.sel;if(s){$('satName').textContent=s.name;
  [['rLat',s.lat,2],['rLon',s.lon,2],['rAlt',s.alt,0],['rSpd',s.spd,2],['rAz',s.az,1],['rEl',s.el,1],['rRng',s.rng,0],['rDop',s.dop,0]].forEach(([i,v,d])=>$(i).textContent=F(v,d));
  $('rEl').className='v '+(s.el>0?'text-emerald-400':'');}
 drawMap();drawPolar();countdown(m.t)};
H.spec=m=>{S.spec=m;const st=m.stats;
 $('tN').textContent=F(st.noise);$('tP').textContent=F(st.peak_f,4);$('tS').textContent=F(st.snr);$('tO').textContent=F(st.obw);$('tC').textContent=st.carriers.join(' · ')||'none';
 $('fr').textContent=`${F(m.f0,3)} – ${F(m.f1,3)} MHz`;
 const n=m.bins.length;if(specC.data.labels.length!==n||specC._f0!==m.f0||specC._f1!==m.f1){specC._f0=m.f0;specC._f1=m.f1;
  specC.data.labels=m.bins.map((_,i)=>(m.f0+(m.f1-m.f0)*i/(n-1)).toFixed(3))}
 specC.data.datasets[0].data=m.bins;specC.options.scales.y.min=st.noise-8;specC.options.scales.y.max=st.noise+60;specC.update('none');
 const w=$('wf'),c=w.getContext('2d');c.drawImage(w,0,-1);const im=c.createImageData(n,1);
 m.bins.forEach((d,i)=>{const x=Math.max(0,Math.min(1,(d-st.noise+3)/45)),q=v=>255*Math.max(0,Math.min(1,1.5-Math.abs(v)));
  im.data.set([q(4*x-3),q(4*x-2),q(4*x-1),255],i*4)});c.putImageData(im,0,w.height-1)};

/* ---------- map ---------- */
const map=$('map'),mx=map.getContext('2d');map.width=1440;map.height=720;
const X=l=>(l+180)/360*1440,Y=l=>(90-l)/180*720;let landC=null;
fetch('https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json').then(r=>r.json()).then(t=>{
 const sc=t.transform.scale,tr=t.transform.translate;
 const A=t.arcs.map(a=>{let x=0,y=0;return a.map(p=>{x+=p[0];y+=p[1];return[x*sc[0]+tr[0],y*sc[1]+tr[1]]})});
 const ring=r=>{let p=[];r.forEach(i=>{const a=i<0?A[~i].slice().reverse():A[i];p.push(...(p.length?a.slice(1):a))});return p};
 landC=document.createElement('canvas');landC.width=1440;landC.height=720;const c=landC.getContext('2d');
 c.fillStyle='#0f2236';c.strokeStyle='#27557c';c.lineWidth=1;
 const o=t.objects.land;(o.geometries||[o]).forEach(g=>{(g.type==='Polygon'?[g.arcs]:g.arcs).forEach(poly=>{c.beginPath();
  poly.forEach(r=>{ring(r).forEach((p,i)=>i?c.lineTo(X(p[0]),Y(p[1])):c.moveTo(X(p[0]),Y(p[1])));c.closePath()});c.fill('evenodd');c.stroke()})});
}).catch(()=>{});
function poly(pts,c){c.beginPath();let pl=null;pts.forEach(p=>{if(pl===null||Math.abs(p[1]-pl)>180)c.moveTo(X(p[1]),Y(p[0]));else c.lineTo(X(p[1]),Y(p[0]));pl=p[1]});c.stroke()}
function drawMap(){const c=mx;c.fillStyle='#06101c';c.fillRect(0,0,1440,720);if(landC)c.drawImage(landC,0,0);
 c.strokeStyle='#14263b';c.lineWidth=1;c.beginPath();for(let l=-180;l<=180;l+=30){c.moveTo(X(l),0);c.lineTo(X(l),720)}for(let l=-90;l<=90;l+=30){c.moveTo(0,Y(l));c.lineTo(1440,Y(l))}c.stroke();
 const [dec,sl]=S.sun,d=Math.abs(dec)<.05?.05:dec,rad=Math.PI/180;c.fillStyle='rgba(0,0,10,.5)';c.beginPath();
 for(let l=-180;l<=180;l+=2){const la=Math.atan(-Math.cos((l-sl)*rad)/Math.tan(d*rad))/rad;l===-180?c.moveTo(X(l),Y(la)):c.lineTo(X(l),Y(la))}
 c.lineTo(1440,d>0?720:0);c.lineTo(0,d>0?720:0);c.closePath();c.fill();
 c.fillStyle='#fde047';c.beginPath();c.arc(X(sl),Y(dec),7,0,7);c.fill();
 S.pos.forEach(p=>{if(p[0]>900)return;c.fillStyle=p[2]?'#34d399':'#38bdf8';c.fillRect(X(p[1])-1.5,Y(p[0])-1.5,p[2]?5:3,p[2]?5:3)});
 c.lineWidth=2;c.setLineDash([6,5]);c.strokeStyle='#fbbf24';poly(S.track,c);c.setLineDash([]);
 const s=S.selv;if(s){const la=s.lat*rad,lo=s.lon*rad,r=S.foot*rad,pts=[];
  for(let b=0;b<=360;b+=6){const t=b*rad,l2=Math.asin(Math.sin(la)*Math.cos(r)+Math.cos(la)*Math.sin(r)*Math.cos(t));
   let o2=lo+Math.atan2(Math.sin(t)*Math.sin(r)*Math.cos(la),Math.cos(r)-Math.sin(la)*Math.sin(l2));o2=((o2/rad+540)%360)-180;pts.push([l2/rad,o2])}
  c.strokeStyle='rgba(251,191,36,.6)';c.lineWidth=1.5;poly(pts,c);
  c.fillStyle='#fbbf24';c.beginPath();c.arc(X(s.lon),Y(s.lat),7,0,7);c.fill();c.font='22px monospace';c.fillText(s.name,X(s.lon)+12,Y(s.lat)-10)}
 c.strokeStyle='#22d3ee';c.lineWidth=2;const ox=X(S.obs.lon),oy=Y(S.obs.lat);c.beginPath();c.moveTo(ox-9,oy);c.lineTo(ox+9,oy);c.moveTo(ox,oy-9);c.lineTo(ox,oy+9);c.stroke()}

/* ---------- polar sky plot ---------- */
function drawPolar(){const c=$('polar').getContext('2d'),R=135,cx=150,cy=150,rad=Math.PI/180;c.clearRect(0,0,300,300);
 c.strokeStyle='#1b2a42';c.fillStyle='#64748b';c.font='11px monospace';[0,30,60].forEach(e=>{c.beginPath();c.arc(cx,cy,R*(90-e)/90,0,7);c.stroke()});
 c.beginPath();c.moveTo(cx-R,cy);c.lineTo(cx+R,cy);c.moveTo(cx,cy-R);c.lineTo(cx,cy+R);c.stroke();
 [['N',cx-3,cy-R-3],['E',cx+R+3,cy+4],['S',cx-3,cy+R+11],['W',cx-R-12,cy+4]].forEach(a=>c.fillText(...a));
 const P=(az,el)=>[cx+R*(90-el)/90*Math.sin(az*rad),cy-R*(90-el)/90*Math.cos(az*rad)],p=S.passes[S.pi];
 if(p){c.strokeStyle='#2dd4bf';c.lineWidth=2;c.beginPath();p.prof.forEach((q,i)=>{const[x,y]=P(q[1],q[2]);i?c.lineTo(x,y):c.moveTo(x,y)});c.stroke()}
 const s=S.selv;if(s&&s.el>0){const[x,y]=P(s.az,s.el);c.fillStyle='#fbbf24';c.beginPath();c.arc(x,y,6,0,7);c.fill()}}

/* ---------- passes / doppler ---------- */
function renderPasses(){const now=Date.now()/1000,up=S.passes.filter(p=>p.los>now);S.passes=up;if(S.pi>=up.length)S.pi=0;
 $('pt').innerHTML=up.map((p,i)=>`<tr class="cursor-pointer ${i===S.pi?'sel':''}" data-i="${i}"><td>${hms(p.aos)}</td><td>${Math.round((p.los-p.aos)/60)}m</td><td>${p.max_el}</td><td>${p.az_aos}→${p.az_los}</td></tr>`).join('')||'<tr><td colspan=4 class="k">No passes ≥5° in 48h</td></tr>';
 document.querySelectorAll('#pt tr[data-i]').forEach(r=>r.onclick=()=>{S.pi=+r.dataset.i;renderPasses();drawDoppler();drawPolar()})}
const dopC=new Chart($('dopC'),{type:'line',data:{labels:[],datasets:[{data:[],borderColor:'#2dd4bf',pointRadius:0,tension:.3}]},options:{animation:false,maintainAspectRatio:false,plugins:{legend:{display:false}},
 scales:{x:{ticks:{color:'#64748b',maxTicksLimit:6},title:{display:true,text:'min from AOS',color:'#64748b'}},y:{ticks:{color:'#64748b'},grid:{color:'#14263b'}}}}});
function drawDoppler(){const p=S.passes[S.pi];if(!p){dopC.data.labels=[];dopC.data.datasets[0].data=[];dopC.update();return}
 dopC.data.labels=p.prof.map(q=>(q[0]/60).toFixed(1));dopC.data.datasets[0].data=p.prof.map(q=>+(-S.freq*q[3]/299792.458/1e3).toFixed(2));dopC.update()}
let beeped=0;
function countdown(t){const p=S.passes.find(p=>p.los>t);if(!p){$('cd').textContent='—';return}
 const f=s=>pad(Math.floor(s/60))+':'+pad(Math.floor(s%60));
 if(p.aos<=t){$('cd').textContent='IN PASS · LOS '+f(p.los-t);$('cd').className='mt-2 text-center text-lg text-emerald-300'}
 else{$('cd').textContent='AOS in '+(p.aos-t>3600?Math.floor((p.aos-t)/3600)+'h ':'')+f((p.aos-t)%3600);$('cd').className='mt-2 text-center text-lg text-sky-300';
  if(p.aos-t<60&&beeped!==p.aos&&$('beep').checked){beeped=p.aos;beep()}}
 if(Math.floor(t)%30===0)renderPasses()}
function beep(){try{const a=new AudioContext(),o=a.createOscillator();o.connect(a.destination);o.frequency.value=880;o.start();o.stop(a.currentTime+.4)}catch(e){}}

/* ---------- charts ---------- */
const specC=new Chart($('spec'),{type:'line',data:{labels:[],datasets:[{data:[],borderColor:'#38bdf8',borderWidth:1.2,pointRadius:0,fill:true,backgroundColor:'rgba(56,189,248,.12)'}]},
 options:{animation:false,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#64748b',maxTicksLimit:8},grid:{color:'#14263b'}},y:{ticks:{color:'#64748b'},grid:{color:'#14263b'}}}}});
const kpC=new Chart($('kpC'),{type:'bar',data:{labels:[],datasets:[{data:[],backgroundColor:[]}]},options:{maintainAspectRatio:false,plugins:{legend:{display:false}},
 scales:{x:{display:false},y:{min:0,max:9,ticks:{color:'#64748b'},grid:{color:'#14263b'}}}}});

/* ---------- controls ---------- */
$('satIn').onchange=e=>{const i=S.names.indexOf(e.target.value);if(i>=0)send({cmd:'select',idx:i})};
$('grp').onchange=e=>send({cmd:'group',group:e.target.value});
$('oGo').onclick=()=>send({cmd:'observer',lat:$('oLat').value,lon:$('oLon').value,alt:$('oAlt').value});
$('gps').onclick=()=>navigator.geolocation&&navigator.geolocation.getCurrentPosition(p=>{$('oLat').value=p.coords.latitude.toFixed(4);$('oLon').value=p.coords.longitude.toFixed(4);$('oAlt').value=Math.round(p.coords.altitude||0);$('oGo').click()});
$('fq').onchange=()=>{S.freq=parseFloat($('fq').value)*1e6;send({cmd:'freq',hz:S.freq});drawDoppler()};
$('sGo').onclick=()=>send({cmd:'sdr',center:parseFloat($('sC').value)*1e6,rate:parseFloat($('sR').value)*1e6,gain:$('sG').value===''?'auto':parseFloat($('sG').value),auto:$('auto').checked});
$('auto').onchange=$('sGo').onclick;
const vOpt=()=>$('vSel').selectedOptions[0],vSync=()=>{$('vOpen').href=vOpt().dataset.open};vSync();$('vSel').onchange=vSync;
$('vGo').onclick=()=>{const ch=vOpt().dataset.ch;if(!ch){window.open(vOpt().dataset.open,'_blank');return}$('vid').src='https://www.youtube.com/embed/live_stream?channel='+ch+'&autoplay=1&mute=1'};
$('vPaste').onclick=()=>{const v=$('vUrl').value.trim(),m=v.match(/(?:v=|youtu\.be\/|\/live\/|\/embed\/)([\w-]{11})/)||v.match(/^([\w-]{11})$/);m?$('vid').src='https://www.youtube.com/embed/'+m[1]+'?autoplay=1&mute=1':alert('No YouTube video ID found in that text')};
const loadIm=()=>{$('imE').classList.add('hidden');$('im').classList.remove('hidden');$('im').src=$('imSel').value+'?t='+Date.now()};
$('imSel').onchange=loadIm;$('im').onerror=()=>{$('im').classList.add('hidden');$('imE').classList.remove('hidden')};

/* ---------- feeds ---------- */
const jget=async u=>(await fetch(u)).json(), tag=d=>d.mock?' <span class="text-amber-400">[OFFLINE DEMO]</span>':d.stale?' <span class="text-amber-400">[stale]</span>':'';
async function feeds(){
 try{const d=await jget('/data/wx');$('wKp').textContent=F(d.kp,1);$('wKp').style.color=d.kp>=5?'#f87171':d.kp>=4?'#fbbf24':'#34d399';$('wS').textContent=F(d.sfi,0);$('wA').textContent=F(d.a,0);$('wX').textContent=d.xray||'–';
  $('wxs').innerHTML=tag(d);kpC.data.labels=d.forecast.map((_,i)=>i);kpC.data.datasets[0].data=d.forecast;kpC.data.datasets[0].backgroundColor=d.forecast.map(v=>v>=5?'#f87171':v>=4?'#fbbf24':'#34d399');kpC.update()}catch(e){}
 try{const d=await jget('/data/launches');$('launch').innerHTML=d.items.map(i=>`<li><span class="text-amber-300">${i.date}</span> ${i.name}</li>`).join('')+tag(d)}catch(e){}
 try{const d=await jget('/data/news');$('news').innerHTML=d.items.map(i=>`<li><a href="${i.link}" target="_blank" rel="noopener">${i.title}</a> <span class="k">${i.date}</span></li>`).join('')+tag(d)}catch(e){}
 try{const d=await jget('/data/apod');$('apT').innerHTML=d.title+tag(d);$('apX').textContent=d.text;if(d.img){$('apImg').src=d.img;$('apImg').classList.remove('hidden')}}catch(e){}
 try{const d=await jget('/data/mars');$('mars').innerHTML=d.items.map(i=>`<a href="${i.url}" target="_blank" rel="noopener"><img src="${i.url}" class="rounded w-full" loading="lazy"><div class="k">${i.label}</div></a>`).join('')||'<div class="k col-span-2">Mars feed unreachable right now.</div>'}catch(e){}
 try{const d=await jget('/data/insat');$('inS').innerHTML=tag(d);$('insat').innerHTML=d.items.map(i=>`<a href="${i.url}" target="_blank" rel="noopener"><img src="${i.url}" class="rounded w-full" loading="lazy" onerror="this.style.display='none'"><div class="k">${i.label}</div></a>`).join('')||'<div class="k col-span-3">MOSDAC feed unreachable right now.</div>'}catch(e){}
 try{const d=await jget('/data/isro');$('isroN').innerHTML=d.items.map(i=>`<li><a href="${i.link}" target="_blank" rel="noopener">${i.title}</a> <span class="k">${i.date}</span></li>`).join('')+tag(d)}catch(e){}
}
setInterval(()=>{const n=new Date();$('utc').textContent=n.toISOString().slice(0,19).replace('T',' ')+' UTC'},500);
connect();loadIm();feeds();setInterval(feeds,300000);setInterval(loadIm,600000);
</script></body></html>
"""

if __name__ == "__main__":
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8000))
    print(f"\n  ◉ GROUND STATION  ->  http://{'localhost' if host == '127.0.0.1' else host}:{port}\n")
    try:
        asyncio.get_running_loop()
        in_loop = True            # Jupyter / Colab: an event loop is already running
    except RuntimeError:
        in_loop = False
    if in_loop:
        threading.Thread(target=lambda: uvicorn.run(app, host=host, port=port, log_level="warning"), daemon=True).start()
        print("  Running in a notebook: server started in a background thread.")
    else:
        uvicorn.run(app, host=host, port=port, log_level="warning")
