#!/usr/bin/env python3
"""Create a fresh photos.db with the full schema the pipeline expects.

The analyzer's own ensure_table() creates a base schema and then adds columns
one ALTER at a time, but two things it cannot know about live outside it:

- `photo_blacklist`, which the renderer LEFT JOINs and the analyzer checks.
  Neither creates it.
- `exif_focal_35mm`, `exif_lens`, `exif_file_size` and `album`, which were
  added by the backfill scripts on the original deployment and are read by the
  renderer but not created by ensure_table().

Running this once against a new database produces a schema that every script
in this repo can use. It is idempotent: existing columns and tables are kept.

Usage:
    python scripts/init_db.py [path/to/photos.db]

Defaults to DB_PATH from config.py, or ./photos.db.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import config as cfg
except ImportError:
    cfg = None


BASE_COLUMNS = {
    "path": "TEXT PRIMARY KEY",
    "caption": "TEXT",
    "type": "TEXT",
    "memory_score": "REAL",
    "beauty_score": "REAL",
    "reason": "TEXT",
    "width": "INTEGER",
    "height": "INTEGER",
    "orientation": "TEXT",
    "used_at": "TEXT",
    "exif_json": "TEXT",
    "raw_json": "TEXT",
    "exif_datetime": "TEXT",
    "exif_make": "TEXT",
    "exif_model": "TEXT",
    "exif_iso": "INTEGER",
    "exif_exposure_time": "REAL",
    "exif_f_number": "REAL",
    "exif_focal_length": "REAL",
    "exif_gps_lat": "REAL",
    "exif_gps_lon": "REAL",
    "exif_gps_alt": "REAL",
    "side_caption": "TEXT",
    "exif_city": "TEXT",
    "immich_asset_id": "TEXT",
    "phash": "INTEGER",
    # Added by the backfills on the original deployment; the renderer reads all
    # of them, so a fresh database needs them too.
    "exif_focal_35mm": "REAL",
    "exif_lens": "TEXT",
    "exif_file_size": "INTEGER",
    "album": "TEXT",
    "people_json": "TEXT",
}

EXTRA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_photo_scores_asset ON photo_scores(immich_asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_photo_scores_memory ON photo_scores(memory_score)",
    "CREATE INDEX IF NOT EXISTS idx_photo_scores_used ON photo_scores(used_at)",
    "CREATE INDEX IF NOT EXISTS idx_photo_scores_phash ON photo_scores(phash)",
]


def init(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    c.execute("CREATE TABLE IF NOT EXISTS photo_scores (path TEXT PRIMARY KEY)")

    existing = {row[1] for row in c.execute("PRAGMA table_info(photo_scores)").fetchall()}
    for name, decl in BASE_COLUMNS.items():
        if name not in existing:
            if name == "path":
                continue  # table always has the primary key
            c.execute(f"ALTER TABLE photo_scores ADD COLUMN {name} {decl}")

    # The renderer LEFT JOINs this; the analyzer reads it.
    c.execute(
        "CREATE TABLE IF NOT EXISTS photo_blacklist ("
        "immich_asset_id TEXT PRIMARY KEY, "
        "reason TEXT, "
        "created_at TEXT)"
    )

    # Face filter allowlist (used only when FACE_FILTER_ENABLED is true).
    c.execute(
        "CREATE TABLE IF NOT EXISTS face_allowlist ("
        "person_id TEXT PRIMARY KEY, "
        "name TEXT)"
    )

    for stmt in EXTRA_INDEXES:
        c.execute(stmt)

    conn.commit()
    cols = [row[1] for row in c.execute("PRAGMA table_info(photo_scores)").fetchall()]
    conn.close()
    print(f"[OK] {db_path}")
    print(f"     photo_scores: {len(cols)} columns")
    print("     tables: photo_scores, photo_blacklist, face_allowlist")


def main() -> None:
    if len(sys.argv) > 1:
        db_path = Path(sys.argv[1]).expanduser()
    elif cfg is not None:
        db_path = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent.parent / db_path
    else:
        db_path = Path(__file__).resolve().parent.parent / "photos.db"
    init(db_path)


if __name__ == "__main__":
    main()