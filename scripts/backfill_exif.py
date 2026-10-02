#!/usr/bin/env python3
"""
Backfill EXIF specs (ISO / exposure / aperture / focal length) for all
photos in photos.db from Immich's exifInfo API.

Why: the analyze pipeline reads EXIF from cached *preview* files, which
Immich generates without EXIF, so only ~17/7736 photos have specs.
Immich's metadata table has the real values for every asset — pull them
via the paginated list_assets() sweep, no per-asset API calls, no image
downloads.

Idempotent: only updates rows where specs are missing/None.
Run inside the InkTime container: docker exec InkTime python3 /app/scripts/backfill_exif.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from immich_client import ImmichConfig, ImmichClient

DB_PATH = Path(getattr(cfg, "DB_PATH", "./data/photos.db") or "./data/photos.db")
if not DB_PATH.is_absolute():
    DB_PATH = Path(__file__).resolve().parent.parent / DB_PATH


def _parse_float(v):
    """exposureTime may be '1/391' (string fraction) or a float."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if "/" in s:
            num, _, den = s.partition("/")
            try:
                return float(num) / float(den)
            except Exception:
                return None
        try:
            return float(s)
        except Exception:
            return None
    try:
        return float(v)
    except Exception:
        return None


def main() -> None:
    client = ImmichClient(ImmichConfig.from_config_module(cfg))
    conn = sqlite3.connect(str(DB_PATH))

    # Map asset_id -> current DB spec values, only for rows needing specs
    missing_ids = {
        r[0]
        for r in conn.execute(
            "SELECT immich_asset_id FROM photo_scores "
            "WHERE immich_asset_id IS NOT NULL AND immich_asset_id != '' "
            "AND (exif_iso IS NULL OR exif_f_number IS NULL "
            "     OR exif_exposure_time IS NULL OR exif_focal_length IS NULL)"
        ).fetchall()
    }
    print(f"[INFO] {len(missing_ids)} assets need EXIF backfill", flush=True)
    if not missing_ids:
        print("[DONE] nothing to do")
        return

    done = 0
    no_metadata = 0
    for asset in client.list_assets():
        aid = asset.get("id")
        if not aid or aid not in missing_ids:
            continue
        ei = asset.get("exifInfo") or {}
        iso = ei.get("iso")
        fnum = ei.get("fNumber")
        etime = _parse_float(ei.get("exposureTime"))
        flen = ei.get("focalLength")
        if iso is None and fnum is None and etime is None and flen is None:
            no_metadata += 1
            continue
        try:
            conn.execute(
                "UPDATE photo_scores SET exif_iso=?, exif_f_number=?, "
                "exif_exposure_time=?, exif_focal_length=? WHERE immich_asset_id=?",
                (
                    int(iso) if iso is not None else None,
                    float(fnum) if fnum is not None else None,
                    etime,
                    float(flen) if flen is not None else None,
                    aid,
                ),
            )
            done += 1
        except Exception as e:
            print(f"[WARN] {aid}: {e}", flush=True)
        if done % 500 == 0 and done:
            conn.commit()
            print(f"[INFO] {done} updated...", flush=True)

    conn.commit()
    conn.close()
    print(f"[DONE] updated={done} no_metadata={no_metadata}")


if __name__ == "__main__":
    main()
