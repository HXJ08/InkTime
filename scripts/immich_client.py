#!/usr/bin/env python3
"""Minimal Immich API client for InkTime.

Provides:
  - list_assets()              paginated list of all image assets
  - download_original(asset_id, dest_path)
  - get_asset_info(asset_id)   metadata (taken date, EXIF, GPS, etc.)
  - filter_options             helpers to apply album/isFavorite/isArchived filters

Authentication uses an Immich API key, sent via the `x-api-key` header.
Get one in Immich: Account Settings -> API Keys -> New API Key.

Immich API reference: https://api.immich.app/
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import requests


@dataclass
class ImmichConfig:
    base_url: str
    api_key: str
    request_timeout: int = 60
    page_size: int = 1000
    include_archived: bool = False
    favorites_only: bool = False
    album_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_config_module(cls, cfg) -> "ImmichConfig":
        """Build from a config.py module (or any object with the right attrs)."""
        api_key_raw = str(
            getattr(cfg, "IMMICH_API_KEY", None)
            or os.environ.get("IMMICH_API_KEY", "")
        )
        # Support "env:VAR_NAME" pattern
        api_key = api_key_raw.strip()
        if api_key.startswith("env:"):
            api_key = os.environ.get(api_key[4:].strip(), "")

        return cls(
            base_url=str(getattr(cfg, "IMMICH_URL", "") or "").rstrip("/"),
            api_key=api_key,
            request_timeout=int(getattr(cfg, "IMMICH_TIMEOUT", 60) or 60),
            page_size=int(getattr(cfg, "IMMICH_PAGE_SIZE", 1000) or 1000),
            include_archived=bool(getattr(cfg, "IMMICH_INCLUDE_ARCHIVED", False)),
            favorites_only=bool(getattr(cfg, "IMMICH_FAVORITES_ONLY", False)),
            album_ids=list(getattr(cfg, "IMMICH_ALBUM_IDS", []) or []),
        )


class ImmichError(RuntimeError):
    pass


class ImmichClient:
    def __init__(self, cfg: ImmichConfig):
        if not cfg.base_url:
            raise ImmichError("IMMICH_URL is not set in config")
        if not cfg.api_key:
            raise ImmichError(
                "IMMICH_API_KEY is not set. Either set it in config.py, or set the "
                "IMMICH_API_KEY environment variable (or use 'env:VAR_NAME' in config)."
            )
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": cfg.api_key,
            "Accept": "application/json",
        })

    # ---- low-level HTTP ----

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url}{path}"

    def _request_json(self, method: str, path: str, **kwargs) -> dict | list:
        url = self._url(path)
        kwargs.setdefault("timeout", self.cfg.request_timeout)
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):  # 3 attempts
            try:
                r = self.session.request(method, url, **kwargs)
                if r.status_code == 429 or r.status_code >= 500:
                    raise ImmichError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code == 401:
                    raise ImmichError(f"Unauthorized (401). Check IMMICH_API_KEY. {r.text[:200]}")
                if r.status_code == 403:
                    raise ImmichError(f"Forbidden (403). {r.text[:200]}")
                if r.status_code >= 400:
                    raise ImmichError(f"HTTP {r.status_code}: {r.text[:200]}")
                if not r.content:
                    return {}
                return r.json()
            except (requests.RequestException, ImmichError) as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                break
        raise ImmichError(f"Immich request failed after 3 attempts: {last_err}")

    def _request_bytes(self, method: str, path: str, **kwargs) -> bytes:
        url = self._url(path)
        kwargs.setdefault("timeout", self.cfg.request_timeout)
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                r = self.session.request(method, url, **kwargs)
                # 404 = endpoint definitely doesn't exist (e.g. wrong Immich version).
                # Raise IMMEDIATELY so the caller can try the next candidate URL
                # without burning ~7s on retries against a dead endpoint.
                if r.status_code == 404:
                    raise ImmichError(f"HTTP 404: {r.text[:200]}")
                if r.status_code >= 400:
                    raise ImmichError(f"HTTP {r.status_code}: {r.text[:200]}")
                return r.content
            except (requests.RequestException, ImmichError) as e:
                # Don't retry on 404 — it's deterministic, not transient.
                if isinstance(e, ImmichError) and "HTTP 404" in str(e):
                    raise
                last_err = e
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                break
        raise ImmichError(f"Immich download failed after 3 attempts: {last_err}")

    # ---- high-level operations ----

    def ping(self) -> dict:
        """Health check; returns server info."""
        return self._request_json("GET", "/api/server/ping")  # type: ignore[return-value]

    def get_asset_info(self, asset_id: str) -> dict:
        """Full asset info, including EXIF and GPS. Tries both
        /api/assets/{id} (newer) and /api/asset/{id} (older)."""
        last_error: ImmichError | None = None
        for path in (f"/api/assets/{asset_id}", f"/api/asset/{asset_id}"):
            try:
                return self._request_json("GET", path)  # type: ignore[return-value]
            except ImmichError as e:
                last_error = e
                continue
        raise last_error  # type: ignore[misc]

    def download_original(self, asset_id: str, dest_path: Path) -> Path:
        """Download the original file to dest_path. Creates parent dirs as needed.
        Returns dest_path. Tries multiple endpoint patterns for compatibility
        with different Immich versions:
          - /api/assets/{id}/original   (newer Immich, 1.107+)
          - /api/asset/{id}/original    (older Immich, <= 1.106)
          - /api/download/asset/{id}    (newer alternative)
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_path.with_suffix(dest_path.suffix + ".part")

        candidate_paths = [
            f"/api/assets/{asset_id}/original",  # newer Immich
            f"/api/asset/{asset_id}/original",   # older Immich
            f"/api/download/asset/{asset_id}",   # newer alternative
        ]

        last_error: ImmichError | None = None
        for path in candidate_paths:
            try:
                data = self._request_bytes("GET", path)
                tmp.write_bytes(data)
                tmp.replace(dest_path)
                return dest_path
            except ImmichError as e:
                last_error = e
                # 404 is the expected "wrong endpoint" signal; try the next one.
                # 4xx other than 404 / 5xx are surfaced immediately by _request_bytes.
                continue

        raise ImmichError(
            f"Could not download asset {asset_id} from any endpoint; last error: {last_error}"
        )

    def download_preview(self, asset_id: str, dest_path: Path) -> Path:
        """Download the 'preview' size variant (~1-2MB) instead of the
        original (3-50MB). Preview is the resized version (~1440x1440 max
        dimension) that Immich generates automatically. It retains full
        EXIF metadata and works with pillow_heif / PIL just like the
        original. Good enough for pHash, dHash, VLM captioning, and
        image dimensions.

        Immich v3 (>= 1.107) replaced /preview with /thumbnail. We try
        the v3 endpoint first, then fall back to v2/v1 for older servers.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_path.with_suffix(dest_path.suffix + ".part")
        candidate_paths = [
            f"/api/assets/{asset_id}/thumbnail",      # Immich v3 (current)
            f"/api/assets/{asset_id}/preview",        # Immich v2
            f"/api/asset/{asset_id}/preview",         # Immich v1
        ]
        last_error = None
        for path in candidate_paths:
            try:
                data = self._request_bytes("GET", path)
                tmp.write_bytes(data)
                tmp.replace(dest_path)
                return dest_path
            except ImmichError as e:
                last_error = e
                continue
        raise ImmichError(
            f"Could not download preview for {asset_id}; last error: {last_error}"
        )

    def list_assets(self) -> Iterator[dict]:
        """Yield all IMAGE assets, paginated. Each yielded dict has at least:
          id, type, originalFileName, fileCreatedAt, exifInfo, etc.
        Honors favorites_only / include_archived / album_ids from config.
        """
        # If the user asked to scope to specific albums, walk those instead of
        # the global library. Album listing is a different endpoint.
        if self.cfg.album_ids:
            yield from self._list_via_albums(set())
            return

        page = 1
        seen_ids: set[str] = set()
        while True:
            # POST /api/search/metadata is the canonical "list assets" endpoint
            # on Immich. The /api/asset POST endpoint is for bulk operations
            # only and 404s on most Immich versions.
            payload: dict = {
                "page": page,
                "size": self.cfg.page_size,
                "withPeople": False,
                "withExif": True,
            }
            if self.cfg.favorites_only:
                payload["isFavorite"] = True
            # isArchived filter in Immich:
            #   - field absent: return BOTH archived and non-archived
            #   - isArchived=true:  return only archived
            #   - isArchived=false: return only non-archived
            # We want non-archived by default; if the user opts in, drop the
            # filter so archived photos are also included.
            if not self.cfg.include_archived:
                payload["isArchived"] = False

            data = self._request_json("POST", "/api/search/metadata", json=payload)

            # Response shape: { assets: { items: [...], total, nextPage }, ... }
            assets_obj = data.get("assets", {}) if isinstance(data, dict) else {}
            items = assets_obj.get("items", []) if isinstance(assets_obj, dict) else []

            if not items:
                return

            new_count = 0
            for asset in items:
                if not isinstance(asset, dict):
                    continue
                aid = asset.get("id")
                if not aid or aid in seen_ids:
                    continue
                # Only IMAGE type (skip videos)
                if asset.get("type") and asset.get("type") != "IMAGE":
                    continue
                seen_ids.add(aid)
                new_count += 1
                yield asset

            if new_count == 0:
                return
            page += 1

    def _list_via_albums(self, seen_ids: set[str]) -> Iterator[dict]:
        """List assets from specific albums, plus other filters."""
        for album_id in self.cfg.album_ids:
            try:
                # /api/album/:id returns album with asset list
                data = self._request_json("GET", f"/api/album/{album_id}")
            except ImmichError as e:
                print(f"[WARN] failed to read album {album_id}: {e}")
                continue
            assets = data.get("assets", []) if isinstance(data, dict) else []
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                aid = asset.get("id")
                if not aid or aid in seen_ids:
                    continue
                if asset.get("type") and asset.get("type") != "IMAGE":
                    continue
                if self.cfg.favorites_only and not asset.get("isFavorite"):
                    continue
                if not self.cfg.include_archived and asset.get("isArchived"):
                    continue
                seen_ids.add(aid)
                yield asset


# ---- sync-cache helper ----

# Default cache mode: use 'preview' size (1-2MB) instead of originals (3-50MB).
# This keeps the local cache at ~5GB instead of growing to 150GB+.
# Set IMMICH_USE_PREVIEW_CACHE=False to force originals (e.g. for hi-res printing).
try:
    import config as _cache_cfg
    _USE_PREVIEW = bool(getattr(_cache_cfg, "IMMICH_USE_PREVIEW_CACHE", True))
except Exception:
    _USE_PREVIEW = True


def sync_to_cache(
    client: ImmichClient,
    cache_dir: Path,
    *,
    skip_existing: bool = True,
    progress_every: int = 25,
    use_preview: bool | None = None,
) -> Iterator[tuple[dict, Path]]:
    """Walk all assets, download preview/original into cache_dir/{asset_id}{ext}.

    By default uses Immich's preview variant (~1-2MB) instead of the
    original (3-50MB). Preview retains EXIF and is high enough resolution
    for VLM captioning, pHash, and full image composition.

    Pass use_preview=False to override per-call (force originals).
    Set IMMICH_USE_PREVIEW_CACHE=False in config.py to make originals the default.

    If skip_existing=True (default), assets already cached are skipped.
    """
    if use_preview is None:
        use_preview = _USE_PREVIEW
    cache_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for asset in client.list_assets():
        aid = asset.get("id")
        if not aid:
            continue
        # determine extension from ORIGINAL filename (preview may differ)
        orig_name = str(asset.get("originalFileName") or "")
        ext = Path(orig_name).suffix.lower() if orig_name else ""
        if not ext:
            ext = ".jpg"
        dest = cache_dir / f"{aid}{ext}"
        if not (skip_existing and dest.exists() and dest.stat().st_size > 0):
            try:
                if use_preview:
                    client.download_preview(aid, dest)
                else:
                    client.download_original(aid, dest)
            except ImmichError as e:
                print(f"[WARN] failed to download {aid}: {e}")
                continue
        n += 1
        if n % progress_every == 0:
            print(f"[IMMICH] synced {n} assets (latest: {dest.name})")
        yield asset, dest
    print(f"[IMMICH] done. {n} assets synced to {cache_dir} (preview={use_preview})")
