"""
Price Optimizer Node
====================
Sets smart retail prices for each listing using:

1. Margin rules        → minimum 20%, target 25%, max 30% (see PRICING_CONFIG)
2. Psychological pricing → .99 endings, charm pricing
3. Compare-at price    → creates "sale" perception (was $X, now $Y)
4. Competitor lookup   → optional SerpAPI Google Shopping check
   (enabled via SERPAPI_KEY in .env, skipped if not set)

No LLM needed here — pure deterministic logic.
This is intentional: pricing is math, not creativity.

Configuration:
    PRICING_CONFIG's values now come from config.json's
    "layer2.pricing" section by default (see config_loader.py), falling
    back to the hardcoded _DEFAULT_PRICING_CONFIG below if config.json or
    that section is missing — so this module still works standalone.
"""

import os
import math
import requests
from typing import List, Optional

from layer2.state import GraphState, ShopifyListing
from config_loader import get_layer_config


# ── Pricing configuration ─────────────────────────────────────────────────────

_DEFAULT_PRICING_CONFIG = {
    "min_margin_pct":       20.0,   # never go below this (keeping buffer)
    "target_margin_pct":    25.0,   # aim for this
    "max_margin_pct":       30.0,   # hard cap - never exceed this
    "compare_at_multiplier": 1.35,  # compare_at = retail * 1.35 (looks like a discount)
    "min_absolute_profit":   5.0,   # minimum $5 profit per sale
}

_layer2_cfg = get_layer_config("layer2")
PRICING_CONFIG = {**_DEFAULT_PRICING_CONFIG, **_layer2_cfg.get("pricing", {})}

# Price bands for psychological pricing (.99 endings)
# e.g. $12.40 → $11.99, $45.00 → $44.99
CHARM_ENDINGS = [0.99, 0.95, 0.97]


def price_optimizer_node(state: GraphState) -> dict:
    """
    Calculates optimal retail price for each listing.
    Adds compare_at_price for perceived discount.
    """
    print("\n[PriceOptimizer] 💰 Optimising prices...")
    errors = list(state.get("errors", []))
    listings = state.get("listings", [])

    if not listings:
        errors.append("PriceOptimizer: No listings to price.")
        return {"priced_listings": [], "errors": errors}

    priced: List[ShopifyListing] = []

    for listing in listings:
        try:
            retail, compare_at, margin = _calculate_price(
                listing.supplier_price,
                suggested_retail=getattr(listing, "suggested_retail", None),
            )

            # Optional: check competitor prices and adjust if needed
            competitor_price = _get_competitor_price(listing.shopify_title)
            if competitor_price:
                retail, compare_at, margin = _adjust_for_competition(
                    retail, compare_at, listing.supplier_price, competitor_price
                )

            listing.retail_price = retail
            listing.compare_at_price = compare_at
            listing.margin_pct = margin

            print(f"[PriceOptimizer]   {listing.shopify_title[:40]:40s} "
                  f"cost=${listing.supplier_price:.2f} → "
                  f"retail=${retail:.2f} (margin {margin:.1f}%)")

            # Price each real variant too. StorePublisher reads
            # v['retail_price'] off each variant dict when building the
            # Shopify variants payload — if we don't set it here, any
            # product with >=2 variants crashes with KeyError downstream.
            listing.variants = _price_variants(listing.variants, listing.supplier_price, retail)

            priced.append(listing)

        except Exception as e:
            error_msg = f"PriceOptimizer error for '{listing.original_title}': {e}"
            errors.append(error_msg)
            # Still add the listing with a default price so pipeline continues.
            # Use the configured MINIMUM margin, not an arbitrary 10% — a
            # silent 10% fallback margin would undercut the 20% floor set
            # in PRICING_CONFIG for every product that hits this branch.
            cfg = PRICING_CONFIG
            fallback_retail = round(listing.supplier_price / (1 - cfg["min_margin_pct"] / 100), 2)
            listing.retail_price = fallback_retail
            listing.compare_at_price = round(fallback_retail * cfg["compare_at_multiplier"], 2)
            listing.margin_pct = cfg["min_margin_pct"]
            listing.variants = _price_variants(listing.variants, listing.supplier_price, fallback_retail)
            priced.append(listing)

    print(f"[PriceOptimizer] ✅ Priced {len(priced)} listings")
    return {"priced_listings": priced, "errors": errors}


def _calculate_price(cost: float, suggested_retail: Optional[float] = None) -> tuple[float, float, float]:
    """
    Compute retail price. If Layer 1 already supplied a suggested_retail
    (its tiered cost-based multiplier — see cj_api._calculate_retail_price),
    use that as the basis instead of recomputing from a flat margin target.
    Layer 1's tiering already handles cheap items correctly (a $1 item needs
    a much higher multiplier to clear fixed per-order costs); Layer 2's old
    flat 25%-margin-target + $5-minimum-profit formula fought that logic and
    produced margins that blew past its own 30% cap on cheap items (e.g. a
    $1 cost item landed at 49.7% margin instead of the intended ≤30%).

    Falls back to the old margin-target formula only if no suggested_retail
    is available (e.g. a listing that didn't come through Layer 1).
    Returns (retail_price, compare_at_price, actual_margin_pct)
    """
    cfg = PRICING_CONFIG

    if suggested_retail and suggested_retail > cost:
        raw_retail = suggested_retail
    else:
        target_retail = cost / (1 - cfg["target_margin_pct"] / 100)
        min_retail_by_profit = cost + cfg["min_absolute_profit"]
        raw_retail = max(target_retail, min_retail_by_profit)

    retail = _charm_price(raw_retail)
    actual_margin = (retail - cost) / retail * 100

    # Safety floor only — never let a listing go below the minimum margin,
    # regardless of what Layer 1 suggested (protects against a bad/stale
    # CJ price feeding through). No max-margin cap: Layer 1's tiered
    # pricing deliberately runs a higher margin % on cheap items (that's
    # still only a couple dollars of absolute profit) — capping it back
    # down is what caused the mismatch in the first place.
    if actual_margin < cfg["min_margin_pct"]:
        retail = _charm_price(cost / (1 - cfg["min_margin_pct"] / 100))
        actual_margin = (retail - cost) / retail * 100

    compare_at = None
    return retail, compare_at, round(actual_margin, 1)


def _price_variants(variants: list, listing_cost: float, listing_retail: float) -> list:
    """
    Sets a 'retail_price' on every variant dict so StorePublisher can build
    the Shopify variants payload without KeyError'ing.

    CJ variant dicts may carry their own per-variant cost (different sizes/
    colors often cost different amounts to source). We check the common
    key names CJ/Layer1 might use for that; if none are present, every
    variant just inherits the listing-level retail price, which is still
    correct, just not size/color-differentiated.
    """
    if not variants:
        return variants

    for v in variants:
        variant_cost = v.get("supplier_price") or v.get("variant_price") or v.get("price")
        if variant_cost:
            try:
                v_retail, _, _ = _calculate_price(float(variant_cost))
                v["retail_price"] = v_retail
                continue
            except (TypeError, ValueError):
                pass
        # No usable per-variant cost — fall back to the listing's price.
        v["retail_price"] = listing_retail

    return variants


def _charm_price(price: float) -> float:
    """
    Round to nearest psychological price point.
    e.g. $12.43 → $11.99, $47.20 → $46.99
    """
    # Round up to next dollar, then subtract 0.01
    ceiling = math.ceil(price)
    charm = ceiling - 0.01

    # If we're already at .99, keep it
    if abs(price - charm) < 0.05:
        return charm

    # Otherwise check if rounding down makes more sense
    floor_charm = math.floor(price) - 0.01
    if floor_charm > price * 0.92:  # don't go more than 8% below
        return floor_charm

    return charm


def _adjust_for_competition(
    retail: float,
    compare_at: float,
    cost: float,
    competitor_price: float,
) -> tuple[float, float, float]:
    """
    If competitor is cheaper, undercut by 5-10%.
    If we're significantly cheaper, we can raise margin slightly.
    Never go below minimum margin or above maximum margin.
    """
    cfg = PRICING_CONFIG
    min_retail = cost / (1 - cfg["min_margin_pct"] / 100)
    max_retail = cost / (1 - cfg["max_margin_pct"] / 100)

    if competitor_price < retail:
        # Undercut competitor by 5%
        target = competitor_price * 0.95
        retail = max(_charm_price(target), min_retail)
    elif competitor_price > retail * 1.20:
        # We're much cheaper — raise price slightly (but stay competitive)
        target = competitor_price * 0.88
        retail = _charm_price(min(target, retail * 1.15))

    # Ensure we stay within min/max bounds
    retail = max(min_retail, min(retail, max_retail))
    retail = _charm_price(retail)

    actual_margin = (retail - cost) / retail * 100
    compare_at = _charm_price(retail * cfg["compare_at_multiplier"])

    return retail, compare_at, round(actual_margin, 1)


def _get_competitor_price(product_title: str) -> Optional[float]:
    """
    Optional: fetch competitor prices from Google Shopping via SerpAPI.
    Returns None if SERPAPI_KEY is not configured (silently skipped).
    """
    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        return None

    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={
                "engine": "google_shopping",
                "q": product_title,
                "api_key": api_key,
                "num": 5,
            },
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("shopping_results", [])

        prices = []
        for r in results[:5]:
            price_str = r.get("price", "").replace("$", "").replace(",", "").strip()
            try:
                prices.append(float(price_str))
            except ValueError:
                continue

        return min(prices) if prices else None

    except Exception:
        return None