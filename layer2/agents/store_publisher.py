"""
Store Publisher Node
====================
Final node of Layer 2. Takes fully prepared listings and:
  1. Checks for duplicate via local registry + Shopify verification
  2. Uploads processed images to Shopify CDN (REST)
  3. Creates the Shopify product with all fields + SEO metafields (REST)
     — including real color/size/pack options + variants when CJ supplied them
  4. Sets retail price + compare_at_price per variant
  5. Assigns product to its Shopify collection
  6. Links each variant to its own image (reusing gallery images where
     CJ's variant photo is already one of the product's own images)
  7. Attaches any downloaded product videos (GraphQL — REST cannot do this)
  8. Reorders media so the video is FIRST (storefront leads with video)
  9. Publishes immediately (status = "active")

Deduplication strategy:
  - Registry file (fast local check) → if found, verify product still exists on Shopify
  - If Shopify returns 404 (manually deleted) → remove from registry → republish
  - TTL of 90 days → expired entries eligible for refresh
  - Registry auto-purges expired entries on every save

Video attachment strategy:
  - Shopify's REST Admin API (products.json) does NOT accept video media —
    only images. Video requires the GraphQL Admin API:
      1. stagedUploadsCreate  → get a signed upload URL + params
      2. POST the local video file (multipart) to that signed URL
      3. productCreateMedia   → attach the uploaded resource to the product
         (this APPENDS the video after all REST-created images)
      4. productReorderMedia  → move the video to position 0 so it's the
         first thing shown on the storefront product page
  - Video attachment (including reorder) is best-effort: if any step
    fails, the product still publishes successfully with its images.

Variant/option strategy:
  - CJ gives ONE combined string per variant (variantKey → our
    option_value), e.g. "Badge Blue-S", "Style 1-1 PC", or
    "200cm 80cm 15cm 2-Black". We split on the FIRST hyphen into two
    columns whenever MOST variants in the batch split cleanly (>=60%) —
    not requiring 100%, since a single outlier used to silently collapse
    the whole product into one giant combined "Style" dropdown.
  - Each column is labeled by CONTENT, not position (a yoga mat's
    dimensions come BEFORE color; clothing's color comes BEFORE size —
    position-based labeling gets this backwards for some products).
  - A free keyword/regex heuristic (_classify_column) handles the common
    cases (color words, size words/measurements, pack/unit counts like
    "1 PC") instantly with no API cost. Only when the heuristic can't
    confidently label one or both columns do we spend one small
    GPT-4o-mini call to name them (e.g. "Material", "Scent") — the LLM
    ONLY assigns the two option NAMES, never touches the actual
    values/prices/images, so a bad or failed LLM call can never corrupt
    variant data, only fall back to generic "Style"/"Type" labels.
  - Variant images are reused from the general gallery when CJ's variant
    photo is already one of the product's own images (the common case —
    mirrors CJ's own manual listing flow), or uploaded fresh otherwise.
  - inventory_policy is set to "continue" on every variant so customers
    can always order additional quantity (dropshipping has no real fixed
    stock ceiling worth blocking sales over).
"""

import os
import re
import json
import base64
import mimetypes
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

load_dotenv()

from layer2.state import GraphState, ShopifyListing
from config_loader import get_layer_config

# ── Config ────────────────────────────────────────────────────────────────────
_collection_cache: dict = {}
REGISTRY_PATH    = Path("layer2/output/published_pids.json")

# How long a published product stays protected from republishing before
# Layer 2 treats it as eligible for a fresh listing again. Sourced from
# config.json's layer2.registry_ttl_days — falls back to 90 if config.json
# or that key is missing, so this module still works standalone. Set to
# 120 days intentionally (per product decision): old listings are meant to
# be periodically refreshed/republished after this window, not protected
# forever.
try:
    _layer2_cfg = get_layer_config("layer2")
except FileNotFoundError:
    _layer2_cfg = {}

REGISTRY_TTL_DAYS = _layer2_cfg.get("registry_ttl_days", 90)
API_VERSION = "2024-01"

# Shared session with retry/backoff for every Shopify REST + GraphQL call.
# Plain single-attempt requests.* calls were turning ordinary transient
# connect-timeouts (seen against the live store) into permanent failures —
# skipped collection assignments, unlinked variant images, etc. — even
# though the underlying product had published successfully. This does NOT
# retry the product-creation POST itself with different data, only the
# same request; product creation is still wrapped by the caller's own
# try/except so a persistent failure there still surfaces as a failed
# listing rather than silently retrying a possibly-duplicate create.
_shopify_session = requests.Session()
_shopify_retry = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST", "PUT"],
)
_shopify_session.mount("https://", HTTPAdapter(max_retries=_shopify_retry))
_shopify_session.mount("http://", HTTPAdapter(max_retries=_shopify_retry))


# ── Registry helpers ──────────────────────────────────────────────────────────

def _load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            with open(REGISTRY_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_registry(registry: dict) -> None:
    """Save registry, auto-purging entries older than TTL."""
    cutoff = datetime.now() - timedelta(days=REGISTRY_TTL_DAYS)
    registry = {
        pid: entry for pid, entry in registry.items()
        if datetime.fromisoformat(entry.get("published_at", "2000-01-01")) > cutoff
    }
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)


def _register_product(cj_pid: str, product_id: str, title: str) -> None:
    """Add a newly published product to the registry."""
    registry = _load_registry()
    registry[cj_pid] = {
        "shopify_product_id": product_id,
        "title":              title,
        "published_at":       datetime.now().isoformat(),
    }
    _save_registry(registry)


def _remove_from_registry(cj_pid: str) -> None:
    """Remove a cj_pid from registry (e.g. product was deleted from Shopify)."""
    registry = _load_registry()
    registry.pop(cj_pid, None)
    _save_registry(registry)


# ── Shopify helpers (REST) ──────────────────────────────────────────────────────

def _shopify_headers() -> dict:
    return {
        "X-Shopify-Access-Token": os.getenv("SHOPIFY_ACCESS_TOKEN", ""),
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    store = os.getenv("SHOPIFY_STORE_URL", "").rstrip("/")
    return f"https://{store}/admin/api/{API_VERSION}"


def _shopify_product_exists(product_id: str) -> bool:
    """
    Quick check if a product still exists on Shopify.
    Only fetches the id field — minimal data transfer.
    Returns True on network errors (safer to skip than create duplicate).
    """
    try:
        resp = _shopify_session.get(
            f"{_base_url()}/products/{product_id}.json",
            headers=_shopify_headers(),
            params={"fields": "id"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return True   # assume exists on network error


# ── Shopify helpers (GraphQL — video attach + reorder) ──────────────────────────

def _graphql_url() -> str:
    store = os.getenv("SHOPIFY_STORE_URL", "").rstrip("/")
    return f"https://{store}/admin/api/{API_VERSION}/graphql.json"


def _graphql_request(query: str, variables: dict) -> dict:
    """
    Runs a GraphQL Admin API request. Raises on transport/HTTP errors;
    callers are responsible for checking the 'errors' key and any
    mutation-specific userErrors/mediaUserErrors in the returned dict.
    """
    resp = requests.post(
        _graphql_url(),
        headers=_shopify_headers(),
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"Shopify GraphQL error: {data['errors']}")
    return data["data"]


_STAGED_UPLOADS_CREATE = """
mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""

_PRODUCT_CREATE_MEDIA = """
mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
  productCreateMedia(media: $media, productId: $productId) {
    media {
      id
      alt
      mediaContentType
      status
    }
    mediaUserErrors { field message }
  }
}
"""

_PRODUCT_REORDER_MEDIA = """
mutation productReorderMedia($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    job { id }
    mediaUserErrors { field message }
  }
}
"""


def _stage_video_upload(local_path: Path) -> Optional[dict]:
    """Step 1 of video attachment: ask Shopify for a signed upload target."""
    file_size = local_path.stat().st_size
    mime_type = mimetypes.guess_type(str(local_path))[0] or "video/mp4"

    variables = {
        "input": [{
            "resource":    "VIDEO",
            "filename":    local_path.name,
            "mimeType":    mime_type,
            "httpMethod":  "POST",
            "fileSize":    str(file_size),
        }]
    }

    try:
        data = _graphql_request(_STAGED_UPLOADS_CREATE, variables)
        result = data["stagedUploadsCreate"]
        if result["userErrors"]:
            print(f"[StorePublisher]     ⚠️  stagedUploadsCreate error: {result['userErrors']}")
            return None
        targets = result["stagedTargets"]
        return targets[0] if targets else None
    except Exception as e:
        print(f"[StorePublisher]     ⚠️  stagedUploadsCreate failed: {e}")
        return None


def _upload_to_staged_target(local_path: Path, staged_target: dict) -> bool:
    """Step 2 of video attachment: POST the actual video bytes to the signed URL."""
    upload_url = staged_target["url"]
    form_fields = {p["name"]: p["value"] for p in staged_target["parameters"]}

    try:
        with open(local_path, "rb") as f:
            files = {"file": (local_path.name, f, mimetypes.guess_type(str(local_path))[0] or "video/mp4")}
            resp = requests.post(upload_url, data=form_fields, files=files, timeout=120)
        if resp.status_code not in (200, 201, 204):
            print(f"[StorePublisher]     ⚠️  Staged upload returned {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[StorePublisher]     ⚠️  Staged upload failed: {e}")
        return False


def _attach_media_to_product(product_gid: str, resource_url: str) -> Optional[str]:
    """Step 3 of video attachment: attach the uploaded resource as VIDEO media.
    Returns the new media's GID (needed for reordering), or None on failure."""
    variables = {
        "productId": product_gid,
        "media": [{
            "originalSource":     resource_url,
            "mediaContentType":   "VIDEO",
        }],
    }
    try:
        data = _graphql_request(_PRODUCT_CREATE_MEDIA, variables)
        result = data["productCreateMedia"]
        if result["mediaUserErrors"]:
            print(f"[StorePublisher]     ⚠️  productCreateMedia error: {result['mediaUserErrors']}")
            return None
        media_list = result["media"]
        return media_list[0]["id"] if media_list else None
    except Exception as e:
        print(f"[StorePublisher]     ⚠️  productCreateMedia failed: {e}")
        return None


def _reorder_media_to_front(product_gid: str, media_gid: str) -> bool:
    """Step 4: moves the video to position 0 so it leads the gallery."""
    variables = {
        "id": product_gid,
        "moves": [{"id": media_gid, "newPosition": "0"}],
    }
    try:
        data = _graphql_request(_PRODUCT_REORDER_MEDIA, variables)
        result = data["productReorderMedia"]
        if result["mediaUserErrors"]:
            print(f"[StorePublisher]     ⚠️  productReorderMedia error: {result['mediaUserErrors']}")
            return False
        return True
    except Exception as e:
        print(f"[StorePublisher]     ⚠️  productReorderMedia failed: {e}")
        return False


def _attach_videos_to_product(product_id: str, local_video_paths: List[str]) -> int:
    """
    Runs the full upload+attach flow for each local video file, then
    reorders the FIRST successfully attached video to position 0.
    Best-effort throughout — a failure on any individual video (or the
    reorder step) is logged and skipped, never raised.
    """
    if not local_video_paths:
        return 0

    product_gid = f"gid://shopify/Product/{product_id}"
    attached = 0
    first_video_media_gid: Optional[str] = None

    for video_path_str in local_video_paths:
        video_path = Path(video_path_str)
        if not video_path.exists() or not video_path.is_file():
            print(f"[StorePublisher]     ⚠️  Video file not found, skipping: {video_path_str}")
            continue

        staged_target = _stage_video_upload(video_path)
        if not staged_target:
            continue

        if not _upload_to_staged_target(video_path, staged_target):
            continue

        media_gid = _attach_media_to_product(product_gid, staged_target["resourceUrl"])
        if media_gid:
            attached += 1
            print(f"[StorePublisher]     🎬 Attached video: {video_path.name}")
            if first_video_media_gid is None:
                first_video_media_gid = media_gid

    if first_video_media_gid:
        if _reorder_media_to_front(product_gid, first_video_media_gid):
            print(f"[StorePublisher]     🥇 Video moved to first position")
        else:
            print(f"[StorePublisher]     ⚠️  Video attached but reorder-to-front failed "
                  f"(video is still visible, just not first)")

    return attached


# ── Duplicate check ───────────────────────────────────────────────────────────

def _cj_pid_exists(cj_pid: str) -> Optional[str]:
    """
    Three-layer duplicate check:
      1. Not in registry       → new product, publish it
      2. In registry but TTL expired → eligible for refresh, republish
      3. In registry + valid TTL → verify on Shopify:
           - Still exists     → skip (true duplicate)
           - 404 deleted      → remove from registry, republish
    """
    registry = _load_registry()
    if cj_pid not in registry:
        return None

    entry        = registry[cj_pid]
    published_at = datetime.fromisoformat(entry.get("published_at", "2000-01-01"))
    age_days     = (datetime.now() - published_at).days

    if age_days > REGISTRY_TTL_DAYS:
        print(f"[StorePublisher]   🔄 cj_pid {cj_pid} expired ({age_days}d old) — refreshing listing")
        return None

    existing_id = entry["shopify_product_id"]

    if not _shopify_product_exists(existing_id):
        print(f"[StorePublisher]   🔄 Product {existing_id} was deleted from Shopify — republishing")
        _remove_from_registry(cj_pid)
        return None

    print(f"[StorePublisher]   ⚠️  Duplicate — published {age_days}d ago (Shopify id: {existing_id})")
    return existing_id


# ── Collection assignment ─────────────────────────────────────────────────────

def _get_collection_id(collection_name: str) -> Optional[str]:
    """Find Shopify collection ID by title. Checks custom + smart collections."""
    if not collection_name:
        return None
    if collection_name in _collection_cache:
        return _collection_cache[collection_name]

    for endpoint in ["custom_collections", "smart_collections"]:
        try:
            resp = _shopify_session.get(
                f"{_base_url()}/{endpoint}.json",
                headers=_shopify_headers(),
                params={"title": collection_name, "limit": 5},
                timeout=15,
            )
            resp.raise_for_status()
            for col in resp.json().get(endpoint, []):
                if col["title"].strip().lower() == collection_name.strip().lower():
                    cid = str(col["id"])
                    _collection_cache[collection_name] = cid
                    return cid
        except Exception as e:
            print(f"[StorePublisher]   ⚠️  Error searching {endpoint}: {e}")

    print(f"[StorePublisher]   ⚠️  Collection '{collection_name}' not found in Shopify")
    return None


def _assign_to_collection(product_id: str, collection_name: str) -> None:
    """Assign a product to a Shopify collection."""
    collection_id = _get_collection_id(collection_name)
    if not collection_id:
        return
    try:
        resp = _shopify_session.post(
            f"{_base_url()}/collects.json",
            headers=_shopify_headers(),
            json={"collect": {"product_id": product_id, "collection_id": collection_id}},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"[StorePublisher]   ✅ Assigned to collection: {collection_name}")
        elif resp.status_code == 422:
            print(f"[StorePublisher]   ℹ️  Already in collection: {collection_name}")
        else:
            print(f"[StorePublisher]   ⚠️  Collection assign failed: {resp.status_code}")
    except Exception as e:
        print(f"[StorePublisher]   ⚠️  Collection assign error: {e}")


# ── Field validation ───────────────────────────────────────────────────────────

def _validate_listing_pricing(listing: ShopifyListing) -> None:
    """
    Fail fast with a specific, actionable error if pricing fields are missing,
    instead of letting an f-string format spec raise a generic
    'unsupported format string passed to NoneType.__format__' deep inside
    the Shopify payload construction.
    """
    if listing.retail_price is None:
        raise ValueError(
            f"retail_price is None for cj_pid={listing.cj_pid} "
            f"('{listing.original_title}') — check the Layer 2 pricing step "
            f"that builds this ShopifyListing, it never set retail_price."
        )
    if listing.supplier_price is None:
        raise ValueError(
            f"supplier_price is None for cj_pid={listing.cj_pid} "
            f"('{listing.original_title}') — check that Layer 1's supplier_price "
            f"survived the handoff into Layer 2."
        )


def _safe_compare_at_price(listing: ShopifyListing) -> Optional[float]:
    """
    compare_at_price ('was' price / strikethrough) is intentionally left
    unset unless PriceOptimizer explicitly computed a real one (see
    layer2/nodes/price_optimizer.py — compare_at is deliberately None for
    every product right now; no fake discounts). Returning None here (not
    retail_price) means Shopify's Admin shows an empty compare-at field,
    not a confusing "compare price == price" — the payload builder below
    must handle None by omitting the key, not stringifying it.
    """
    return listing.compare_at_price


# ── Variant/option splitting heuristics ─────────────────────────────────────────

_KNOWN_COLOR_WORDS = {
    "black", "white", "red", "blue", "green", "yellow", "pink", "purple",
    "orange", "brown", "grey", "gray", "beige", "cream", "gold", "silver",
    "rose", "navy", "teal", "mint", "apricot", "nude", "khaki", "maroon",
    "turquoise", "lavender", "coral", "haze", "wine", "burgundy", "ivory",
    "tan", "olive", "mustard", "peach", "lilac", "charcoal", "camel",
    "badge", "basil", "carmine", "cherry", "chestnut", "frost", "graphite",
    "sky", "light",
}

_SIZE_PATTERN_WORDS = {
    "xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xl", "3xl", "4xl",
    "one size", "os", "small", "medium", "large",
}

# "1 PC", "2 pcs", "1 pair", "3 sets" — a quantity/unit count, NOT a
# physical size. Checked BEFORE the size check, since e.g. "1 PC" contains
# a digit and would otherwise risk being misread as a size.
_PACK_PATTERN = re.compile(r'^\d+\s*(pc|pcs|piece|pieces|pair|pairs|set|sets)$', re.IGNORECASE)


def _looks_like_pack(value: str) -> bool:
    return bool(_PACK_PATTERN.match(value.strip()))


def _looks_like_color(value: str) -> bool:
    lowered = value.lower()
    return any(word in lowered for word in _KNOWN_COLOR_WORDS)


def _looks_like_size(value: str) -> bool:
    """
    Matches REAL size indicators only:
      - classic size words: S / M / L / XL / etc.
      - measurements with units: "200cm", "15 mm", "10in", '12"'
      - purely numeric values (ignoring spaces): "38", "39 40", "6 7"

    Deliberately does NOT match on "contains any digit" — that was too
    greedy and misclassified things like "Style 1", "Style 2" (arbitrary
    design numbering, nothing to do with physical size) as a size column.
    """
    lowered = value.strip().lower()

    if lowered in _SIZE_PATTERN_WORDS:
        return True

    if re.search(r'\d+\s*(cm|mm|m|in|inch|inches|")', lowered):
        return True

    stripped = lowered.replace(" ", "")
    if stripped.isdigit():
        return True

    return False


def _classify_column(values: List[str]) -> Optional[str]:
    """
    Classifies an entire option column (all values on one side of the
    hyphen, across every variant) as one of: "pack", "color", "size", or
    None (heuristic genuinely unsure — caller should try the LLM fallback).

    Checked in priority order pack > color > size specifically because
    "1 PC" contains a digit and would otherwise wrongly match "size"
    before we get a chance to recognize it as a pack/unit count.

    POSITION-INDEPENDENT: we don't assume "first part is always color" —
    a yoga mat's variantKey is "200cm 80cm 15cm 2-Black" (size FIRST,
    color SECOND), the opposite order from clothing's "Badge Blue-S"
    (color first, size second). Classifying each column on its own
    content, not its position, handles both correctly.
    """
    if any(_looks_like_pack(v) for v in values):
        return "pack"
    if any(_looks_like_color(v) for v in values):
        return "color"
    if any(_looks_like_size(v) for v in values):
        return "size"
    return None


# ── LLM fallback for ambiguous option naming ────────────────────────────────
# The heuristic above handles the vast majority of CJ products for free
# and instantly. It only fails to produce a confident label when a
# column's values don't match any known color/size/pack pattern (e.g. a
# material name, a scent, or a design-numbering scheme we've never seen).
# Rather than keep growing keyword lists forever (we've already hit
# several false-positive bugs doing that), we fall back to ONE small
# GPT-4o-mini call — but ONLY to pick the two option NAMES (e.g.
# "Material", "Style"). The LLM never sees or touches the actual variant
# values/prices/images — it just labels the two columns we already split
# deterministically. If the call fails for any reason, we fall back to
# generic "Style" / "Type" labels — the product still publishes correctly
# either way.

_option_naming_llm = None


def _get_option_naming_llm():
    global _option_naming_llm
    if _option_naming_llm is None:
        try:
            _option_naming_llm = ChatOpenAI(
                model="gpt-4o-mini",
                temperature=0,
                openai_api_key=os.getenv("OPENAI_API_KEY"),
            )
        except Exception as e:
            print(f"[StorePublisher]     ⚠️  Could not init option-naming LLM: {e}")
            _option_naming_llm = False
    return _option_naming_llm


def _llm_classify_option_names(col1_values: List[str], col2_values: List[str]) -> Optional[tuple]:
    """
    Asks GPT-4o-mini to name two Shopify option columns, given a sample of
    each column's distinct values. Returns (name1, name2) or None on any
    failure (missing API key, network error, malformed response) — callers
    must have a non-LLM fallback ready.

    Deliberately constrained to ONLY return two short labels — never asked
    to reformat, correct, or reinterpret the actual values, so there is no
    path by which this call can corrupt real variant data.
    """
    llm = _get_option_naming_llm()
    if not llm:
        return None

    sample1 = col1_values[:8]
    sample2 = col2_values[:8]

    prompt = (
        "You are labeling two columns of an e-commerce product's variant options.\n\n"
        f"Column 1 example values: {sample1}\n"
        f"Column 2 example values: {sample2}\n\n"
        "Pick a short (1-2 word) label for EACH column describing what it represents "
        "(e.g. 'Color', 'Size', 'Material', 'Style', 'Pack', 'Scent', 'Length'). "
        "The two labels must be different from each other.\n\n"
        "Return ONLY valid JSON, no markdown, no explanation:\n"
        '{"option1_name": "...", "option2_name": "..."}'
    )

    try:
        response = llm.invoke([
            SystemMessage(content="You are a precise e-commerce data labeling assistant."),
            HumanMessage(content=prompt),
        ])
        content = response.content.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(content)

        name1 = str(data.get("option1_name", "")).strip()
        name2 = str(data.get("option2_name", "")).strip()

        if not name1 or not name2 or name1.lower() == name2.lower():
            print(f"[StorePublisher]     ⚠️  LLM returned invalid/duplicate option names: {data}")
            return None

        print(f"[StorePublisher]     🤖 LLM classified columns: '{name1}' / '{name2}'")
        return name1, name2

    except Exception as e:
        print(f"[StorePublisher]     ⚠️  Option-naming LLM call failed: {e}")
        return None


def _split_variant_value(value: str) -> Optional[tuple]:
    """
    Splits CJ variant keys handling both "-" and " - " separators.
    Returns (first_part, second_part) or None if can't split cleanly.

    Examples:
        "Style 1-1 pair"      → ("Style 1", "1 pair")
        "Style 1 - 1 pair"    → ("Style 1", "1 pair")
        "Red-S"               → ("Red", "S")
        "Red - S"             → ("Red", "S")
        "Badge Blue-S"        → ("Badge Blue", "S")
        "200cm 80cm 2-Black"  → ("200cm 80cm 2", "Black")
    """
    # Try " - " first (more specific)
    if " - " in value:
        parts = value.split(" - ", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        if left and right:
            return (left, right)

    # Fallback to single "-" (current behavior)
    if "-" in value:
        parts = value.split("-", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        if left and right:
            return (left, right)

    return None  # No clean split


def _derive_option_columns(variants_raw: List[dict]) -> tuple:
    """
    Splits CJ's combined variantKey (e.g. "Badge Blue-S", "Style 1-1 PC",
    "200cm 80cm 15cm 2-Black") into two real Shopify options whenever MOST
    variants in the batch split cleanly on the first hyphen (>=60%) — not
    ALL of them, since a single outlier used to silently collapse the
    whole product into one giant combined "Style" dropdown.

    Labeling order:
      1. Free heuristic (_classify_column) — instant, no cost. Handles
         color/size/pack patterns.
      2. If either column is unclassified, or both classified the same
         way — ONE small GPT-4o-mini call to name them semantically.
      3. If the LLM also fails/is unavailable — generic "Style"/"Type"
         fallback labels. Product always still publishes.

    Returns (option_defs, per_variant_values) where:
      option_defs        = [{"name": ..., "values": [...]}, ...]  (1 or 2 entries)
      per_variant_values  = [(val,) or (val1, val2), ...]  same order as variants_raw
    """
    split_results = []
    for v in variants_raw:
        result = _split_variant_value(v["option_value"])
        if result:
            split_results.append(result)
        else:
            split_results.append(None)

    total = len(variants_raw)
    success_count = sum(1 for r in split_results if r is not None)
    can_split_two = total >= 2 and (success_count / total) >= 0.6

    if not can_split_two:
        values = [v["option_value"] for v in variants_raw]
        seen, unique_values = set(), []
        for val in values:
            if val not in seen:
                seen.add(val)
                unique_values.append(val)
        print(f"[StorePublisher]     ℹ️  Only {success_count}/{total} variants split cleanly "
              f"— using single 'Style' option instead of two columns")
        return (
            [{"name": "Style", "values": unique_values}],
            [(val,) for val in values],
        )

    FALLBACK_SECOND_VALUE = "Standard"
    firsts, seconds = [], []
    for result, v in zip(split_results, variants_raw):
        if result is None:
            print(f"[StorePublisher]     ⚠️  Variant '{v['option_value']}' didn't split cleanly "
                  f"— using fallback second value '{FALLBACK_SECOND_VALUE}'")
            firsts.append(v["option_value"].strip())
            seconds.append(FALLBACK_SECOND_VALUE)
        else:
            firsts.append(result[0])
            seconds.append(result[1])

    col1_type = _classify_column(firsts)
    col2_type = _classify_column(seconds)

    LABELS = {"pack": "Pack", "color": "Color", "size": "Size"}
    name1 = LABELS.get(col1_type)
    name2 = LABELS.get(col2_type)

    # Heuristic couldn't confidently label one/both columns, or both
    # landed on the same label — try the LLM once instead of settling
    # for generic placeholders.
    needs_llm = (name1 is None) or (name2 is None) or (name1 == name2)

    if needs_llm:
        llm_result = _llm_classify_option_names(firsts, seconds)
        if llm_result:
            name1, name2 = llm_result

    # Final safety net if heuristic AND LLM both came up empty/failed
    if not name1:
        name1 = "Style"
    if not name2 or name2 == name1:
        name2 = "Type" if name1 != "Type" else "Option 2"

    def _unique_preserve_order(values: List[str]) -> List[str]:
        seen, out = set(), []
        for v in values:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    option_defs = [
        {"name": name1, "values": _unique_preserve_order(firsts)},
        {"name": name2, "values": _unique_preserve_order(seconds)},
    ]
    per_variant_values = list(zip(firsts, seconds))

    option_defs, per_variant_values = _drop_degenerate_column(option_defs, per_variant_values)
    return option_defs, per_variant_values


def _drop_degenerate_column(option_defs: list, per_variant_values: list) -> tuple:
    """
    Safety net: if either derived option column has only ONE unique value
    across all variants (e.g. CJ tagging a static attribute like
    "Category: Accessories" as if it were a real customer-facing choice),
    that column adds no selection value and just clutters the storefront
    with a useless one-choice dropdown. Drops it and collapses to a single
    meaningful option instead.

    No-op (returns inputs unchanged) if there's only 1 option column
    already, or if BOTH columns are somehow degenerate (rare edge case —
    leave as-is rather than guess which one to keep).
    """
    if len(option_defs) < 2:
        return option_defs, per_variant_values

    col1_unique = len(set(v[0] for v in per_variant_values))
    col2_unique = len(set(v[1] for v in per_variant_values))

    if col1_unique <= 1 and col2_unique <= 1:
        return option_defs, per_variant_values

    if col2_unique <= 1:
        print(f"[StorePublisher]     ℹ️  Dropping degenerate option "
              f"'{option_defs[1]['name']}' (only 1 value: {option_defs[1]['values']})")
        return [option_defs[0]], [(v[0],) for v in per_variant_values]

    if col1_unique <= 1:
        print(f"[StorePublisher]     ℹ️  Dropping degenerate option "
              f"'{option_defs[0]['name']}' (only 1 value: {option_defs[0]['values']})")
        return [option_defs[1]], [(v[1],) for v in per_variant_values]

    return option_defs, per_variant_values


# ── Main node ─────────────────────────────────────────────────────────────────

def store_publisher_node(state: GraphState) -> dict:
    """
    Publishes all image-ready listings to Shopify immediately.
    Skips true duplicates. Republishes deleted or expired products.
    Attaches any downloaded videos after a successful product creation,
    and reorders media so the video leads the gallery.
    """
    print("\n[StorePublisher] 🚀 Publishing to Shopify...")
    errors  = list(state.get("errors", []))
    listings = state.get("image_ready_listings", [])

    if not listings:
        errors.append("StorePublisher: No listings to publish.")
        return {"published_listings": [], "failed_listings": [], "errors": errors}

    published: List[ShopifyListing] = []
    failed:    List[dict]           = []
    skipped:   List[str]            = []
    total_videos_attached = 0

    for i, listing in enumerate(listings):
        print(f"[StorePublisher]   [{i+1}/{len(listings)}] {listing.shopify_title[:50]}...")

        existing_id = _cj_pid_exists(listing.cj_pid)
        if existing_id:
            skipped.append(listing.cj_pid)
            listing.shopify_product_id = existing_id
            listing.published = False
            print(f"[StorePublisher]   ⏭️  Skipped: {listing.shopify_title[:45]}")
            continue

        try:
            _validate_listing_pricing(listing)
            product_id, product_url = _publish_product(listing)
            listing.shopify_product_id  = product_id
            listing.shopify_product_url = product_url
            listing.published = True
            published.append(listing)
            print(f"[StorePublisher]   ✅ Live at: {product_url}")

            _register_product(listing.cj_pid, product_id, listing.shopify_title)

            collection = getattr(listing, "collection", "") or ""
            if collection:
                _assign_to_collection(product_id, collection)

            video_paths = getattr(listing, "processed_video_urls", None) or []
            if video_paths:
                print(f"[StorePublisher]   🎬 Attaching {len(video_paths)} video(s)...")
                n_attached = _attach_videos_to_product(product_id, video_paths)
                total_videos_attached += n_attached
                if n_attached < len(video_paths):
                    print(f"[StorePublisher]   ⚠️  Only {n_attached}/{len(video_paths)} video(s) attached")

        except Exception as e:
            error_msg = f"StorePublisher failed for '{listing.original_title}': {e}"
            print(f"[StorePublisher]   ❌ {error_msg}")
            errors.append(error_msg)
            failed.append({
                "title":  listing.original_title,
                "cj_pid": listing.cj_pid,
                "error":  str(e),
            })

    print(f"\n[StorePublisher] ✅ Published: {len(published)} | "
          f"⏭️  Skipped: {len(skipped)} | ❌ Failed: {len(failed)} | "
          f"🎬 Videos attached: {total_videos_attached}")
    _print_summary(published)

    return {
        "published_listings": published,
        "failed_listings":    failed,
        "errors":             errors,
    }


# ── Product creation ──────────────────────────────────────────────────────────

def _post_product_with_safe_retry(payload: dict, attempts: int = 3):
    """
    POSTs the product-creation payload with a narrow retry: only retries
    when the connection itself never succeeded (DNS/connect timeout —
    Shopify never saw the request), NOT on a read-timeout after the
    request was sent, since Shopify may have already created the product
    server-side in that case and a blind retry would create a duplicate.
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return requests.post(
                f"{_base_url()}/products.json",
                headers=_shopify_headers(),
                json=payload,
                timeout=(10, 30),
            )
        except requests.exceptions.ConnectionError as e:
            last_err = e
            print(f"[StorePublisher]   ⚠️  Connect failed (attempt {attempt}/{attempts}), retrying: {e}")
    raise last_err


def _publish_product(listing: ShopifyListing) -> tuple[str, str]:
    """
    Full Shopify product creation via REST. Returns (product_id, product_url).

    Builds either:
      - TWO real options (e.g. Color + Size, Style + Pack) when CJ's
        variantKey splits consistently enough, giving a clean two-dropdown
        selector, or
      - ONE combined "Style" option as a fallback when it doesn't.

    Variant images are reused from the general gallery when CJ's photo is
    already one of the product's own images (the common case — mirrors
    CJ's own manual listing flow), or uploaded fresh otherwise.

    inventory_policy is set to "continue" so customers can always order
    more quantity of the same variant.

    NOTE: video attachment happens separately, AFTER this call succeeds —
    REST product creation has no video field.
    """
    bullets_html = "".join(f"<li>{b}</li>" for b in listing.bullet_points)
    full_description = (
        f"{listing.shopify_description}<ul>{bullets_html}</ul>"
        if listing.bullet_points else listing.shopify_description
    )
    category = getattr(listing, "category", "") or ""
    compare_at_price = _safe_compare_at_price(listing)
    compare_at_str = f"{compare_at_price:.2f}" if compare_at_price is not None else None
    # ── Handle Tags ──────────────────────────────────────────────────────────
    # Get tags from shopify_tags first, then fall back to tags
    raw_tags = getattr(listing, "shopify_tags", []) or getattr(listing, "tags", [])

    # Clean and validate tags
    clean_tags = []
    seen_tags = set()  # For deduplication

    for tag in raw_tags:
        if tag and isinstance(tag, str):
            # Remove commas and clean
            clean_tag = tag.replace(",", "").strip()
            if clean_tag and clean_tag not in seen_tags:
                clean_tags.append(clean_tag)
                seen_tags.add(clean_tag)

    # ── Add Collection Tags ──────────────────────────────────────────────────
    # Add collection if available and not already present
    collection = getattr(listing, "collection", None)
    if collection:
        collection_tag = f"collection:{collection}"
        if collection_tag not in seen_tags:
            clean_tags.append(collection_tag)
            seen_tags.add(collection_tag)
            print(f"[StorePublisher]   🏷️  Added collection tag: {collection_tag}")

    # Add sub_collection if available and not already present
    sub_collection = getattr(listing, "sub_collection", None) or getattr(listing, "subcategory", None)
    if sub_collection:
        # Check if already exists in any format (e.g., "Sub: Tops" or "sub:Tops")
        sub_exists = False
        sub_lower = sub_collection.lower()

        # Check existing tags for any sub-collection variant
        for existing_tag in clean_tags:
            existing_lower = existing_tag.lower()
            # Check for "Sub: X", "sub:X", "subcategory:X", "Subcategory:X"
            if any(prefix in existing_lower for prefix in ["sub:", "subcategory:", "sub category:"]):
                # Extract the value after the prefix
                for prefix in ["sub:", "subcategory:", "sub category:"]:
                    if prefix in existing_lower:
                        existing_value = existing_lower.split(prefix)[-1].strip()
                        if existing_value == sub_lower:
                            sub_exists = True
                            break
                if sub_exists:
                    break

        if not sub_exists:
            sub_tag = f"sub:{sub_collection}"
            clean_tags.append(sub_tag)
            seen_tags.add(sub_tag)
            print(f"[StorePublisher]   🏷️  Added sub-collection tag: {sub_tag}")

    # Add category as tag if available and not already present
    if category:
        category_tag = f"category:{category}"
        if category_tag not in seen_tags:
            clean_tags.append(category_tag)
            seen_tags.add(category_tag)
            print(f"[StorePublisher]   🏷️  Added category tag: {category_tag}")

    # Add trend keyword as tag if available
    trend_keyword = getattr(listing, "trend_keyword", None)
    if trend_keyword:
        trend_tag = f"trend:{trend_keyword}"
        if trend_tag not in seen_tags:
            clean_tags.append(trend_tag)
            seen_tags.add(trend_tag)
            print(f"[StorePublisher]   🏷️  Added trend tag: {trend_tag}")

    # Limit to 250 tags (Shopify's limit)
    clean_tags = clean_tags[:250]

    # Log final tags
    if clean_tags:
        print(f"[StorePublisher]   🏷️  Final tags ({len(clean_tags)} total): {', '.join(clean_tags)}")
    else:
        print(f"[StorePublisher]   ℹ️  No tags to apply")

    # ── General gallery images ──────────────────────────────────────────────
    general_images = _prepare_images(listing)
    url_to_general_index = {
        url: i for i, url in enumerate(listing.image_urls[:len(general_images)])
    }

    variants_raw = getattr(listing, "variants", None) or []
    has_real_variants = len(variants_raw) >= 2

    # ── Plan variant images: reuse existing gallery image, or queue new upload ──
    variant_image_plan: List[Optional[tuple]] = []
    new_variant_images: List[dict] = []

    if has_real_variants:
        for v in variants_raw:
            img_url = v.get("image")
            if not img_url:
                variant_image_plan.append(None)
            elif img_url in url_to_general_index:
                variant_image_plan.append(("existing", url_to_general_index[img_url]))
            else:
                new_variant_images.append({"src": img_url})
                variant_image_plan.append(("new", len(new_variant_images) - 1))

    all_images = general_images + new_variant_images
    num_general = len(general_images)

    # ── Options + variants payload ──────────────────────────────────────────
    if has_real_variants:
        option_defs, per_variant_values = _derive_option_columns(variants_raw)

        # Log variant options
        if len(option_defs) == 2:
            print(f"[StorePublisher]   🎨 {len(variants_raw)} variants with {len(option_defs)} options: "
                  f"{option_defs[0]['name']} + {option_defs[1]['name']}")
        else:
            print(f"[StorePublisher]   🎨 {len(variants_raw)} variants with single option: {option_defs[0]['name']}")

        variants_payload = []
        for v, values in zip(variants_raw, per_variant_values):
            # Defensive .get(): PriceOptimizer is expected to set
            # retail_price on every variant, but if a variant slipped
            # through without one, fall back to the listing price rather
            # than KeyError-ing and failing the whole product publish.
            variant_price = v.get("retail_price")
            if variant_price is None:
                print(f"[StorePublisher]     ⚠️  Variant '{v.get('option_value', '?')}' "
                      f"missing retail_price — using listing price ${listing.retail_price:.2f}")
                variant_price = listing.retail_price

            # Get variant weight if available
            variant_weight = v.get("weight")

            entry = {
                "price": f"{variant_price:.2f}",
                "sku": v.get("sku", ""),
                "inventory_management": "shopify",
                "inventory_quantity": 999,
                "inventory_policy": "continue",
                "fulfillment_service": "manual",
                "requires_shipping": True,
            }
            if compare_at_str is not None:
                entry["compare_at_price"] = compare_at_str

            # Add weight if available (in grams)
            if variant_weight:
                entry["weight"] = variant_weight

            entry["option1"] = values[0]
            if len(values) > 1:
                entry["option2"] = values[1]
            variants_payload.append(entry)

        options_payload = option_defs
    else:
        options_payload = []
        variants_payload = [{
            "price": f"{listing.retail_price:.2f}",
            "inventory_management": "shopify",
            "inventory_quantity": 999,
            "inventory_policy": "continue",
            "fulfillment_service": "manual",
            "requires_shipping": True,
        }]
        if compare_at_str is not None:
            variants_payload[0]["compare_at_price"] = compare_at_str

    # ── Build Metafields ────────────────────────────────────────────────────
    metafields = [
        {"namespace": "global", "key": "title_tag", "value": listing.seo_title, "type": "single_line_text_field"},
        {"namespace": "global", "key": "description_tag", "value": listing.seo_description,
         "type": "multi_line_text_field"},
        {"namespace": "dropship", "key": "supplier_pid", "value": listing.cj_pid, "type": "single_line_text_field"},
        {"namespace": "dropship", "key": "supplier_cost", "value": str(listing.supplier_price),
         "type": "single_line_text_field"},
    ]

    # Add supplier URL if available
    if hasattr(listing, "cj_product_url") and listing.cj_product_url:
        metafields.append({
            "namespace": "dropship",
            "key": "supplier_url",
            "value": listing.cj_product_url,
            "type": "single_line_text_field"
        })

    # Add discovery date if available
    if hasattr(listing, "discovery_date") and listing.discovery_date:
        metafields.append({
            "namespace": "dropship",
            "key": "discovery_date",
            "value": listing.discovery_date,
            "type": "single_line_text_field"
        })

    # Add source if available
    if hasattr(listing, "source") and listing.source:
        metafields.append({
            "namespace": "dropship",
            "key": "source",
            "value": listing.source,
            "type": "single_line_text_field"
        })

    # ── Build Final Payload ─────────────────────────────────────────────────
    product_payload = {
        "product": {
            "title": listing.shopify_title,
            "body_html": full_description,
            "vendor": "aurelle",
            "product_type": category,
            "tags": ", ".join(clean_tags),  # Join with commas for Shopify
            "status": "active",
            "options": options_payload,
            "variants": variants_payload,
            "images": all_images,
            "metafields": metafields,
        }
    }

    # ── Debug: Log payload summary ──────────────────────────────────────────
    compare_at_display = f"${compare_at_price:.2f}" if compare_at_price is not None else "— (none)"
    print(f"[StorePublisher]     - Compare at: {compare_at_display}")
    print(f"[StorePublisher]   📦 Creating product with:")
    print(f"[StorePublisher]     - Title: {listing.shopify_title[:60]}...")
    print(f"[StorePublisher]     - Price: ${listing.retail_price:.2f}")
    print(f"[StorePublisher]     - Variants: {len(variants_payload)}")
    print(f"[StorePublisher]     - Images: {len(all_images)}")
    print(f"[StorePublisher]     - Tags: {len(clean_tags)}")
    print(f"[StorePublisher]     - Metafields: {len(metafields)}")

    # ── Send to Shopify ─────────────────────────────────────────────────────
    resp = _post_product_with_safe_retry(product_payload)

    if resp.status_code not in (200, 201):
        error_detail = resp.text[:500]
        print(f"[StorePublisher]   ❌ Shopify API error {resp.status_code}: {error_detail}")
        raise ValueError(f"Shopify API error {resp.status_code}: {error_detail}")

    data = resp.json()["product"]
    product_id = str(data["id"])
    store_url = os.getenv("SHOPIFY_STORE_URL", "").rstrip("/")
    product_url = f"https://{store_url}/products/{data['handle']}"

    # ── Verify tags were set correctly ──────────────────────────────────────
    tags_from_response = data.get("tags", "")
    if tags_from_response:
        print(
            f"[StorePublisher]   ✅ Product created with tags: {tags_from_response[:100]}{'...' if len(tags_from_response) > 100 else ''}")
    else:
        print(f"[StorePublisher]   ⚠️  No tags returned in Shopify response!")

    # ── Link variant images ─────────────────────────────────────────────────
    if has_real_variants:
        try:
            _link_variant_images(data, num_general, variant_image_plan)
        except Exception as e:
            print(f"[StorePublisher]     ⚠️  Variant-image linking failed (product still live): {e}")

    # ── Log success ─────────────────────────────────────────────────────────
    print(f"[StorePublisher]   ✅ Product live at: {product_url}")
    print(f"[StorePublisher]   📝 Shopify ID: {product_id}")

    return product_id, product_url

def _link_variant_images(data: dict, num_general_images: int, variant_image_plan: list) -> None:
    """
    Links each variant to its photo. Handles both cases:
      - ("existing", i): the variant's photo was already one of the
        general gallery images — point the variant straight at that image.
      - ("new", j): the variant had a photo NOT already in the gallery —
        it was appended after general_images in the submitted array, so
        its response position is num_general_images + j.
      - None: no image for this variant, nothing to link.

    Shopify returns product["images"] and product["variants"] in the same
    order they were submitted, so both cases resolve via simple index math.
    """
    response_images   = data.get("images", [])
    response_variants = data.get("variants", [])

    for i, plan in enumerate(variant_image_plan):
        if plan is None or i >= len(response_variants):
            continue

        kind, ref = plan
        img_index = ref if kind == "existing" else num_general_images + ref

        if img_index >= len(response_images):
            print(f"[StorePublisher]     ⚠️  Image index out of range for variant {i}, skipping")
            continue

        image_id   = response_images[img_index]["id"]
        variant_id = response_variants[i]["id"]

        try:
            resp = _shopify_session.put(
                f"{_base_url()}/variants/{variant_id}.json",
                headers=_shopify_headers(),
                json={"variant": {"id": variant_id, "image_id": image_id}},
                timeout=15,
            )
            if resp.status_code == 200:
                tag = "reused" if kind == "existing" else "new"
                opts = [
                    response_variants[i].get("option1"),
                    response_variants[i].get("option2"),
                ]
                label = " ".join(o for o in opts if o)
                print(f"[StorePublisher]     🎨 Linked {tag} image → {label}")
            else:
                print(f"[StorePublisher]     ⚠️  Variant image link failed ({resp.status_code}) "
                      f"for variant {i}")
        except requests.RequestException as e:
            print(f"[StorePublisher]     ⚠️  Variant image link error: {e}")


def _prepare_images(listing: ShopifyListing) -> List[dict]:
    images = []
    for img_ref in listing.processed_image_urls[:10]:
        if not img_ref:
            continue
        path = Path(img_ref)
        if path.exists() and path.is_file():
            with open(path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
            images.append({"attachment": encoded, "filename": path.name})
        elif img_ref.startswith("http"):
            images.append({"src": img_ref})
    return images


def _print_summary(published: List[ShopifyListing]) -> None:
    if not published:
        return
    print("\n" + "="*65)
    print("  LAYER 2 COMPLETE — PRODUCTS LIVE ON SHOPIFY")
    print("="*65)
    for p in published:
        compare_price = p.compare_at_price if p.compare_at_price is not None else p.retail_price
        margin = p.margin_pct if p.margin_pct is not None else 0.0
        n_videos = len(getattr(p, "processed_video_urls", []) or [])
        n_variants = len(getattr(p, "variants", []) or [])
        print(f"\n  {p.shopify_title}")
        print(f"  Price:  ${p.retail_price:.2f}  (was ${compare_price:.2f})")
        print(f"  Margin: {margin:.1f}%")
        if n_variants >= 2:
            print(f"  Variants: {n_variants}")
        if n_videos:
            print(f"  Video:  {n_videos} attached (leading position)")
        print(f"  URL:    {p.shopify_product_url}")
    print("\n  → Layer 3 (Order Router) will handle incoming orders")
    print("="*65 + "\n")