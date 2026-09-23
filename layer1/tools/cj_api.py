"""
CJ Dropshipping API Tool
Docs: https://developers.cjdropshipping.com/
"""

import os
import time
import json
import requests
from typing import List, Optional
from datetime import datetime, timedelta

from config_loader import get_layer_config

_token_cache: dict = {"token": None, "expires_at": datetime.min}
CJ_BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"

# CJ's video download/playback server rejects requests without this header.
CJ_VIDEO_REFERER = "https://developers.cjdropshipping.com/"

# Applied uniformly to both the main product price AND each variant's
# price, so a "Black" variant and a "Green" variant of the same product
# carry the same margin logic. Kept as one constant so parse_cj_product()
# and get_product_variant_list() can never drift out of sync.
# Now sourced from config.json's layer1.cj_price_multiplier — falls back to
# 1.33 (10% margin, profit is 10% of retail) if config.json or that key is
# missing, so this module still works standalone.
try:
    _layer1_cfg = get_layer_config("layer1")
except FileNotFoundError:
    _layer1_cfg = {}

CJ_PRICE_MULTIPLIER = _layer1_cfg.get("cj_price_multiplier", 1.33)

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0        # seconds, network errors: 2s, 4s, 8s
_RATE_LIMIT_BACKOFF_BASE = 5.0   # seconds, HTTP 429/5xx: 5s, 10s, 20s

# ── Shared session (connection pooling / keep-alive) ────────────────────────
# Every CJ call previously went through the bare requests.request(...)
# convenience function, which opens a brand-new TCP+TLS connection from
# scratch on EVERY call, even to the same host back-to-back. That means
# every request pays full connection-setup latency, and any transient
# handshake slowness/jitter shows up as a ConnectTimeout on the very FIRST
# attempt of every product — which is what "ConnectTimeout on every
# product" in production looks like. A shared Session with a pooled
# HTTPAdapter reuses the underlying TCP+TLS connection across calls to the
# same host, so only the very first request in a run pays the full
# handshake cost.
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# (connect_timeout, read_timeout) — a bit more headroom on connect since
# that's the phase seeing timeouts, without also stretching how long we
# wait on a stalled read.
_CJ_TIMEOUT = (15, 25)

def _calculate_retail_price(supplier_price: float) -> float:
    """
    Tiered markup — a flat multiplier badly underprices cheap items, since
    fixed per-order costs (payment processing ~2.9%+$0.30, etc.) don't
    scale down with price. Cheap items need a much higher multiplier just
    to clear those fixed costs and leave real profit.
    """
    if supplier_price < 1.0:
        multiplier = 3.0
    elif supplier_price < 3.0:
        multiplier = 2.2
    elif supplier_price < 15.0:
        multiplier = 1.8
    elif supplier_price < 50.0:
        multiplier = 1.6
    else:
        multiplier = 1.4
    return round(supplier_price * multiplier, 2)


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """
    Wraps requests with retry-on-failure and backoff. Retries on:
      - network-level issues (ConnectTimeout, ConnectionError, ReadTimeout)
      - HTTP 429 (rate limited) and 5xx server errors — these are valid
        HTTP responses, not exceptions, so they need their own check here
        rather than relying on the network-error except block below.
        Honors a Retry-After response header when CJ provides one; falls
        back to a longer exponential backoff than plain network errors get
        (5s/10s/20s vs 2s/4s/8s) since a 429 means "you are over the rate
        limit right now," not "this one request glitched."
    Returns the (possibly still-failing) response after exhausting retries
    so the caller's existing raise_for_status()/error handling still fires
    as before — this function only adds retries, it doesn't change what
    counts as a final failure.
    Uses the shared, connection-pooled _session (see above) instead of
    requests.request() so repeated calls to the same host reuse their
    TCP+TLS connection instead of renegotiating one from scratch every time.
    """
    last_exc = None
    last_resp = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = _session.request(method, url, **kwargs)
        except (requests.ConnectTimeout, requests.ConnectionError, requests.ReadTimeout) as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                print(f"[CJ]   ⚠️ Network error (attempt {attempt}/{_MAX_RETRIES}) on {url.split('?')[0]}: "
                      f"{type(e).__name__} — retrying in {wait:.0f}s...")
                time.sleep(wait)
                continue
            else:
                print(f"[CJ]   ❌ Gave up after {_MAX_RETRIES} attempts on {url.split('?')[0]}: {e}")
                raise
        else:
            last_exc = None

        if resp.status_code == 429 or resp.status_code >= 500:
            last_resp = resp
            if attempt < _MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = _RATE_LIMIT_BACKOFF_BASE * (2 ** (attempt - 1))
                else:
                    wait = _RATE_LIMIT_BACKOFF_BASE * (2 ** (attempt - 1))
                print(f"[CJ]   ⚠️ HTTP {resp.status_code} (attempt {attempt}/{_MAX_RETRIES}) on "
                      f"{url.split('?')[0]} — retrying in {wait:.0f}s...")
                time.sleep(wait)
                continue
            else:
                print(f"[CJ]   ❌ Still HTTP {resp.status_code} after {_MAX_RETRIES} attempts on {url.split('?')[0]}")
                return resp  # let caller's raise_for_status() surface the final failure as before

        return resp

    if last_exc:
        raise last_exc
    return last_resp


def _get_token() -> str:
    global _token_cache
    if _token_cache["token"] and datetime.now() < _token_cache["expires_at"]:
        return _token_cache["token"]

    email   = os.getenv("CJ_EMAIL")
    api_key = os.getenv("CJ_API_KEY")

    if not email or not api_key:
        raise EnvironmentError("CJ_EMAIL and CJ_API_KEY must be set in .env")

    print(f"[CJ] Authenticating with email: {email[:4]}***")
    resp = _request_with_retry(
        "POST",
        f"{CJ_BASE_URL}/authentication/getAccessToken",
        json={"email": email, "password": api_key},
        timeout=30,
        headers={"Content-Type": "application/json"},
    )
    print(f"[CJ] Auth response status: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    print(f"[CJ] Auth response: result={data.get('result')}, message={data.get('message')}")

    if not data.get("result"):
        raise ValueError(f"CJ auth failed: {data.get('message')}")

    token = data["data"]["accessToken"]
    _token_cache = {
        "token": token,
        "expires_at": datetime.now() + timedelta(hours=23),
    }
    return token


def search_products(
    keyword: str,
    max_results: int = 10,
    max_price_usd: float = 100.0,
    sort_by: str = "orders",
) -> List[dict]:
    """Search CJ Dropshipping for products matching a keyword."""
    token = _get_token()
    headers = {"CJ-Access-Token": token}
    sort_map = {"orders": "ORDERS", "price": "PRICE", "date": "CREATED_TIME"}

    params = {
        "productNameEn": keyword,
        "pageNum":  1,
        "pageSize": 20,   # always fetch 20, filter after
        "sortField": sort_map.get(sort_by, "ORDERS"),
        "sortType": "DESC",
    }

    try:
        resp = _request_with_retry(
            "GET",
            f"{CJ_BASE_URL}/product/list",
            headers=headers,
            params=params,
            timeout=_CJ_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("result"):
            print(f"[CJ] API returned no result for '{keyword}': {data.get('message')}")
            return []

        products = data.get("data", {}).get("list", [])
        print(f"[CJ]   Raw products from API for '{keyword}': {len(products)}")

        if products:
            print(f"[CJ]   Sample product keys: {list(products[0].keys())}")
            print(f"[CJ]   Sample product: sellPrice={products[0].get('sellPrice')}, "
                  f"listedNum={products[0].get('listedNum')}, "
                  f"productImage={'yes' if products[0].get('productImage') else 'no'}")

        filtered = _filter_products(products, max_price_usd)
        print(f"[CJ]   After filtering: {len(filtered)} products")
        return filtered[:max_results]

    except requests.RequestException as e:
        print(f"[CJ] API error for '{keyword}': {e}")
        return []


def _parse_price(price_val) -> float:
    """
    CJ returns prices as floats OR range strings like '7.96 -- 9.12' or '200.00-240.00'.
    We always take the lower bound (best case cost for margin calculation).
    """
    if price_val is None:
        return 0.0
    price_str = str(price_val).strip()
    for sep in [" -- ", " - ", "--"]:
        if sep in price_str:
            price_str = price_str.split(sep)[0].strip()
            break
    try:
        return float(price_str)
    except ValueError:
        return 0.0


def _filter_products(products: List[dict], max_price: float) -> List[dict]:
    """
    Relaxed filters — only remove products with no image or price=0.
    Let the Scorer node handle quality ranking.
    """
    filtered = []
    for p in products:
        try:
            price = _parse_price(p.get("sellPrice"))
            if price <= 0 or price > max_price:
                continue
            if not p.get("productImage"):
                continue
            p["_parsed_price"] = price
            filtered.append(p)
        except (ValueError, TypeError):
            continue
    return filtered


def parse_cj_product(raw: dict, trend_keyword: str, trend_score: int) -> dict:
    """Normalise a raw CJ product dict into our internal format."""
    try:
        supplier_price = float(raw.get("_parsed_price") or _parse_price(raw.get("sellPrice")))
    except (TypeError, ValueError):
        supplier_price = 0.0

    if supplier_price <= 0:
        return None

    # suggested_retail = round(supplier_price * CJ_PRICE_MULTIPLIER, 2)
    # margin_pct       = round((suggested_retail - supplier_price) / suggested_retail * 100, 1)

    suggested_retail = _calculate_retail_price(supplier_price)
    margin_pct = round((suggested_retail - supplier_price) / suggested_retail * 100, 1)

    orders_count  = int(raw.get("listedNum") or raw.get("sellCount") or 0)

    # NOTE: previously this read raw["productWeight"] (the item's physical
    # weight in kg/g) into "rating" — a copy/paste bug that fed the Scorer
    # pure noise labeled as a 0-5 supplier rating (weight values would
    # frequently clamp to the 1.0-5.0 bound in either direction regardless
    # of actual product quality). CJ's /product/list search response does
    # not appear to expose a genuine star-rating field, so until that's
    # confirmed against the live API response we fall back to a flat
    # neutral 4.0 for every product rather than fake differentiation.
    # TODO: check a live CJ /product/list payload for a real rating/score
    # field (e.g. "evaluateScore", "starRating") and wire it in here if one exists.
    rating = _parse_price(raw.get("evaluateScore") or raw.get("starRating") or 4.0)
    shipping_days = int(raw.get("shippingTime") or 14)
    pid           = raw.get("pid", "")
    main_image    = raw.get("productImage", "")

    # Pacing gap before this product's detail calls. parse_cj_product()
    # fires up to 3 sequential CJ requests per product (media, possibly
    # video, variants) with NOTHING pacing them between products -- the
    # existing time.sleep(0.3) in _fetch_and_collect only spaces out
    # KEYWORD searches, not the per-product detail fetches that happen for
    # every single result of every search. CONFIRMED in production: CJ
    # returns HTTP 429 (rate limited) once request volume gets high enough
    # -- this isn't theoretical burstiness, it's a real per-account/token
    # rate limit. An initial 0.2s gap here wasn't enough and 429s still
    # showed up (see _request_with_retry's 429/5xx retry handling above,
    # which is the other half of this fix); bumped to 0.5s. If 429s persist
    # even with both fixes, increase this further or cut max_products_per_keyword.
    time.sleep(0.5)

    # Fetch all product media (images + videos)
    print(f"[CJ]   Fetching all media for pid {pid}...")
    media = get_all_media(pid, main_image)
    image_urls = media["images"]
    video_urls = media["videos"]
    # print(f"[CJ]   Found {len(image_urls)} images, {len(video_urls)} videos")
    # if video_urls:
    #     print(f"[CJ]     ↳ video[0]: {video_urls[0]}")

    time.sleep(0.5)  # pacing gap before the variant-list call

    # Fetch all color/size variants for this product — this is what lets
    # the storefront show a "Black / Green / Yellow" selector instead of
    # publishing only the single main variant.
    variants = get_product_variant_list(pid)
    # if variants:
    #     print(f"[CJ]   Found {len(variants)} variants: "
    #           f"{[v['option_value'] for v in variants]}")

    return {
        "title":            raw.get("productNameEn", ""),
        "cj_pid":           pid,
        "category":         raw.get("categoryName") or "General",
        "supplier_price":   supplier_price,
        "suggested_retail": suggested_retail,
        "margin_pct":       margin_pct,
        "shipping_days":    shipping_days,
        "rating":           min(max(rating, 1.0), 5.0),
        "orders_count":     orders_count,
        "trend_keyword":    trend_keyword,
        "trend_score":      trend_score,
        "image_url":        main_image,
        "image_urls":       image_urls,
        "video_urls":       video_urls,
        "variants":         variants,   # NEW: [{vid, sku, option_value, image, supplier_price, retail_price, weight}, ...]
        "cj_product_url":   f"https://app.cjdropshipping.com/product-detail.html?id={pid}",
    }


def get_product_detail(pid: str, features: Optional[List[str]] = None) -> Optional[dict]:
    """Fetch full product detail from CJ including images and variants summary."""
    token = _get_token()
    params: dict = {"pid": pid}
    if features:
        params["features"] = features
    try:
        resp = _request_with_retry(
            "GET",
            f"{CJ_BASE_URL}/product/query",
            headers={"CJ-Access-Token": token},
            params=params,
            timeout=_CJ_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data") if data.get("result") else None
    except Exception as e:
        print(f"[CJ] Error fetching detail for {pid}: {e}")
        return None


def get_product_variant_list(pid: str) -> List[dict]:
    """
    Fetch ALL variants (colors/sizes/etc.) for a product via CJ's dedicated
    variant-list endpoint:

        GET /product/variant/query?pid={pid}

    (Distinct from /product/variant/queryByVid, which fetches ONE variant
    by its own vid — that's a different lookup, not what we need here.)

    CJ does NOT return a separate "option name" (e.g. "Color") — each
    variant just has a `variantKey` string like "Black" or "Black-XXL"
    (hyphen-joined values with no documented label). We use that raw
    string as-is as a single Shopify option value ("option_value" below),
    which is the only 100%-reliable interpretation without guessing at
    CJ's internal naming conventions. If a product varies by both color
    AND size, the dropdown will show combined values like "Black-XXL"
    rather than two separate Color/Size selectors — still fully
    selectable, just not split into two option columns.

    Deduplicates by option_value (CJ occasionally lists the same variant
    twice across warehouses) — keeps the first occurrence.

    Returns [] on any failure or if the product has no variant data —
    callers should treat that as "single-variant product" and fall back
    to today's default-variant behavior.
    """
    token = _get_token()
    try:
        resp = _request_with_retry(
            "GET",
            f"{CJ_BASE_URL}/product/variant/query",
            headers={"CJ-Access-Token": token},
            params={"pid": pid},
            timeout=_CJ_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("result"):
            return []

        raw_variants = data.get("data") or []
        seen_values = set()
        variants: List[dict] = []

        for rv in raw_variants:
            option_value = (rv.get("variantKey") or "").strip()
            if not option_value or option_value in seen_values:
                continue
            seen_values.add(option_value)

            try:
                supplier_price = float(rv.get("variantSellPrice") or 0)
            except (TypeError, ValueError):
                supplier_price = 0.0
            if supplier_price <= 0:
                continue  # unusable — skip rather than publish a $0 variant

            retail_price = round(supplier_price * CJ_PRICE_MULTIPLIER, 2)

            variants.append({
                "vid":             rv.get("vid", ""),
                "sku":             rv.get("variantSku", ""),
                "option_value":    option_value,
                "image":           rv.get("variantImage") or None,
                "supplier_price":  supplier_price,
                "retail_price":    retail_price,
                "weight":          rv.get("variantWeight"),
            })

        return variants

    except requests.RequestException as e:
        print(f"[CJ]   ⚠️ get_product_variant_list failed for pid {pid}: {e}")
        return []
    except (ValueError, TypeError, KeyError) as e:
        print(f"[CJ]   ⚠️ get_product_variant_list bad response for pid {pid}: {e}")
        return []


def get_product_videos(pid: str) -> List[str]:
    """
    Fetch REAL, playable video URLs via CJ's dedicated video resolver:
        POST /product/queryVideosByProductId, body {"productId": pid}
    Returns [] on any failure — video is a bonus asset, never worth
    failing product ingestion over.
    """
    token = _get_token()
    try:
        resp = _request_with_retry(
            "POST",
            f"{CJ_BASE_URL}/product/queryVideosByProductId",
            headers={"CJ-Access-Token": token, "Content-Type": "application/json"},
            json={"productId": pid},
            timeout=_CJ_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if not (data.get("result") or data.get("success")):
            return []

        video_urls: List[str] = []
        for item in data.get("data") or []:
            url = item.get("videoUrl")
            state = item.get("videoState", "ON_STATE")
            if url and str(url).startswith("http") and state == "ON_STATE":
                video_urls.append(url)
            elif url:
                print(f"[CJ]   ⚠️ pid {pid}: skipped video entry (state={state}, url={url!r})")

        return video_urls

    except requests.RequestException as e:
        print(f"[CJ]   ⚠️ queryVideosByProductId failed for pid {pid}: {e}")
        return []
    except (ValueError, TypeError) as e:
        print(f"[CJ]   ⚠️ queryVideosByProductId bad response for pid {pid}: {e}")
        return []


def download_video(video_url: str, dest_path: str, timeout: int = 60) -> bool:
    """
    Downloads a CJ video with the Referer header CJ's server requires.
    Returns True on success, False on any failure.
    """
    try:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        resp = _request_with_retry(
            "GET",
            video_url,
            headers={"Referer": CJ_VIDEO_REFERER},
            timeout=timeout,
            stream=True,
        )
        resp.raise_for_status()

        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        if os.path.getsize(dest_path) == 0:
            print(f"[CJ]   ⚠️ Downloaded video is empty: {video_url}")
            os.remove(dest_path)
            return False

        return True

    except requests.RequestException as e:
        print(f"[CJ]   ❌ Video download failed for {video_url}: {e}")
        return False
    except OSError as e:
        print(f"[CJ]   ❌ Could not write video file {dest_path}: {e}")
        return False


def get_product_variants(pid: str) -> Optional[dict]:
    """Backward-compat alias — this is the OLD single-object detail fetch,
    NOT the variant list. Use get_product_variant_list(pid) for variants."""
    return get_product_detail(pid)


def get_all_images(pid: str, main_image: str) -> list:
    """Backward-compat wrapper — images only. Prefer get_all_media()."""
    return get_all_media(pid, main_image)["images"]


def get_all_media(pid: str, main_image: str) -> dict:
    """
    Fetch ALL product media (images + videos) for a product.
    Returns {"images": [...], "videos": [...]}.
    """
    detail = get_product_detail(pid, features=["enable_video"])
    if not detail:
        print(f"[CJ]   ⚠️ pid {pid}: falling back to main image only "
              f"(detail fetch failed after retries — see error above)")
        return {"images": [main_image] if main_image else [], "videos": []}

    # ── Images ──────────────────────────────────────────────────────────
    image_urls: list = []
    image_set = detail.get("productImageSet")
    if image_set is None:
        image_set = detail.get("productImage", "")

    if isinstance(image_set, list):
        image_urls = [u for u in image_set if u]
    elif isinstance(image_set, str) and image_set:
        try:
            parsed = json.loads(image_set)
            if isinstance(parsed, list):
                image_urls = [u for u in parsed if u]
            elif isinstance(parsed, str) and parsed:
                image_urls = [parsed]
        except (ValueError, TypeError):
            image_urls = [u.strip() for u in image_set.split(",") if u.strip()]

    detail_main = detail.get("bigImage") or ""
    if detail_main and detail_main not in image_urls:
        image_urls.insert(0, detail_main)
    if main_image and main_image not in image_urls:
        image_urls.insert(0, main_image)

    seen_img = set()
    unique_images = []
    for url in image_urls:
        if url and url not in seen_img:
            seen_img.add(url)
            unique_images.append(url)

    # ── Videos ──────────────────────────────────────────────────────────
    has_video_signal = bool(detail.get("isVideo") in (1, "1", True)) or bool(detail.get("productVideo"))
    video_urls: List[str] = []
    if has_video_signal:
        time.sleep(0.3)  # pacing gap: this follows get_product_detail() with no gap otherwise
        video_urls = get_product_videos(pid)

    return {"images": unique_images[:10], "videos": video_urls}