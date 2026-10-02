"""Add missing album assets to photo_scores from Immich.

Set INKTIME_ALBUM_ID to the Immich album UUID you want to ingest, and
INKTIME_ALBUM to the tag stored in the `album` column (default "Post").
This is a one-off repair script: album assets are inserted with memory_score
0 and no city, so the analyzer will not score them until it next runs.
"""
import sys, sqlite3, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from immich_client import ImmichConfig, ImmichClient
import config as cfg
from PIL import Image
from PIL.ExifTags import TAGS

client = ImmichClient(ImmichConfig.from_config_module(cfg))
DB_PATH = Path(getattr(cfg, "DB_PATH", "./data/photos.db"))
if not DB_PATH.is_absolute():
    DB_PATH = Path(__file__).resolve().parent.parent / DB_PATH
CACHE_DIR = Path(getattr(cfg, "IMMICH_DOWNLOAD_DIR", "./cache/immich"))
if not CACHE_DIR.is_absolute():
    CACHE_DIR = Path(__file__).resolve().parent.parent / CACHE_DIR

album_id = os.environ.get("INKTIME_ALBUM_ID", "")
ALBUM_TAG = os.environ.get("INKTIME_ALBUM", "Post")
if not album_id:
    raise SystemExit("Set INKTIME_ALBUM_ID=<immich album uuid> before running.")

# Get all Post album assets
body = {'albumIds': [album_id], 'isArchived': False}
res = client._request_json('POST', '/api/search/metadata', json=body)
assets = res.get('assets', {}).get('items', [])
print(f'Post album: {len(assets)} assets')

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Check which are already in DB
existing = set()
for row in c.execute("SELECT immich_asset_id FROM photo_scores WHERE immich_asset_id IS NOT NULL"):
    existing.add(row[0])

added = 0
for a in assets:
    aid = a.get('id', '')
    if aid in existing:
        continue

    # Download thumbnail
    ext = '.jpg'
    mime = a.get('originalMimeType', '')
    if 'heic' in mime: ext = '.heic'
    elif 'png' in mime: ext = '.png'
    elif 'dng' in mime or 'raw' in mime: ext = '.dng'

    cache_path = CACHE_DIR / f'{aid}{ext}'
    if not cache_path.exists():
        try:
            client.download_preview(aid, cache_path)
        except Exception as e:
            print(f'  SKIP {aid[:8]}: download failed: {e}')
            continue

    # Extract EXIF
    exif_data = {}
    try:
        img = Image.open(cache_path)
        raw_exif = img._getexif() or {}
        for tag_id, val in raw_exif.items():
            tag = TAGS.get(tag_id, tag_id)
            exif_data[str(tag)] = val
    except Exception:
        pass

    # Get date
    date_str = ''
    for key in ['DateTimeOriginal', 'DateTimeDigitized', 'DateTime']:
        if key in exif_data:
            d = str(exif_data[key])
            date_str = d[:10].replace(':', '-')
            break

    # Get coords
    lat = lon = None
    gps = exif_data.get('GPSInfo')
    if gps:
        try:
            def _dms(v):
                d, m, s = v
                return float(d) + float(m)/60 + float(s)/3600
            lat = _dms(gps[2])
            lon = _dms(gps[4])
            if gps[1] == 'S': lat = -lat
            if gps[3] == 'W': lon = -lon
        except: pass

    # Get dimensions
    w = a.get('exifImageWidth') or 0
    h = a.get('exifImageHeight') or 0

    # Get EXIF fields
    iso = exif_data.get('ISOSpeedRatings')
    fnum = exif_data.get('FNumber')
    if fnum: fnum = float(fnum)
    et = exif_data.get('ExposureTime')
    if et:
        try: et = float(et[0]) / float(et[1])
        except: et = float(et)
    focal = exif_data.get('FocalLength')
    if focal:
        try: focal = float(focal)
        except: pass

    # Get make/model/lens
    make = str(exif_data.get('Make', '') or '')
    model = str(exif_data.get('Model', '') or '')
    lens = str(exif_data.get('LensModel', '') or '')

    # 35mm equiv
    focal35 = exif_data.get('FocalLengthIn35mmFilm')

    # Build exif_json
    exif_json = json.dumps(exif_data, default=str)[:5000]

    c.execute("""
        INSERT INTO photo_scores
            (path, exif_json, side_caption, memory_score,
             exif_gps_lat, exif_gps_lon, exif_city,
             immich_asset_id, used_at,
             exif_iso, exif_f_number, exif_exposure_time, exif_focal_35mm,
             exif_make, exif_model, exif_lens,
             width, height, exif_file_size, album)
        VALUES (?, ?, NULL, 0,
                ?, ?, NULL,
                ?, NULL,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?)
    """, (
        str(cache_path), exif_json,
        lat, lon, aid,
        iso, fnum, et, focal35,
        make, model, lens,
        w, h, os.path.getsize(cache_path) if cache_path.exists() else 0,
        ALBUM_TAG,
    ))
    added += 1
    print(f'  Added: {aid[:8]} {a.get("originalFileName","")} {date_str}')

conn.commit()

# Verify
total_post = c.execute("SELECT count(*) FROM photo_scores WHERE album='Post'").fetchone()[0]
print(f'\nDone. Added: {added}, total Post: {total_post}')
conn.close()
