"""
Shared GraphState for Layer 1 - Market Intelligence Pipeline
All nodes read from and write to this TypedDict.
"""

from typing import TypedDict, List, Optional
from pydantic import BaseModel


class Product(BaseModel):
    """A candidate product discovered by the pipeline."""
    title: str
    cj_pid: str                  # CJ Dropshipping product ID
    category: str
    supplier_price: float        # USD, cost from CJ
    suggested_retail: float      # USD, recommended sell price
    margin_pct: float            # (retail - supplier) / retail * 100
    shipping_days: int           # estimated delivery days
    rating: float                # CJ supplier rating 0-5
    orders_count: int            # total orders on CJ (social proof)
    trend_keyword: str           # keyword that surfaced this product
    trend_score: int             # Google Trends interest score 0-100
    image_url: str
    image_urls: List[str] = []   # all product images from CJ detail
    video_urls: List[str] = []   # all product videos from CJ detail (may be empty)
    variants: List[dict] = []    # NEW: [{vid, sku, option_value, image, supplier_price, retail_price, weight}, ...]
    cj_product_url: str
    score: float = 0.0           # composite score assigned by Scorer node
    collection: str = ""         # Shopify collection assigned by LLM scorer

    # ── NEW fields ─────────────────────────────────────────────────────────
    source: str = "seed"         # "trend" | "seed" | "new_arrival" | "region_trend"
    discovery_date: str = ""     # ISO date string, set at parse time
    shopify_tags: List[str] = [] # e.g. ["Trending Now", "New In", "Trend: Korea"]

    collection: str = ""  # Shopify collection assigned by LLM scorer
    sub_collection: str = ""  # NEW: Shopify sub-collection assigned by LLM scorer

    # ── Regional trends ──────────────────────────────────────────────────
    region: str = ""             # "" for non-regional products, else region key
                                  # e.g. "US", "Europe", "Korea", "Japan", "LatinAmerica"
    region_label: str = ""       # human-readable label, e.g. "South Korea"


class GraphState(TypedDict):
    """
    The single source of truth flowing through all Layer 1 nodes.
    Each node receives this dict and returns a partial update.
    """
    # --- inputs ---
    niches: List[str]                      # e.g. ["general"] or specific categories
    max_trends: int                        # how many TREND-derived keywords to keep (Google+TikTok+IG, post-LLM aggregation)
    max_seed_keywords: Optional[int]       # NEW: how many SEED_KEYWORDS to use; None/0 = unlimited (all ~250)
    max_products_per_keyword: int          # how many CJ products per keyword

    # --- regional trends inputs ---
    regions: List[str]                     # region keys to fetch, e.g. ["US","Europe","Korea","Japan","LatinAmerica"]
    max_region_trends: int                 # how many trend keywords to keep PER region

    # --- intermediate ---
    trend_keywords: List[str]              # output of TrendScout node (from Google Trends + social)
    seed_keywords: List[str]              # (possibly capped) SEED_KEYWORDS passed directly to CJ
    trend_scores: List[tuple]             # [(keyword, score), ...] — avoids re-fetching
    region_trend_keywords: dict           # {region_key: [(keyword, score), ...], ...} — per-region trends
    raw_products: List[dict]              # output of SupplierFinder node (raw CJ JSON)
    scored_products: List[Product]        # output of Scorer node



    # --- output ---
    winning_products: List[Product]       # final ranked shortlist
    report_path: Optional[str]            # path to saved JSON/CSV report
    errors: List[str]                     # any non-fatal errors encountered

    # --- dedup cache ---
    seen_pids_path: str                   # path to seen_pids.json for cross-run dedup