"""
Layer 1 Agent Nodes
===================
TrendScout  →  SupplierFinder  →  Scorer
"""

import os
import json
import time
from typing import List
from datetime import date
from collections import defaultdict

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from layer1.state import GraphState, Product
from layer1.tools.apify_trends import (
    get_trending_keywords, get_social_trending_keywords,
    SEED_KEYWORDS, build_random_seed_list,
)
from layer1.tools.cj_api import search_products, parse_cj_product
from config_loader import get_layer_config


_llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0.2,
    openai_api_key=os.getenv("OPENAI_API_KEY"),
)


# ── Collection definitions ────────────────────────────────────────────────────

COLLECTIONS = {

    # ==============================================
    # 1. FASHION 👗
    # ==============================================
    "Fashion": {
        "description": (
            "Modern fashion essentials curated for confident women — dresses, tops, "
            "bottoms, matching sets and outerwear for every occasion."
        ),
        "products": [

            # ── Dresses ──
            "maxi dress",
            "midi dress",
            "mini dress",
            "evening dress",
            "party dress",
            "casual dress",
            "bodycon dress",
            "sweater dress",

            # ── Tops ──
            "t-shirt",
            "blouse",
            "shirt",
            "tank top",
            "bodysuit",
            "crop top",
            "sweater",
            "hoodie",

            # ── Bottoms ──
            "jeans",
            "pants",
            "leggings",
            "shorts",
            "skirt",

            # ── Matching Sets ──
            "lounge set",
            "two piece set",
            "knit set",
            "active set",

            # ── Outerwear ──
            "jacket",
            "coat",
            "blazer",
            "cardigan",
            "vest",
        ]
    },

    # ==============================================
    # 2. SHOES 👠
    # ==============================================
    "Shoes": {
        "description": (
            "Everyday and statement footwear — from sneakers to heels — "
            "designed for comfort and style."
        ),
        "products": [
            "sneakers",
            "heels",
            "sandals",
            "flats",
            "boots",
            "slippers",
        ]
    },

    # ==============================================
    # 3. JEWELRY 💎
    # ==============================================
    "Jewelry": {
        "description": (
            "Timeless jewelry designed to elevate every outfit. "
            "Elegant, modern and effortless pieces for every occasion."
        ),
        "products": [

            # ── Necklaces ──
            "pendant necklace",
            "chain necklace",
            "choker",
            "layered necklace",

            # ── Earrings ──
            "stud earrings",
            "hoop earrings",
            "drop earrings",
            "ear cuff",

            # ── Bracelets ──
            "bangle",
            "chain bracelet",
            "tennis bracelet",
            "charm bracelet",

            # ── Rings ──
            "stackable rings",
            "statement ring",
            "adjustable ring",

            # ── Watches & Anklets ──
            "watch",
            "anklet",
        ]
    },

    # ==============================================
    # 4. ACCESSORIES 👜
    # ==============================================
    "Accessories": {
        "description": (
            "Everyday carry and finishing touches — bags, sunglasses, hair "
            "accessories, and travel essentials."
        ),
        "products": [

            # ── Bags ──
            "tote bag",
            "crossbody bag",
            "shoulder bag",
            "handbag",
            "backpack",
            "mini bag",
            "evening bag",

            # ── Sunglasses ──
            "sunglasses",

            # ── Hair Accessories ──
            "hair clips",
            "headband",
            "scrunchie",
            "hair claw clip",

            # ── Belts, Wallets ──
            "belt",
            "wallet",
            "card holder",

            # ── Phone Accessories ──
            "phone case",
            "phone strap",
            "magsafe accessory",

            # ── Travel Accessories ──
            "cosmetic bag",
            "jewelry case",
            "travel organizer",
            "passport holder",
            "luggage tag",
        ]
    },

    # ==============================================
    # 5. WELLNESS 🌿
    # ==============================================
    "Wellness": {
        "description": (
            "Everything you need to move, recover and recharge — fitness, "
            "yoga, recovery, massage, posture and hydration essentials."
        ),
        "products": [

            # ── Fitness ──
            "resistance bands",
            "dumbbells",
            "jump rope",
            "pilates ring",
            "pilates ball",

            # ── Yoga ──
            "yoga mat",
            "yoga block",
            "yoga strap",

            # ── Recovery ──
            "foam roller",
            "stretch strap",
            "massage ball",

            # ── Massage ──
            "massage gun",
            "massage roller stick",
            "foot massager",

            # ── Posture ──
            "back support",
            "posture corrector",
            "seat cushion",

            # ── Hydration ──
            "water bottle",
            "tumbler",
            "shaker bottle",
        ]
    }
}

COLLECTION_NAMES = list(COLLECTIONS.keys())

# ── Sub-collection definitions ────────────────────────────────────────────────
#
# Each of the 5 top-level collections has its own set of sub-collections.
# The Scorer's LLM assigns EXACTLY ONE sub-collection per product, alongside
# the top-level collection. The sub-collection name is then added as a
# Shopify tag, so a Shopify "sub-collection" is just an automated collection
# with the rule "Tagged with <sub-collection name>" — no schema change needed
# on the Shopify side.

SUB_COLLECTIONS = {
    "Fashion": [
        "Dresses", "Tops", "Bottoms", "Matching Sets", "Outerwear",
    ],
    "Shoes": [
        "Sneakers", "Heels", "Sandals", "Flats", "Boots", "Slippers",
    ],
    "Jewelry": [
        "Necklaces", "Earrings", "Bracelets", "Rings", "Watches", "Anklets",
    ],
    "Accessories": [
        "Bags", "Sunglasses", "Hair Accessories", "Belts",
        "Wallets & Card Holders", "Phone Accessories", "Travel Accessories",
    ],
    "Wellness": [
        "Fitness", "Yoga", "Recovery", "Massage", "Posture", "Hydration",
    ],
}

# ── Regional trend definitions ────────────────────────────────────────────────
#
# These are NOT product collections — they're an overlay on top of the 5
# collections above. A product can be in "Beauty Tools" AND carry the
# "Trend: Korea" tag at the same time. Each region maps to:
#   - one or more Google Trends `geo` codes (merged together)
#   - one TikTok/Instagram country code (Apify only supports one at a time,
#     so we use the region's primary/largest market as the proxy signal)
#
# To add a region: add an entry to config.json's layer1.region_definitions.
# Nothing else needs to change — the rest of the pipeline (TrendScout,
# SupplierFinder, tagging, reporting) reads this dict.

# Fallback used only if config.json (or its layer1.region_definitions key)
# is missing — keeps this module importable/runnable standalone. The
# "real" definitions now live in config.json so you can add/edit/disable a
# region without touching code. Single primary geo per region by default:
# Google Trends rate-limits hard even for ONE geo (see production logs —
# 429s + backoff on nearly every call), so merging e.g. GB+DE+FR for Europe
# tripled the live load for that one region. Add more codes back (e.g.
# ["GB", "DE", "FR"]) in config.json once the Google Trends cache has
# warmed up and you're comfortable with the added latency on cache-miss days.
_DEFAULT_REGIONS = {
    "US": {
        "label": "United States",
        "geo_codes": ["US"],
        "tiktok_country": "US",
        "tag": "Trend: USA",
    },
    "Europe": {
        "label": "Europe",
        "geo_codes": ["GB"],
        "tiktok_country": "GB",
        "tag": "Trend: Europe",
    },
    "Korea": {
        "label": "South Korea",
        "geo_codes": ["KR"],
        "tiktok_country": "KR",
        "tag": "Trend: Korea",
    },
    "Japan": {
        "label": "Japan",
        "geo_codes": ["JP"],
        "tiktok_country": "JP",
        "tag": "Trend: Japan",
    },
    "LatinAmerica": {
        "label": "Latin America",
        "geo_codes": ["BR"],   # primary market; add "MX" back once cache is warm
        "tiktok_country": "BR",
        "tag": "Trend: Latin America",
    },
    "Africa": {
        "label": "Africa",
        "geo_codes": ["ZA"],   # primary market; add "NG"/"EG" back once cache is warm
        "tiktok_country": "ZA",
        "tag": "Trend: Africa",
    },
}

try:
    _layer1_cfg = get_layer_config("layer1")
except FileNotFoundError:
    _layer1_cfg = {}

# To add/edit/disable a region: edit config.json's layer1.region_definitions
# — nothing else needs to change, the rest of the pipeline (TrendScout,
# SupplierFinder, tagging, reporting) reads this dict.
REGIONS = _layer1_cfg.get("region_definitions") or _DEFAULT_REGIONS

REGION_KEYS = list(REGIONS.keys())


# ── Off-niche safety net ──────────────────────────────────────────────────────
#
# Even with carefully scoped keywords, CJ's search can occasionally surface
# completely off-niche products (e.g. a generic "drawer organizer" keyword
# once matched a 7-drawer ROLLING TOOL CHEST, not a vanity organizer). This
# blocklist is a deterministic, zero-cost safety net that runs BEFORE the
# Scorer's LLM step — it doesn't rely on the LLM correctly judging every
# edge case, it just hard-rejects unmistakably off-niche titles outright.

_OFF_NICHE_BLOCKLIST = [
    # Automotive
    "roof rack", "cross bars", "bicycle chain", "bike chain",
    "car parts", "engine", "brake pad", "spark plug",
    "for ford", "for toyota", "for honda", "for chevrolet",
    "for jeep", "for tesla", "for nissan",

    # Garage / workshop tools
    "tool chest", "tool cabinet", "tool box", "rolling tool",
    "cable management", "garage storage", "wrench set", "socket set",
    "drill bit", "power drill", "circular saw",
]


def _is_off_niche(title: str) -> bool:
    """
    Returns True if a product title unmistakably belongs to a category
    with no connection to any of our 5 real collections (Fashion, Shoes,
    Jewelry, Accessories, Wellness). Deliberately conservative — only
    matches unambiguous automotive/garage/industrial terms, never
    anything that could plausibly be a legitimate item in our niche.
    """
    title_lower = title.lower()
    return any(term in title_lower for term in _OFF_NICHE_BLOCKLIST)


# ── Beauty-consumable safety net ──────────────────────────────────────────────
#
# Aurelle dropped its Beauty Tools collection and no longer carries ANY
# topical/consumable beauty product (skincare, makeup, hair-care, bath
# products) in ANY of its 5 real collections. Previously the ONLY defense
# against these was the Scorer LLM's elimination rule — which is exactly
# why creams/oils were still leaking through: an LLM elimination step is
# probabilistic, not a hard filter. This blocklist mirrors
# _OFF_NICHE_BLOCKLIST / _is_mens_product: a deterministic, zero-cost check
# that runs BEFORE the LLM ever sees the product, so it doesn't depend on
# the LLM correctly applying the rule on every single batch.
#
# A short list of legitimate ACCESSORY products (makeup bags, cosmetic
# cases, jewelry cases) is checked FIRST and short-circuits to "safe",
# since those are real Accessories-collection products, not consumables —
# a naive "cosmetic" or "makeup" substring match would wrongly block them.

_BEAUTY_SAFE_EXCEPTIONS = [
    "makeup bag", "cosmetic bag", "makeup pouch", "cosmetic pouch",
    "makeup organizer", "cosmetic organizer", "makeup case", "cosmetic case",
    "travel makeup bag", "makeup brush bag", "makeup brush holder",
    "jewelry case", "jewelry box", "jewelry organizer",
]

_BEAUTY_CONSUMABLE_TERMS = [
    # Skincare
    "essential oil", "face oil", "body oil", "hair oil", "cuticle oil", "massage oil",
    "serum", "face cream", "body cream", "hand cream", "night cream", "day cream",
    "eye cream", "bb cream", "cc cream", "lotion", "moisturizer", "moisturiser",
    "cleanser", "toner", "exfoliator", "face scrub", "body scrub",
    "sheet mask", "face mask", "clay mask", "peel off mask",
    "sunscreen", "spf",
    # Hair care
    "shampoo", "conditioner", "hair mask", "hairspray", "hair serum",
    # Bath
    "bath bomb", "body wash", "shower gel", "bar soap", "liquid soap",
    # Makeup
    "foundation cream", "concealer", "lipstick", "lip gloss", "lip balm",
    "eyeshadow", "mascara", "blush", "highlighter", "contour palette",
    "makeup remover", "makeup palette", "makeup brush set",
    # Consumables / supplements
    "protein powder", "detox tea", "vitamin gummies", "supplement capsules",
]


def _is_beauty_consumable(title: str, category: str = "") -> bool:
    """
    Returns True if a product is a topical/consumable beauty, hair-care,
    bath, or makeup product — none of which belong in any of our 5
    collections. Checks BOTH title and CJ's own categoryName, since CJ
    sometimes categorizes a product correctly (e.g. "Skin Care") even when
    the title itself doesn't contain an obvious giveaway word.
    """
    text = f"{title} {category}".lower()
    if any(exc in text for exc in _BEAUTY_SAFE_EXCEPTIONS):
        return False
    return any(term in text for term in _BEAUTY_CONSUMABLE_TERMS)


# ── Men's-product safety net ──────────────────────────────────────────────────
#
# Aurelle is women-only across all 5 collections. Previously the ONLY line
# of defense against men's products was the Scorer LLM's elimination rule —
# which means a men's item slipped through any time the LLM missed it in a
# given batch. This blocklist mirrors _OFF_NICHE_BLOCKLIST: a deterministic,
# zero-cost check that runs BEFORE the LLM ever sees the product, so it
# doesn't depend on the LLM correctly applying the rule every single batch.
#
# Deliberately conservative — only matches titles that are UNAMBIGUOUSLY
# men's-marketed. Neutral/unisex items (e.g. plain "watch", "backpack") are
# intentionally left for the LLM's judgment, since those can legitimately be
# sold to women.

import re as _re

# Plain substrings are safe here (won't false-positive inside another word).
_MENS_SUBSTRING_TERMS = [
    "for men", "male grooming", "beard", "mustache", "moustache",
    "grandpa", "husband gift", "boyfriend gift", "groomsman",
    "gentleman", "gentlemen", "dad gift", "father's day gift",
    "unisex men", "men's collection", "boys'", "for boys",
]

# These need WORD-BOUNDARY matching, because naive substring checks false-
# positive inside "wo-MAN'S" / "wo-MENS": "Women's Floral Maxi Dress"
# contains "man's" as a raw substring, so a plain `in` check would wrongly
# flag legitimate women's products. \b keeps "men's"/"mens" from matching
# inside "women's"/"womens". Same reasoning for \bmale\b — "female"
# doesn't have a word boundary before its embedded "male", so this is safe.
_MENS_WORD_BOUNDARY_TERMS = [
    r"\bmen's\b", r"\bmens\b", r"\bman's\b", r"\bmale\b",
]

_MENS_BLOCKLIST_PATTERN = _re.compile(
    r"(" + "|".join(_re.escape(t) for t in _MENS_SUBSTRING_TERMS)
    + r"|" + "|".join(_MENS_WORD_BOUNDARY_TERMS) + r")"
)


def _is_mens_product(title: str, category: str = "") -> bool:
    """
    Returns True if a product unmistakably targets men. Deliberately
    conservative (same philosophy as _is_off_niche) — only matches explicit
    "men's"/"for men" style phrasing, never anything that could plausibly be
    a legitimate unisex or women's item.

    Checks BOTH title and CJ's categoryName — CJ frequently labels a
    product's real category as e.g. "Men's Clothing" or "Men's Shoes" even
    when the title itself is gender-neutral ("Classic Sneakers"), so
    category is often the more reliable signal of the two.

    Uses word-boundary matching for "men's"/"mens"/"man's"/"male"
    specifically, because a plain substring check would false-positive on
    "Women's"/"Womens"/"Female" (which contain those as raw substrings).
    """
    text = f"{title} {category}".lower()
    return bool(_MENS_BLOCKLIST_PATTERN.search(text))


# ── Seen-PIDs cache (cross-run deduplication) ─────────────────────────────────

def _load_seen_pids(path: str) -> set:
    """Load previously discovered product IDs from disk."""
    try:
        with open(path, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_seen_pids(path: str, seen: set) -> None:
    """Persist seen PIDs back to disk, capping at 5000 entries."""
    entries = list(seen)[-5000:]  # keep recent 5k to avoid unbounded growth
    with open(path, "w") as f:
        json.dump(entries, f)


# ── NODE 1: TrendScout ────────────────────────────────────────────────────────

def trend_scout_node(state: GraphState) -> dict:
    """
    Fetch Google Trends + TikTok/Instagram (via Apify) keywords, AND pass
    through the full SEED_KEYWORDS list. Google + social signals are merged
    BEFORE the LLM normalization step below, since TikTok/Instagram hashtags
    need the same "strip brand/marketing fluff -> generic product type"
    treatment as branded Google queries do.

    The seed list stays separate so SupplierFinder can tag products correctly:
      - trend keywords  → source = "trend"   (Google + TikTok + Instagram)
      - seed keywords   → source = "seed"

    NOTE: TikTok/Instagram trends are pulled via layer1.tools.apify_trends,
    which caches results for 7 days (SOCIAL_TRENDS_MAX_AGE_DAYS) so the
    Apify actors only actually run about once a week, regardless of how
    often this pipeline itself runs — protects your Apify credit.
    """
    print("\n[TrendScout] 🔍 Fetching Google Trends + social trends data...")
    errors = list(state.get("errors", []))

    # ── Always include seeds (capped by max_seed_keywords if provided) ─────
    max_seed_kw = state.get("max_seed_keywords")
    seed_keywords = build_random_seed_list(max_seed_kw)

    try:
        raw_trends = get_trending_keywords(
            niches=state["niches"],
            max_keywords=state["max_trends"] * 2,
            timeframe="medium",
            geo="US",
        )
    except Exception as e:
        error_msg = f"TrendScout: Google Trends error: {str(e)}"
        print(f"[TrendScout] ❌ {error_msg}")
        errors.append(error_msg)
        raw_trends = []

    try:
        social_trends = get_social_trending_keywords(
            seed_keywords=seed_keywords,
            max_keywords=state["max_trends"] * 2,
        )
        print(f"[TrendScout] Found {len(social_trends)} TikTok/Instagram trends")
    except Exception as e:
        error_msg = f"TrendScout: Social trends error: {str(e)}"
        print(f"[TrendScout] ❌ {error_msg}")
        errors.append(error_msg)
        social_trends = []

    combined_trends = list(raw_trends) + list(social_trends)

    if not combined_trends:
        errors.append("TrendScout: No Google or social trends found, using seeds only.")
        filtered_keywords, trend_scores = [], []
    else:
        filtered_keywords, trend_scores = _aggregate_trend_categories(
            combined_trends, max_count=state["max_trends"], errors=errors, label="global",
        )

    print(f"[TrendScout] ✅ Trend keywords: {filtered_keywords}")
    print(f"[TrendScout] ✅ Seed keywords: {len(seed_keywords)} always-on keywords included")

    # ── Per-region trends (US / Europe / Korea / Japan / Latin America) ────
    region_trend_keywords = _fetch_region_trends(state, seed_keywords, errors)

    return {
        "trend_keywords":        filtered_keywords,
        "seed_keywords":         seed_keywords,
        "trend_scores":          trend_scores,
        "region_trend_keywords": region_trend_keywords,
        "errors":                errors,
    }


def _aggregate_trend_categories(
    combined_trends: list,
    max_count: int,
    errors: List[str],
    label: str = "global",
) -> tuple:
    """
    Shared LLM aggregation step: takes raw (keyword, score) pairs from Google
    Trends / TikTok / Instagram and collapses them into `max_count` clean,
    generic, on-niche product categories with summed scores.

    Used both for the main global trend feed and for each per-region feed
    (`_fetch_region_trends`), so a Korean hashtag and a US Google Trends
    query get the exact same "strip brand fluff -> generic product type"
    treatment.

    Returns (filtered_keywords: List[str], trend_scores: List[Tuple[str,int]]).
    Returns ([], []) on any failure (LLM error, bad JSON, etc.) — trends are
    a bonus signal, never worth failing the pipeline over.
    """
    if not combined_trends:
        return [], []

    print(f"[TrendScout] [{label}] {len(combined_trends)} combined raw trends. Filtering with LLM...")
    keywords_text = "\n".join(f"- {kw} (score: {score})" for kw, score in combined_trends)

    messages = [
        SystemMessage(content=(
            "You are a lifestyle e-commerce product research expert specialising in "
            "dropshipping for a women's store called Aurelle. Aurelle carries EXACTLY "
            "five collections and nothing else: Fashion (dresses, tops, bottoms, matching "
            "sets, outerwear), Shoes, Jewelry (necklaces, earrings, bracelets, rings, "
            "watches, anklets), Accessories (bags, sunglasses, hair accessories, belts, "
            "wallets, phone accessories, travel organizers), and Wellness (fitness, yoga, "
            "recovery, massage, posture, hydration). Aurelle does NOT sell skincare, "
            "makeup, hair-care/bath products, supplements, furniture, or home decor of any "
            "kind, and does not carry men's-only products.\n\n"
            "You will receive raw trending search queries AND social media hashtags (from Google "
            "Trends, TikTok, and Instagram). Many of these are BRANDED long-tail queries because "
            "that is how real shoppers search, and some are hashtags because that is how social "
            "trends surface. Your job has two steps:\n\n"
            "1. EXTRACT the underlying GENERIC product category from each branded, long-tail, or "
            "hashtag query — but ONLY if that category is one of our 5 collections above. Strip "
            "ALL of the following, not just brand names:\n"
            "   - Brand/retailer names\n"
            "   - Product line names\n"
            "   - Hashtag formatting and generic hype tags ('#', 'fyp', 'viral', 'trending')\n"
            "   - Marketing/descriptive modifiers that don't change the product TYPE\n"
            "   Keep ONLY the core product type + its single defining attribute if one exists.\n"
            "   Example: 'fjallraven kanken style mini backpack purse' -> 'mini backpack'\n"
            "   Example: '#guashaskincare' -> EXCLUDE (skincare tool/consumable — not a collection we carry)\n"
            "   Example: 'la roche-posay toleriane double repair face moisturizer spf 30' -> EXCLUDE "
            "(skincare consumable, not in our niche — do NOT output 'face moisturizer' or any "
            "cream/oil/serum/lotion/shampoo/makeup category, no matter how high its trend score is)\n\n"
            "2. AGGREGATE AGGRESSIVELY — merge any categories that describe the same underlying product "
            "and SUM their scores. Pick ONE canonical name per product type.\n\n"
            "Only keep dropship-friendly physical products within our niche (Fashion, Shoes, Jewelry, "
            "Accessories, Wellness — see definitions above). NEVER output a brand name. NEVER output "
            "automotive parts, garage/workshop tools, or general industrial hardware. NEVER output "
            "skincare, makeup, hair-care, bath products, or supplements — these categories don't exist "
            "in our store no matter how they're trending. NEVER output furniture or home decor. NEVER "
            "output men's-only products. If the input data contains mostly off-niche trends, it is "
            "completely fine to return fewer than the requested number of categories, or an empty "
            "array — do not stretch to fill the count with an off-niche category.\n\n"
            "Return ONLY a JSON array:\n"
            "[{\"category\": \"mini backpack\", \"score\": 2600}, ...]"
        )),
        HumanMessage(content=(
            f"Extract the best {max_count} generic product categories:\n\n"
            f"{keywords_text}\n\n"
            f"Return format: [{{\"category\": \"keyword1\", \"score\": 1234}}, ...]"
        )),
    ]

    # ── Call the LLM with retries for transient rate-limit/quota hiccups ───
    max_attempts = 3
    content = ""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = _llm.invoke(messages)
            content = (response.content or "").strip().replace("```json", "").replace("```", "").strip()
            if content:
                break
            finish_reason = None
            try:
                finish_reason = response.response_metadata.get("finish_reason")
            except Exception:
                pass
            last_error = f"empty response content (finish_reason={finish_reason})"
        except Exception as e:
            last_error = str(e)

        if attempt < max_attempts:
            wait_s = 2 ** attempt  # 2s, 4s
            print(f"[TrendScout] [{label}] ⚠️ LLM attempt {attempt}/{max_attempts} failed "
                  f"({last_error}). Retrying in {wait_s}s...")
            time.sleep(wait_s)

    if not content:
        error_msg = (
            f"TrendScout: LLM normalization error [{label}] after {max_attempts} attempts "
            f"({last_error}). Likely rate limit / quota on the OpenAI key — falling back to "
            f"non-LLM keyword selection."
        )
        print(f"[TrendScout] ❌ {error_msg}")
        errors.append(error_msg)
        return _fallback_aggregate_trends(combined_trends, max_count, label)

    try:
        parsed = json.loads(content)

        category_scores: dict = defaultdict(int)
        for item in parsed:
            category = str(item["category"]).strip().lower()
            score = int(item.get("score", 50))
            category_scores[category] += score

        # ── Deterministic backstop on the LLM's OWN output ──────────────────
        # The prompt above instructs the model to never output beauty
        # consumables / off-niche / mens-only categories, but an instruction
        # is not a guarantee — this is the same "don't trust the LLM alone"
        # philosophy as the blocklists that run against individual products
        # later in the pipeline. Filtering HERE means a banned category can
        # never even become a CJ search keyword in the first place, instead
        # of relying only on catching the resulting products afterward.
        blocked_categories = []
        safe_category_scores = {}
        for cat, score in category_scores.items():
            if _is_beauty_consumable(cat) or _is_off_niche(cat) or _is_mens_product(cat):
                blocked_categories.append(cat)
            else:
                safe_category_scores[cat] = score

        if blocked_categories:
            print(f"[TrendScout] [{label}] 🚫 LLM proposed off-niche/beauty/mens categories "
                  f"(dropped before any CJ search happened): {blocked_categories}")

        sorted_categories = sorted(
            safe_category_scores.items(), key=lambda x: x[1], reverse=True
        )[:max_count]

        filtered_keywords = [cat for cat, _ in sorted_categories]
        trend_scores = sorted_categories

        print(f"[TrendScout] [{label}] ✅ Keywords: {filtered_keywords}")
        return filtered_keywords, trend_scores

    except Exception as e:
        error_msg = f"TrendScout: LLM JSON parse error [{label}]: {str(e)}. Raw content: {content[:200]!r}"
        print(f"[TrendScout] ❌ {error_msg}")
        errors.append(error_msg)
        return _fallback_aggregate_trends(combined_trends, max_count, label)


def _fallback_aggregate_trends(combined_trends: list, max_count: int, label: str) -> tuple:
    """
    Non-LLM fallback used when the aggregation LLM call fails after retries
    (e.g. free-tier rate limit / quota exhausted). Skips the "extract generic
    category from branded query" cleanup, but still gives usable, deduped,
    on-niche-ish keywords so World Trends / TrendScout keeps working instead
    of silently returning zero products.
    """
    import re
    cleaned_scores: dict = defaultdict(int)
    for kw, score in combined_trends:
        # Light cleanup: strip hashtag symbols and generic hype words, dedupe.
        kw_clean = re.sub(r"#", "", kw).strip().lower()
        kw_clean = re.sub(r"\b(fyp|viral|trending|foryou|foryoupage)\b", "", kw_clean).strip()
        kw_clean = re.sub(r"\s+", " ", kw_clean)
        if not kw_clean:
            continue
        cleaned_scores[kw_clean] += int(score)

    # Same deterministic backstop as the LLM path — the fallback skips the
    # "extract generic category" cleanup, so it's even more likely to carry
    # raw beauty/mens/off-niche terms straight through if not filtered here.
    safe_scores = {
        kw: score for kw, score in cleaned_scores.items()
        if not (_is_beauty_consumable(kw) or _is_off_niche(kw) or _is_mens_product(kw))
    }

    sorted_categories = sorted(safe_scores.items(), key=lambda x: x[1], reverse=True)[:max_count]
    filtered_keywords = [cat for cat, _ in sorted_categories]
    print(f"[TrendScout] [{label}] ↩️ Fallback (non-LLM) keywords: {filtered_keywords}")
    return filtered_keywords, sorted_categories


def _fetch_region_trends(
    state: GraphState,
    seed_keywords: List[str],
    errors: List[str],
) -> dict:
    """
    Fetch Google Trends + TikTok/Instagram trends SEPARATELY for each region
    in state["regions"] (default: all of REGIONS), so a "Trends" collection
    can be built per region (e.g. "Korea Trends", "Europe Trends") instead
    of a single flattened global trend list.

    Returns {region_key: [(keyword, score), ...], ...} — already run through
    the same LLM aggregation as the global trend feed, capped at
    state["max_region_trends"] keywords per region.
    """
    regions = state.get("regions") or REGION_KEYS
    max_region_trends = state.get("max_region_trends") or 5

    result: dict = {}
    for region_key in regions:
        cfg = REGIONS.get(region_key)
        if not cfg:
            print(f"[TrendScout] ⚠️ Unknown region '{region_key}' — skipping (check REGIONS in nodes.py)")
            continue

        print(f"\n[TrendScout] 🌍 Fetching trends for region: {cfg['label']} ({region_key})...")

        # Google Trends: merge across all geo codes for this region (e.g.
        # Europe = GB + DE + FR combined), keeping the max score per keyword.
        region_google: dict = {}
        for geo_code in cfg["geo_codes"]:
            try:
                geo_trends = get_trending_keywords(
                    niches=state["niches"],
                    max_keywords=max_region_trends * 2,
                    timeframe="medium",
                    geo=geo_code,
                )
                for kw, score in geo_trends:
                    region_google[kw] = max(region_google.get(kw, 0), score)
            except Exception as e:
                error_msg = f"TrendScout: Google Trends error [{region_key}/{geo_code}]: {str(e)}"
                print(f"[TrendScout] ❌ {error_msg}")
                errors.append(error_msg)

        # TikTok/Instagram: one country proxy per region (Apify actors take
        # a single country per call).
        try:
            region_social = get_social_trending_keywords(
                seed_keywords=seed_keywords,
                max_keywords=max_region_trends * 2,
                country=cfg["tiktok_country"],
            )
        except Exception as e:
            error_msg = f"TrendScout: Social trends error [{region_key}]: {str(e)}"
            print(f"[TrendScout] ❌ {error_msg}")
            errors.append(error_msg)
            region_social = []

        combined = list(region_google.items()) + list(region_social)
        filtered_keywords, trend_scores = _aggregate_trend_categories(
            combined, max_count=max_region_trends, errors=errors, label=cfg["label"],
        )
        result[region_key] = trend_scores  # [(keyword, score), ...]

        # Small pacing gap between regions so 5-6 back-to-back LLM calls
        # don't burst a low-tier rate limit (this is on top of the
        # per-call retry/backoff inside _aggregate_trend_categories).
        time.sleep(1.0)

    return result


# ── NODE 2: SupplierFinder ────────────────────────────────────────────────────

def supplier_finder_node(state: GraphState) -> dict:
    """
    Search CJ Dropshipping using both trend keywords and seed keywords.

    For each keyword we make TWO passes:
      • pass 1  sort=ORDERS       → proven sellers  (source: "trend" or "seed")
      • pass 2  sort=CREATED_TIME → new arrivals    (source: "new_arrival")

    Products are then deduplicated by pid, filtered through the off-niche
    blocklist (_is_off_niche) AND the men's-product blocklist
    (_is_mens_product), and any pid already seen in a previous pipeline run
    (seen_pids.json) is dropped so every daily run surfaces fresh products.
    """
    print("\n[SupplierFinder] 🛒 Searching CJ Dropshipping (dual-sort strategy)...")

    errors        = list(state.get("errors", []))
    trend_kws     = state.get("trend_keywords", [])
    seed_kws      = state.get("seed_keywords", [])
    region_kws    = state.get("region_trend_keywords", {}) or {}
    seen_pids_path = state.get("seen_pids_path", "layer1/output/seen_pids.json")

    if not trend_kws and not seed_kws and not any(region_kws.values()):
        errors.append("SupplierFinder: No keywords to search.")
        return {"raw_products": [], "errors": errors}

    # ── Load cross-run seen PIDs ───────────────────────────────────────────
    seen_pids = _load_seen_pids(seen_pids_path)
    print(f"[SupplierFinder] Loaded {len(seen_pids)} previously seen PIDs (will skip these)")

    score_lookup = {kw.lower(): score for kw, score in state.get("trend_scores", [])}

    # ── Build keyword job list ─────────────────────────────────────────────
    keyword_jobs: List[tuple] = []
    for kw in trend_kws:
        keyword_jobs.append((kw, "trend"))
    for kw in seed_kws:
        if kw not in trend_kws:           # no need to search duplicates
            keyword_jobs.append((kw, "seed"))

    print(f"[SupplierFinder] {len(trend_kws)} trend kw + {len(seed_kws)} seed kw → {len(keyword_jobs)} unique jobs")

    # ── Per-keyword dedup (avoid same pid from two passes of same keyword) ─
    run_seen_pids: set = set(seen_pids)   # grows within this run
    all_raw_products: List[dict] = []
    today_str = date.today().isoformat()
    blocked_count = 0

    for keyword, base_source in keyword_jobs:
        trend_score = score_lookup.get(keyword.lower(), 50)

        blocked_count += _fetch_and_collect(
            keyword=keyword,
            sort_by="orders",
            source=base_source,
            trend_score=trend_score,
            max_results=state["max_products_per_keyword"],
            run_seen_pids=run_seen_pids,
            all_raw_products=all_raw_products,
            today_str=today_str,
            errors=errors,
        )

        blocked_count += _fetch_and_collect(
            keyword=keyword,
            sort_by="date",
            source="new_arrival",
            trend_score=trend_score,
            max_results=state["max_products_per_keyword"],
            run_seen_pids=run_seen_pids,
            all_raw_products=all_raw_products,
            today_str=today_str,
            errors=errors,
        )

    # ── Regional trend keywords (US / Europe / Korea / Japan / LatAm) ──────
    # These get ONE pass (sort=orders, proven sellers) tagged with
    # source="region_trend" + a region tag, e.g. "Trend: Korea". They still
    # count toward the same 5 product collections (Beauty Tools, Jewelry,
    # etc.) — region is an overlay, not a replacement niche.
    region_trend_keywords = state.get("region_trend_keywords", {}) or {}
    region_job_count = sum(len(v) for v in region_trend_keywords.values())
    if region_job_count:
        print(f"[SupplierFinder] 🌍 {region_job_count} region-trend keywords across "
              f"{len(region_trend_keywords)} regions")

    for region_key, kw_scores in region_trend_keywords.items():
        cfg = REGIONS.get(region_key, {"label": region_key, "tag": f"Trend: {region_key}"})
        for keyword, kw_score in kw_scores:
            blocked_count += _fetch_and_collect(
                keyword=keyword,
                sort_by="orders",
                source="region_trend",
                trend_score=kw_score,
                max_results=state["max_products_per_keyword"],
                run_seen_pids=run_seen_pids,
                all_raw_products=all_raw_products,
                today_str=today_str,
                errors=errors,
                region_key=region_key,
                region_label=cfg["label"],
                region_tag=cfg["tag"],
            )

    # ── Persist newly seen PIDs ────────────────────────────────────────────
    _save_seen_pids(seen_pids_path, run_seen_pids)

    print(f"[SupplierFinder] ✅ Total unique products collected: {len(all_raw_products)}")
    if blocked_count:
        print(f"[SupplierFinder] 🚫 Total off-niche/mens products blocked: {blocked_count}")
    return {"raw_products": all_raw_products, "errors": errors}


def _fetch_and_collect(
    keyword: str,
    sort_by: str,
    source: str,
    trend_score: int,
    max_results: int,
    run_seen_pids: set,
    all_raw_products: list,
    today_str: str,
    errors: list,
    region_key: str = "",
    region_label: str = "",
    region_tag: str = "",
) -> int:
    """
    Single CJ search call — appends unique, on-niche, women-appropriate
    products to all_raw_products. Returns the count of products blocked by
    the deterministic safety nets (_is_off_niche / _is_mens_product), for
    run-level logging.

    region_key/region_label/region_tag are only set for source="region_trend"
    jobs, so a product can carry BOTH its normal source tag and a region tag.
    """
    label = f"'{keyword}' [{sort_by}]" + (f" [{region_label}]" if region_label else "")
    print(f"[SupplierFinder]   Searching: {label}...")
    blocked_count = 0
    try:
        products = search_products(
            keyword=keyword,
            max_results=max_results,
            max_price_usd=500.0,
            sort_by=sort_by,
        )

        added = 0
        for raw in products:
            parsed = parse_cj_product(raw, keyword, trend_score)
            if parsed is None:
                continue

            if _is_off_niche(parsed["title"]):
                print(f"[SupplierFinder]     🚫 Blocked off-niche: {parsed['title'][:55]}")
                blocked_count += 1
                continue

            if _is_mens_product(parsed["title"], parsed.get("category", "")):
                print(f"[SupplierFinder]     🚫 Blocked mens product: {parsed['title'][:55]}")
                blocked_count += 1
                continue

            if _is_beauty_consumable(parsed["title"], parsed.get("category", "")):
                print(f"[SupplierFinder]     🚫 Blocked beauty consumable: {parsed['title'][:55]}")
                blocked_count += 1
                continue

            pid = parsed["cj_pid"]
            if pid in run_seen_pids:
                continue                  # already seen this run or a previous run

            run_seen_pids.add(pid)
            parsed["source"] = source
            parsed["discovery_date"] = today_str
            parsed["region"] = region_key
            parsed["region_label"] = region_label
            parsed["shopify_tags"] = _build_shopify_tags(source, keyword, region_tag)
            all_raw_products.append(parsed)
            added += 1

        print(f"[SupplierFinder]   → {added} new products added from {label}")
        time.sleep(0.3)

    except Exception as e:
        error_msg = f"SupplierFinder error for {label}: {str(e)}"
        print(f"[SupplierFinder] ❌ {error_msg}")
        errors.append(error_msg)

    return blocked_count


def _build_shopify_tags(source: str, keyword: str, region_tag: str = "") -> List[str]:
    """
    Generate Shopify tags based on where the product came from.
    These map directly to Shopify automated collections or marketing labels.

    region_tag (e.g. "Trend: Korea") is added independently of `source` —
    it drives the world-trends collections, layered on top of whichever of
    the 5 product collections the Scorer later assigns.
    """
    tags = []
    if source == "trend":
        tags.append("Trending Now")
    if source == "new_arrival":
        tags.append("New In")
    if source == "seed":
        tags.append("Staff Pick")
    if region_tag:
        tags.append(region_tag)
    # Always add the discovery keyword as a tag for Shopify filtering
    tags.append(f"kw:{keyword}")
    return tags


# ── NODE 3: Scorer ────────────────────────────────────────────────────────────

SCORING_WEIGHTS = {
    "trend_score":   0.30,
    "margin_pct":    0.25,
    "orders_count":  0.20,
    "shipping_days": 0.15,
    "rating":        0.10,
}

# Source bonus applied before normalisation — rewards new arrivals slightly
# so they can compete with established products that have high order counts.
SOURCE_BONUS = {
    "trend":        5.0,
    "new_arrival":  8.0,   # new arrivals get the biggest boost (no order history yet)
    "seed":         0.0,
    "region_trend": 5.0,   # same treatment as global "trend" — proven sellers, fresh signal
}

# ── Minimum quality bar for winning products ──────────────────────────────────
#
# Previously, the Scorer LLM step only ELIMINATED products that violated a
# hard rule (mens/off-niche/religious/etc). It never enforced a positive
# quality floor. With batch_size=5, a batch of 5 legitimately on-niche but
# low-scoring products (poor margin, no orders, slow shipping) had NOTHING
# for the LLM to eliminate, so all 5 passed straight through as "winners".
#
# This threshold is enforced in CODE (not left to the LLM) as a final
# filter, so it's applied consistently regardless of batch composition or
# whether a given batch hit the LLM-error fallback path.
#
# Score is 0-100 (see _compute_score). Tune based on your real score
# distribution — print/log scored products from a real run and look at the
# distribution before changing this.
MIN_WINNING_SCORE = 60.0


def scorer_node(state: GraphState) -> dict:
    print("\n[Scorer] 📊 Scoring and ranking products...")
    errors = list(state.get("errors", []))
    raw_products = state.get("raw_products", [])

    if not raw_products:
        errors.append("Scorer: No products to score.")
        return {"scored_products": [], "winning_products": [], "errors": errors}

    # ── Step 1: Compute scores ─────────────────────────────────────────────
    scored = []
    for p_dict in raw_products:
        try:
            product = Product(**p_dict)
            product.score = _compute_score(product)
            scored.append(product)
        except Exception as e:
            errors.append(f"Scorer: Could not parse product: {e}")

    scored.sort(key=lambda p: p.score, reverse=True)

    # ── Ensure diversity: include ALL products from each source ────────────
    by_source: dict = defaultdict(list)
    for p in scored:
        by_source[p.source].append(p)

    top_candidates: List[Product] = []
    for source_group in by_source.values():
        top_candidates.extend(source_group)  # Take ALL products from each source
    top_candidates.sort(key=lambda p: p.score, reverse=True)  # Re-sort by score

    all_candidates = top_candidates

    print(f"[Scorer] Scored {len(scored)} products. Will process {len(all_candidates)} candidates in batches...")
    _print_source_breakdown(all_candidates)

    # ── Step 2: LLM qualitative filter + collection assignment in batches ──
    batch_size = 5
    all_winning_products = []
    total_batches = (len(all_candidates) + batch_size - 1) // batch_size

    print(f"[Scorer] 📦 Processing {len(all_candidates)} candidates in {total_batches} batches of {batch_size}...")

    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, len(all_candidates))
        batch_candidates = all_candidates[start_idx:end_idx]

        print(f"\n[Scorer] 🚀 Processing batch {batch_num + 1}/{total_batches} ({len(batch_candidates)} products)...")

        product_list_text = "\n".join([
            f"{i + 1}. {p.title} | ${p.supplier_price} cost | {p.margin_pct}% margin | "
            f"{p.orders_count} orders | {p.shipping_days}d shipping | "
            f"source: {p.source} | keyword: {p.trend_keyword}"
            for i, p in enumerate(batch_candidates)
        ])

        collections_list = "\n".join(
            f"- {name} → sub-collections: [{', '.join(subs)}]"
            for name, subs in SUB_COLLECTIONS.items()
        )

        messages = [
            SystemMessage(content=(
                "You are a lifestyle e-commerce expert reviewing dropshipping product candidates "
                "for a store called Aurelle, which is exclusively dedicated to women and spans "
                "FIVE collections: Fashion, Shoes, Jewelry, Accessories, and Wellness.\n\n"
                "ELIMINATE only products that clearly have:\n"
                "- Obvious luxury brand counterfeits (Chanel, Dior, Gucci, etc.)\n"
                "- Prescription or medical-grade claims requiring certification\n"
                "- Dangerous ingredients or unverifiable safety claims\n"
                "- Any topical/consumable beauty products (skincare, makeup, oils, creams) — "
                "we no longer carry beauty consumables in any collection\n"
                "- Any religious symbols or iconography, including but not limited to: crosses, "
                "crucifixes, Stars of David, crescent moons, Om symbols, Buddha images, angel motifs, "
                "or any jewelry/items explicitly marketed with religious meaning (e.g., 'cross necklace', "
                "'faith ring', 'prayer bracelet', 'holy medal'). This applies across ALL collections.\n"
                "- Any seating furniture, including all types of chairs (e.g., office chairs, dining "
                "chairs, accent chairs, folding chairs, rocking chairs, bar stools, sofas, couches, "
                "settees, benches, ottomans, or any item primarily designed for sitting or reclining)\n"
                "- Purely FUNCTIONAL/industrial home or garage storage products (storage bins, "
                "shelving units, tool chests, closet organizers, etc.) — these have no home in any "
                "of our 5 collections\n"
                "- Any products targeting men (men's clothing, accessories, grooming, footwear, etc.)\n"
                "- Unisex products that are predominantly marketed toward or stereotypically associated with men\n"
                "- NO connection to any of our 5 collections (Fashion, Shoes, Jewelry, Accessories, "
                "Wellness). This explicitly EXCLUDES: automotive parts/accessories, garage/workshop "
                "tools, general cable-management or industrial storage hardware, kitchen utensils, "
                "pet products, and general consumer electronics unrelated to our niches.\n\n"
                "KEEP products even if they are:\n"
                "- Generic or unbranded — that is the dropshipping model\n"
                "- Low order count — especially if source is 'new_arrival', these are intentionally fresh\n"
                "- Feminine or women-focused designs, colors, and styles\n"
                "- Products that could be used by women even if not explicitly labeled 'for women' "
                "(e.g., neutral jewelry, wellness tools, watches, bags)\n"
                "- Secular symbols or motifs that have no religious connotation (e.g., stars, hearts, "
                "flowers, geometric shapes, animals, initials, zodiac signs)\n"
                "- Travel and vanity organization pieces (cosmetic bags, jewelry cases, travel "
                "organizers) — these are core Accessories products\n\n"
                "Your goal is to KEEP as many ON-NICHE, WOMEN-APPROPRIATE products as possible. "
                "Only eliminate with a clear reason.\n\n"
                "Assign each kept product to EXACTLY ONE collection AND EXACTLY ONE "
                "sub-collection within that collection (use the exact names, no variations). "
                "The sub-collection MUST belong to the collection you assigned — never mix "
                "a sub-collection from one collection with a different collection.\n\n"
                f"{collections_list}\n\n"
                "Return ONLY a JSON array, no explanation, no markdown:\n"
                "[{\"index\": 1, \"collection\": \"Fashion\", \"sub_collection\": \"Dresses\"}, ...]"
            )),
            HumanMessage(content=(
                f"Review these {len(batch_candidates)} candidates (batch {batch_num + 1} of {total_batches}) "
                f"and select the winners:\n\n"
                f"{product_list_text}\n\n"
                f"Return format: [{{\"index\": 1, \"collection\": \"Fashion\", \"sub_collection\": \"Dresses\"}}, ...]"
            )),
        ]

        try:
            response = _llm.invoke(messages)
            raw = response.content.strip().replace("```json", "").replace("```", "").strip()
            llm_results = json.loads(raw)

            batch_winners = []
            for item in llm_results:
                if isinstance(item, dict):
                    idx = int(item.get("index", 0))
                    collection = item.get("collection", "")
                    sub_collection = item.get("sub_collection", "")
                else:
                    idx = int(item)
                    collection = ""
                    sub_collection = ""

                # Guard against the LLM inventing a collection name that
                # isn't one of our real 5 — falls back to leaving it
                # unassigned rather than creating a phantom collection.
                if collection not in COLLECTION_NAMES:
                    if collection:
                        print(f"[Scorer]   ⚠️  LLM returned unknown collection '{collection}' — clearing it")
                    collection = ""
                    sub_collection = ""

                # Guard against a sub-collection that doesn't belong to the
                # assigned collection (LLM mixing categories, typos, etc.)
                valid_subs = SUB_COLLECTIONS.get(collection, [])
                if sub_collection not in valid_subs:
                    if sub_collection:
                        print(f"[Scorer]   ⚠️  LLM returned '{sub_collection}' which isn't a valid "
                              f"sub-collection of '{collection}' — clearing it")
                    sub_collection = ""

                if 1 <= idx <= len(batch_candidates):
                    product = batch_candidates[idx - 1]

                    # ── Post-LLM safety net (mens products) ─────────────────
                    # Deterministic backstop in case the LLM misses a subtle
                    # mens-marketed item despite the elimination rule in the
                    # prompt above. Mirrors the pre-search block in
                    # _fetch_and_collect, but catches anything that slipped
                    # through because the pre-search blocklist is
                    # deliberately conservative.
                    if _is_mens_product(product.title, product.category):
                        print(f"[Scorer]   🚫 Post-LLM block (mens product): {product.title[:55]}")
                        continue

                    # ── Post-LLM safety net (beauty consumables) ────────────
                    # Same reasoning as the mens check above: the elimination
                    # rule in the prompt is a strong instruction, not a
                    # guarantee, so this deterministic backstop catches
                    # anything (creams, oils, serums, etc.) that slips
                    # through an LLM batch despite the instruction.
                    if _is_beauty_consumable(product.title, product.category):
                        print(f"[Scorer]   🚫 Post-LLM block (beauty consumable): {product.title[:55]}")
                        continue

                    product.collection = collection
                    product.sub_collection = sub_collection
                    if sub_collection:
                        product.shopify_tags.append(f"Sub: {sub_collection}")
                    batch_winners.append(product)

            print(f"[Scorer] ✅ Batch {batch_num + 1} selected {len(batch_winners)} winning products")
            all_winning_products.extend(batch_winners)

        except Exception as e:
            error_msg = f"Scorer LLM filter error in batch {batch_num + 1}: {e}. Using top scorers from this batch (above quality bar)."
            print(f"[Scorer] ⚠️  {error_msg}")
            errors.append(error_msg)
            # ── Fallback also respects the minimum score floor ─────────────
            # Previously this blindly kept batch_candidates[:3] regardless of
            # score, which could reintroduce low-quality winners any time the
            # LLM call failed/hiccuped — exactly the bug we're fixing.
            fallback = [
                p for p in batch_candidates[:3]
                if p.score >= MIN_WINNING_SCORE
                and not _is_mens_product(p.title, p.category)
                and not _is_beauty_consumable(p.title, p.category)
            ]
            all_winning_products.extend(fallback)

    # ── Final processing ──────────────────────────────────────────────────
    print(f"\n[Scorer] ✅ LLM selected {len(all_winning_products)} winning products total from {total_batches} batches")
    _print_source_breakdown(all_winning_products)

    # ── Enforce minimum quality bar (code-level, not LLM-dependent) ────────
    # Applied at the end so the LLM still sees full batches for niche/
    # collection judgment, but no product below the score floor can end up
    # in the final winning set — regardless of which batch it was in or
    # whether the LLM path or the error fallback path produced it.
    before_count = len(all_winning_products)
    all_winning_products = [p for p in all_winning_products if p.score >= MIN_WINNING_SCORE]
    dropped = before_count - len(all_winning_products)
    if dropped:
        print(f"[Scorer] 🚫 Dropped {dropped} winning product(s) below "
              f"MIN_WINNING_SCORE={MIN_WINNING_SCORE}")

    all_winning_products.sort(key=lambda p: p.score, reverse=True)

    print(f"\n[Scorer] 📚 Final collection breakdown:")
    _print_collection_breakdown(all_winning_products)

    _print_subcollection_breakdown(all_winning_products)

    return {
        "scored_products": scored,
        "winning_products": all_winning_products,
        "errors": errors,
    }

def _print_source_breakdown(products: List) -> None:
    counts: dict = defaultdict(int)
    for p in products:
        counts[p.source] += 1
    breakdown = " | ".join(f"{src}: {n}" for src, n in sorted(counts.items()))
    print(f"[Scorer]   Source breakdown — {breakdown}")


def _print_collection_breakdown(products: List) -> None:
    """
    Per-collection counts of the FINAL winning set. Also flags any of the
    5 defined collections that came back with zero winners — this is the
    signal to check for either (a) thin/absent raw CJ supply for that
    collection's keywords, or (b) the Scorer LLM elimination rules being
    too aggressive for that category (see the Home Essentials / storage
    ban conflict this was fixed for).
    """
    counts: dict = defaultdict(int)
    for p in products:
        counts[p.collection or "Unassigned"] += 1
    for name in COLLECTION_NAMES:
        marker = "⚠️  ZERO WINNERS" if counts.get(name, 0) == 0 else ""
        print(f"[Scorer]     {name}: {counts.get(name, 0)} {marker}")
    if counts.get("Unassigned"):
        print(f"[Scorer]     Unassigned: {counts['Unassigned']}")


def _print_subcollection_breakdown(products: List) -> None:
    """Per-collection, per-sub-collection counts of the FINAL winning set."""
    from collections import defaultdict
    counts: dict = defaultdict(lambda: defaultdict(int))
    for p in products:
        counts[p.collection or "Unassigned"][p.sub_collection or "Unassigned"] += 1

    for collection_name in COLLECTION_NAMES:
        subs = SUB_COLLECTIONS.get(collection_name, [])
        print(f"[Scorer]     {collection_name}:")
        for sub in subs:
            n = counts.get(collection_name, {}).get(sub, 0)
            marker = "⚠️  ZERO" if n == 0 else ""
            print(f"[Scorer]       - {sub}: {n} {marker}")
        unassigned = counts.get(collection_name, {}).get("Unassigned", 0)
        if unassigned:
            print(f"[Scorer]       - Unassigned sub-collection: {unassigned}")

# ── Scoring formula ───────────────────────────────────────────────────────────

def _compute_score(p: Product) -> float:
    trend_norm    = min(p.trend_score / 100.0, 1.0)
    margin_norm   = min(p.margin_pct / 70.0, 1.0)
    orders_norm   = min(p.orders_count / 5000.0, 1.0)
    shipping_norm = max(0.0, (30 - p.shipping_days) / 30.0)
    rating_norm   = min(max((p.rating - 1.0) / 4.0, 0.0), 1.0)

    base_score = (
        SCORING_WEIGHTS["trend_score"]   * trend_norm    +
        SCORING_WEIGHTS["margin_pct"]    * margin_norm   +
        SCORING_WEIGHTS["orders_count"]  * orders_norm   +
        SCORING_WEIGHTS["shipping_days"] * shipping_norm +
        SCORING_WEIGHTS["rating"]        * rating_norm
    )

    # Apply source bonus (new arrivals penalised less for low order count)
    bonus = SOURCE_BONUS.get(p.source, 0.0)
    raw = base_score * 100 + bonus
    return round(min(max(raw, 0.0), 100.0), 2)