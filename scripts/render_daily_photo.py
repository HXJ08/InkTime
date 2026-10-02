#!/usr/bin/env python3
# -*- coding: utf-8 -*-


"""
Daily album rendering script:
- Pick a “today in history” photo from photos.db / photo_scores
- Render to 480x800 following the InkTime simulator layout
- Use Liberation Sans to draw a clean info panel:
    line 1: ISO · focal · aperture · shutter
    line 2: date (left) + location (right)
  (battery icon is drawn by the ESP32 firmware on-device; the server
  preview renders a copy of the icon so previews match the frame)
- Convert to 4-color e-ink (black/white/red/yellow) and save as BIN (1 byte per pixel, row-major)
- Also export a latest.h header array for ESP32 to #include directly
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import json
import datetime as dt
import os
from typing import List, Dict, Any, Tuple, Optional
import pillow_heif
pillow_heif.register_heif_opener()
from PIL import Image, ImageDraw, ImageFont, ImageOps
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg


TODAY = dt.date.today()

# === Path config (from config.py) ===
ROOT_DIR = Path(__file__).resolve().parent.parent  # repo root

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "output/inktime") or "output/inktime")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_PATH = Path(str(getattr(cfg, "FONT_PATH", "") or "")).expanduser()
if str(FONT_PATH) and not FONT_PATH.is_absolute():
    FONT_PATH = (ROOT_DIR / FONT_PATH).resolve()

FONT_BOLD_PATH = Path(str(getattr(cfg, "FONT_BOLD_PATH", "") or "")).expanduser()
if str(FONT_BOLD_PATH) and not FONT_BOLD_PATH.is_absolute():
    FONT_BOLD_PATH = (ROOT_DIR / FONT_BOLD_PATH).resolve()

MEMORY_THRESHOLD = float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)
# Avoid picking photos that were used in the last N days. Fall back to
# recently-used if no fresh candidates are available.
USED_COOLDOWN_DAYS = int(getattr(cfg, "USED_COOLDOWN_DAYS", 30) or 30)
DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)

# Optional album filter. The `album` column is written by the album backfill
# (scripts/backfill_post.py), not by the analyzer. Empty string renders from
# everything scored; set it (e.g. "Post") to restrict to one tagged album.
ALBUM_FILTER = str(getattr(cfg, "ALBUM_FILTER", "") or "")

# E-ink dimensions
CANVAS_WIDTH = 480
CANVAS_HEIGHT = 800

# Bottom text area height (2 lines: specs + date/location)
TEXT_AREA_HEIGHT = 100

# Battery icon position (keep in sync with esp32/ink-display-7C-photo/battery_icons.h)
# ESP32 overlays the battery icon on the framebuffer; the renderer draws the
# same icon in its preview so previews match the frame.
# Position: aligned with the specs row (text_area_top + 16 = 676), 2px up
# for optical centering against the 20px-tall text.
BATTERY_ICON_W = 24
BATTERY_ICON_H = 24
BATTERY_ICON_X = CANVAS_WIDTH - 16 - BATTERY_ICON_W         # 440 (16px from right)
BATTERY_ICON_Y = CANVAS_HEIGHT - 88                          # 712 (specs row, right-aligned with specs text)


# ========== DB and EXIF handling ==========

def extract_date_from_exif(exif_json: Optional[str]) -> str:
    """
    Extract the capture date from an EXIF JSON, return YYYY-MM-DD, or empty string on failure.
    Logic mirrors review_web.py.
    """
    if not exif_json:
        return ""
    try:
        data = json.loads(exif_json)
    except Exception:
        return ""
    dt_str = data.get("datetime") or data.get("dateTimeOriginal") or data.get("DateTime")
    if not dt_str:
        return ""
    try:
        date_part = str(dt_str).split("T")[0].split()[0]
        parts = date_part.replace(":", "-").split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""
    return ""


def load_sim_rows() -> List[Dict[str, Any]]:
    """
    Core fields used by InkTime:
    - path: photo path
    - exif_json: for parsing date / GPS
    - memory_score: memory score
    - exif_gps_lat / exif_gps_lon / exif_city: location info (purely local, no network)
    - exif_make / exif_model / exif_lens: camera + lens (Apple-style panel)
    - width / height / exif_file_size: resolution + size
    - exif_iso / exif_f_number / exif_exposure_time / exif_focal_35mm: specs
      (exif_focal_35mm = 35mm-equivalent focal length, matches Apple Photos)
    """
    if not DB_PATH.exists():
        raise SystemExit(f"Database file not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    sql = """
        SELECT photo_scores.path,
               photo_scores.exif_json,
               photo_scores.memory_score,
               photo_scores.exif_gps_lat,
               photo_scores.exif_gps_lon,
               photo_scores.exif_city,
               photo_scores.immich_asset_id,
               photo_scores.used_at,
               photo_scores.exif_iso,
               photo_scores.exif_f_number,
               photo_scores.exif_exposure_time,
               photo_scores.exif_focal_35mm,
               photo_scores.exif_make,
               photo_scores.exif_model,
               photo_scores.exif_lens,
               photo_scores.width,
               photo_scores.height,
               photo_scores.exif_file_size
        FROM photo_scores
        LEFT JOIN photo_blacklist ON photo_scores.immich_asset_id = photo_blacklist.immich_asset_id
        WHERE photo_scores.exif_json IS NOT NULL
          AND photo_blacklist.immich_asset_id IS NULL
    """
    params: list = []
    if ALBUM_FILTER:
        sql += " AND photo_scores.album = ?"
        params.append(ALBUM_FILTER)

    rows = c.execute(sql, params).fetchall()
    conn.close()

    items: List[Dict[str, Any]] = []
    for (path, exif_json, memory_score, gps_lat, gps_lon, exif_city,
         immich_asset_id, used_at, exif_iso, exif_f_number, exif_exposure_time,
         exif_focal_35mm, exif_make, exif_model, exif_lens, width, height,
         exif_file_size) in rows:
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        # Belt-and-suspenders: filter Screenshot etc. again
        if "screenshot" in str(path).lower():
            continue

        try:
            y, m, d = map(int, date_str.split("-"))
        except Exception:
            continue
        md = f"{m:02d}-{d:02d}"

        item = {
            "path": str(path),
            "date": date_str,  # YYYY-MM-DD
            "md": md,          # MM-DD
            "memory": float(memory_score) if memory_score is not None else -1.0,
            "lat": gps_lat,
            "lon": gps_lon,
            "city": exif_city or "",
            "used_at": used_at,  # last time this photo was rendered, or None
            "immich_asset_id": immich_asset_id or "",
            "iso": exif_iso,
            "f_number": exif_f_number,
            "exposure_time": exif_exposure_time,
            "focal_length": exif_focal_35mm,
            "make": (exif_make or "").strip(),
            "model": (exif_model or "").strip(),
            "lens": (exif_lens or "").strip(),
            "width": width,
            "height": height,
            "file_size": exif_file_size,
        }

        # Fallback: fill missing fields from exif_json (Immich exifInfo)
        try:
            ei = json.loads(exif_json) if exif_json else {}
        except Exception:
            ei = {}
        if not item["iso"] and ei.get("iso") is not None:
            item["iso"] = ei["iso"]
        if not item["f_number"] and ei.get("fNumber") is not None:
            try: item["f_number"] = float(ei["fNumber"])
            except: pass
        if not item["exposure_time"] and ei.get("exposureTime") is not None:
            et = ei["exposureTime"]
            try:
                if isinstance(et, str) and "/" in et:
                    num, den = et.split("/")
                    item["exposure_time"] = float(num) / float(den)
                else:
                    item["exposure_time"] = float(et)
            except: pass
        if not item["focal_length"] and ei.get("focalLength") is not None:
            try: item["focal_length"] = float(ei["focalLength"])
            except: pass
        if not item["city"] and ei.get("city"):
            item["city"] = ei["city"]
        if not item["lat"] and ei.get("latitude") is not None:
            try: item["lat"] = float(ei["latitude"])
            except: pass
        if not item["lon"] and ei.get("longitude") is not None:
            try: item["lon"] = float(ei["longitude"])
            except: pass
        if not item["make"] and ei.get("make"):
            item["make"] = ei["make"]
        if not item["model"] and ei.get("model"):
            item["model"] = ei["model"]
        if not item["lens"] and ei.get("lensModel"):
            item["lens"] = ei["lensModel"]
        if not item["width"] and ei.get("exifImageWidth"):
            item["width"] = ei["exifImageWidth"]
        if not item["height"] and ei.get("exifImageHeight"):
            item["height"] = ei["exifImageHeight"]
        if not item["file_size"] and ei.get("fileSizeInByte"):
            item["file_size"] = ei["fileSizeInByte"]

        items.append(item)

    return items


# ========== “Today in history” selection ==========

def md_to_day_of_year(md: str) -> Optional[int]:
    """Convert 'MM-DD' to the day-of-year (1..365) in a non-leap year."""
    try:
        m, d = map(int, md.split("-"))
        days_before = [0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        if m < 1 or m > 12:
            return None
        return days_before[m] + d
    except Exception:
        return None


def day_of_year_to_md(day: int) -> str:
    # Pick a non-leap year (e.g. 2001/2005); only day-of-year matters.
    base = dt.date(2001, 1, 1) + dt.timedelta(days=day - 1)
    return f"{base.month:02d}-{base.day:02d}"


def choose_photos_for_today(items: List[Dict[str, Any]], today: dt.date, count: int = 5) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Selection rule (multi-photo version, by month-day):
    - Pick high-memory photos (any day), prefer fresh + closest to today
    - Within each phase, prefer fresh photos (not used in USED_COOLDOWN_DAYS days)
    - Only fall back to global top-memory if no candidates above threshold at all
    """
    if not items:
        raise RuntimeError("No usable photos")

    # Build per-month-day index, sorted by date proximity
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"Cannot parse today's month-day: {target_md}")

    import random
    cutoff = (today - dt.timedelta(days=USED_COOLDOWN_DAYS)).isoformat()

    def is_fresh(p):
        used_at = p.get("used_at")
        if not used_at:
            return True
        return used_at[:10] < cutoff

    # Build day-distance-ordered list of mds, starting from target_md
    # and walking back through the year
    def md_distance(md):
        try:
            doy = md_to_day_of_year(md)
            if doy is None:
                return 999
            # Walk back from target; wrap around year end
            d = (target_doy - doy) % 365
            return d
        except Exception:
            return 999

    all_mds_sorted = sorted(by_md.keys(), key=md_distance)

    def pick_photos(filter_fn, limit):
        """Pick fresh photos first across all matching days; use stale only as fallback."""
        candidates = []
        seen_keys = set()
        for md in all_mds_sorted:
            for p in by_md[md]:
                if p.get("memory", -1.0) <= MEMORY_THRESHOLD:
                    continue
                if not filter_fn(p):
                    continue
                key = p.get("path") or p.get("immich_asset_id")
                if key and key in seen_keys:
                    continue
                if key:
                    seen_keys.add(key)
                candidates.append(p)

        candidates.sort(key=lambda p: (
            0 if is_fresh(p) else 1,
            md_distance(p["md"]),
            -p.get("memory", -1.0),
        ))
        return candidates[:limit]

    chosen_list: List[Dict[str, Any]] = []
    primary_md = None
    used_stale = False

    picked = pick_photos(lambda p: True, count)
    chosen_list.extend(picked)
    if chosen_list:
        primary_md = chosen_list[0]["md"]
        used_stale = any(not is_fresh(p) for p in chosen_list)

    if chosen_list:
        info = {
            "target_md": target_md,
            "used_md": primary_md or "",
            "day_offset": 0,
            "candidate_count": len(chosen_list),
            "total_count_md": len(chosen_list),
            "threshold": MEMORY_THRESHOLD,
            "fallback_global_max": False,
            "used_stale_fallback": used_stale,
            "cooldown_days": USED_COOLDOWN_DAYS,
        }
        return chosen_list[:count], info

    # Fallback: global top-memory photos
    sorted_all = sorted(items, key=lambda x: x.get("memory", -1.0), reverse=True)
    chosen_list = sorted_all[:count]
    used_stale = any(not is_fresh(p) for p in chosen_list)
    info = {
        "target_md": target_md,
        "used_md": chosen_list[0]["md"] if chosen_list else "",
        "day_offset": None,
        "candidate_count": len(chosen_list),
        "total_count_md": len(items),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
        "used_stale_fallback": used_stale,
        "cooldown_days": USED_COOLDOWN_DAYS,
    }
    return chosen_list, info


# ========== Drawing + dithering ==========

# 4-color e-ink palette (RGB)
PALETTE = [
    (0, 0, 0),         # 0 = black
    (255, 255, 255),   # 1 = white
    (255, 0, 0),       # 2 = red
    (255, 255, 0),     # 3 = yellow
]


def nearest_palette_color(r, g, b):
    best = 0
    best_d = 1e18
    for i, (pr, pg, pb) in enumerate(PALETTE):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d = d
            best = i
    return best, *PALETTE[best]


def wrap_text_chinese(draw: ImageDraw.ImageDraw,
                      text: str,
                      font: ImageFont.FreeTypeFont,
                      max_width: int,
                      max_lines: int) -> List[str]:
    """
    Simple CJK-aware line wrapping (by character width).
    """
    if not text:
        return []
    lines: List[str] = []
    line = ""
    for ch in text:
        test = line + ch
        w = draw.textlength(test, font=font)
        if w <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = ch
            if len(lines) >= max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)
    return lines


def format_date_display(date_str: str) -> str:
    """
    "YYYY-MM-DD" -> "YYYY.M.D"
    """
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) < 3:
        return date_str
    y = parts[0]
    try:
        m = str(int(parts[1]))
        d = str(int(parts[2]))
    except Exception:
        return date_str
    return f"{y}.{m}.{d}"


def format_shutter(exposure_time) -> str:
    """Apple Photos style shutter: '1/24s', '1.3s', '0.5s'."""
    try:
        et_f = float(exposure_time)
    except Exception:
        return ""
    if et_f <= 0:
        return ""
    if et_f >= 1:
        return f"{et_f:g}s"
    if et_f >= 0.1:
        # 0.5s, 0.3s, 1/8s-ish: Apple shows decimal for >= 0.1s
        return f"{et_f:g}s"
    # 1/x form
    x = 1.0 / et_f
    # round to nice values: if within 1% of an integer, use it
    xi = round(x)
    if abs(x - xi) / x < 0.05:
        return f"1/{xi}s"
    return f"1/{x:.1f}s"


def format_resolution(item: Dict[str, Any]) -> str:
    """'5712 x 4284' or ''"""
    w = item.get("width")
    h = item.get("height")
    if w and h:
        try:
            return f"{int(w)} x {int(h)}"
        except Exception:
            return ""
    return ""


def format_file_size(item: Dict[str, Any]) -> str:
    """'2.4 MB' or '850 KB'"""
    fs = item.get("file_size")
    if not fs:
        return ""
    try:
        b = float(fs)
    except Exception:
        return ""
    if b >= 1024 * 1024:
        return f"{b / (1024*1024):.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{int(b)} B"


def format_device_name(item: Dict[str, Any]) -> str:
    """Apple Photos style: 'Apple iPhone 15 Pro' -> 'iPhone 15 Pro' (drop redundant Apple)."""
    make = item.get("make", "")
    model = item.get("model", "")
    if not model:
        return make
    if not make:
        return model
    # Apple shows just "iPhone 15 Pro" for its own devices
    if make.lower() == "apple" and model.lower().startswith("iphone"):
        return model
    if model.lower().startswith(make.lower()):
        return model
    return f"{make} {model}"


def format_specs_row(item: Dict[str, Any]) -> str:
    """
    Apple Photos style specs row: 'ISO 1250 · 77mm · f/2.8 · 1/24s'
    Only includes parts that exist.
    """
    parts: List[str] = []
    iso = item.get("iso")
    if iso is not None:
        try:
            parts.append(f"ISO {int(iso)}")
        except Exception:
            pass
    fl = item.get("focal_length")
    if fl is not None:
        try:
            parts.append(f"{format_focal(fl, item):.0f}mm")
        except Exception:
            pass
    fn = item.get("f_number")
    if fn is not None:
        try:
            parts.append(f"f/{float(fn):g}")
        except Exception:
            pass
    et = item.get("exposure_time")
    if et is not None:
        s = format_shutter(et)
        if s:
            parts.append(s)
    return " · ".join(parts)


# Canonical optical 35mm-equivalent focal lengths (mm). Apple's 0xA405 tag
# drifts off the optical value by ±1-2mm due to digital zoom / sensor crop
# (e.g. 3x telephoto reports 78 instead of the true 77mm). Snap to the
# nearest optical value to match what Apple Photos shows.
OPTICAL_FOCAL_LENGTHS = [
    13.0, 14.0, 24.0, 26.0, 28.0, 35.0, 48.0, 50.0, 52.0, 65.0, 77.0, 120.0,
]


def format_focal(focal_mm: Any, item: Dict[str, Any]) -> float:
    """
    Return the focal length to display. For Apple devices, snap the
    35mm-equivalent to the nearest canonical optical focal length (only when
    close — within ~6%), so 78 -> 77 and digital-zoom garbage like 156 stays
    untouched as a genuine zoom value. Non-Apple cameras are left as-is.
    """
    try:
        mm = float(focal_mm)
    except Exception:
        return 0.0
    make = (item.get("make") or "").strip()
    if make.lower() != "apple":
        return mm
    best = min(OPTICAL_FOCAL_LENGTHS, key=lambda o: abs(o - mm))
    tolerance = max(2.0, best * 0.06)
    if abs(best - mm) <= tolerance:
        return best
    return mm


def format_lens_display(item: Dict[str, Any]) -> str:
    """
    Lens label, Apple-Photos style:
    - iPhone/Apple: map 35mm-equiv focal to a friendly name, e.g. 'Telephoto 77mm f/2.8'
    - Other cameras (Sony/Canon/...): use the EXIF lens name if present, else derive.
    """
    make = (item.get("make") or "").strip()
    fl = item.get("focal_length")
    fn = item.get("f_number")

    def fl_str():
        if fl is None:
            return ""
        try:
            return f"{float(fl):.0f}mm"
        except Exception:
            return ""

    def f_str():
        if fn is None:
            return ""
        try:
            return f"f/{float(fn):g}"
        except Exception:
            return ""

    # Apple devices: derive a friendly label from the 35mm-equiv focal length
    if make.lower() == "apple":
        try:
            flf = float(fl) if fl is not None else 0.0
        except Exception:
            flf = 0.0
        # Classify by 35mm-equivalent focal length (Apple's naming convention)
        if flf <= 0:
            name = ""
        elif flf < 18:
            name = "Ultra Wide"
        elif flf < 30:
            name = "Wide"
        elif flf < 60:
            name = "Telephoto"
        else:
            name = "Telephoto"
        parts = [x for x in (name, fl_str(), f_str()) if x]
        if parts:
            return " ".join(parts)

    # Non-Apple: prefer the real EXIF lens name
    lens = item.get("lens", "")
    if lens:
        return lens
    parts = [x for x in (fl_str(), f_str()) if x]
    return " ".join(parts)


def format_location(lat, lon, city: str) -> str:
    """
    Location string:
    - city if available
    - otherwise if lat/lon exist, use "lat, lon" (5 decimals)
    - otherwise empty string (do not write “unknown location”)
    """
    if city and str(city).strip():
        return str(city).strip()
    if lat is None or lon is None:
        return ""
    try:
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except Exception:
        return ""


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD_PATH if bold else FONT_PATH
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        try:
            return ImageFont.truetype(str(FONT_PATH), size)
        except Exception:
            return ImageFont.load_default()


# Battery icon bitmap (80% level) — matches esp32/ink-display-7C-photo/battery_icons.h
# 24x24, 1 byte/pixel: 0=black, 1=white, 2=red, 3=yellow
# Used only for server-side PREVIEW rendering; the ESP32 firmware overlays the
# real live battery level on-device from the same icon data.
#
# Loaded from the actual firmware header so the preview always matches what
# the device draws. Path resolution: look for battery_icons.h next to this
# file's repo checkout, or in common locations; fall back to a compiled-in
# copy of the 80% icon.
_PREVIEW_ICON_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "esp32" / "ink-display-7C-photo" / "battery_icons.h",
    Path(__file__).resolve().parent / "esp32" / "ink-display-7C-photo" / "battery_icons.h",
    Path(__file__).resolve().parent / "battery_icons.h",
    Path("/mnt/Data/InkTime/esp32/ink-display-7C-photo/battery_icons.h"),
]

def _load_preview_battery_icon() -> bytes:
    import re
    for cand in _PREVIEW_ICON_CANDIDATES:
        try:
            txt = cand.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        m = re.search(
            r"static const uint8_t battery_icon_080\[[^]]*\] PROGMEM = \{(.*?)\};",
            txt, re.S)
        if not m:
            continue
        vals = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", m.group(1))]
        if len(vals) == BATTERY_ICON_W * BATTERY_ICON_H:
            print(f"[ICON] battery preview icon loaded from {cand}")
            return bytes(vals)
    print("[WARN] battery_icons.h not found; using built-in 80% icon fallback")
    return _FALLBACK_BATTERY_ICON

# Fallback: 80% icon copied from battery_icons.h (used only if the header
# file is missing on the server)
_FALLBACK_BATTERY_ICON = bytes([
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x00, 0x01,
    0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
])

PREVIEW_BATTERY_ICON = _load_preview_battery_icon()


def draw_battery_icon(draw: "ImageDraw.ImageDraw", x: int, y: int) -> None:
    """Draw the battery icon bitmap at (x, y). 0=black, 1=transparent, 2/3=red/yellow."""
    for row in range(BATTERY_ICON_H):
        for col in range(BATTERY_ICON_W):
            v = PREVIEW_BATTERY_ICON[row * BATTERY_ICON_W + col]
            if v == 0:
                draw.point((x + col, y + row), fill=(0, 0, 0))
            elif v == 2:
                draw.point((x + col, y + row), fill=(200, 0, 0))
            elif v == 3:
                draw.point((x + col, y + row), fill=(200, 180, 0))


def render_image(item: Dict[str, Any]) -> Image.Image:
    """
    Render a 480x800 RGB image (portrait) for the chosen item:
    - Top photo area: [0, CANVAS_HEIGHT - TEXT_AREA_HEIGHT)
    - Bottom TEXT_AREA_HEIGHT pixels are the info panel:
      line 1: ISO · focal · f/ · shutter   [battery icon drawn by ESP32 firmware]
      line 2: date (left) + location (right)
    """
    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # ---------- Load original and apply EXIF orientation ----------
    img_path = Path(item["path"])
    if not img_path.exists():
        aid = item.get("immich_asset_id")
        if aid and getattr(cfg, "USE_IMMICH", False):
            try:
                from immich_client import ImmichConfig, ImmichClient
                client = ImmichClient(ImmichConfig.from_config_module(cfg))
                cache_dir = Path(str(getattr(cfg, "IMMICH_DOWNLOAD_DIR", "./cache/immich") or "./cache/immich"))
                if not cache_dir.is_absolute():
                    cache_dir = (Path(__file__).resolve().parent / cache_dir).resolve()
                for ext in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"):
                    candidate = cache_dir / f"{aid}{ext}"
                    if candidate.exists():
                        img_path = candidate
                        break
                else:
                    try:
                        info = client.get_asset_info(aid)
                        orig_name = str(info.get("originalFileName") or "")
                        orig_suffix = Path(orig_name).suffix.lower() if orig_name else ""
                        if orig_suffix not in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"):
                            orig_suffix = ".jpg"
                    except Exception:
                        orig_suffix = ".jpg"
                    img_path = cache_dir / f"{aid}{orig_suffix}"
                    client.download_preview(aid, img_path)
                print(f"[IMMICH] re-downloaded {aid} -> {img_path}")
            except Exception as e:
                raise RuntimeError(f"Image not found and re-download failed: {img_path} ({e})")
        else:
            raise RuntimeError(f"Image not found: {img_path}")
    img = Image.open(img_path)

    img = ImageOps.exif_transpose(img).convert("RGB")

    img_w, img_h = img.size
    if img_w == 0 or img_h == 0:
        raise RuntimeError(f"Invalid image size: {img.size}")

    # ---------- Photo area ----------
    img_area_w = CANVAS_WIDTH
    img_area_h = CANVAS_HEIGHT - TEXT_AREA_HEIGHT  # bottom reserved for text

    # “fill then crop”: resize to at least cover the area, then center-crop
    scale = max(img_area_w / img_w, img_area_h / img_h)
    draw_w = int(img_w * scale)
    draw_h = int(img_h * scale)

    img_resized = img.resize((draw_w, draw_h), Image.LANCZOS)

    left = max(0, (draw_w - img_area_w) // 2)
    top = max(0, (draw_h - img_area_h) // 2)
    right = left + img_area_w
    bottom = top + img_area_h
    img_cropped = img_resized.crop((left, top, right, bottom))

    # paste onto the top
    canvas.paste(img_cropped, (0, 0))

    # ---------- Bottom text area (info panel) ----------
    padding_x = 24
    text_area_top = CANVAS_HEIGHT - TEXT_AREA_HEIGHT          # 700
    # Location right edge aligns exactly with battery icon right edge (x=463)
    loc_right_x = BATTERY_ICON_X + BATTERY_ICON_W - 1         # 463

    # Divider line under the photo, separating it from the text panel
    draw.line([(0, text_area_top), (CANVAS_WIDTH, text_area_top)], fill=(190, 190, 190), width=2)

    # Fonts
    font_reg_18 = _load_font(18, bold=False)    # date / location
    font_reg_20 = _load_font(20, bold=False)    # specs row

    # Line 1: specs row (ISO · focal · f/ · shutter) — same row as battery icon
    y = text_area_top + 12                       # 712
    specs = format_specs_row(item)
    if specs:
        draw.text((padding_x, y), specs, font=font_reg_20, fill=(0, 0, 0))
    y += 36

    # Line 2: date (left) + location (right)
    date_display = format_date_display(item["date"])
    loc_display = format_location(item.get("lat"), item.get("lon"), item.get("city") or "")
    if date_display:
        draw.text((padding_x, y), date_display, font=font_reg_18, fill=(0, 0, 0))
    if loc_display:
        loc_w = draw.textlength(loc_display, font=font_reg_18)
        loc_x = loc_right_x - loc_w
        if loc_x < padding_x:
            loc_x = padding_x
        draw.text((loc_x, y), loc_display, font=font_reg_18, fill=(0, 0, 0))

    return canvas


def apply_four_color_dither(img: Image.Image) -> Image.Image:
    """
    Apply Floyd–Steinberg dithering to the image, quantizing to 4 colors (black/white/red/yellow).
    """
    img = img.convert("RGB")
    w, h = img.size
    pixels = img.load()

    err_r = [0.0] * w
    err_g = [0.0] * w
    err_b = [0.0] * w
    next_err_r = [0.0] * w
    next_err_g = [0.0] * w
    next_err_b = [0.0] * w

    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            r = max(0.0, min(255.0, r + err_r[x]))
            g = max(0.0, min(255.0, g + err_g[x]))
            b = max(0.0, min(255.0, b + err_b[x]))

            idx, pr, pg, pb = nearest_palette_color(r, g, b)

            # write back the quantized color
            pixels[x, y] = (pr, pg, pb)

            # error
            er = r - pr
            eg = g - pg
            eb = b - pb

            # Floyd–Steinberg:
            #        *   7/16
            #   3/16 5/16 1/16
            if x + 1 < w:
                err_r[x + 1] += er * (7.0 / 16.0)
                err_g[x + 1] += eg * (7.0 / 16.0)
                err_b[x + 1] += eb * (7.0 / 16.0)
            if y + 1 < h:
                if x > 0:
                    next_err_r[x - 1] += er * (3.0 / 16.0)
                    next_err_g[x - 1] += eg * (3.0 / 16.0)
                    next_err_b[x - 1] += eb * (3.0 / 16.0)
                next_err_r[x] += er * (5.0 / 16.0)
                next_err_g[x] += eg * (5.0 / 16.0)
                next_err_b[x] += eb * (5.0 / 16.0)
                if x + 1 < w:
                    next_err_r[x + 1] += er * (1.0 / 16.0)
                    next_err_g[x + 1] += eg * (1.0 / 16.0)
                    next_err_b[x + 1] += eb * (1.0 / 16.0)

        if y + 1 < h:
            # shift next_err_* into the current row and clear next_err_*
            for i in range(w):
                err_r[i] = next_err_r[i]
                err_g[i] = next_err_g[i]
                err_b[i] = next_err_b[i]
                next_err_r[i] = 0.0
                next_err_g[i] = 0.0
                next_err_b[i] = 0.0

    return img


def image_to_palette_bin(img: Image.Image) -> bytes:
    """
    Convert the already-quantized image to BIN:
    - row-major, top-to-bottom, left-to-right
    - 1 byte per pixel: 0=black, 1=white, 2=red, 3=yellow
    """
    img = img.convert("RGB")
    if img.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise RuntimeError(f"Wrong image size: {img.size}, expected {(CANVAS_WIDTH, CANVAS_HEIGHT)}")

    data = bytearray(CANVAS_WIDTH * CANVAS_HEIGHT)
    idx_map = {c: i for i, c in enumerate(PALETTE)}  # (r,g,b) -> index

    for y in range(CANVAS_HEIGHT):
        for x in range(CANVAS_WIDTH):
            r, g, b = img.getpixel((x, y))
            key = (int(r), int(g), int(b))
            idx = idx_map.get(key)
            if idx is None:
                idx, _, _, _ = nearest_palette_color(r, g, b)
            data[y * CANVAS_WIDTH + x] = idx

    return bytes(data)


def write_h_array(bin_path: Path, h_path: Path, array_name: str = "daily_bin"):
    """
    Convert BIN to a C-array header latest.h:
    const unsigned int daily_bin_size = ...;
    const uint8_t daily_bin[] = { 0x00, 0x01, ... };
    """
    data = bin_path.read_bytes()
    with open(h_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated from render_daily_photo.py\n")
        f.write(f"// Size = {len(data)} bytes (480x800, 1 byte/pixel)\n\n")
        f.write(f"const unsigned int {array_name}_size = {len(data)};\n")
        f.write(f"const uint8_t {array_name}[] = {{\n    ")

        for i, b in enumerate(data):
            f.write(f"0x{b:02X}, ")
            if (i + 1) % 16 == 0:
                f.write("\n    ")

        f.write("\n};\n")


# ========== Main flow ==========

def main():
    items = load_sim_rows()
    if not items:
        raise SystemExit("No usable photos (exif_json empty or parse failed).")

    photos, info = choose_photos_for_today(items, TODAY, count=DAILY_PHOTO_QUANTITY)

    print("[INFO] Target month-day:", info["target_md"])
    print("[INFO] Used month-day:", info["used_md"])
    print("[INFO] Walk-back days (day_offset):", info["day_offset"])
    print("[INFO] Candidates above threshold:", info["candidate_count"])
    print("[INFO] Total for the day:", info["total_count_md"])
    print("[INFO] Used global-max fallback:", info["fallback_global_max"])

    if not photos:
        raise SystemExit("Selection result is empty.")

    import shutil

    # Render each of today's selected photos one by one
    for idx, chosen in enumerate(photos):
        print(f"[INFO] Photo #{idx} selected:", chosen["path"])
        print("[INFO] Capture date:", chosen["date"])
        print("[INFO] Memory score:", chosen["memory"])
        print("[DEBUG] Specs:", format_specs_row(chosen))
        print("[DEBUG] City:", chosen.get("city", ""))

        # Render the full composite (photo + specs + date + location).
        img = render_image(chosen)

        # Dither to 4-color e-ink style
        img_dithered = apply_four_color_dither(img)

        # Save preview PNG (already dithered), indexed by position.
        # The battery icon is drawn server-side here so the preview shows what
        # the frame will look like; the .bin below stays clean because the
        # ESP32 firmware overlays the REAL battery level on-device.
        preview_img = img_dithered.copy()
        preview_draw = ImageDraw.Draw(preview_img)
        draw_battery_icon(preview_draw, BATTERY_ICON_X, BATTERY_ICON_Y)
        preview_path = BIN_OUTPUT_DIR / f"preview_{idx}.png"
        preview_img.save(preview_path)
        print(f"[OK] Preview PNG saved: {preview_path}")

        # Convert to BIN: photo_0.bin, photo_1.bin, ...
        bin_data = image_to_palette_bin(img_dithered)
        bin_path = BIN_OUTPUT_DIR / f"photo_{idx}.bin"
        with open(bin_path, "wb") as f:
            f.write(bin_data)
        print(f"[OK] BIN generated: {bin_path} (size {len(bin_data)} bytes)")

        # Mark this photo as used so the next render avoids it
        chosen_aid = chosen.get("immich_asset_id") or chosen.get("path")
        if chosen_aid:
            try:
                import sqlite3 as _sq
                _c = _sq.connect(str(DB_PATH))
                _c.execute(
                    "UPDATE photo_scores SET used_at = ? WHERE immich_asset_id = ? OR path = ?",
                    (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     str(chosen_aid), str(chosen.get("path", ""))),
                )
                _c.commit()
                _c.close()
            except Exception as _e:
                print(f"[WARN] failed to mark photo used: {_e}")

        # Header array: photo_0.h, photo_1.h, with distinct array names
        h_path = BIN_OUTPUT_DIR / f"photo_{idx}.h"
        array_name = f"daily_bin_{idx}"
        write_h_array(bin_path, h_path, array_name=array_name)
        print(f"[OK] Header array generated: {h_path}")

    # For backward compat, also generate latest.* pointing at the first one
    first_bin = BIN_OUTPUT_DIR / "photo_0.bin"
    first_h = BIN_OUTPUT_DIR / "photo_0.h"
    first_preview = BIN_OUTPUT_DIR / "preview_0.png"
    latest_bin = BIN_OUTPUT_DIR / "latest.bin"
    latest_h = BIN_OUTPUT_DIR / "latest.h"
    latest_preview = BIN_OUTPUT_DIR / "preview.png"

    if first_bin.exists():
        shutil.copyfile(first_bin, latest_bin)
        print(f"[OK] Updated latest.bin -> {first_bin.name}")
    if first_h.exists():
        shutil.copyfile(first_h, latest_h)
        print(f"[OK] Updated latest.h -> {first_h.name}")
    if first_preview.exists():
        shutil.copyfile(first_preview, latest_preview)
        print(f"[OK] Updated preview.png -> {first_preview.name}")


if __name__ == "__main__":
    main()
