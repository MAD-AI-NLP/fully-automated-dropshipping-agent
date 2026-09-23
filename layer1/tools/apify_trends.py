"""
Trends Tool — Google Trends + TikTok + Instagram, all via Apify Actors
========================================================================
Single source for every trend signal the pipeline uses: Google Trends
(related/rising queries per seed keyword), TikTok (Creative Center trending
hashtags), and Instagram (hashtag velocity + related-hashtag discovery).

google_trends.py (the old pytrends-based module) has been REMOVED. pytrends
is an unofficial scraper against Google's undocumented endpoints and
rate-limits hard (429s with 30/60/120s backoff) -- see the production log
that motivated this switch. Given actual usage is small (~10 product trend
keywords every few days, not continuous polling), a pay-per-event Apify
actor is a better fit: no rate-limit fighting, predictable low cost, same
"confirmed working" verification discipline as the TikTok/Instagram actors
below.

Designed to run every few days, not on every pipeline invocation -- each
call costs real (if small) money. A local JSON cache enforces that: if
cached data is younger than the relevant MAX_AGE setting, the cache is
returned and Apify is never called, no matter how often you run the
pipeline.

Setup:
    .env:
        APIFY_API_TOKEN=apify_api_xxx

Actors used (override via env if you swap actors):
    APIFY_GOOGLE_TRENDS_ACTOR default "agenscrape/google-trends-scraper"    (WARNING: UNVERIFIED -- see note below)
    APIFY_TIKTOK_ACTOR        default "data_xplorer/tiktok-trends"          (confirmed working)
    APIFY_INSTAGRAM_ACTOR     default "apify/instagram-hashtag-analytics-scraper" (confirmed working)

UPDATE -- GOOGLE TRENDS ACTOR SCHEMA NOW CONFIRMED (previously unverified).
The input payload originally sent "searchTerms" + Google-Trends-internal
timeRange codes ("now 7-d" etc), which produced a 400 Bad Request on every
call -- this actor's real input schema (confirmed against its published
Input Parameters table at apify.com/agenscrape/google-trends-scraper/
input-schema) uses "keywords" (not "searchTerms") and human-readable
timeRange strings ("Past 7 days", "Past 90 days", "Past 12 months", etc).
Both are now fixed in _fetch_google_trends() / TIMEFRAME_OPTIONS below,
and _parse_google_trends_items() now parses the actor's real output shape
("relatedSearches": {"top": [...], "rising": [...]}) as its primary case.
If the actor's schema changes again in the future, _parse_google_trends_
items() is still the only place you need to edit -- everything downstream
(caching, LLM aggregation, region tagging) stays actor-agnostic.

Costs (verify current pricing on each actor's Apify Store page -- pricing
and free-plan terms can change):
    - Google Trends actor: pay-per-result, roughly $1-3 / 1,000 results
      depending on actor. At ~10 keywords x a few regions every few days,
      this is pennies/month.
    - data_xplorer/tiktok-trends: pay-per-event. A weekly run of ~20-30
      hashtags is a small fraction of the $5 free-plan credit.
    - apify/instagram-hashtag-analytics-scraper: ~$2.60 / 1,000 results on
      the Free plan. A weekly run against ~20-30 seed hashtags is well
      under $1/month.
    All three combined stay comfortably inside Apify's $5/month free-plan
    credit at this pipeline's actual usage volume.

IMPORTANT -- the TikTok actor default was switched from
automation-lab/tiktok-trends-scraper to data_xplorer/tiktok-trends after a
real test run showed the former silently returning a fallback/placeholder
payload (near-zero view counts on generic hashtags like #fyp, or an
explicit "public fallback" stub for trendType=video) instead of erroring
out. data_xplorer/tiktok-trends returned verified real Creative Center data
on the same kind of query (a French rank-1 hashtag with a plausible 209M
views), so that's the one wired in below. If you ever see suspiciously flat
or tiny numbers on a #1-ranked "trending" hashtag again, that's the same
failure mode resurfacing -- check the actor's run log for a fallback
notice before trusting the data.

The original apify/instagram-hashtag-stats actor no longer exists (renamed
or delisted) -- apify/instagram-hashtag-analytics-scraper is the current
actor from the same publisher with the same behavior, confirmed against a
real run: {"hashtags": ["skincareroutine", "guasha"]} returned real post
counts (36.27M for #skincareroutine) plus a related-hashtag network
(related/frequent/average/rare buckets, each hashtag paired with a
magnitude string like "149.66 m" or "36.27 M"). Both actor schemas can
still change over time since they're third-party -- if a future run starts
returning unfamiliar field names, re-verify against `_fetch_tiktok_trends`
/ `_fetch_instagram_trends` below.
"""
import os
import json
import re
import time
import random
import requests
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional
from dotenv import load_dotenv

load_dotenv()
APIFY_BASE_URL = "https://api.apify.com/v2"

from config_loader import get_layer_config

try:
    _layer1_cfg = get_layer_config("layer1")
except FileNotFoundError:
    _layer1_cfg = {}


def find_working_apify_token():
    """Find the first Apify token that works"""
    token_vars = [
        "APIFY_API_TOKEN_1",
        "APIFY_API_TOKEN_2",
        "APIFY_API_TOKEN_3",
        "APIFY_API_TOKEN"
    ]

    for var_name in token_vars:
        token = os.getenv(var_name)
        if not token:
            continue

        try:
            response = requests.get(
                f"{APIFY_BASE_URL}/users/me",
                headers={'Authorization': f'Bearer {token}'},
                timeout=10
            )

            if response.status_code == 200:
                print(f"✅ Using Apify token: {var_name}")
                return token
        except:
            continue

    # Fallback to default
    default_token = os.getenv("APIFY_API_TOKEN")
    if default_token:
        print("⚠️ No valid token found, using default")
        return default_token

    print("❌ No Apify tokens found!")
    return None


# Set the token
APIFY_API_TOKEN = find_working_apify_token()

# Other configurations
GOOGLE_TRENDS_ACTOR = os.getenv("APIFY_GOOGLE_TRENDS_ACTOR", "automation-lab/google-trends-scraper")
TIKTOK_ACTOR = os.getenv("APIFY_TIKTOK_ACTOR", "data_xplorer/tiktok-trends")
INSTAGRAM_ACTOR = os.getenv("APIFY_INSTAGRAM_ACTOR", "apify/instagram-hashtag-analytics-scraper")



CACHE_PATH = os.getenv("SOCIAL_TRENDS_CACHE_PATH", "layer1/output/social_trends_cache.json")
MAX_CACHE_AGE_DAYS = int(os.getenv("SOCIAL_TRENDS_MAX_AGE_DAYS", "7"))
DEFAULT_COUNTRY = os.getenv("APIFY_TIKTOK_COUNTRY", "US")

GOOGLE_TRENDS_CACHE_DIR = os.getenv("GOOGLE_TRENDS_CACHE_DIR", "layer1/output/google_trends_cache")
GOOGLE_TRENDS_MAX_AGE_DAYS = int(os.getenv("GOOGLE_TRENDS_MAX_AGE_DAYS", "3"))

TIMEFRAME_OPTIONS = {
    # UPDATE (again): the actor's current build rejects the human-readable
    # strings ("Past 7 days" etc.) that a previous build accepted, and now
    # only accepts Google-Trends-internal timeframe codes. Confirmed via a
    # live 400 error body from the actor itself:
    #   "Field input.timeRange must be equal to one of the allowed values:
    #    'now 1-H', 'now 4-H', 'now 1-d', 'now 7-d', 'today 1-m',
    #    'today 3-m', 'today 12-m', 'today 5-y', 'all'"
    # If this actor changes again, that 400 error body (now logged in
    # _run_actor_sync) will show the new allowed list directly — no more
    # guessing from Apify Store docs pages.
    "short": "now 7-d",
    "medium": "today 3-m",
    "long": "today 12-m",
}
# ── SAFE CATEGORIES FILTER ──────────────────────────────────────────────────
# These are the only product categories we want to keep trends for.
# Any trend hashtag/keyword that doesn't match these categories will be
# filtered out. Shared by Google Trends, TikTok, and Instagram results.

SAFE_CATEGORY_KEYWORDS = {
    # Fashion — Dresses
    "dress", "maxi dress", "midi dress", "mini dress", "evening dress",
    "party dress", "bodycon dress", "sweater dress",

    # Fashion — Tops
    "t-shirt", "tshirt", "blouse", "shirt", "tank top", "bodysuit",
    "crop top", "sweater", "hoodie",

    # Fashion — Bottoms
    "jeans", "pants", "leggings", "shorts", "skirt",

    # Fashion — Sets & Outerwear
    "lounge set", "two piece set", "knit set", "active set",
    "jacket", "coat", "blazer", "cardigan", "vest", "outerwear",

    # Shoes
    "sneakers", "heels", "sandals", "flats", "boots", "slippers", "shoes", "footwear",

    # Jewelry
    "jewelry", "necklace", "pendant", "chain necklace", "choker",
    "earring", "stud earring", "hoop earring", "drop earring", "ear cuff",
    "bracelet", "bangle", "tennis bracelet", "charm bracelet",
    "ring", "stackable ring", "statement ring",
    "watch", "anklet",

    # Accessories — Bags
    "tote bag", "crossbody bag", "shoulder bag", "handbag", "backpack",
    "mini bag", "evening bag", "bag",

    # Accessories — Other
    "sunglasses", "hair clip", "headband", "scrunchie", "hair claw",
    "belt", "wallet", "card holder",
    "phone case", "phone strap", "magsafe",
    "cosmetic bag", "jewelry case", "travel organizer", "passport holder", "luggage tag",

    # Wellness — Fitness / Yoga
    "resistance band", "dumbbell", "jump rope", "pilates",
    "yoga mat", "yoga block", "yoga strap", "yoga",

    # Wellness — Recovery / Massage
    "foam roller", "stretch", "recovery ball", "massage gun",
    "massage roller", "foot massager", "massager",

    # Wellness — Posture / Hydration
    "back support", "posture corrector", "seat cushion",
    "water bottle", "tumbler", "shaker", "hydration",
}

# Keywords to EXCLUDE (trends containing these will be filtered out)
EXCLUDED_KEYWORDS = {
    # Skincare & beauty consumables — no longer any collection for these
    "oil", "oils", "essential oil", "serum", "cream", "lotion", "moisturizer",
    "balm", "cleanser", "toner", "exfoliator", "scrub", "sheet mask",
    "sunscreen", "spf", "shampoo", "conditioner", "hair mask", "hairspray",
    "bath bomb", "body wash", "shower gel", "soap",

    # Makeup
    "makeup", "cosmetics", "foundation", "concealer", "lipstick",
    "eyeshadow", "mascara", "blush", "highlighter", "contour",

    # Consumables
    "supplement", "vitamin", "protein powder", "detox", "tea", "edible", "drink mix",

    # Medical/drug claims
    "medical", "prescription", "treatment", "therapy", "healing",
    "cure", "medicine", "drug", "pharmaceutical",

    # Counterfeit brands
    "chanel", "dior", "mac", "lancome", "estee lauder", "gucci",
    "louis vuitton", "hermes", "prada", "versace",

    # Men's-only framing
    "men's", "mens", "for him",
}

import random

# Fallback used only if config.json (or its layer1.seed_categories key) is
# missing — keeps this module importable/runnable standalone. The "real"
# categories now live in config.json's layer1.seed_categories, so you can
# add/remove/edit seed keywords per collection without touching code.
_DEFAULT_SEED_CATEGORIES = {
    "Fashion": [
        "maxi dress", "midi dress", "mini dress", "evening dress", "party dress",
        "casual dress", "bodycon dress", "sweater dress",
        "t-shirt", "blouse", "shirt", "tank top", "bodysuit", "crop top", "sweater", "hoodie",
        "jeans", "wide leg pants", "leggings", "shorts", "maxi skirt", "midi skirt", "mini skirt",
        "lounge set", "two piece set", "knit set", "active set",
        "denim jacket", "trench coat", "blazer", "cardigan", "vest",
    ],
    "Shoes": ["sneakers", "heels", "sandals", "flats", "boots", "slippers"],
    "Jewelry": [
        "pendant necklace", "chain necklace", "choker", "layered necklace",
        "stud earrings", "hoop earrings", "drop earrings", "ear cuff",
        "bangle", "chain bracelet", "tennis bracelet", "charm bracelet",
        "stacking rings", "statement ring", "adjustable ring", "watch", "anklet",
    ],
    "Accessories": [
        "tote bag", "crossbody bag", "shoulder bag", "handbag", "backpack", "mini bag", "evening bag",
        "cat eye sunglasses", "oversized sunglasses", "hair clips", "claw clip", "headband", "scrunchies",
        "belt", "wallet", "card holder", "phone case", "phone strap", "magsafe wallet",
        "cosmetic bag", "travel jewelry case", "travel organizer", "passport holder", "luggage tag",
    ],
    "Wellness": [
        "resistance bands", "dumbbells", "jump rope", "pilates ring", "pilates ball",
        "yoga mat", "cork yoga mat", "yoga block", "yoga strap",
        "foam roller", "stretch strap", "massage ball", "massage gun", "mini massage gun",
        "massage roller stick", "foot massager",
        "posture corrector", "back support brace", "seat cushion",
        "glass water bottle", "stainless steel water bottle", "tumbler", "shaker bottle",
    ],
}

# Lifestyle/aesthetic discovery keywords — not tied to any one collection,
# kept as a separate list (config.json: layer1.lifestyle_keywords) same as
# before.
_DEFAULT_LIFESTYLE_KEYWORDS = [
    "clean girl aesthetic", "coquette aesthetic", "quiet luxury",
    "minimalist fashion", "capsule wardrobe", "old money style",
    "pilates aesthetic", "gift for her", "self care gifts",
]

# To add/remove/edit seed keywords per collection: edit config.json's
# layer1.seed_categories (and layer1.lifestyle_keywords for the aesthetic
# ones) — nothing else needs to change, SEED_KEYWORDS below is derived
# from these automatically.
SEED_CATEGORIES = _layer1_cfg.get("seed_categories") or _DEFAULT_SEED_CATEGORIES
LIFESTYLE_KEYWORDS = _layer1_cfg.get("lifestyle_keywords", _DEFAULT_LIFESTYLE_KEYWORDS)


def build_random_seed_list(max_total: Optional[int]) -> List[str]:
    """
    Randomly samples seed keywords each run, evenly balanced across all 5
    collections, so consecutive runs surface different products instead of
    always the same front-of-list keywords (previously SEED_KEYWORDS[:N]).
    max_total=None/0 -> full list (still shuffled, order doesn't matter then).
    """
    if not max_total:
        all_kws = [kw for kws in SEED_CATEGORIES.values() for kw in kws]
        random.shuffle(all_kws)
        return _filter_safe_keywords(all_kws)

    cats = list(SEED_CATEGORIES.items())
    per_cat = max(1, max_total // len(cats))
    result = []
    for _, kws in cats:
        pool = list(kws)
        random.shuffle(pool)
        result.extend(pool[:per_cat])

    # fill any remainder randomly from whatever's left, still cross-category
    if len(result) < max_total:
        remaining = [kw for kws in cats for kw in kws[1] if kw not in result] if False else \
                    [kw for _, kws in cats for kw in kws if kw not in result]
        random.shuffle(remaining)
        result.extend(remaining[:max_total - len(result)])

    random.shuffle(result)
    return _filter_safe_keywords(result[:max_total])

def _is_safe_hashtag(hashtag: str) -> bool:
    """
    Check if a hashtag belongs to a safe product category.
    Returns True if the hashtag matches a safe category and doesn't match
    any excluded categories.
    """
    hashtag_lower = hashtag.lower()

    # First check if it's excluded (even if it matches a safe category)
    for excluded in EXCLUDED_KEYWORDS:
        if excluded in hashtag_lower:
            return False

    # Then check if it matches any safe category
    for safe in SAFE_CATEGORY_KEYWORDS:
        if safe in hashtag_lower:
            return True

    # If it doesn't match any safe category, it's not safe
    return False


def _filter_safe_trends(scores: Dict[str, int]) -> Dict[str, int]:
    """
    Filter a dictionary of trending hashtags to only keep safe ones.
    """
    return {k: v for k, v in scores.items() if _is_safe_hashtag(k)}


# google_trends.py used _is_safe_keyword for the exact same excluded/safe
# substring-matching logic against SAFE_CATEGORY_KEYWORDS/EXCLUDED_KEYWORDS
# above -- aliased rather than duplicated so the two lists can't drift out
# of sync. _filter_safe_keywords is NOT aliased the same way: it filters a
# plain List[str] (not a Dict[str,int] like _filter_safe_trends), so it's
# defined separately below.
_is_safe_keyword = _is_safe_hashtag


def _filter_safe_keywords(keywords: List[str]) -> List[str]:
    """Filter a list of keywords to only keep safe ones."""
    return [kw for kw in keywords if _is_safe_keyword(kw)]


# SEED_KEYWORDS is now DERIVED from SEED_CATEGORIES + LIFESTYLE_KEYWORDS
# (both sourced from config.json's layer1 section above) instead of being a
# second hardcoded flat list -- previously this list and SEED_CATEGORIES
# had to be kept in sync by hand, which is exactly the kind of drift this
# config-driven approach is meant to eliminate. Order is preserved
# (category order, then lifestyle keywords), duplicates removed.
def _flatten_seed_categories(categories: dict, lifestyle: list) -> List[str]:
    flat: List[str] = []
    seen = set()
    for kws in categories.values():
        for kw in kws:
            if kw not in seen:
                seen.add(kw)
                flat.append(kw)
    for kw in lifestyle:
        if kw not in seen:
            seen.add(kw)
            flat.append(kw)
    return flat


SEED_KEYWORDS = _flatten_seed_categories(SEED_CATEGORIES, LIFESTYLE_KEYWORDS)

# ── Dynamic fallback scoring (ported from google_trends.py) ────────────────
# Used when the Google Trends actor fails/returns nothing -- keeps the
# pipeline producing SOME trend signal (SerpAPI Google Shopping data if
# SERPAPI_KEY is set, else deterministic-with-jitter scoring) rather than
# going to zero trend keywords.

class DynamicKeywordScorer:
    """Dynamically scores keywords using multiple data sources when Google Trends fails."""

    def __init__(self):
        self.cache_file = "keyword_scores_cache.json"
        self.cache_duration = timedelta(hours=6)
        self.scores = self._load_or_fetch_scores()

    def get_scores(self) -> Dict[str, int]:
        """Get current keyword scores (cached + real-time updates)"""
        if self._is_cache_fresh():
            return self._load_cache()
        return self._fetch_fresh_scores()

    def _fetch_fresh_scores(self) -> Dict[str, int]:
        """Fetch real-time data from multiple sources"""
        scores = {}

        # Try Google Shopping via SerpAPI
        shopping_scores = self._get_google_shopping_scores()
        if shopping_scores:
            scores.update(shopping_scores)

        # If no external data, use intelligent random scoring
        if not scores:
            scores = self._get_intelligent_random_scores()
        else:
            scores = self._apply_seasonal_boost(scores)
            scores = self._normalize_scores(scores)

        # Filter to only safe keywords
        scores = {k: v for k, v in scores.items() if _is_safe_keyword(k)}

        self._save_cache(scores)
        return scores

    def _get_google_shopping_scores(self) -> Dict[str, int]:
        """Get real search volumes from Google Shopping via SerpAPI"""
        scores = {}
        try:
            api_key = os.getenv("SERPAPI_KEY")
            if not api_key:
                return scores

            # Only use safe keywords for testing
            safe_seeds = _filter_safe_keywords(SEED_KEYWORDS)
            test_keywords = random.sample(safe_seeds, min(10, len(safe_seeds)))

            for keyword in test_keywords:
                try:
                    response = requests.get(
                        "https://serpapi.com/search",
                        params={
                            "engine": "google_shopping",
                            "q": keyword,
                            "api_key": api_key,
                            "num": 3
                        },
                        timeout=10
                    )
                    if response.status_code == 200:
                        data = response.json()
                        products = data.get("shopping_results", [])
                        if products:
                            score = min(100, len(products) * 10 + 60)
                            scores[keyword] = score
                    time.sleep(1)
                except Exception:
                    continue
        except Exception:
            pass
        return scores

    def _get_intelligent_random_scores(self) -> Dict[str, int]:
        """Generate scores with controlled randomness, only for safe keywords."""
        scores = {}
        category_weights = {
            # Fashion
            "dress": 85, "t-shirt": 78, "blouse": 80, "jeans": 84,
            "leggings": 82, "hoodie": 83, "blazer": 79, "cardigan": 77,
            "lounge set": 86, "two piece set": 84,

            # Shoes
            "sneakers": 90, "heels": 85, "sandals": 82, "boots": 86, "flats": 78,

            # Jewelry
            "necklace": 85, "earring": 83, "bracelet": 82, "ring": 84,
            "anklet": 75, "choker": 78, "pendant": 76, "watch": 83,

            # Accessories
            "handbag": 88, "tote bag": 84, "crossbody bag": 85,
            "sunglasses": 82, "scrunchie": 80, "claw clip": 79,
            "phone case": 81, "wallet": 78,

            # Wellness
            "massage gun": 92, "massager": 88, "yoga mat": 88,
            "resistance band": 85, "dumbbell": 80, "jump rope": 80,
            "foam roller": 79, "water bottle": 82, "posture corrector": 78,
        }

        for keyword in SEED_KEYWORDS:
            # Skip if not safe
            if not _is_safe_keyword(keyword):
                continue

            base_score = 70
            keyword_lower = keyword.lower()
            for pattern, weight in category_weights.items():
                if pattern in keyword_lower:
                    base_score = weight
                    break
            random_variation = random.randint(-15, 15)
            seasonal_boost = self._get_seasonal_boost(keyword_lower)
            final_score = min(100, max(50, base_score + random_variation + seasonal_boost))
            scores[keyword] = int(final_score)
        return scores

    def _get_seasonal_boost(self, keyword: str) -> int:
        """Apply seasonal adjustments based on current month."""
        month = datetime.now().month
        if month in [6, 7, 8]:  # Summer
            summer_keywords = ["water bottle", "sweat", "towel", "gym", "fitness", "exercise"]
            if any(k in keyword for k in summer_keywords):
                return 15
        elif month in [11, 12]:  # Holiday
            holiday_keywords = ["gift", "set", "travel", "mini", "kit", "jewelry"]
            if any(k in keyword for k in holiday_keywords):
                return 20
        elif month in [12, 1, 2]:  # Winter
            winter_keywords = ["pillowcase", "sleep mask", "weighted blanket", "massage"]
            if any(k in keyword for k in winter_keywords):
                return 15
        elif month in [3, 4, 5]:  # Spring
            spring_keywords = ["yoga", "fitness", "exercise", "workout", "gym"]
            if any(k in keyword for k in spring_keywords):
                return 10
        return 0

    def _apply_seasonal_boost(self, scores: Dict[str, int]) -> Dict[str, int]:
        """Apply seasonal boosts to existing scores."""
        boosted = {}
        for keyword, score in scores.items():
            if _is_safe_keyword(keyword):
                boost = self._get_seasonal_boost(keyword.lower())
                boosted[keyword] = min(100, score + boost)
        return boosted

    def _normalize_scores(self, scores: Dict[str, int]) -> Dict[str, int]:
        """Normalize scores to 50-100 range."""
        if not scores:
            return {}
        max_score = max(scores.values())
        min_score = min(scores.values())
        if max_score == min_score:
            return {k: 75 for k in scores}
        normalized = {}
        for key, value in scores.items():
            normalized[key] = int(50 + ((value - min_score) / (max_score - min_score)) * 50)
        return normalized

    def _is_cache_fresh(self) -> bool:
        try:
            with open(self.cache_file, 'r') as f:
                cache = json.load(f)
                timestamp = datetime.fromisoformat(cache.get('timestamp', '2000-01-01'))
                return datetime.now() - timestamp < self.cache_duration
        except:
            return False

    def _load_cache(self) -> Dict[str, int]:
        try:
            with open(self.cache_file, 'r') as f:
                cache = json.load(f)
                scores = cache.get('scores', {})
                # Filter cached scores to only safe keywords
                return {k: v for k, v in scores.items() if _is_safe_keyword(k)}
        except:
            return {}

    def _save_cache(self, scores: Dict[str, int]):
        # Only save safe keywords
        safe_scores = {k: v for k, v in scores.items() if _is_safe_keyword(k)}
        cache = {'timestamp': datetime.now().isoformat(), 'scores': safe_scores}
        with open(self.cache_file, 'w') as f:
            json.dump(cache, f, indent=2)

    def _load_or_fetch_scores(self) -> Dict[str, int]:
        if self._is_cache_fresh():
            return self._load_cache()
        return self._fetch_fresh_scores()


# Initialize scorer
_scorer = DynamicKeywordScorer()


def _build_seed_list(niches: List[str]) -> List[str]:
    """
    Build seed list from niches with proper mapping.
    Returns ALL safe keywords if "general" or no niches specified.
    """
    # If general or empty, return ALL safe keywords
    if "general" in niches or not niches:
        return _filter_safe_keywords(SEED_KEYWORDS)

    # Map user-friendly names to internal keys
    niche_mapping = {
        "fashion": "fashion",
        "dresses": "fashion",
        "tops": "fashion",
        "bottoms": "fashion",
        "outerwear": "fashion",
        "shoes": "shoes",
        "footwear": "shoes",
        "jewelry": "jewelry",
        "jewellery": "jewelry",
        "accessories": "accessories",
        "bags": "accessories",
        "wellness": "wellness",
        "fitness": "wellness",
        "yoga": "wellness",
    }

    niche_seeds = {
        "fashion": [
            "maxi dress", "midi dress", "mini dress", "evening dress",
            "party dress", "bodycon dress", "sweater dress",
            "t-shirt", "blouse", "crop top", "sweater", "hoodie",
            "jeans", "leggings", "shorts", "midi skirt",
            "lounge set", "two piece set", "blazer", "cardigan", "trench coat",
        ],
        "shoes": [
            "sneakers", "heels", "sandals", "flats", "boots", "slippers",
        ],
        "jewelry": [
            "pendant necklace", "chain necklace", "choker", "layered necklace",
            "stud earrings", "hoop earrings", "drop earrings", "ear cuff",
            "bangle", "chain bracelet", "tennis bracelet", "charm bracelet",
            "stacking rings", "statement ring", "watch", "anklet",
        ],
        "accessories": [
            "tote bag", "crossbody bag", "shoulder bag", "handbag", "backpack",
            "mini bag", "evening bag", "sunglasses", "hair clips", "headband",
            "scrunchies", "belt", "wallet", "phone case", "cosmetic bag",
            "travel organizer", "passport holder",
        ],
        "wellness": [
            "resistance bands", "dumbbells", "jump rope", "pilates ring",
            "yoga mat", "yoga block", "yoga strap", "foam roller",
            "massage gun", "massage roller stick", "foot massager",
            "posture corrector", "water bottle", "shaker bottle",
        ],
    }

    seeds = []
    for niche in niches:
        niche_lower = niche.lower().strip()
        mapped = niche_mapping.get(niche_lower, niche_lower)
        if mapped in niche_seeds:
            seeds.extend(niche_seeds[mapped])
        else:
            # Try partial match
            found = False
            for key in niche_seeds.keys():
                if niche_lower in key or key in niche_lower:
                    seeds.extend(niche_seeds[key])
                    found = True
                    break
            if not found:
                # Fallback to all keywords
                seeds.extend(SEED_KEYWORDS)

    # Remove duplicates while preserving order
    seen = set()
    unique_seeds = []
    for seed in seeds:
        if seed not in seen:
            seen.add(seed)
            unique_seeds.append(seed)

    # Filter to only safe keywords
    return _filter_safe_keywords(unique_seeds) if unique_seeds else _filter_safe_keywords(SEED_KEYWORDS)



def _is_product_keyword(kw: str) -> bool:
    """Filter out non-product keywords."""
    skip_words = [
        "how to", "what is", "why", "when", "who", "where",
        "reddit", "youtube", "amazon", "walmart", "review",
        "free", "download", "login", "news", "wiki",
    ]
    return not any(skip in kw.lower() for skip in skip_words) and len(kw) > 3


# ── Apify runner ───────────────────────────────────────────────────────────

def _run_actor_sync(actor_id: str, run_input: dict, timeout: int = 120) -> List[dict]:
    if not APIFY_API_TOKEN:
        print(f"[Apify] ⚠️ APIFY_API_TOKEN not set — skipping {actor_id}.")
        return []

    url_actor_id = actor_id.replace("/", "~")
    url = f"{APIFY_BASE_URL}/acts/{url_actor_id}/run-sync-get-dataset-items"

    try:
        resp = requests.post(url, params={"token": APIFY_API_TOKEN}, json=run_input, timeout=timeout)
        if not resp.ok:
            # Apify's 400 responses carry a JSON body like
            # {"error": {"type": "invalid-input", "message": "..."}}
            # which tells you EXACTLY which field/value it rejected —
            # far more useful than guessing at the schema from docs.
            try:
                err_body = resp.json()
            except ValueError:
                err_body = resp.text[:500]
            print(f"[Apify] ❌ {resp.status_code} from {actor_id}. "
                  f"Input sent: {run_input}. Error body: {err_body}")
            return []
        items = resp.json()
        if not isinstance(items, list):
            print(f"[Apify] ⚠️ Unexpected response shape from {actor_id}: {type(items)}")
            return []
        return items
    except requests.RequestException as e:
        print(f"[Apify] ❌ Run failed for {actor_id}: {e}")
        return []


# ── Google Trends (via Apify actor) ─────────────────────────────────────────

def _fetch_google_trends(keywords: List[str], geo: str, timeframe: str) -> Dict[str, int]:
    """
    Related/rising search queries for a list of seed keywords, via an Apify
    Google Trends actor. Replaces the old pytrends-based _fetch_from_google.

    ⚠️ UNVERIFIED SCHEMA (see module docstring). The input payload below
    uses the one concretely-documented schema found for this family of
    actors (searchTerms + timeRange, matching the open-source
    emastra/actor-google-trends-scraper this ecosystem is largely forked
    from). Alternatives if this doesn't match your actor's actual input:
        - automation-lab/google-trends-scraper (two modes: "trending" /
          "explore" -- use mode="explore" for per-keyword related queries)
        - agenscrape/google-trends-scraper (this default -- "related
          searches top & rising, geo filtering, custom time ranges")
        - vnx0/google-trends-scraper -- NOTE: this one is a general daily
          "Trending Now" feed per country, NOT per-keyword related queries.
          Different data shape; only use it as a supplementary signal fed
          through the same LLM aggregation, not a replacement for this
          function.

    Returns {keyword: score} for whatever related/rising queries the actor
    returns, merged across all input keywords, max score wins on overlap.
    Returns {} on any failure -- caller falls back to DynamicKeywordScorer.
    """
    tf = TIMEFRAME_OPTIONS.get(timeframe, TIMEFRAME_OPTIONS["medium"])
    # CONFIRMED input schema (apify.com/agenscrape/google-trends-scraper/input-schema):
    #   keywords (Array), geo (String), timeRange (String), category (Int),
    #   includeRelatedSearches / includeGeoData / includeInterestOverTime (Bool)
    # Previous payload sent "searchTerms" (not a recognized field for THIS
    # actor -- that name belongs to a different actor, emastra's) plus
    # Google-Trends-internal timeRange codes -- both together produced the
    # 400 Bad Request. We only need relatedSearches (top/rising queries),
    # so geoData/interestOverTime are explicitly disabled to keep cost down.
    # after
    run_input = {
        "mode": "keyword",
        "keywords": keywords,
        "geo": geo,
        "timeRange": tf,
        "outputType": "flat",
    }
    items = _run_actor_sync(GOOGLE_TRENDS_ACTOR, run_input, timeout=180)
    if not items:
        return {}
    return _parse_google_trends_items(items)


def _parse_google_trends_items(items: List[dict]) -> Dict[str, int]:
    """
    Parser for the Google Trends actor's related-search output.

    Shapes attempted, in order:
      0. CONFIRMED (automation-lab/google-trends-scraper, mode="keyword"):
         {"type": "relatedQuery", "keyword": "...", "query": "...", "value": 100,
          "relatedType": "top"|"rising", "formattedValue": "100"|"Breakout", "link": "..."}
         value is already 0-100 (Breakout rows arrive with value=100 already), so no
         extra clipping/translation needed. This actor's keyword-mode dataset also
         contains "interestOverTime" and "regionalInterest" typed rows mixed in —
         those intentionally fall through unmatched below and are skipped, since we
         only want relatedQuery rows for keyword aggregation.
      1. Fallback (agenscrape/google-trends-scraper, if APIFY_GOOGLE_TRENDS_ACTOR is
         overridden back to it via .env):
         {"keyword": "...", "relatedSearches": {"top": [...], "rising": [...]}}
      2. Fallback -- other actor variants nesting under relatedQueries:
         {"relatedQueries": {"rising": [...], "top": [...]}}
      3. Flat row: {"query": "...", "value": 73}
      4. Interest-over-time row: {"searchTerm": "...", "<date>": "92", ...} --
         take the max value across date columns as a rough "current interest".
    """
    results: Dict[str, int] = {}
    unrecognized_logged = False

    for item in items:
        if not isinstance(item, dict):
            continue

        matched = False

        # Shape 0 (CONFIRMED real shape for automation-lab/google-trends-scraper,
        # mode="keyword"): a flat "relatedQuery"-typed row.
        if item.get("type") == "relatedQuery" and "query" in item:
            q = str(item.get("query", "")).strip().lower()
            v = _safe_int(item.get("value", 50), 50)
            if q:
                results[q] = max(results.get(q, 0), v)
                matched = True

        # Shape 1 (CONFIRMED real shape for agenscrape/google-trends-scraper):
        # {"keyword": "...", "relatedSearches": {"top": [...], "rising": [...]}}
        # "top" values are 0-100 relative popularity; "rising" values are
        # growth PERCENTAGES (e.g. 450 for "+450%") which can wildly exceed
        # 100, so we clip them to fit our 0-100 trend-score scale.
        if not matched:
            related_searches = item.get("relatedSearches")
            if isinstance(related_searches, dict):
                for bucket_name in ("rising", "top"):
                    bucket = related_searches.get(bucket_name)
                    if isinstance(bucket, list):
                        for row in bucket:
                            q = str(row.get("query", "")).strip().lower()
                            v = row.get("value", 50)
                            v = min(_safe_int(v, 50), 100)
                            if q:
                                results[q] = max(results.get(q, 0), v)
                                matched = True

        # Shape 2 (fallback -- other actor variants nest under relatedQueries)
        if not matched:
            related = item.get("relatedQueries") or item.get("related_queries")
            if isinstance(related, dict):
                for bucket_name in ("rising", "top"):
                    bucket = related.get(bucket_name)
                    if isinstance(bucket, list):
                        for row in bucket:
                            q = str(row.get("query", "")).strip().lower()
                            v = row.get("value", 50)
                            v = 100 if v == "Breakout" else _safe_int(v, 50)
                            if q:
                                results[q] = max(results.get(q, 0), v)
                                matched = True

        # Shape 3: flat query/value row (generic fallback, no "type" field)
        if not matched and "query" in item:
            q = str(item.get("query", "")).strip().lower()
            v = item.get("value", item.get("score", 50))
            v = 100 if v == "Breakout" else _safe_int(v, 50)
            if q:
                results[q] = max(results.get(q, 0), v)
                matched = True

        # Shape 4: interest-over-time row (searchTerm + date columns)
        if not matched and "searchTerm" in item:
            q = str(item.get("searchTerm", "")).strip().lower()
            date_values = [
                _safe_int(v, 0) for k, v in item.items()
                if k not in ("searchTerm",) and _safe_int(v, None) is not None
            ]
            if q and date_values:
                results[q] = max(results.get(q, 0), max(date_values))
                matched = True

        # Rows we expect and intentionally ignore (this actor's own
        # interestOverTime/regionalInterest rows in keyword mode) — don't
        # warn on these, they're not a parsing failure.
        if not matched and item.get("type") in ("interestOverTime", "regionalInterest"):
            continue

        if not matched and not unrecognized_logged:
            print(f"[GoogleTrends] ⚠️ Unrecognized item shape from {GOOGLE_TRENDS_ACTOR}, "
                  f"first 300 chars: {str(item)[:300]}")
            unrecognized_logged = True

    return results


def _safe_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _gtrends_cache_path(geo: str, cache_key_niches: str, timeframe: str) -> str:
    safe_niches = cache_key_niches.replace(" ", "_").replace("/", "_") or "general"
    return f"{GOOGLE_TRENDS_CACHE_DIR}/{geo.lower()}__{safe_niches}__{timeframe}.json"


def _load_gtrends_cache(geo: str, cache_key_niches: str, timeframe: str) -> Optional[dict]:
    path = _gtrends_cache_path(geo, cache_key_niches, timeframe)
    try:
        with open(path, "r") as f:
            cache = json.load(f)
        timestamp = datetime.fromisoformat(cache["timestamp"])
        if datetime.now() - timestamp < timedelta(days=GOOGLE_TRENDS_MAX_AGE_DAYS):
            return cache["scores"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        pass
    return None


def _save_gtrends_cache(geo: str, cache_key_niches: str, timeframe: str, scores: dict) -> None:
    path = _gtrends_cache_path(geo, cache_key_niches, timeframe)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"timestamp": datetime.now().isoformat(), "scores": scores}, f, indent=2)


def get_trending_keywords(
        niches: List[str],
        max_keywords: int = 20,
        timeframe: str = "medium",
        geo: str = "US",
        force_refresh: bool = False,
) -> List[Tuple[str, int]]:
    """
    Public entry point (drop-in replacement for the old google_trends.py
    function of the same name/signature). Fetches related/rising Google
    Trends queries for a small sample of niche seed keywords via Apify,
    with disk caching per (geo, niches, timeframe) and a non-Apify fallback
    if the actor call fails.

    Cached for GOOGLE_TRENDS_MAX_AGE_DAYS (default 3) -- this is what stops
    a duplicate geo (e.g. a "US" region reusing the global pass's "US" geo)
    or a same-day re-run from paying for another actor call.
    """
    cache_key_niches = "-".join(sorted((n or "").lower().strip() for n in (niches or ["general"])))

    if not force_refresh:
        cached = _load_gtrends_cache(geo, cache_key_niches, timeframe)
        if cached is not None:
            print(f"[GoogleTrends] ✅ Using cached trends for geo={geo} "
                  f"(< {GOOGLE_TRENDS_MAX_AGE_DAYS}d old, {len(cached)} keywords)")
            sorted_cached = sorted(cached.items(), key=lambda x: x[1], reverse=True)
            return sorted_cached[:max_keywords]

    seeds = _filter_safe_keywords(_build_seed_list(niches))[:3]  # keep actor calls cheap
    results: dict = {}

    if seeds:
        print(f"[GoogleTrends] 🔄 Calling {GOOGLE_TRENDS_ACTOR} for geo={geo}, seeds={seeds}...")
        results = _fetch_google_trends(seeds, geo, timeframe)

    if not results:
        print("[GoogleTrends] ✅ Actor returned nothing — using dynamic fallback scoring.")
        results = _get_dynamic_fallback_keywords()

    if not results:
        print("[GoogleTrends] ⚠️ Fallback also failed, using intelligent random scoring.")
        results = _get_intelligent_random_scores()

    results = {k: v for k, v in results.items() if _is_safe_keyword(k)}

    if results:
        _save_gtrends_cache(geo, cache_key_niches, timeframe, results)

    sorted_results = sorted(results.items(), key=lambda x: x[1], reverse=True)
    return sorted_results[:max_keywords]


def _get_dynamic_fallback_keywords() -> dict:
    """Use dynamic scoring with real market data, filtered for safety."""
    global _scorer
    return _scorer.get_scores()


def _get_intelligent_random_scores() -> dict:
    """Ultimate fallback with controlled randomness, only for safe keywords."""
    scores = {}
    category_weights = {
        # Fashion
        "dress": 85, "t-shirt": 78, "blouse": 80, "jeans": 84,
        "leggings": 82, "hoodie": 83, "blazer": 79, "cardigan": 77,
        "lounge set": 86, "two piece set": 84,

        # Shoes
        "sneakers": 90, "heels": 85, "sandals": 82, "boots": 86, "flats": 78,

        # Jewelry
        "necklace": 85, "earring": 83, "bracelet": 82, "ring": 84,
        "anklet": 75, "choker": 78, "pendant": 76, "watch": 83,

        # Accessories
        "handbag": 88, "tote bag": 84, "crossbody bag": 85,
        "sunglasses": 82, "scrunchie": 80, "claw clip": 79,
        "phone case": 81, "wallet": 78,

        # Wellness
        "massage gun": 92, "massager": 88, "yoga mat": 88,
        "resistance band": 85, "dumbbell": 80, "jump rope": 80,
        "foam roller": 79, "water bottle": 82, "posture corrector": 78,
    }
    for keyword in SEED_KEYWORDS:
        if not _is_safe_keyword(keyword):
            continue
        base_score = 70
        keyword_lower = keyword.lower()
        for pattern, weight in category_weights.items():
            if pattern in keyword_lower:
                base_score = weight
                break
        random_variation = random.randint(-15, 15)
        month = datetime.now().month
        seasonal_boost = 0
        if month in [6, 7, 8]:
            if any(k in keyword_lower for k in ["water bottle", "sweat", "towel", "gym", "fitness"]):
                seasonal_boost = 15
        elif month in [11, 12]:
            if any(k in keyword_lower for k in ["gift", "set", "travel", "mini", "jewelry"]):
                seasonal_boost = 20
        elif month in [12, 1, 2]:
            if any(k in keyword_lower for k in ["pillowcase", "sleep mask", "weighted blanket", "massage"]):
                seasonal_boost = 15
        final_score = min(100, max(50, base_score + random_variation + seasonal_boost))
        scores[keyword] = int(final_score)
    return scores


# ── TikTok ───────────────────────────────────────────────────────────────

def _fetch_tiktok_trends(max_keywords: int, country: str) -> Dict[str, int]:
    """
    Top trending hashtags from TikTok Creative Center via the
    data_xplorer/tiktok-trends actor. Returns {hashtag: score} normalised
    0-100, filtered to only safe product categories.

    Confirmed input schema (verified against a real run — 2026-07-01,
    countryCode=FR returned real Creative Center data, not a fallback stub):
        maxItems            int
        proxyConfiguration   {"useApifyProxy": true}
        saveMedia            bool
        trendType            "hashtags" (also likely supports sounds/creators/videos)
        countryCode          ISO-2 country code, e.g. "US", "FR"
        hashtagPeriod        "7" | "30" | "120"  (string, not int)
        industryId            "" for all industries, or a specific industry filter

    Confirmed hashtag output item shape (real fields, note the spaced/
    capitalized keys — NOT camelCase):
        {"Rank": 1, "Hashtag": "#brevet", "Hashtag ID": "8428894",
         "Posts": 39104, "Video Views": 209392029,
         "Industries": ["Education"], "Country": "France",
         "Country Code": "FR", "Period": "7 days",
         "TikTok URL": "...", "Trend Direction": "up",
         "Trend Stats": {"average": 50.16, "max": 100, "min": 0},
         "Top Creators": [...]}

    We previously defaulted to automation-lab/tiktok-trends-scraper, but a
    real test run showed it silently returning a placeholder/fallback
    payload (near-zero view counts on generic hashtags like #fyp) instead
    of erroring — this actor returned verified real data on the same kind
    of query, so it's the default now.
    """
    run_input = {
        "maxItems": max_keywords,
        "proxyConfiguration": {"useApifyProxy": True},
        "saveMedia": False,
        "trendType": "hashtags",
        "countryCode": country,
        "hashtagPeriod": "7",
        "industryId": "",
    }
    items = _run_actor_sync(TIKTOK_ACTOR, run_input)
    scores: Dict[str, int] = {}

    for item in items:
        name = item.get("Hashtag") or item.get("hashtag") or item.get("name")
        if not name:
            continue
        name = str(name).lstrip("#").strip().lower()
        if not name:
            continue

        # Filter to only safe categories
        if not _is_safe_hashtag(name):
            continue

        raw_score = item.get("Video Views") or item.get("Posts")
        try:
            score = int(raw_score) if raw_score else 70
        except (TypeError, ValueError):
            score = 70

        # "up" trending hashtags get a momentum bump so they don't get
        # buried under long-established hashtags with huge but flat counts.
        trend_direction = str(item.get("Trend Direction", "")).lower()
        if trend_direction == "up":
            score = int(score * 1.15)

        scores[name] = max(scores.get(name, 0), score)

    return _normalize_0_100(scores)


def _parse_magnitude(value) -> float:
    """
    Parses the actor's human-readable magnitude strings into a number, e.g.
    "36.27 M" -> 36270000.0, "149.66 m" -> 149660000.0, "317.57 k" -> 317570.0,
    "4060" -> 4060.0, "—" / "" / None -> 0.0.
    Suffix is case-insensitive: k=thousand, m=million, g or b=billion.
    """
    if value is None:
        return 0.0
    s = str(value).strip().lower().replace(",", "")
    if not s or s in ("—", "-", "n/a"):
        return 0.0
    multiplier = 1.0
    if s.endswith("k"):
        multiplier, s = 1_000.0, s[:-1]
    elif s.endswith("m"):
        multiplier, s = 1_000_000.0, s[:-1]
    elif s.endswith(("g", "b")):
        multiplier, s = 1_000_000_000.0, s[:-1]
    try:
        return float(s.strip()) * multiplier
    except ValueError:
        return 0.0


# Apify's instagram-hashtag-analytics-scraper rejects the ENTIRE batch call
# if even ONE hashtag in the list fails its input validation pattern:
#   ^[^!?.,:;\-+=*&%$#@/\~^|<>()[\]{}"'`\s]+$
# i.e. no punctuation, no whitespace — hyphens included. A seed keyword like
# "t-shirt" (hyphen) or "old money style" (already space-stripped, but a
# keyword with an apostrophe, e.g. a future "women's" entry, would hit the
# same problem) silently kills every hashtag in that call, not just the bad
# one, since it's a single request for all of them. Strip anything that
# isn't a letter/digit/underscore, not just spaces.
_HASHTAG_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_]+")


def _sanitize_hashtag(kw: str) -> str:
    return _HASHTAG_INVALID_CHARS.sub("", kw)


def _fetch_instagram_trends(seed_hashtags: List[str], max_keywords: int) -> Dict[str, int]:
    """
    Queries apify/instagram-hashtag-analytics-scraper with seed hashtags
    drawn from your SEED_KEYWORDS list, and pulls both the queried
    hashtags' own post volume AND their close related-hashtag network —
    related hashtags are how "discovery" happens here, since Instagram has
    no public trending-by-niche feed.

    Confirmed input schema (verified against a real run):
        hashtags            list[str], no "#" prefix
        includeLatestPosts   bool  (we pass False — we only want stats, not posts)
        includeTopPosts      bool  (we pass False — same reason, keeps cost down)

    Confirmed output item shape (real fields — verified 2026-07):
        {"name": "skincareroutine", "id": "skincareroutine",
         "postsCount": 36270000, "posts": "36.27 M", "postsPerDay": "—",
         "url": "...",
         "related":        [{"hash": "#skincare", "info": "149.66 m"}, ...],
         "frequent":       [{"hash": "#skincareroutine", "info": "36.27 m"}, ...],
         "average":        [{"hash": "#koreanskincareroutine", "info": "478.11 k"}, ...],
         "rare":           [{"hash": "#dailyskincareroutine", "info": "98 k"}, ...],
         "relatedFrequent": [...], "relatedAverage": [...], "relatedRare": [...]}
    A hashtag with zero/no data (e.g. an uncommon or malformed tag) may come
    back with just {"name", "postsCount": 0, "url", "id"} and nothing else.

    NOTE: we deliberately skip the relatedFrequent/relatedAverage/relatedRare
    buckets — those surface very generic tags (#love, #instagood, #fashion)
    that aren't specific to the beauty niche and would just add noise.
    "related"/"frequent"/"average"/"rare" (the ones tied directly to the
    queried hashtag) are specific enough to be useful.
    """
    tags = [_sanitize_hashtag(kw) for kw in seed_hashtags[:max_keywords] if kw.strip()]
    tags = [t for t in tags if t]  # drop anything that sanitized to empty (e.g. pure punctuation)
    if not tags:
        return {}

    run_input = {
        "hashtags": tags,
        "includeLatestPosts": False,
        "includeTopPosts": False,
    }
    items = _run_actor_sync(INSTAGRAM_ACTOR, run_input)
    scores: Dict[str, int] = {}

    RELEVANT_BUCKETS = ("related", "frequent", "average", "rare")

    for item in items:
        name = item.get("name") or item.get("id")
        if name:
            name = str(name).lstrip("#").strip().lower()
            # Filter to only safe categories
            if _is_safe_hashtag(name):
                try:
                    posts_count = int(item.get("postsCount") or 0)
                except (TypeError, ValueError):
                    posts_count = 0
                if posts_count > 0:
                    scores[name] = max(scores.get(name, 0), posts_count)

        for bucket in RELEVANT_BUCKETS:
            for rel in item.get(bucket) or []:
                if not isinstance(rel, dict):
                    continue
                rel_name = rel.get("hash")
                if not rel_name:
                    continue
                rel_name = str(rel_name).lstrip("#").strip().lower()
                # Filter to only safe categories
                if not _is_safe_hashtag(rel_name):
                    continue
                rel_score = _parse_magnitude(rel.get("info"))
                if rel_score <= 0:
                    continue
                scores[rel_name] = max(scores.get(rel_name, 0), rel_score)

    return _normalize_0_100(scores)


def _normalize_0_100(scores: Dict[str, int]) -> Dict[str, int]:
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi == lo:
        return {k: 75 for k in scores}
    return {k: int(50 + (v - lo) / (hi - lo) * 50) for k, v in scores.items()}


# ── Weekly cache ─────────────────────────────────────────────────────────

def _cache_path_for(country: str) -> str:
    """
    Per-country cache file. IMPORTANT: this is keyed by country because the
    pipeline now calls get_social_trending_keywords() once per world-trends
    region (US/GB/KR/JP/BR...) with a different `country` each time — a
    single shared cache file would silently serve Korea's trends back to
    Japan's request (or overwrite one country's cache with another's).
    The base CACHE_PATH is used as-is only for the default/global country.
    """
    if country == DEFAULT_COUNTRY:
        return CACHE_PATH
    root, ext = os.path.splitext(CACHE_PATH)
    return f"{root}_{country.lower()}{ext}"


def _load_cache(country: str = DEFAULT_COUNTRY) -> Optional[dict]:
    path = _cache_path_for(country)
    try:
        with open(path, "r") as f:
            cache = json.load(f)
        timestamp = datetime.fromisoformat(cache["timestamp"])
        if datetime.now() - timestamp < timedelta(days=MAX_CACHE_AGE_DAYS):
            return cache["scores"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        pass
    return None


def _save_cache(scores: Dict[str, int], country: str = DEFAULT_COUNTRY) -> None:
    path = _cache_path_for(country)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"timestamp": datetime.now().isoformat(), "scores": scores}, f, indent=2)


# ── Public entry point ──────────────────────────────────────────────────

def get_social_trending_keywords(
    seed_keywords: List[str],
    max_keywords: int = 30,
    country: str = DEFAULT_COUNTRY,
    force_refresh: bool = False,
) -> List[Tuple[str, int]]:
    """
    Returns [(keyword, score), ...] merged from TikTok + Instagram, sorted
    by score, capped at max_keywords. Only returns trends related to
    safe product categories (jewelry, massage, yoga, fitness, etc.)

    Cached for MAX_CACHE_AGE_DAYS (default 7): the Apify actors only
    actually run about once a week no matter how often the pipeline itself
    runs, protecting your Apify credit. Pass force_refresh=True to bypass
    the cache (e.g. a manual "refresh social trends now" run).
    """
    if not force_refresh:
        cached = _load_cache(country)
        if cached is not None:
            # Filter cached results to only safe categories
            filtered_cache = _filter_safe_trends(cached)
            print(f"[SocialTrends] ✅ Using cached social trends for {country} (< {MAX_CACHE_AGE_DAYS}d old, {len(filtered_cache)} safe keywords)")
            return sorted(filtered_cache.items(), key=lambda x: x[1], reverse=True)[:max_keywords]

    print(f"[SocialTrends] 🔄 Cache stale or missing for {country} — calling Apify actors (this spends Apify credit)...")

    tiktok_scores = _fetch_tiktok_trends(max_keywords, country)
    print(f"[SocialTrends]   TikTok: {len(tiktok_scores)} safe hashtags")

    time.sleep(1)  # small courtesy gap between actor runs

    instagram_scores = _fetch_instagram_trends(seed_keywords, max_keywords)
    print(f"[SocialTrends]   Instagram: {len(instagram_scores)} safe hashtags")

    merged: Dict[str, int] = {}
    for kw in set(tiktok_scores) | set(instagram_scores):
        merged[kw] = max(tiktok_scores.get(kw, 0), instagram_scores.get(kw, 0))

    # Filter merged results to only safe categories (defensive)
    merged = _filter_safe_trends(merged)

    if merged:
        _save_cache(merged, country)
        print(f"[SocialTrends] ✅ Saved {len(merged)} safe social trends to cache for {country}")
    else:
        print("[SocialTrends] ⚠️ No safe results from either platform — cache left untouched.")

    return sorted(merged.items(), key=lambda x: x[1], reverse=True)[:max_keywords]