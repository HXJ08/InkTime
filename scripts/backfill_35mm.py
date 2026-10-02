#!/usr/bin/env python3
"""
Backfill 35mm-equivalent focal length (EXIF tag 0xA405 FocalLengthIn35mmFilm)
for all photos in photos.db.

WHY: Immich's exifInfo only surfaces the *physical* focal length
(e.g. iPhone main camera = 6.765mm). Apple Photos (and the InkTime frame's
target look) shows the *35mm-equivalent* focal length (e.g. 24mm / 77mm),
which lives in raw EXIF tag 0xA405 only — Immich's API does not expose it.

For full-frame cameras (Sony A7M3, etc.) 0xA405 equals the physical focal
length, so the display stays correct either way. For crop-sensor and
smartphone photos it corrects 6.765mm -> 24mm.

Source of truth: the original file's raw EXIF (originals, not previews —
Immich previews are EXIF-stripped). So we download each original once.

Idempotent: skips rows that already have exif_focal_35mm.
Run inside InkTime container: docker exec InkTime python3 /app/scripts/backfill_35mm.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from immich_client import ImmichConfig, ImmichClient

DB_PATH = Path(getattr(cfg, "DB_PATH", "./data/photos.db") or "./data/photos.db")
if not DB_PATH.is_absolute():
    DB_PATH = Path(__file__).resolve().parent.parent / DB_PATH

# Register HEIC/HIF opener once (global, thread-safe enough for our use)
import pillow_heif
pillow_heif.register_heif_opener()
from PIL import Image
from PIL.ExifTags import TAGS


def extract_35mm_equiv(path: Path) -> float | None:
    """Return the 35mm-equivalent focal length from raw EXIF, or None."""
    try:
        img = Image.open(path)
        raw = img.info.get("exif", b"")
        if not raw:
            return None
        exif = Image.Exif()
        exif.load(raw)
        # FocalLengthIn35mmFilm is tag 0xA405, inside ExifIFD (0x8769)
        ifd = exif.get_ifd(0x8769)
        v = ifd.get(0xA405)
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def main() -> None:
    client = ImmichClient(ImmichConfig.from_config_module(cfg))
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()

    # Ensure column exists
    cols = [r[1] for r in c.execute("PRAGMA table_info(photo_scores)").fetchall()]
    if "exif_focal_35mm" not in cols:
        c.execute("ALTER TABLE photo_scores ADD COLUMN exif_focal_35mm REAL")
        conn.commit()

    # Only rows with a physical focal length (i.e. real photos, not screenshots)
    missing = c.execute(
        "SELECT immich_asset_id FROM photo_scores "
        "WHERE immich_asset_id IS NOT NULL AND immich_asset_id != '' "
        "AND exif_focal_length IS NOT NULL "
        "AND exif_focal_35mm IS NULL"
    ).fetchall()
    ids = [r[0] for r in missing]
    print(f"[INFO] {len(ids)} assets need 35mm-equiv backfill", flush=True)

    done = 0
    fallback = 0
    errors = 0

    def work(aid):
        tmp = Path(tempfile.mkstemp(suffix=".img", prefix="fl35_")[1])
        try:
            client.download_original(aid, tmp)
            eq = extract_35mm_equiv(tmp)
            return aid, eq
        except Exception as e:
            return aid, f"__ERR__:{e}"
        finally:
            tmp.unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(work, aid) for aid in ids]
        for fut in as_completed(futs):
            aid, eq = fut.result()
            if isinstance(eq, str) and eq.startswith("__ERR__"):
                errors += 1
                if errors <= 10:
                    print(f"[WARN] {aid}: {eq}", flush=True)
                continue
            # Fallback to physical focal length when 0xA405 absent (full-frame)
            if eq is None:
                phy = c.execute(
                    "SELECT exif_focal_length FROM photo_scores WHERE immich_asset_id=?",
                    (aid,),
                ).fetchone()
                eq = float(phy[0]) if phy and phy[0] is not None else None
                fallback += 1
            if eq is not None:
                c.execute(
                    "UPDATE photo_scores SET exif_focal_35mm=? WHERE immich_asset_id=?",
                    (eq, aid),
                )
                done += 1
            if (done + fallback) % 200 == 0 and (done + fallback) > 0:
                conn.commit()
                print(f"[INFO] {done} updated / {fallback} fallback...", flush=True)

    conn.commit()
    conn.close()
    print(f"[DONE] updated={done} fallback_to_physical={fallback} errors={errors}")


if __name__ == "__main__":
    main()
