"""Example configuration for InkTime. Copy to config.py and edit.

Copy with:  cp config.example.py config.py

config.py is gitignored. Keep every secret in it (or in the environment, using
the "env:VAR_NAME" indirection) and never in a tracked file.
"""

from pathlib import Path

# ---------------------------------------------------------------- storage

# SQLite database. Relative paths resolve against the repo root.
DB_PATH = "photos.db"

# Renderer output: photo_N.bin / photo_N.h / preview_N.png / latest.*
# Serve this directory over HTTP. The URL prefix must match
# DAILY_PHOTO_PATH_PREFIX in the ESP32 sketch.
BIN_OUTPUT_DIR = "output/inktime"

# Where the ESP32 requests frames from, relative to the server root.
# Only used to document the contract; the sketch holds the same string.
DOWNLOAD_KEY = "CHANGEME_32_HEX_DOWNLOAD_KEY"

# Fonts for the info panel. Point at a real TTF or leave blank to fall back
# to Pillow's bitmap default (readable, but not the intended look).
FONT_PATH = ""        # e.g. /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf
FONT_BOLD_PATH = ""   # e.g. /usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf

# ---------------------------------------------------------------- selection

# Minimum memory score to be eligible for a frame (0-100).
MEMORY_THRESHOLD = 70.0

# Don't reuse a photo within this many days.
USED_COOLDOWN_DAYS = 30

# How many alternative frames to render per day. The frame picks one at random,
# so this is also how unpredictable a day's photo is.
DAILY_PHOTO_QUANTITY = 5

# Optional album filter for the renderer. The `album` column is populated by
# scripts/backfill_post.py, not by the analyzer. Leave empty to render from
# every scored photo; set it (e.g. "Post") to restrict to one tagged album.
ALBUM_FILTER = ""

# ---------------------------------------------------------------- source

# False: read plain image files from IMAGE_DIR. True: pull from Immich.
USE_IMMICH = True
IMAGE_DIR = ""

# Immich. The API key needs asset.read, asset.view, asset.download and
# asset.update. asset.view is a separate scope: without it thumbnails and
# originals return 403 even for the owner.
IMMICH_URL = "http://localhost:2283"
IMMICH_API_KEY = "env:IMMICH_API_KEY"
IMMICH_TIMEOUT = 60
IMMICH_PAGE_SIZE = 1000
IMMICH_INCLUDE_ARCHIVED = False
IMMICH_FAVORITES_ONLY = False
IMMICH_ALBUM_IDS = []          # restrict ingestion to these album ids
IMMICH_DOWNLOAD_DIR = "./cache/immich"

# Legacy NAS mount path, used only by the local-directory flow.
NAS_MOUNT_POINT = "/mnt/photo"
NAS_MOUNT_URL = ""
NAS_RETRY_TIMES = 3
NAS_RETRY_SLEEP_SEC = 2.0

# ---------------------------------------------------------------- scoring

# Vision-model endpoints, tried in order with per-channel cooldown on failure.
# api_key supports "env:VAR_NAME" so the value itself never lands in config.py.
API_CHANNELS = [
    {
        "api_url": "https://api.example.com/v1/chat/completions",
        "model": "some-vision-model",
        "api_key": "env:VLM_API_KEY",
    },
]

# Legacy single-channel keys, used only when API_CHANNELS is empty.
API_URL = ""
MODEL_NAME = ""
API_KEY = ""

BATCH_LIMIT = None            # None = no cap
TIMEOUT = 600                 # per request, seconds
CHANNEL_FAILOVER_COOLDOWN_SEC = 300

# Long edge the image is resized to before it is sent to the model.
VLM_MAX_LONG_EDGE = 2560

# Face handling. ALLOWED_PERSON_IDS are the people whose faces may appear;
# leave the filter off and skip the allowlist entirely if you don't need it.
FACE_FILTER_ENABLED = False
ALLOWED_PERSON_IDS = []
ALLOW_UNNAMED_FACES = False

# ---------------------------------------------------------------- dedupe

DEDUPE_ENABLED = True
# Differences at or below this are the same photo regardless of time.
DEDUPE_PHASH_DUP_THRESHOLD = 5
# Wider differences count as duplicates only inside the burst window below.
DEDUPE_PHASH_BURST_THRESHOLD = 12
DEDUPE_BURST_WINDOW_HOURS = 3.0
DEDUPE_DELETE_CACHE = True

# ---------------------------------------------------------------- geo

# GeoNames-style CSV with lat,lon,name_en,name_zh columns. Ship your own.
WORLD_CITIES_CSV = "data/world_cities.csv"
CITY_GRID_DEG = 1.0
CITY_MAX_DISTANCE_KM = 80.0

# Home location, used to decide whether a photo counts as travel.
# Set these to your own. Do not leave another person's coordinates here.
HOME_LAT = 0.0
HOME_LON = 0.0
HOME_RADIUS_KM = 60.0
