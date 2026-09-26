"""
Image Agent Node
=================
For each listing:
  1. Downloads product images AS-IS — no background removal, no
     re-compositing, no resizing. CJ's supplied images are used unchanged.
     (If you ever want the rembg/white-bg/2048px pipeline back, that logic
     is straightforward to re-add — just say so.)
  2. Downloads any resolved product videos (from Layer 1's video_urls,
     which come from CJ's queryVideosByProductId resolver — see cj_api.py)
     to local disk, so they're ready for Layer 2's publish step.

Falls back gracefully: if a download fails, the original CJ URL is kept
so nothing blocks the rest of the pipeline.
"""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from layer2.state import GraphState, ShopifyListing
from layer1.tools.cj_api import download_video


# ── Config ─────────────────────────────────────────────────────────────────────
IMAGE_DIR = Path(tempfile.gettempdir()) / "dropship_images"
VIDEO_DIR = Path(tempfile.gettempdir()) / "dropship_videos"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

# Shared session with retry/backoff — CJ's CDN times out fairly often, and
# a single 15s attempt with no retry (the previous behavior) turns a
# transient blip into a permanent fallback-to-original-URL for that image.
_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=1.0,          # 1s, 2s, 4s between attempts
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))


def _stable_filename(url: str, product_id: str, default_ext: str, directory: Path) -> Path:
    """
    Deterministic cache key for a URL. The old code used hash(url) % 10000,
    but Python randomizes str hashing per-process (PYTHONHASHSEED), so the
    same image/video got a different filename — and therefore was
    re-downloaded from scratch — every single run. md5 is stable across
    runs/processes, which also lets us skip re-downloading unchanged files.
    """
    ext = Path(url.split("?")[0]).suffix or default_ext
    digest = hashlib.md5(url.encode()).hexdigest()[:12]
    return directory / f"{product_id}_{digest}{ext}"


def image_agent_node(state: GraphState) -> dict:
    """
    Downloads product images (unmodified) and videos for all listings.
    Falls back gracefully: if a download fails, keeps the original URL
    (images) or simply omits that video (videos — no safe URL fallback,
    see download_video()'s Referer requirement in cj_api.py).
    """
    print("\n[ImageAgent] 🖼️  Downloading product media (images unmodified)...")
    errors = list(state.get("errors", []))
    listings = state.get("priced_listings", [])

    if not listings:
        errors.append("ImageAgent: No listings received.")
        return {"image_ready_listings": [], "errors": errors}

    processed: List[ShopifyListing] = []

    for i, listing in enumerate(listings):
        print(f"[ImageAgent]   [{i+1}/{len(listings)}] {listing.shopify_title[:45]}...")

        # ── Images: download as-is, no processing ───────────────────────────
        processed_image_urls = []
        for url in listing.image_urls[:10]:   # max 10 images per product
            if not url:
                continue
            local_path = _download_image_unmodified(url, listing.cj_pid)
            processed_image_urls.append(str(local_path) if local_path else url)

        listing.processed_image_urls = processed_image_urls if processed_image_urls else listing.image_urls

        # ── Videos: download via the CJ resolver URLs (see cj_api.py) ───────
        source_video_urls = getattr(listing, "video_urls", None) or []
        processed_video_urls = []
        for url in source_video_urls:
            if not url:
                continue
            local_path = _download_video_for_listing(url, listing.cj_pid)
            if local_path:
                processed_video_urls.append(str(local_path))
            # No fallback to the raw CJ URL here on purpose: CJ's video
            # server requires a specific Referer header to serve the file
            # (see CJ_VIDEO_REFERER in cj_api.py) and explicitly discourages
            # hotlinking it to end customers — so unlike images, if the
            # download fails there's no safe URL to fall back to. It's
            # simply dropped for this run, and logged below.

        if source_video_urls and not processed_video_urls:
            print(f"[ImageAgent]     ⚠️  {len(source_video_urls)} video(s) found but none downloaded successfully")

        listing.processed_video_urls = processed_video_urls
        processed.append(listing)

    total_videos = sum(len(getattr(l, "processed_video_urls", [])) for l in processed)
    print(f"[ImageAgent] ✅ Processed images for {len(processed)} listings "
          f"({total_videos} videos downloaded)")
    return {"image_ready_listings": processed, "errors": errors}


def _download_image_unmodified(url: str, product_id: str) -> Optional[Path]:
    """
    Downloads a product image and saves it EXACTLY as CJ provided it —
    no background removal, no recompositing, no resize. Returns the local
    path, or None if the download fails (caller falls back to the original
    URL in that case, which store_publisher can still use directly).
    """
    filename = _stable_filename(url, product_id, ".jpg", IMAGE_DIR)

    # Already downloaded in a previous run — skip the network round-trip.
    if filename.exists() and filename.stat().st_size > 0:
        return filename

    try:
        resp = _session.get(url, timeout=(5, 15), headers={
            "User-Agent": "Mozilla/5.0 (compatible; DropshipBot/1.0)"
        })
        resp.raise_for_status()

        with open(filename, "wb") as f:
            f.write(resp.content)
        return filename

    except Exception as e:
        print(f"[ImageAgent]     ⚠️  Image download failed for {url[:60]}: {e}")
        return None


def _download_video_for_listing(url: str, product_id: str) -> Optional[Path]:
    """
    Downloads a resolved CJ video URL (from Layer 1's video_urls, produced
    by cj_api.get_product_videos) to local disk, using cj_api.download_video
    so the required Referer header is sent correctly.
    """
    filename = _stable_filename(url, product_id, ".mp4", VIDEO_DIR)

    if filename.exists() and filename.stat().st_size > 0:
        return filename

    success = download_video(url, str(filename))
    if success:
        return filename

    print(f"[ImageAgent]     ⚠️  Video download failed for {url[:60]}")
    return None