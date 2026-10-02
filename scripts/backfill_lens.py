#!/usr/bin/env python3
"""Backfill lens + file size + resolution into photos.db from Immich exifInfo."""
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg
from immich_client import ImmichConfig, ImmichClient

DB_PATH = Path(getattr(cfg, "DB_PATH", "./data/photos.db") or "./data/photos.db")
if not DB_PATH.is_absolute():
    DB_PATH = Path(__file__).resolve().parent.parent / DB_PATH

def main():
    client = ImmichClient(ImmichConfig.from_config_module(cfg))
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    # add columns if missing
    cols = [r[1] for r in c.execute("PRAGMA table_info(photo_scores)").fetchall()]
    if "exif_lens" not in cols:
        c.execute("ALTER TABLE photo_scores ADD COLUMN exif_lens TEXT")
    if "exif_file_size" not in cols:
        c.execute("ALTER TABLE photo_scores ADD COLUMN exif_file_size INTEGER")
    conn.commit()

    missing = c.execute(
        "SELECT immich_asset_id FROM photo_scores "
        "WHERE immich_asset_id IS NOT NULL AND immich_asset_id != '' "
        "AND (exif_lens IS NULL OR exif_file_size IS NULL)"
    ).fetchall()
    print(f"[INFO] {len(missing)} assets need lens/size backfill", flush=True)

    done = 0
    for (aid,) in missing:
        try:
            info = client.get_asset_info(aid)
        except Exception as e:
            print(f"[WARN] {aid}: {e}", flush=True)
            continue
        ei = info.get("exifInfo") or {}
        lens = ei.get("lensModel")
        fsize = ei.get("fileSizeInByte")
        w = ei.get("exifImageWidth")
        h = ei.get("exifImageHeight")
        make = ei.get("make")
        model = ei.get("model")
        iso = ei.get("iso")
        fnum = ei.get("fNumber")
        etime = ei.get("exposureTime")
        flen = ei.get("focalLength")
        c.execute(
            "UPDATE photo_scores SET exif_lens=?, exif_file_size=?, width=?, height=?, "
            "exif_make=?, exif_model=?, exif_iso=?, exif_f_number=?, "
            "exif_exposure_time=?, exif_focal_length=? WHERE immich_asset_id=?",
            (lens, fsize, w, h, make, model, iso, fnum, etime, flen, aid),
        )
        done += 1
        if done % 200 == 0:
            conn.commit()
            print(f"[INFO] {done} updated...", flush=True)
    conn.commit()
    conn.close()
    print(f"[DONE] updated={done}")

if __name__ == "__main__":
    main()
