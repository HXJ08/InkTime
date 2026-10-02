#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import base64
import json
import sqlite3
import os
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import io
from PIL import Image, ExifTags, ImageOps
import pillow_heif
pillow_heif.register_heif_opener()
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg
import shutil


# =======================
# NAS dropout guard (macOS /Volumes)
# =======================
NAS_MOUNT_URL = str(getattr(cfg, "NAS_MOUNT_URL", "") or "").strip()
NAS_MOUNT_POINT = Path(str(getattr(cfg, "NAS_MOUNT_POINT", "/Volumes/photo") or "/Volumes/photo")).expanduser()
NAS_RETRY_TIMES = int(getattr(cfg, "NAS_RETRY_TIMES", 3) or 3)
NAS_RETRY_SLEEP_SEC = float(getattr(cfg, "NAS_RETRY_SLEEP_SEC", 2.0) or 2.0)


def _is_mount_ok() -> bool:
    """Check whether the NAS is still mounted (best-effort, conservative)."""
    try:
        # Network volumes under /Volumes are usually mount points
        if NAS_MOUNT_POINT and NAS_MOUNT_POINT.exists():
            if os.path.ismount(str(NAS_MOUNT_POINT)):
                return True
        # Fallback: as long as the photo root is accessible, treat it as OK
        return IMAGE_DIR.exists()
    except Exception:
        return False


def _try_remount_nas() -> bool:
    """Try to remount the NAS.

    Only runs when NAS_MOUNT_URL is configured; prefers macOS AppleScript mount,
    so credentials saved in the keychain can be reused.
    """
    if not NAS_MOUNT_URL:
        return False

    print(f"[WARN] Detected possible NAS dropout, trying to remount: {NAS_MOUNT_URL}")

    # 1) AppleScript (recommended): mount volume "afp://..." / "smb://..."
    try:
        subprocess.run(
            ["osascript", "-e", f'mount volume "{NAS_MOUNT_URL}"'],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        pass

    # 2) Fallback: if you prefer the command line, swap to mount_afp / mount_smbfs.

    # Wait for the volume to appear
    for _ in range(10):
        if _is_mount_ok():
            print("[INFO] NAS remount succeeded, continuing.")
            return True
        time.sleep(0.5)

    print("[WARN] NAS remount failed (volume still unavailable).")
    return False


def _read_bytes_with_nas_retry(path: Path) -> bytes:
    """Read a file: on NAS dropout-like errors, try remounting and retrying."""
    last_err: Exception | None = None

    # Only enable remount logic for files inside the photo library path, to avoid touching local files.
    try:
        in_photo_dir = str(path).startswith(str(IMAGE_DIR))
    except Exception:
        in_photo_dir = False

    for attempt in range(1, max(1, NAS_RETRY_TIMES) + 1):
        try:
            return path.read_bytes()
        except OSError as e:
            last_err = e

            # Not in the photo library dir, or no NAS URL configured: just raise
            if not in_photo_dir or not NAS_MOUNT_URL:
                raise

            # Common network-volume disconnect errors: 57 Socket is not connected; also 5/6 I/O errors.
            # Don't be too strict: check mount state first; if dropped, try remounting.
            if not _is_mount_ok():
                print(f"[WARN] Read failed (attempt {attempt}/{NAS_RETRY_TIMES}): {e}")
                ok = _try_remount_nas()
                if ok:
                    # Retry immediately after remount
                    continue

            # If mount still looks fine but read still fails, sleep per the retry policy
            if attempt < NAS_RETRY_TIMES:
                print(f"[WARN] Read failed (attempt {attempt}/{NAS_RETRY_TIMES}), will retry: {e}")
                time.sleep(max(0.1, NAS_RETRY_SLEEP_SEC))
                continue

            raise

    # Shouldn't reach here in theory
    if last_err:
        raise last_err
    raise OSError("Failed to read file")


# ================== Config section (from config.py) ==================

ROOT_DIR = Path(__file__).resolve().parent.parent  # repo root

# Image directory to scan
IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

# SQLite database path
DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

# ---- Duplicate / near-duplicate detection ----
# Two layers:
#   1. Perceptual hash (dHash, 64 bits) + Hamming distance: catches exact copies
#      and very-similar images (re-encodes, color shifts, same scene).
#   2. EXIF datetime proximity (default 3h) + looser hash distance: catches
#      burst photos (same event, similar composition, taken seconds apart).
DEDUPE_ENABLED = bool(getattr(cfg, "DEDUPE_ENABLED", True))
# Strict threshold: a hash Hamming distance <= this is treated as a duplicate
# regardless of timestamp.
DEDUPE_PHASH_DUP_THRESHOLD = int(getattr(cfg, "DEDUPE_PHASH_DUP_THRESHOLD", 5))
# Looser threshold: combined with the time window below, used to catch bursts.
DEDUPE_PHASH_BURST_THRESHOLD = int(getattr(cfg, "DEDUPE_PHASH_BURST_THRESHOLD", 12))
# Time window (hours) for the burst criterion.
DEDUPE_BURST_WINDOW_HOURS = float(getattr(cfg, "DEDUPE_BURST_WINDOW_HOURS", 3.0))
# Whether to delete the local cache file when a duplicate is detected.
DEDUPE_DELETE_CACHE = bool(getattr(cfg, "DEDUPE_DELETE_CACHE", True))

# Photo source: local directory vs. Immich
USE_IMMICH = bool(getattr(cfg, "USE_IMMICH", False))

# Immich config (only consulted when USE_IMMICH=True)
IMMICH_DOWNLOAD_DIR: Path | None = None
IMMICH_ASSET_INDEX: dict[str, dict] = {}  # asset_id -> asset metadata
if USE_IMMICH:
    try:
        from immich_client import ImmichConfig, ImmichClient, sync_to_cache
    except ImportError as e:
        raise SystemExit(
            f"USE_IMMICH=True but immich_client.py could not be imported: {e}. "
            "Make sure immich_client.py is in the project root."
        )
    _immich_cfg = ImmichConfig.from_config_module(cfg)
    _immich_client = ImmichClient(_immich_cfg)
    # Sanity-check the server is reachable
    try:
        _immich_client.ping()
        print(f"[IMMICH] connected to {_immich_cfg.base_url}")
    except Exception as e:
        raise SystemExit(f"[IMMICH] cannot reach {_immich_cfg.base_url}: {e}")
    _cache = Path(str(getattr(cfg, "IMMICH_DOWNLOAD_DIR", "./cache/immich") or "./cache/immich"))
    if not _cache.is_absolute():
        _cache = (ROOT_DIR / _cache).resolve()
    _cache.mkdir(parents=True, exist_ok=True)
    IMMICH_DOWNLOAD_DIR = _cache

# ---- Channel list (auto-failover on 429) ----

def _resolve_api_key(value: str) -> str:
    """Resolve api_key field. Supports the "env:VAR_NAME" pattern so the
    key can be kept out of config.py / git. Returns "" for an unset env var
    (so the request will hit the network and the server's 401/403 will be
    the actionable error rather than failing at startup)."""
    if not value:
        return ""
    v = value.strip()
    if v.startswith("env:"):
        return os.environ.get(v[4:].strip(), "")
    return v

# MiniMax CN is the default when nothing is configured. If you want to keep
# using a local LM Studio setup, set MINIMAX_API_KEY="" in your environment
# and put the LM Studio URL in API_CHANNELS.
_DEFAULT_API_URL = "https://api.minimax.chat/v1/chat/completions"
_DEFAULT_MODEL = "MiniMax-VL-01"

_raw_channels = getattr(cfg, "API_CHANNELS", None) or []
if not _raw_channels:
    # Backward compat: build a single channel from the legacy single-variable config
    _compat_url = str(
        getattr(cfg, "API_URL", None)
        or os.environ.get("API_URL")
        or os.environ.get("LMSTUDIO_URL", _DEFAULT_API_URL)
    )
    _compat_model = str(
        getattr(cfg, "MODEL_NAME", None)
        or os.environ.get("MODEL_NAME")
        or os.environ.get("LMSTUDIO_MODEL", _DEFAULT_MODEL)
    )
    _compat_key = _resolve_api_key(
        str(
            getattr(cfg, "API_KEY", None)
            or os.environ.get("MINIMAX_API_KEY")
            or os.environ.get("API_KEY")
            or os.environ.get("LMSTUDIO_API_KEY", "")
        )
    )
    _raw_channels = [
        {"api_url": _compat_url, "model_name": _compat_model, "api_key": _compat_key}
    ]

# Resolve any "env:..." placeholders inside API_CHANNELS entries
API_CHANNELS: list[dict] = []
for _ch in _raw_channels:
    _resolved = dict(_ch)
    _resolved["api_key"] = _resolve_api_key(str(_resolved.get("api_key", "")))
    API_CHANNELS.append(_resolved)
_channel_index: int = 0  # remember the index of the last successful channel
_channel_cooldown_until: list[float] = [0.0] * len(API_CHANNELS)
_channel_inflight: list[int] = [0] * len(API_CHANNELS)
_channel_lock = threading.Lock()  # protect _channel_index from concurrent access

# How many images to process at most; None = no limit
BATCH_LIMIT = getattr(cfg, "BATCH_LIMIT", None)

# Request timeout (seconds)
TIMEOUT = float(getattr(cfg, "TIMEOUT", 600) or 600)

# Cooldown (seconds) to deprioritize a channel after it fails.
# E.g. if A fails and B succeeds, subsequent requests prefer B instead of always hitting A first.
_raw_failover_cooldown = getattr(cfg, "CHANNEL_FAILOVER_COOLDOWN_SEC", 300)
if _raw_failover_cooldown is None:
    CHANNEL_FAILOVER_COOLDOWN_SEC = 300.0
else:
    CHANNEL_FAILOVER_COOLDOWN_SEC = float(_raw_failover_cooldown)

# Long edge of the image to resize to before sending to VLM (pixels).
# 0 means no resize.
# Local inference can keep a higher value; cloud inference should lower it (less token/cost).
VLM_MAX_LONG_EDGE = int(getattr(cfg, "VLM_MAX_LONG_EDGE", 2560) or 2560)

# Chinese cities database location
WORLD_CITIES_CSV = Path(str(getattr(cfg, "WORLD_CITIES_CSV", "data/world_cities.csv") or "data/world_cities.csv")).expanduser()
if not WORLD_CITIES_CSV.is_absolute():
    WORLD_CITIES_CSV = (ROOT_DIR / WORLD_CITIES_CSV).resolve()

CITY_GRID_DEG = float(getattr(cfg, "CITY_GRID_DEG", 1.0) or 1.0)
CITY_MAX_DISTANCE_KM = float(getattr(cfg, "CITY_MAX_DISTANCE_KM", 80.0) or 80.0)
HOME_LAT = float(getattr(cfg, "HOME_LAT", 22.543096) or 22.543096)
HOME_LON = float(getattr(cfg, "HOME_LON", 114.057865) or 114.057865)
HOME_RADIUS_KM = float(getattr(cfg, "HOME_RADIUS_KM", 60.0) or 60.0)
# ==================================================

# Debug mode (controlled by --debug CLI flag)
DEBUG: bool = False

# Whether exiftool is available: when missing, only degrade GPS/some EXIF, don't abort
EXIFTOOL_AVAILABLE = False

def require_exiftool() -> None:
    global EXIFTOOL_AVAILABLE
    EXIFTOOL_AVAILABLE = shutil.which("exiftool") is not None
    if not EXIFTOOL_AVAILABLE:
        print(
            "[WARN] exiftool not found; skipping exiftool-assisted GPS/EXIF reading (does not affect main flow).\n"
            " For more complete GPS info, please install:\n"
            "       macOS: brew install exiftool\n"
            "       Ubuntu/Debian: sudo apt-get install -y libimage-exiftool-perl\n"
            "       Windows: choco install exiftool"
        )

def encode_image_to_b64(path: Path) -> str:
    """Read the image and (optionally) resize its long edge, re-encode as JPEG, then base64.

    Purpose:
    1) Control input resolution (especially for 200MP-class huge images) to avoid cost/latency spikes;
    2) Avoid certain libvips JPEG decode errors (e.g. extra bytes before the marker) by re-encoding.
    """
    data = _read_bytes_with_nas_retry(path)

    # Try to open with PIL's fault-tolerant loader, then re-encode into clean JPEG bytes
    try:
        img = Image.open(io.BytesIO(data))
        # Apply EXIF orientation
        try:
            img = ImageOps.exif_transpose(img)  # type: ignore
        except Exception:
            pass

        # Normalize color mode: JPEG needs RGB
        if img.mode in ("RGBA", "LA"):
            # If a transparency channel exists, composite over white
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Optional resize
        try:
            w, h = img.size
            long_edge = max(w, h)
            if VLM_MAX_LONG_EDGE and long_edge > VLM_MAX_LONG_EDGE:
                scale = float(VLM_MAX_LONG_EDGE) / float(long_edge)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), resample=Image.LANCZOS)
        except Exception:
            pass

        out = io.BytesIO()
        # quality 92 is a good balance between size and look; optimize is slower but usually smaller
        img.save(out, format="JPEG", quality=92, optimize=True)
        clean_bytes = out.getvalue()
        return base64.b64encode(clean_bytes).decode("utf-8")

    except Exception:
        # Fallback: if PIL can't open it, return original bytes (so upstream errors are more obvious)
        return base64.b64encode(data).decode("utf-8")


def ensure_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_scores (
            path              TEXT PRIMARY KEY,
            caption           TEXT,
            type              TEXT,
            memory_score      REAL,
            beauty_score      REAL,
            reason            TEXT,
            width             INTEGER,
            height            INTEGER,
            orientation       TEXT,
            used_at           TEXT,
            exif_json         TEXT,
            raw_json          TEXT,
            exif_datetime     TEXT,
            exif_make         TEXT,
            exif_model        TEXT,
            exif_iso          INTEGER,
            exif_exposure_time REAL,
            exif_f_number     REAL,
            exif_focal_length REAL,
            exif_gps_lat      REAL,
            exif_gps_lon      REAL,
            exif_gps_alt      REAL,
            side_caption      TEXT,
            exif_city         TEXT,
            immich_asset_id   TEXT,
            phash             INTEGER
        )
        """
    )
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_json TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN width INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN height INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN orientation TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN used_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN immich_asset_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN phash INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_datetime TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_make TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_model TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_iso INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_exposure_time REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_f_number REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_focal_length REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_lat REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_lon REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_alt REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN side_caption TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_city TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()

# Generate one-line caption
def generate_side_caption(image_path: Path) -> str | None:
    system_prompt = (
        "You write short English voiceover captions (6-18 words, max 20) for an e-ink photo frame.\n"
        "Output ONLY the caption — no preamble, no explanation, no quotation marks, no reasoning, no analysis.\n"
        "Voice: subtle, slightly witty, dry humor or a touch of poetry. Avoid clichés (moments, journey, soul). Do not describe the image.\n"
        "One plain-text sentence, nothing else."
    )
    user_prompt = "Write the caption now."
    try:
        img_b64 = encode_image_to_b64(image_path)
    except Exception:
        return None

    def _build(ch):
        headers = {"Content-Type": "application/json"}
        key = ch.get("api_key", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": ch["model_name"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                        },
                    ],
                },
            ],
            "temperature": 0.7,
            "max_tokens": 1024,  # was 64; MiniMax-M3 uses ~600-800 tokens for reasoning, need room for caption"
            "top_p": 0.9,
            "stream": False,
        }
        return ch["api_url"], headers, body

    try:
        resp = _post_with_channel_fallback(_build, timeout=min(120, TIMEOUT))
    except Exception:
        return None

    if not resp.ok:
        return None

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None

    if not isinstance(content, str):
        content = str(content)

    # MiniMax-M3 wraps its answer in a reasoning block. Strip it cleanly.
    import re as _re
    # 1) Strip leading <think>...</think> block (greedy, multi-line)
    cleaned = _re.sub(
        r"\s*<think>.*?</think>\s*",
        " ", content, count=1, flags=_re.DOTALL,
    ).strip()

    # 2) If the response was cut off mid-reasoning (finish_reason=length and
    #    still inside a <think> block), return None — the model didn't
    #    produce the actual caption.
    finish_reason = ""
    try:
        finish_reason = data["choices"][0].get("finish_reason", "") or ""
    except Exception:
        pass
    if not cleaned and "<think>" in content:
        return None

    # 3) If the response still contains no sentence-like text (model only
    #    produced reasoning, or output looks like JSON), return None.
    if not cleaned or len(cleaned) < 4:
        return None

    # 3) Strip markdown ```json fences if any.
    fence = _re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, flags=_re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    # 4) Trim quotes and stray markers around the caption.
    cleaned = cleaned.strip().strip("\"“”\'").strip()

    # 5) Collapse internal newlines/whitespace to single spaces.
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()

    # 6) Take only the first non-empty sentence to keep it short.
    parts = [p.strip() for p in _re.split(r"(?<=[.!?])\s+", cleaned) if p.strip()]
    if parts:
        cleaned = parts[0]

    return cleaned or None


def list_images(limit: int | None = None) -> list[Path]:
    """Enumerate images to analyze. Dispatches to Immich or local scan.

    Returns a list of Path objects pointing to the actual image bytes
    (local cache file for Immich-sourced images, original path for local).

    For Immich-sourced images, the asset_id is stored in the global
    IMMICH_ASSET_INDEX keyed by the returned path, so downstream code can
    look it up via IMMICH_ASSET_INDEX.get(str(path)) to persist it in DB.
    """
    if USE_IMMICH:
        return _list_immich_assets(limit=limit)
    return _list_local_images(limit=limit)


def _list_local_images(limit: int | None = None) -> list[Path]:
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic", ".heif"}
    files = []
    print("[INFO] Recursively scanning image directory, please wait...")
    scanned = 0
    for p in IMAGE_DIR.rglob("*"):
        scanned += 1
        if scanned % 500 == 0:
            print(f"[SCAN] Files scanned: {scanned} ...")
        if p.is_file() and p.suffix.lower() in exts:
            if is_screenshot(p):
                continue
            files.append(p)
    print(f"[INFO] Scan complete: found {len(files)} images (total files seen: {scanned}).")
    if limit is not None:
        files = files[:limit]
    return files


def _list_immich_assets(limit: int | None = None) -> list[Path]:
    """Walk Immich assets, download to local cache, return cache paths.

    Side effect: populates IMMICH_ASSET_INDEX[str(path)] = asset_metadata
    so that downstream DB writes can persist the asset_id.
    """
    assert IMMICH_DOWNLOAD_DIR is not None, "IMMICH_DOWNLOAD_DIR not initialized"
    IMMICH_ASSET_INDEX.clear()
    paths: list[Path] = []
    n = 0
    # Pre-load blacklisted asset_ids so we skip them in the loop below.
    # The blacklist lives in photos.db (photo_blacklist table) and is populated
    # by the /dashboard web UI when the user hides a photo.
    blacklisted: set[str] = set()
    try:
        if DB_PATH and Path(str(DB_PATH)).exists():
            with sqlite3.connect(str(DB_PATH), timeout=10) as _bl_conn:
                for (aid,) in _bl_conn.execute("SELECT immich_asset_id FROM photo_blacklist").fetchall():
                    if aid:
                        blacklisted.add(aid)
    except Exception as _e:
        print(f"[BLACKLIST] warning: could not load blacklist: {_e}", file=sys.stderr)

    if blacklisted:
        print(f"[BLACKLIST] skipping {len(blacklisted)} user-hidden assets")

    # ---- Face filter ----
    # Skip photos with detected faces unless Jack (or other allowed person)
    # is one of them. Landscapes / cats / objects (no faces) always pass.
    face_filter_on = bool(getattr(cfg, "FACE_FILTER_ENABLED", False))
    allow_unnamed = bool(getattr(cfg, "ALLOW_UNNAMED_FACES", False))
    # Resolve allowed_ids: DB first, then config.py fallback.
    allowed_ids: set[str] = set()
    try:
        with sqlite3.connect(str(DB_PATH), timeout=10) as _al_conn:
            _al_conn.execute(
                "CREATE TABLE IF NOT EXISTS face_allowlist ("
                "person_id TEXT PRIMARY KEY, person_name TEXT, "
                "added_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual')"
            )
            _al_rows = _al_conn.execute(
                "SELECT person_id FROM face_allowlist"
            ).fetchall()
        allowed_ids = {r[0] for r in _al_rows if r[0]}
        if not allowed_ids:
            cfg_ids = list(getattr(cfg, "ALLOWED_PERSON_IDS", []) or [])
            allowed_ids = set(cfg_ids)
    except Exception:
        allowed_ids = set(getattr(cfg, "ALLOWED_PERSON_IDS", []) or [])
    # Module-level side-channel: id -> [name,...] for dashboards / debugging
    if not hasattr(globals(), "IMMICH_FACE_INDEX"):
        globals()["IMMICH_FACE_INDEX"] = {}
    face_index = globals()["IMMICH_FACE_INDEX"]
    face_skip_count = 0
    face_keep_count = 0
    if face_filter_on:
        print(f"[FACE-FILTER] on; allowed person_ids={len(allowed_ids)}; "
              f"allow_unnamed_faces={allow_unnamed}")

    print(f"[IMMICH] enumerating assets, syncing to {IMMICH_DOWNLOAD_DIR} ...")
    for asset, cache_path in sync_to_cache(_immich_client, IMMICH_DOWNLOAD_DIR):
        aid = asset.get("id")
        if not aid:
            continue
        # Screenshot filter: skip if filename starts with "Screenshot" or its Chinese equivalent
        orig_name = str(asset.get("originalFileName") or "")
        if is_screenshot_name(orig_name):
            continue
        # Blacklist filter: skip assets the user has hidden via the dashboard.
        # We skip BEFORE downloading, so no cache file is created and no VLM
        # call is made for hidden photos.
        if aid in blacklisted:
            continue

        # Face filter: skip photos with detected faces unless an allowed
        # person (Jack) is among them. We do this check from the asset
        # metadata that's already in hand (no extra HTTP call), so it's
        # essentially free.
        people_list = asset.get("people") or []
        face_index[aid] = [
            {"id": (p.get("id") or ""), "name": (p.get("name") or "")}
            for p in people_list if p.get("id")
        ]
        if face_filter_on and people_list:
            person_ids = {p.get("id") for p in people_list if p.get("id")}
            has_named = any((p.get("name") or "") for p in people_list)
            has_allowed = bool(person_ids & allowed_ids)
            if has_allowed:
                face_keep_count += 1
                # pass through
            elif allow_unnamed and not has_named:
                # photos with only unlabeled faces pass when allow_unnamed
                face_keep_count += 1
            else:
                face_skip_count += 1
                continue

        IMMICH_ASSET_INDEX[str(cache_path)] = asset
        paths.append(cache_path)
        n += 1
        if limit is not None and n >= limit:
            break
    print(f"[IMMICH] {n} images ready for analysis (cache: {IMMICH_DOWNLOAD_DIR})")
    if face_filter_on:
        print(f"[FACE-FILTER] kept {face_keep_count}, skipped {face_skip_count} "
              f"(photos with non-allowed people)")
    return paths


def is_screenshot_name(name: str) -> bool:
    """Lightweight Screenshot filter for Immich-sourced files (where we only
    have a basename, not a full path)."""
    if not name:
        return False
    n = name.lower()
    return (
        n.startswith("screenshot")
        or n.startswith("screen shot")
        or n.startswith("screen_")
    )

# Exclude Screenshot images
def is_screenshot(path: Path) -> bool:
    s = str(path)
    return "screenshot" in s.lower()


def filter_unscored(conn: sqlite3.Connection, paths: list[Path]) -> list[Path]:
    if not paths:
        return []

    cur = conn.cursor()
    placeholders = ",".join("?" for _ in paths)
    rows = cur.execute(
        f"SELECT path FROM photo_scores WHERE path IN ({placeholders})",
        [str(p) for p in paths],
    ).fetchall()
    already = {row[0] for row in rows}
    return [p for p in paths if str(p) not in already]


def _convert_gps_to_deg(value):
    try:
        d, m, s = value
        return float(d[0]) / float(d[1]) + float(m[0]) / float(m[1]) / 60.0 + float(s[0]) / float(s[1]) / 3600.0
    except Exception:
        return None


def read_gps_with_exiftool(path: Path):
    if not EXIFTOOL_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        # exiftool not installed: skip
        return None
    except subprocess.CalledProcessError:
        return None

    try:
        data = json.loads(result.stdout)[0]
    except Exception:
        return None

    lat = data.get("GPSLatitude")
    lon = data.get("GPSLongitude")
    alt = data.get("GPSAltitude")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat),
        "lon": float(lon),
        "alt": float(alt) if alt is not None else None,
    }


def read_exif(path: Path) -> dict:
    info: dict = {}
    try:
        img = Image.open(path)
        try:
            width, height = img.size
            info["width"] = int(width)
            info["height"] = int(height)
            if width > height:
                info["orientation"] = "landscape"
            elif height > width:
                info["orientation"] = "portrait"
            else:
                info["orientation"] = "square"
        except Exception:
            pass

        # Use the public API getexif() — works across JPEG / PNG / WebP / HEIC
        # (_getexif() is a JPEG-only private method, HEIC does not support it)
        exif_obj = img.getexif()
        if not exif_obj:
            # Fallback: try _getexif() (older Pillow or special formats)
            try:
                exif_raw = img._getexif() or {}
            except (AttributeError, Exception):
                exif_raw = {}
        else:
            exif_raw = dict(exif_obj)
    except Exception:
        return info

    exif = {}
    for tag_id, value in exif_raw.items():
        tag = ExifTags.TAGS.get(tag_id, tag_id)
        exif[tag] = value

    # Basic fields
    info["datetime"] = exif.get("DateTimeOriginal") or exif.get("DateTime")
    info["make"] = exif.get("Make")
    info["model"] = exif.get("Model")
    info["iso"] = exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity")
    info["exposure_time"] = exif.get("ExposureTime")
    info["f_number"] = exif.get("FNumber")
    info["focal_length"] = exif.get("FocalLength")

    # If main EXIF is missing DateTimeOriginal, try the ExifIFD sub-IFD
    if not info.get("datetime"):
        try:
            exif_ifd = exif_obj.get_ifd(ExifTags.IFD.Exif)
            if exif_ifd:
                dt = exif_ifd.get(0x9003)  # DateTimeOriginal
                if dt:
                    info["datetime"] = dt
                if not info.get("iso"):
                    info["iso"] = exif_ifd.get(0x8827)  # ISOSpeedRatings
                if not info.get("exposure_time"):
                    info["exposure_time"] = exif_ifd.get(0x829A)  # ExposureTime
                if not info.get("f_number"):
                    info["f_number"] = exif_ifd.get(0x829D)  # FNumber
                if not info.get("focal_length"):
                    info["focal_length"] = exif_ifd.get(0x920A)  # FocalLength
        except Exception:
            pass

    # GPS info: prefer get_ifd() (HEIC compatible), fall back to legacy
    lat = lon = None

    # Path 1: get_ifd(GPSInfo) (recommended, works for HEIC and JPEG)
    try:
        gps_ifd = exif_obj.get_ifd(ExifTags.IFD.GPSInfo)
        if gps_ifd:
            gps_tags = {}
            for k, v in gps_ifd.items():
                name = ExifTags.GPSTAGS.get(k, k)
                gps_tags[name] = v

            lat_ref = gps_tags.get("GPSLatitudeRef")
            lat_raw = gps_tags.get("GPSLatitude")
            lon_ref = gps_tags.get("GPSLongitudeRef")
            lon_raw = gps_tags.get("GPSLongitude")

            if lat_raw and lat_ref:
                lat = _convert_gps_to_deg(lat_raw)
                if lat is not None and lat_ref in ["S", "s"]:
                    lat = -lat
            if lon_raw and lon_ref:
                lon = _convert_gps_to_deg(lon_raw)
                if lon is not None and lon_ref in ["W", "w"]:
                    lon = -lon
    except Exception:
        pass

    # Path 2: fall back to legacy GPSInfo dict (some JPEGs may go this route)
    if lat is None or lon is None:
        gps_info = exif.get("GPSInfo")
        if isinstance(gps_info, dict):
            gps_tags = {}
            for k, v in gps_info.items():
                name = ExifTags.GPSTAGS.get(k, k)
                gps_tags[name] = v

            lat_ref = gps_tags.get("GPSLatitudeRef")
            lat_raw = gps_tags.get("GPSLatitude")
            lon_ref = gps_tags.get("GPSLongitudeRef")
            lon_raw = gps_tags.get("GPSLongitude")

            if lat_raw and lat_ref:
                lat = _convert_gps_to_deg(lat_raw)
                if lat is not None and lat_ref in ["S", "s"]:
                    lat = -lat
            if lon_raw and lon_ref:
                lon = _convert_gps_to_deg(lon_raw)
                if lon is not None and lon_ref in ["W", "w"]:
                    lon = -lon

    info["gps_lat"] = lat
    info["gps_lon"] = lon

    if info.get("gps_lat") is None or info.get("gps_lon") is None:
        gps = read_gps_with_exiftool(path)
        if gps is not None:
            info["gps_lat"] = gps["lat"]
            info["gps_lon"] = gps["lon"]
            if gps.get("alt") is not None:
                info["gps_alt"] = gps["alt"]

    return info


def in_home(lat: float | None, lon: float | None) -> bool:
    """Check whether a point is within the “home/base” radius."""
    if lat is None or lon is None:
        return False
    try:
        d = haversine_km(float(lat), float(lon), float(HOME_LAT), float(HOME_LON))
        return d <= float(HOME_RADIUS_KM)
    except Exception:
        return False


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "00:00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


import csv
import math
from typing import Dict, List, Tuple, Optional

CityRecord = Tuple[float, float, str, str]  # (lat, lon, name_zh, name_en)

_CITY_CACHE_CITIES: List[CityRecord] | None = None
_CITY_CACHE_GRID: Dict[Tuple[int, int], List[int]] | None = None

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c

def grid_key(lat: float, lon: float) -> Tuple[int, int]:
    gx = int(math.floor(lat / CITY_GRID_DEG))
    gy = int(math.floor(lon / CITY_GRID_DEG))
    return gx, gy

def load_world_cities(csv_path: Path) -> Tuple[List[CityRecord], Dict[Tuple[int, int], List[int]]]:
    if not csv_path.exists():
        raise SystemExit(f"[FATAL] Cities index file not found: {csv_path}")

    cities: List[CityRecord] = []
    grid_index: Dict[Tuple[int, int], List[int]] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float((row.get("lat") or "").strip())
                lon = float((row.get("lon") or "").strip())
            except Exception:
                continue
            name_en = (row.get("name_en") or "").strip()
            name_zh = (row.get("name_zh") or "").strip()
            cities.append((lat, lon, name_zh, name_en))

    for idx, (lat, lon, name_zh, name_en) in enumerate(cities):
        key = grid_key(lat, lon)
        grid_index.setdefault(key, []).append(idx)

    print(f"[INFO] Loaded Chinese cities DB: {csv_path}")
    return cities, grid_index

def find_nearest_city(
    lat: float,
    lon: float,
    cities: List[CityRecord],
    grid_index: Dict[Tuple[int, int], List[int]],
    max_km: float = 80.0,
) -> str:
    if not cities:
        return ""

    gx, gy = grid_key(lat, lon)

    def collect_candidates(radius: int) -> List[int]:
        cand: List[int] = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                bucket = grid_index.get((gx + dx, gy + dy))
                if bucket:
                    cand.extend(bucket)
        return cand

    candidates = collect_candidates(radius=1)
    if not candidates:
        candidates = collect_candidates(radius=2)
    if not candidates:
        return ""

    best_idx: Optional[int] = None
    best_dist = float("inf")

    for idx in candidates:
        city_lat, city_lon, name_zh, name_en = cities[idx]
        d = haversine_km(lat, lon, city_lat, city_lon)
        if d < best_dist:
            best_dist = d
            best_idx = idx

    if best_idx is None or best_dist > max_km:
        return ""

    _, _, name_zh, name_en = cities[best_idx]
    return name_en or name_zh or ""

def get_city_resolver():
    global _CITY_CACHE_CITIES, _CITY_CACHE_GRID
    if _CITY_CACHE_CITIES is None or _CITY_CACHE_GRID is None:
        _CITY_CACHE_CITIES, _CITY_CACHE_GRID = load_world_cities(WORLD_CITIES_CSV)

    def resolve(lat: float | None, lon: float | None) -> str:
        if lat is None or lon is None:
            return ""
        return find_nearest_city(lat, lon, _CITY_CACHE_CITIES, _CITY_CACHE_GRID, max_km=CITY_MAX_DISTANCE_KM)

    return resolve



# =======================
# Channel load balancing: auto-failover on error
# =======================
def _reserve_next_channel(tried: set[int]) -> int | None:
    n = len(API_CHANNELS)
    now = time.monotonic()
    with _channel_lock:
        start = _channel_index % n
        ordered = [(start + i) % n for i in range(n) if ((start + i) % n) not in tried]

        ready_idle: list[int] = []
        ready_busy: list[int] = []
        cooling_idle: list[int] = []
        cooling_busy: list[int] = []

        for idx in ordered:
            cooling = _channel_cooldown_until[idx] > now
            busy = _channel_inflight[idx] > 0
            if not cooling and not busy:
                ready_idle.append(idx)
            elif not cooling:
                ready_busy.append(idx)
            elif not busy:
                cooling_idle.append(idx)
            else:
                cooling_busy.append(idx)

        candidates = ready_idle or ready_busy or cooling_idle or cooling_busy
        if not candidates:
            return None

        idx = candidates[0]
        _channel_inflight[idx] += 1
        return idx


def _release_channel(idx: int) -> None:
    with _channel_lock:
        if 0 <= idx < len(_channel_inflight) and _channel_inflight[idx] > 0:
            _channel_inflight[idx] -= 1


def _mark_channel_failure(idx: int, ch_label: str, reason: str) -> None:
    if CHANNEL_FAILOVER_COOLDOWN_SEC <= 0:
        return

    until = time.monotonic() + CHANNEL_FAILOVER_COOLDOWN_SEC
    with _channel_lock:
        if 0 <= idx < len(_channel_cooldown_until):
            _channel_cooldown_until[idx] = max(_channel_cooldown_until[idx], until)

    print(
        f"[WARN] Channel {ch_label} entered cooldown for {CHANNEL_FAILOVER_COOLDOWN_SEC:g}s: {reason}"
    )


def _mark_channel_success(idx: int) -> None:
    global _channel_index
    with _channel_lock:
        _channel_index = idx
        if 0 <= idx < len(_channel_cooldown_until):
            _channel_cooldown_until[idx] = 0.0


def _post_with_channel_fallback(
    payload_builder,
    timeout: float = TIMEOUT,
    response_parser=None,
) -> requests.Response | tuple:
    """Try each channel in turn to send the request; on error, auto-failover to the next channel.

    Args:
        payload_builder: callable(channel_dict) -> (url, headers, json_body)
        timeout: request timeout in seconds
        response_parser: optional, callable(response) -> parsed_result.
            If provided, called after a 2xx response; if it raises, treat as failure and failover.
            If not provided, return the response object directly.
    Returns:
        Returns requests.Response when response_parser is not provided;
        returns response_parser(result) when it is.
    Raises:
        RuntimeError: all channels failed
    """
    n = len(API_CHANNELS)
    if n == 0:
        raise RuntimeError("No VLM channels configured (API_CHANNELS is empty)")

    last_error: str | None = None
    tried: set[int] = set()

    for _ in range(n):
        idx = _reserve_next_channel(tried)
        if idx is None:
            break
        tried.add(idx)
        ch = API_CHANNELS[idx]
        url, headers, body = payload_builder(ch)
        ch_label = ch.get("model_name", url)

        try:
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            except Exception as e:
                print(f"[WARN] Channel {ch_label} request error: {e}, trying next channel")
                last_error = str(e)
                _mark_channel_failure(idx, ch_label, f"request error: {e}")
                continue

            if not resp.ok:
                print(f"[WARN] Channel {ch_label} returned HTTP {resp.status_code}, switching to next channel")
                last_error = f"HTTP {resp.status_code}"
                _mark_channel_failure(idx, ch_label, f"HTTP {resp.status_code}")
                if DEBUG:
                    try:
                        _body_str = json.dumps(body, ensure_ascii=False)
                        # base64 content too long: print truncated
                        import re as _re
                        _body_debug = _re.sub(
                            r'("data:[^;]+;base64,)([A-Za-z0-9+/=]{200})[A-Za-z0-9+/=]+',
                            r'\1\2…<truncated>',
                            _body_str,
                        )
                        print(f"[DEBUG] Request body (base64 truncated):\n{_body_debug}")
                    except Exception:
                        pass
                    try:
                        print(f"[DEBUG] Response body:\n{resp.text}")
                    except Exception:
                        pass
                continue

            # 2xx success: if a parser is provided, try to parse
            if response_parser is not None:
                try:
                    parsed = response_parser(resp)
                except Exception as e:
                    print(f"[WARN] Channel {ch_label} response parse failed: {e}, switching to next channel")
                    last_error = str(e)
                    _mark_channel_failure(idx, ch_label, f"response parse failed: {e}")
                    continue
                _mark_channel_success(idx)
                return parsed

            # No parser: return response directly
            _mark_channel_success(idx)
            return resp
        finally:
            _release_channel(idx)

    # All channels failed
    raise RuntimeError(
        f"All {n} channels failed (last error: {last_error}); please check your channel config"
    )


def call_vlm(image_path: Path) -> dict:
    try:
        img_b64 = encode_image_to_b64(image_path)
    except Exception as e:
        raise RuntimeError(f"Failed to read image: {e}")

    exif_info = read_exif(image_path)
    exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

    system_prompt = (
        "You are a \"personal photo album evaluation assistant\" skilled at understanding real photos and scoring them from both memory value and aesthetic perspectives.\n"
        "You will receive a photo (provided as a base64-encoded image). Your task is to:\n"
        "1) Describe the photo content in detail in English (about 30-100 words),\n"
        "2) Classify the photo into one or more types: people / children / cats / family / travel / landscape / food / pets / daily-life / document / clutter / other. A photo may belong to multiple types.\n"
        "3) Give a 0-100 \"memory score\" (memory_score), accurate to one decimal place,\n"
        "4) Give a 0-100 \"aesthetic score\" (beauty_score), accurate to one decimal place,\n"
        "5) Give a brief English reason (within 20-30 words).\n\n"

        "[Memory Score (memory_score) Evaluation Method]\n"
        "First determine the score range based on memory value, then refine:\n"
        "How to determine the memory_score range:\n"
        "- Garbage / random snapshot / meaningless record: below 40.0 (typically 0-25; if barely identifiable but no story, do not exceed 39.9).\n"
        "- Slightly memorable: centered at 65.0 (mostly 58.1-70.3).\n"
        "- Decent memory value: centered at 75 (mostly 68.7-82.4).\n"
        "- Particularly memorable, strongly worth keeping: centered at 85 (mostly 79.1-95.9).\n"
        "How to refine memory_score further (bonuses can stack):\n"
        "- People & relationships: prominent faces, human interaction, or a group photo -> significantly higher;\n"
        "- Event: birthday / gathering / ceremony / stage / obvious event -> slightly higher;\n"
        "- Rarity & irreproducibility: clearly \"this moment will not come again\" -> significantly higher;\n"
        "- Emotional intensity: laughter, tears, surprise, hugs, interaction, strong atmosphere -> slightly higher;\n"
        "- Information density: the image clearly conveys what happened -> slightly higher;\n"
        "- Beautiful scenery: dramatic natural landscape, or a well-composed image -> slightly higher;\n"
        "- Travel significance: away from home, landmarks, travel scenes -> slightly higher.\n\n"

        "- Image quality: blurry, out of focus, ghosting, unclear -> slightly lower.\n\n"

        "[Special Handling for Important Subjects]\n"
        "If the image contains: children / cats / pets, these subjects are more likely to have high memory value. Center at 75 and significantly raise the score.\n"

        "[Handling Clearly Low-Value Images]\n"
        "For the following low-value images, memory_score MUST be compressed to 0-25 (max 39):\n"
        "- Nude, vulgar, pornographic, or otherwise offensive content.\n"
        "- Bills, receipts, ads, random clutter, test images, screenshots, etc.\n\n"

        "[Aesthetic Score (beauty_score) Evaluation Method]\n"
        "The beauty score judges only visuals: composition, lighting, clarity, color, subject prominence.\n"
        "Do not let \"children / cat / travel\" topics hijack the beauty score: subject is not necessarily beautiful.\n\n"

        "Output STRICTLY as JSON, in the following format:\n"
        "{\n"
        "  \"caption\": \"...\",\n"
        "  \"type\": \"people/family/travel/... can include multiple types\",\n"
        "  \"memory_score\": a number 0.0-100.0, accurate to 1 decimal place,\n"
        "  \"beauty_score\": a number 0.0-100.0, accurate to 1 decimal place,\n"
        "  \"reason\": \"within 20-30 English words\"\n"
        "}\n"
        "Do not output any extra text, no comments."
    )

    user_text = (
        "Below is the photo. Please complete the task above based on the image itself.\n"
    )

    def _build(ch):
        headers = {"Content-Type": "application/json"}
        key = ch.get("api_key", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": ch["model_name"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}"
                            },
                        },
                    ],
                },
            ],
            "temperature": 0.2,
            "stream": False,
        }
        return ch["api_url"], headers, body

    def _parse_vlm_response(resp):
        """Parse a VLM response; raise on failure to trigger channel failover.

        Tolerant of: MiniMax-M3 reasoning blocks (`<think>...</think>` prefix),
        markdown ```json fences, and any prose before/after the JSON object.
        Strategy: try strict parse, then strip <think> block, then strip code
        fences, then extract the first top-level {...} block.
        """
        import json as _json
        import re as _re
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        # 1) Try strict parse
        try:
            return _json.loads(content)
        except Exception:
            pass

        # 2) Strip leading <think>...</think> reasoning block
        cleaned = _re.sub(
            r"^\s*<think>.*?</think>\s*", "", content, count=1, flags=_re.DOTALL
        ).strip()

        # 3) Strip markdown ```json ... ``` fences
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=_re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(1))
            except Exception:
                pass

        # 4) Strip surrounding fences and retry
        if cleaned.startswith("```"):
            cleaned = _re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = _re.sub(r"\s*```\s*$", "", cleaned)

        try:
            return _json.loads(cleaned)
        except Exception:
            pass

        # 5) Last resort: extract first top-level {...} block
        m = _re.search(r"\{.*\}", cleaned, flags=_re.DOTALL)
        if m:
            return _json.loads(m.group(0))

        raise _json.JSONDecodeError("no JSON found in VLM response", content, 0)

    result = _post_with_channel_fallback(_build, timeout=TIMEOUT, response_parser=_parse_vlm_response)

    return result, exif_info


def _compute_phash(image_path: Path, hash_size: int = 8) -> int:
    """Compute a perceptual dHash of an image. Returns an int (64-bit for hash_size=8).

    dHash: resize to (hash_size+1) x hash_size grayscale, then compare each
    pixel to its right neighbor. Bits form a hash where perceptually similar
    images produce hashes with small Hamming distance.
    """
    from PIL import Image
    try:
        img = Image.open(image_path)
        img = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    except Exception as e:
        raise RuntimeError(f"phash: cannot open/resize {image_path}: {e}")
    pixels = list(img.getdata())
    h = 0
    for row in range(hash_size):
        base = row * (hash_size + 1)
        for col in range(hash_size):
            if pixels[base + col] > pixels[base + col + 1]:
                h |= 1 << (row * hash_size + col)
    # SQLite INTEGER is signed int64; coerce unsigned 64-bit dhash to signed
    # so values with the high bit set don't overflow.
    if h >= 1 << 63:
        h -= 1 << 64
    return h


def _hamming_distance(h1: int, h2: int) -> int:
    if h1 == h2:
        return 0
    # Mask to 64 bits: hashes are stored sign-coerced (negative when the high
    # bit is set), and Python's XOR on mixed sign values yields a negative
    # result whose bin() representation undercounts. Masking both operands
    # restores the true unsigned popcount.
    mask = (1 << 64) - 1
    return bin((h1 & mask) ^ (h2 & mask)).count("1")


def _is_duplicate(new_phash: int, new_dt_str: str | None, conn) -> tuple[bool, str]:
    """Check whether (new_phash, new_dt_str) is a duplicate of any existing row.

    Returns (is_dup, reason). reason is "" if not a duplicate.
    Skips rows where phash IS NULL (older rows without a stored hash).
    """
    if not DEDUPE_ENABLED:
        return False, ""

    try:
        rows = conn.execute(
            "SELECT path, phash, exif_datetime FROM photo_scores WHERE phash IS NOT NULL"
        ).fetchall()
    except Exception:
        return False, ""

    from datetime import datetime, timedelta
    new_dt = None
    if new_dt_str:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S"):
            try:
                new_dt = datetime.strptime(new_dt_str, fmt)
                break
            except Exception:
                continue
    burst_window = timedelta(hours=DEDUPE_BURST_WINDOW_HOURS)

    for path, phash, exif_dt in rows:
        if phash is None:
            continue
        try:
            dist = _hamming_distance(new_phash, int(phash))
        except Exception:
            continue

        # Criterion 1: strict perceptual match
        if dist <= DEDUPE_PHASH_DUP_THRESHOLD:
            return True, f"perceptual dup of {path} (hamming={dist})"

        # Criterion 2: time window + looser perceptual match (burst detection)
        if new_dt and exif_dt:
            old_dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S"):
                try:
                    old_dt = datetime.strptime(str(exif_dt), fmt)
                    break
                except Exception:
                    continue
            if old_dt is not None:
                if abs(new_dt - old_dt) < burst_window and dist <= DEDUPE_PHASH_BURST_THRESHOLD:
                    delta = new_dt - old_dt
                    return True, (
                        f"burst dup of {path} (hamming={dist}, "
                        f"time_delta={delta.total_seconds():+.0f}s)"
                    )

    return False, ""


def _process_one_photo(path: Path, city_resolver, conn) -> dict | None:
    """Process one photo: dedup check, then VLM + EXIF.

    On success, returns a dict with all DB fields; on failure, returns None.
    Returns a special "duplicate" sentinel dict {"_duplicate": True, "reason": "..."}
    if the photo was rejected by the dedup check (caller handles it).
    This function only does compute + network IO (no DB writes); thread-safe.
    """
    t_photo_start = time.perf_counter()

    # ---- Early-bail: every VLM channel is in cooldown ----
    # Without this, a 429 storm burns through the whole batch (every photo
    # raises "All N channels failed", which costs 1-3s of wasted sync I/O
    # per photo even though the answer is deterministic). Once we hit this
    # state, the rest of the batch will not produce new results until the
    # cooldown expires -- so return a sentinel that tells the caller to
    # stop processing any more photos and exit the loop cleanly.
    try:
        import time as _t
        _now = _t.monotonic()
        if API_CHANNELS and all(_channel_cooldown_until[i] > _now for i in range(len(API_CHANNELS))):
            return {"_all_channels_in_cooldown": True, "path": str(path)}
    except Exception:
        pass

    # ---- Duplicate / near-duplicate pre-check ----
    if DEDUPE_ENABLED:
        try:
            new_phash = _compute_phash(path)
        except Exception as e:
            # If we can't compute a hash, log and proceed (don't block on this)
            print(f"[WARN] phash failed for {path}: {e}")
            new_phash = None

        if new_phash is not None:
            # We need the EXIF datetime for the burst window check. Reuse
            # the exif extractor, but be tolerant of failure.
            exif_dt_str: str | None = None
            try:
                exif_info_quick = read_exif(path)
                exif_dt_str = exif_info_quick.get("datetime")
            except Exception:
                pass

            is_dup, reason = _is_duplicate(new_phash, exif_dt_str, conn)
            if is_dup:
                print(f"[DEDUP] skip {path.name}: {reason}")
                if DEDUPE_DELETE_CACHE:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                return {"_duplicate": True, "_phash": new_phash, "reason": reason, "path": str(path)}

    try:
        result, exif_info = call_vlm(path)
    except Exception as e:
        print(f"[WARN] Model call failed: {e}")
        return None

    caption = str(result.get("caption", "")).strip()
    ptype = str(result.get("type", "")).strip()
    try:
        memory_score = float(result.get("memory_score", 0.0))
    except Exception:
        memory_score = 0.0
    try:
        beauty_score = float(result.get("beauty_score", 0.0))
    except Exception:
        beauty_score = 0.0
    reason = str(result.get("reason", "")).strip()

    side_caption = generate_side_caption(path)

    width = exif_info.get("width")
    height = exif_info.get("height")
    orientation = exif_info.get("orientation")

    exif_datetime = exif_info.get("datetime")
    exif_make = exif_info.get("make")
    exif_model_val = exif_info.get("model")

    def _to_int(v):
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    def _to_float(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    exif_iso = _to_int(exif_info.get("iso"))
    exif_exposure_time = _to_float(exif_info.get("exposure_time"))
    exif_f_number = _to_float(exif_info.get("f_number"))
    exif_focal_length = _to_float(exif_info.get("focal_length"))
    exif_gps_lat = _to_float(exif_info.get("gps_lat"))
    exif_gps_lon = _to_float(exif_info.get("gps_lon"))
    exif_gps_alt = _to_float(exif_info.get("gps_alt"))

    if exif_gps_lat is not None and exif_gps_lon is not None:
        exif_city = city_resolver(exif_gps_lat, exif_gps_lon)
    else:
        exif_city = ""

    lat = exif_info.get("gps_lat")
    lon = exif_info.get("gps_lon")
    if lat is not None and lon is not None and not in_home(lat, lon):
        memory_score = min(memory_score + 5.0, 100.0)

    t_photo_end = time.perf_counter()

    # Look up the Immich asset id (if this photo came from Immich) so the
    # DB row can be traced back to the source even if the cache is cleared.
    immich_asset_id: str | None = None
    if USE_IMMICH:
        asset = IMMICH_ASSET_INDEX.get(str(path))
        if asset:
            immich_asset_id = str(asset.get("id") or "") or None

    return {
        "path": str(path),
        "caption": caption,
        "type": ptype,
        "memory_score": memory_score,
        "beauty_score": beauty_score,
        "reason": reason,
        "width": width,
        "height": height,
        "orientation": orientation,
        "exif_json": json.dumps(exif_info, ensure_ascii=False, default=str),
        "raw_json": json.dumps(result, ensure_ascii=False),
        "exif_datetime": exif_datetime,
        "exif_make": exif_make,
        "exif_model": exif_model_val,
        "exif_iso": exif_iso,
        "exif_exposure_time": exif_exposure_time,
        "exif_f_number": exif_f_number,
        "exif_focal_length": exif_focal_length,
        "exif_gps_lat": exif_gps_lat,
        "exif_gps_lon": exif_gps_lon,
        "exif_gps_alt": exif_gps_alt,
        "side_caption": side_caption,
        "exif_city": exif_city,
        "immich_asset_id": immich_asset_id,
        "phash": new_phash,
        "cost": t_photo_end - t_photo_start,
    }


def _save_result_to_db(cur, conn, rec: dict):
    """Write a single processing result to the database.

    Column/placeholder/value alignment (27 columns total):
      INSERT ... (path, caption, type, memory_score, beauty_score, reason,
                  width, height, orientation, used_at,
                  exif_json, raw_json,
                  exif_datetime, exif_make, exif_model,
                  exif_iso, exif_exposure_time, exif_f_number, exif_focal_length,
                  exif_gps_lat, exif_gps_lon, exif_gps_alt,
                  side_caption, exif_city,
                  immich_asset_id, phash, people_json)
      VALUES (?, ?, ?, ?, ?, ?,
              ?, ?, ?, COALESCE((SELECT used_at FROM photo_scores WHERE path = ?), NULL),
              ?, ?,
              ?, ?, ?,
              ?, ?, ?, ?,
              ?, ?, ?,
              COALESCE((SELECT side_caption FROM photo_scores
                        WHERE immich_asset_id = ? OR path = ?), ?),
              ?,
              ?, ?, ?)

    29 placeholders -> 29 bound values below.
    """
    cur.execute(
        """
        INSERT OR REPLACE INTO photo_scores
        (path, caption, type, memory_score, beauty_score, reason,
         width, height, orientation, used_at,
         exif_json, raw_json,
         exif_datetime, exif_make, exif_model,
         exif_iso, exif_exposure_time, exif_f_number, exif_focal_length,
         exif_gps_lat, exif_gps_lon, exif_gps_alt,
         side_caption, exif_city,
         immich_asset_id, phash, people_json)
        VALUES (?, ?, ?, ?, ?, ?,
                ?, ?, ?, COALESCE((SELECT used_at FROM photo_scores WHERE path = ?), NULL),
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                COALESCE((SELECT side_caption FROM photo_scores
                          WHERE immich_asset_id = ? OR path = ?), ?),
                ?,
                ?, ?, ?)
        """,
        (
            rec["path"],
            rec["caption"],
            rec["type"],
            rec["memory_score"],
            rec["beauty_score"],
            rec["reason"],
            rec["width"],
            rec["height"],
            rec["orientation"],
            # used_at COALESCE: lookup key only (no extra value consumed beyond the SELECT key itself).
            rec["path"],
            rec["exif_json"],
            rec["raw_json"],
            rec["exif_datetime"],
            rec["exif_make"],
            rec["exif_model"],
            rec["exif_iso"],
            rec["exif_exposure_time"],
            rec["exif_f_number"],
            rec["exif_focal_length"],
            rec["exif_gps_lat"],
            rec["exif_gps_lon"],
            rec["exif_gps_alt"],
            # side_caption COALESCE: lookup key (asset_id), lookup key (path), new value.
            rec.get("immich_asset_id"),
            rec["path"],
            rec["side_caption"],
            rec["exif_city"],
            rec.get("immich_asset_id"),
            rec.get("phash"),
            json.dumps(_lookup_people_json(rec.get("immich_asset_id")), ensure_ascii=False),
        ),
    )
    conn.commit()


def _lookup_people_json(asset_id):
    """Return the [{id, name}] list for an asset from IMMICH_FACE_INDEX."""
    try:
        return globals().get("IMMICH_FACE_INDEX", {}).get(asset_id, [])
    except Exception:
        return []


def _print_result(rec: dict):
    """Print a summary of a single photo's processing result."""
    print(f" type     : {rec['type']}")
    print(f" memory   : {rec['memory_score']:.1f}")
    print(f" beauty   : {rec['beauty_score']:.1f}")
    if rec["side_caption"]:
        print(f" caption  : {rec['side_caption']}")
    else:
        print(" caption  : (none)")
    print(f" describe : {rec['caption']}")
    print(f" reason   : {rec['reason']}")


def _verify_system(conn):
    """Self-check: report on DB/cache integrity, phash coverage, dedup effectiveness."""
    print("=" * 60)
    print("[verify] self-check report")
    print("=" * 60)
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0]
    with_phash = cur.execute("SELECT COUNT(*) FROM photo_scores WHERE phash IS NOT NULL").fetchone()[0]
    without_phash = total - with_phash
    print(f"\n[DB] total rows         : {total}")
    print(f"[DB] with phash          : {with_phash}")
    print(f"[DB] without phash       : {without_phash}  (run --backfill-phash to fix)")

    # Check cache directory state
    if USE_IMMICH and IMMICH_DOWNLOAD_DIR is not None:
        cache_root = IMMICH_DOWNLOAD_DIR
    else:
        cache_root = IMAGE_DIR
    if cache_root.exists():
        cache_files = list(cache_root.rglob("*"))
        cache_files = [p for p in cache_files if p.is_file()]
    else:
        cache_files = []
    print(f"\n[cache] root            : {cache_root}")
    print(f"[cache] files            : {len(cache_files)}")

    # Find orphans: cache files not referenced in DB
    db_paths = {row[0] for row in cur.execute("SELECT path FROM photo_scores").fetchall()}
    orphans = [p for p in cache_files if str(p) not in db_paths]
    print(f"[cache] orphans (in dir, not in DB) : {len(orphans)}")
    if orphans and len(orphans) <= 10:
        for p in orphans[:10]:
            print(f"  - {p}")

    # Find dead DB rows: DB entries whose file no longer exists
    dead = []
    for row in cur.execute("SELECT path FROM photo_scores").fetchall():
        p = Path(row[0])
        if not p.exists():
            dead.append(row[0])
    print(f"\n[DB] dead rows (file missing on disk) : {len(dead)}")
    if dead and len(dead) <= 10:
        for p in dead[:10]:
            print(f"  - {p}")

    # Dedup stats: phash distribution
    if with_phash > 0:
        # Group by 4-bit phash prefix as a rough clustering
        cluster_size: dict[str, int] = {}
        for (ph,) in cur.execute("SELECT phash FROM photo_scores WHERE phash IS NOT NULL").fetchall():
            # top 8 bits of phash as a rough cluster key
            key = f"{(int(ph) >> 56) & 0xFF:02x}"
            cluster_size[key] = cluster_size.get(key, 0) + 1
        big_clusters = {k: v for k, v in cluster_size.items() if v > 1}
        print(f"\n[dedup] phash top-byte clusters with > 1 image: {len(big_clusters)}")
        if big_clusters:
            top = sorted(big_clusters.items(), key=lambda kv: -kv[1])[:5]
            for k, v in top:
                print(f"  cluster {k}: {v} images (likely candidates for duplicate check)")

    # Threshold report
    print(f"\n[dedup] settings")
    print(f"  enabled                : {DEDUPE_ENABLED}")
    print(f"  phash dup threshold    : {DEDUPE_PHASH_DUP_THRESHOLD}")
    print(f"  phash burst threshold  : {DEDUPE_PHASH_BURST_THRESHOLD}")
    print(f"  burst window (hours)   : {DEDUPE_BURST_WINDOW_HOURS}")
    print(f"  delete cache on dup    : {DEDUPE_DELETE_CACHE}")
    print()


def _find_dups(conn):
    """Scan the DB for near-duplicate groups, report only (no I/O changes)."""
    print("=" * 60)
    print("[find-dups] scanning for duplicate groups")
    print("=" * 60)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT path, phash, exif_datetime FROM photo_scores WHERE phash IS NOT NULL"
    ).fetchall()
    if not rows:
        print("[find-dups] no rows with phash; nothing to do. Run --backfill-phash first.")
        return

    # Build clusters: pair-wise within dedup window
    from datetime import datetime, timedelta
    def _parse_dt(s):
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    items = []
    for path, ph, dt in rows:
        items.append({"path": path, "phash": int(ph), "dt": _parse_dt(dt)})

    # Sort by datetime (None last) to enable O(n) cluster detection
    items.sort(key=lambda x: (x["dt"] is None, x["dt"] or datetime.min))
    burst_window = timedelta(hours=DEDUPE_BURST_WINDOW_HOURS)

    # Two-tier clustering:
    #   * Strict (hamming <= DEDUPE_PHASH_DUP_THRESHOLD) - any time
    #   * Burst (within time window + hamming <= DEDUPE_PHASH_BURST_THRESHOLD)
    parent = list(range(len(items)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Burst pass: O(n) since items are sorted by time
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a["dt"] and b["dt"]:
                if b["dt"] - a["dt"] > burst_window:
                    break
            d = _hamming_distance(a["phash"], b["phash"])
            if d <= DEDUPE_PHASH_BURST_THRESHOLD:
                union(i, j)

    # Strict pass: full pairwise
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            d = _hamming_distance(items[i]["phash"], items[j]["phash"])
            if d <= DEDUPE_PHASH_DUP_THRESHOLD:
                union(i, j)

    # Group by root parent
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        r = find(i)
        groups.setdefault(r, []).append(i)

    dup_groups = [g for g in groups.values() if len(g) > 1]
    print(f"\n[find-dups] {len(items)} images scanned, {len(dup_groups)} duplicate groups found\n")
    for gi, g in enumerate(dup_groups[:20], start=1):
        print(f"  group {gi} ({len(g)} images):")
        for idx in g:
            it = items[idx]
            dts = it["dt"].strftime("%Y-%m-%d %H:%M") if it["dt"] else "?"
            print(f"    [{dts}] hamming-vs-first={_hamming_distance(items[g[0]]['phash'], it['phash']):2d}  {it['path']}")
        if gi == 20 and len(dup_groups) > 20:
            print(f"  ... ({len(dup_groups) - 20} more groups)")
    if not dup_groups:
        print("[find-dups] no duplicates found at current thresholds.")


def _backfill_phash(conn):
    """Compute phash for every DB row that doesn't have one. No VLM calls."""
    print("=" * 60)
    print("[backfill-phash] computing phash for rows without one")
    print("=" * 60)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT path FROM photo_scores WHERE phash IS NULL"
    ).fetchall()
    total = len(rows)
    if total == 0:
        print("[backfill-phash] nothing to do.")
        return
    print(f"[backfill-phash] {total} rows to process")

    done = 0
    skipped = 0
    for i, (path,) in enumerate(rows, start=1):
        p = Path(path)
        if not p.exists():
            skipped += 1
            continue
        try:
            h = _compute_phash(p)
            cur.execute("UPDATE photo_scores SET phash = ? WHERE path = ?", (h, path))
            done += 1
        except Exception as e:
            print(f"[backfill-phash] failed {path}: {e}")
            skipped += 1
        if i % 50 == 0:
            print(f"  ... {i}/{total} ({done} done, {skipped} skipped)")
    conn.commit()
    print(f"[backfill-phash] done. {done} updated, {skipped} skipped.")


def _report_dedup_stats(conn):
    """Show how effective dedup has been, and current threshold config."""
    print("=" * 60)
    print("[dedup-stats] configuration and current state")
    print("=" * 60)
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0]
    with_phash = cur.execute("SELECT COUNT(*) FROM photo_scores WHERE phash IS NOT NULL").fetchone()[0]
    print(f"\n[state]")
    print(f"  total rows                : {total}")
    print(f"  rows with phash           : {with_phash}  ({with_phash / total * 100:.1f}%)" if total else "  rows with phash           : 0")
    print(f"\n[thresholds]")
    print(f"  enabled                   : {DEDUPE_ENABLED}")
    print(f"  strict (hamming <= )      : {DEDUPE_PHASH_DUP_THRESHOLD}")
    print(f"  burst (hamming <= + time) : {DEDUPE_PHASH_BURST_THRESHOLD}")
    print(f"  burst window (hours)      : {DEDUPE_BURST_WINDOW_HOURS}")
    print(f"  delete cache on hit       : {DEDUPE_DELETE_CACHE}")
    print(f"\n[note]")
    print(f"  per-run dedup-skip count is logged in each analyze-*.log as [DEDUP] lines.")
    print(f"  after running the analyzer a few times you can grep for 'skip' in logs/ to count.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Analyze photos and generate scores")
    parser.add_argument("--cache", action="store_true",
                        help="Debug only: cache the file list to skip directory scanning; not for production sync")
    parser.add_argument("-j", "--concurrency", type=int, default=1,
                        help="Number of concurrent worker threads (default 1, i.e. serial)")
    parser.add_argument("--debug", action="store_true",
                        help="Debug mode: print request and response body on failure")
    parser.add_argument("--verify", action="store_true",
                        help="Self-check: scan DB + cache, report orphans/missing/dedup stats. Exits after report.")
    parser.add_argument("--find-dups", action="store_true",
                        help="Find near-duplicate groups in the existing DB without re-analyzing. Exits after report.")
    parser.add_argument("--backfill-phash", action="store_true",
                        help="Compute phash for all DB rows missing one (no VLM calls). Exits after.")
    parser.add_argument("--dedup-stats", action="store_true",
                        help="Print dedup statistics: skipped vs processed, thresholds in effect. Exits after.")
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        print("[INFO] Debug mode enabled")

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        print(f"[INFO] Concurrent mode: {concurrency} worker threads")

    # ---- Self-check / verify modes (exit before any actual analysis) ----
    if any([args.verify, args.find_dups, args.backfill_phash, args.dedup_stats]):
        conn_check = sqlite3.connect(str(DB_PATH))
        try:
            if args.dedup_stats:
                _report_dedup_stats(conn_check)
            elif args.verify:
                _verify_system(conn_check)
            elif args.find_dups:
                _find_dups(conn_check)
            elif args.backfill_phash:
                _backfill_phash(conn_check)
        finally:
            conn_check.close()
        return

    filelist_path = ROOT_DIR / "filelist.txt"
    cache_path = ROOT_DIR / ".filelist_cache.txt"

    if args.cache:
        print("[WARN] --cache is only recommended for debug speedups; not for production.")
        print("[WARN] Using cache skips re-scanning: new photos will not be discovered, and records of deleted photos may remain in the DB.")

    if args.cache and cache_path.exists():
        print(f"[INFO] Reading cached file list: {cache_path}")
        cached = cache_path.read_text(encoding="utf-8").strip().splitlines()
        imgs = [Path(p) for p in cached if p.strip()]
        print(f"[INFO] Loaded {len(imgs)} files from cache.")
    else:
        print("[INFO] Scanning image directory...")
        imgs = list_images()
        if args.cache:
            cache_path.write_text("\n".join(str(p) for p in imgs), encoding="utf-8")
            print(f"[INFO] Wrote cache file: {cache_path}")

    filelist_path.write_text("\n".join(str(p) for p in imgs), encoding="utf-8")
    print(f"[INFO] Updated filelist.txt, {len(imgs)} files total.")
    if not imgs:
        raise SystemExit(f"No image files in directory: {IMAGE_DIR}")

    imgs = [p for p in imgs if not is_screenshot(p)]
    if not imgs:
        raise SystemExit("[INFO] All images were excluded by the Screenshot filter; nothing to process.")

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    ensure_table(conn)
    city_resolver = get_city_resolver()

    # =======================
    # Sync delete: remove DB rows for files no longer on disk/NAS
    # Only operate on records with the current IMAGE_DIR prefix, to avoid touching other historical paths.
    # =======================
    image_dir_prefix = str(IMAGE_DIR)

    try:
        # Use a temp table to avoid SQLite param limit issues with very long IN (...)
        conn.execute("DROP TABLE IF EXISTS _temp_existing_paths")
        conn.execute("CREATE TEMP TABLE _temp_existing_paths (path TEXT PRIMARY KEY)")

        # Batch-insert the current scanned file list
        CHUNK = 2000
        total_files = len(imgs)
        inserted = 0
        for i in range(0, total_files, CHUNK):
            chunk = imgs[i : i + CHUNK]
            conn.executemany(
                "INSERT OR IGNORE INTO _temp_existing_paths(path) VALUES (?)",
                [(str(p),) for p in chunk],
            )
            inserted += len(chunk)
            if inserted % 10000 == 0:
                print(f"[CLEAN] Wrote existing-file list: {inserted}/{total_files} ...")

        # Delete: rows in DB whose file is no longer on disk
        cur_clean = conn.cursor()
        before_cnt = cur_clean.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ?",
            (image_dir_prefix + "%",),
        ).fetchone()[0]

        cur_clean.execute(
            """
            DELETE FROM photo_scores
            WHERE path LIKE ?
              AND NOT EXISTS (
                    SELECT 1 FROM _temp_existing_paths t
                    WHERE t.path = photo_scores.path
              )
            """,
            (image_dir_prefix + "%",),
        )
        deleted = cur_clean.rowcount if cur_clean.rowcount is not None else 0
        conn.commit()

        after_cnt = cur_clean.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ?",
            (image_dir_prefix + "%",),
        ).fetchone()[0]

        if deleted > 0:
            print(f"[CLEAN] Pruned {deleted} stale DB rows (current dir: {before_cnt} -> {after_cnt}).")
        else:
            print("[CLEAN] DB and disk are in sync; no cleanup needed.")

    except Exception as e:
        # Cleanup failure should not block the main flow
        print(f"[WARN] DB cleanup failed (ignored, does not affect main flow): {e}")

    cur_test = conn.cursor()
    # Only count analyzed photos under the current IMAGE_DIR; ignore other paths/historical entries in progress
    counted = cur_test.execute(
        "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ?",
        (image_dir_prefix + "%",),
    ).fetchone()[0]
    print(f"[INFO] DB already has {counted} analyzed photos (current dir only).")

    target_paths = filter_unscored(conn, imgs)
    if not target_paths:
        print("[INFO] All images are already in photo_scores.")
        conn.close()
        return

    if BATCH_LIMIT is not None:
        target_paths = target_paths[:BATCH_LIMIT]

    # Progress bar: based on the “snapshot at startup”.
    # total = already-analyzed (current dir) + to-process-this-run (from filter_unscored)
    already_done = counted
    total = already_done + len(target_paths)
    print(f"[INFO] This run will process {len(target_paths)} images (snapshot total {total}, already analyzed {already_done}).")

    cur = conn.cursor()
    db_lock = threading.Lock()   # protect SQLite write operations
    start_time = time.time()

    if concurrency <= 1:
        # ==================== Serial mode (original logic) ====================
        for idx, path in enumerate(target_paths, start=1):
            t_photo_start = time.perf_counter()
            sep = "=" * 60
            print("\n" + sep)
            print(f"[{idx}/{len(target_paths)}] processing: {path}")

            rec = _process_one_photo(path, city_resolver, conn)
            if rec is None:
                continue
            if rec.get("_duplicate"):
                # Duplicate was already logged by the dedup check; just skip
                continue
            if rec.get("_all_channels_in_cooldown"):
                # Every VLM channel is in cooldown; further photos would just
                # burn sync I/O. Stop the batch cleanly and let the next run
                # pick up where we left off.
                print(f"[WARN] All VLM channels are in cooldown; stopping batch at {idx}/{len(target_paths)}.")
                print("[WARN] Next run will retry these photos when the cooldown expires.")
                break

            _print_result(rec)
            _save_result_to_db(cur, conn, rec)

            t_photo_end = time.perf_counter()
            total_cost = t_photo_end - t_photo_start

            processed_now = already_done + idx
            denom = total if total > 0 else 1
            progress = max(0.0, min(1.0, processed_now / denom))

            bar_width = 30
            filled = int(bar_width * progress)
            bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

            elapsed = time.time() - start_time
            avg_per = elapsed / idx if idx > 0 else 0
            remaining = max(total - processed_now, 0)
            eta = format_eta(remaining * avg_per) if avg_per > 0 else "00:00:00"

            print(f"[progress] {bar} {progress*100:5.1f}% {processed_now}/{total} last {total_cost:4.1f}s eta {eta} ")
    else:
        # ==================== Concurrent mode ====================
        print_lock = threading.Lock()
        completed_count = 0
        completed_lock = threading.Lock()

        def _worker(idx: int, path: Path) -> tuple[int, Path, dict | None]:
            return idx, path, _process_one_photo(path, city_resolver, conn)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(_worker, idx, path): (idx, path)
                for idx, path in enumerate(target_paths, start=1)
            }

            bail = False
            for future in as_completed(futures):
                idx, path, rec = future.result()

                with completed_lock:
                    completed_count += 1
                    done_so_far = completed_count

                with print_lock:
                    sep = "=" * 60
                    print("\n" + sep)
                    print(f"[{done_so_far}/{len(target_paths)}] done: {path}")

                    if rec is not None:
                        if rec.get("_duplicate"):
                            # Already logged by dedup check
                            pass
                        elif rec.get("_all_channels_in_cooldown"):
                            # All VLM channels are in cooldown. Subsequent
                            # futures will hit the same sentinel, so cancel
                            # the remaining work to avoid wasting CPU/network.
                            bail = True
                            print(f"[WARN] All VLM channels in cooldown (detected at idx {done_so_far}/{len(target_paths)}); cancelling remaining work.")
                        else:
                            _print_result(rec)
                            with db_lock:
                                _save_result_to_db(cur, conn, rec)
                    else:
                        print(" (failed, skipped)")

                    processed_now = already_done + done_so_far
                    denom = total if total > 0 else 1
                    progress = max(0.0, min(1.0, processed_now / denom))

                    bar_width = 30
                    filled = int(bar_width * progress)
                    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

                    elapsed = time.time() - start_time
                    avg_per = elapsed / done_so_far if done_so_far > 0 else 0
                    remaining = max(total - processed_now, 0)
                    eta = format_eta(remaining * avg_per) if avg_per > 0 else "00:00:00"

                    cost_str = f"{rec['cost']:4.1f}s" if rec else "N/A"
                    print(f"[progress] {bar} {progress*100:5.1f}% {processed_now}/{total} last {cost_str} eta {eta} ")

                if bail:
                    # Drain pending futures without printing progress for them.
                    for f in futures:
                        f.cancel()
                    break

    conn.close()
    print("\n[done] This batch finished.")


if __name__ == "__main__":
    require_exiftool()
    main()
