"""
Layer 1 Pipeline — Market Intelligence
=======================================
Builds and runs the LangGraph graph:

  START → TrendScout → SupplierFinder → Scorer → Reporter → END

Usage:
    python -m layer1.pipeline
    # or import and call run_layer1() from your orchestrator

Configuration:
    All the knobs below (niches, max_trends, regions, etc.) now come from
    config.json's "layer1" section by default — see config_loader.py.
    Pass an explicit argument to run_layer1(...) to override any single
    value for a one-off call without touching config.json.
"""

import json
import os
from datetime import datetime
from pathlib import Path


from langgraph.graph import StateGraph, END

from layer1.state import GraphState
from layer1.agents.nodes import trend_scout_node, supplier_finder_node, scorer_node, REGION_KEYS
from dotenv import load_dotenv
load_dotenv()

from config_loader import get_layer_config

# Fallback defaults, used only if config.json has no "layer1" section at all
# (or is missing individual keys) — keeps this module importable/runnable
# even without a config.json present.
_DEFAULTS = {
    "niches":                   ["fashion", "shoes", "jewelry", "accessories", "wellness"],
    "max_trends":               15,
    "max_seed_keywords":        40,
    "max_products_per_keyword": 30,
    "regions":                  None,   # None -> REGION_KEYS (all regions) at call time
    "max_region_trends":        10,
    "seen_pids_path":           "layer1/output/seen_pids.json",
}


def build_graph() -> StateGraph:
    graph = StateGraph(GraphState)

    graph.add_node("TrendScout",     trend_scout_node)
    graph.add_node("SupplierFinder", supplier_finder_node)
    graph.add_node("Scorer",         scorer_node)
    graph.add_node("Reporter",       reporter_node)

    graph.set_entry_point("TrendScout")
    graph.add_edge("TrendScout",     "SupplierFinder")
    graph.add_edge("SupplierFinder", "Scorer")
    graph.add_edge("Scorer",         "Reporter")
    graph.add_edge("Reporter",       END)

    return graph.compile()


def reporter_node(state: GraphState) -> dict:
    """
    Final node: saves winning products to JSON + prints a summary.
    The JSON output is what Layer 2 (Listing Generator) reads as input.
    """
    winners = state.get("winning_products", [])
    output_dir = Path("layer1/output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"winning_products.json"

    winners_data = [p.model_dump() for p in winners]

    with open(output_path, "w") as f:
        json.dump(winners_data, f, indent=2)

    # ── Console summary ────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  LAYER 1 COMPLETE — BEAUTY & SKINCARE WINNING PRODUCTS")
    print("="*60)

    from collections import defaultdict
    by_collection: dict = defaultdict(list)
    for p in winners:
        by_collection[p.collection or "Unassigned"].append(p)

    source_icon = {"trend": "📈", "seed": "⭐", "new_arrival": "🆕", "region_trend": "🌍"}

    rank = 1
    for collection_name, products in by_collection.items():
        print(f"\n📦 {collection_name}")
        print("-" * 50)
        for p in products:
            icon = source_icon.get(p.source, "•")
            print(f"\n  #{rank} {icon} {p.title}")
            region_suffix = f"  |  Region: {p.region_label}" if p.region_label else ""
            print(f"      Source:   {p.source}{region_suffix}  |  Tags: {', '.join(p.shopify_tags)}")
            print(f"      Cost:     ${p.supplier_price:.2f}")
            print(f"      Retail:   ${p.suggested_retail:.2f}")
            print(f"      Margin:   {p.margin_pct}%")
            print(f"      Score:    {p.score}/100")
            print(f"      Keyword:  {p.trend_keyword} (trend score: {p.trend_score})")
            print(f"      Shipping: ~{p.shipping_days} days")
            print(f"      CJ Link:  {p.cj_product_url}")
            rank += 1

    # ── Source summary ─────────────────────────────────────────────────────
    source_counts: dict = defaultdict(int)
    for p in winners:
        source_counts[p.source] += 1
    print("\n📊 Product mix:")
    for src, count in sorted(source_counts.items()):
        label = {
            "trend": "📈 Trending", "seed": "⭐ Evergreen",
            "new_arrival": "🆕 New Arrivals", "region_trend": "🌍 World Trends",
        }.get(src, src)
        print(f"   {label}: {count}")

    # ── World Trends breakdown by region ────────────────────────────────────
    region_counts: dict = defaultdict(int)
    for p in winners:
        if p.region:
            region_counts[p.region_label or p.region] += 1
    if region_counts:
        print("\n🌍 World Trends collection, by region:")
        for region_label, count in sorted(region_counts.items(), key=lambda x: -x[1]):
            print(f"   {region_label}: {count}")

    if state.get("errors"):
        print(f"\n⚠️  Non-fatal errors encountered: {len(state['errors'])}")
        for e in state["errors"]:
            print(f"   - {e}")

    print(f"\n📄 Full results saved to: {output_path}")
    print("="*60)
    print("  → Pass this file to Layer 2 (Listing Generator) to publish products")
    print("="*60 + "\n")

    return {"report_path": str(output_path)}


def run_layer1(
    niches: list = None,
    max_trends: int = None,
    max_seed_keywords: int = None,
    max_products_per_keyword: int = None,
    regions: list = None,
    max_region_trends: int = None,
) -> dict:
    """
    Run the full Layer 1 pipeline and return the final state.

    Every argument defaults to None, meaning "use config.json's 'layer1'
    section". Pass an explicit value to override just that one setting for
    a single call without editing config.json.

    Strategy:
    - TrendScout fetches Google + TikTok + Instagram trends, aggregates them
      via LLM into `max_trends` canonical trend keywords, AND passes through
      SEED_KEYWORDS (capped to `max_seed_keywords` if provided)
    - SupplierFinder searches CJ with each keyword (trend + seed, deduped) twice:
        pass 1 (sort=orders)  → proven sellers tagged as "trend" or "seed"
        pass 2 (sort=date)    → new arrivals tagged as "new_arrival"
    - PIDs already seen in previous runs are skipped (seen_pids.json)
    - Scorer applies a source bonus so new arrivals compete fairly
    - Products carry shopify_tags: ["Trending Now"], ["New In"], ["Staff Pick"]

    Args:
        niches: product niches to target, e.g. ["beauty & skincare"]
        max_trends: number of TREND-derived keywords to keep after Google +
            TikTok + Instagram aggregation (each costs an LLM call + CJ search)
        max_seed_keywords: number of SEED_KEYWORDS to search directly against
            CJ. None = falls through to config.json (or ~250 if config also
            omits it). Pass an int (e.g. 10 or 20) to cap it for a cheap/fast
            test run, since each seed keyword = 2 CJ API calls.
        max_products_per_keyword: CJ results kept per keyword per sort pass
        regions: which world-trend regions to pull ("US", "Europe", "Korea",
            "Japan", "LatinAmerica", "Africa" — see REGIONS in
            layer1/agents/nodes.py). Pass [] to disable World Trends entirely.
        max_region_trends: number of trend keywords kept PER region after
            LLM aggregation (each region ≈ max_region_trends × 1 CJ pass)

    Total unique keywords searched ≈ max_trends + max_seed_keywords (minus
    any overlap, which is deduped automatically). Total CJ API calls ≈
    that number × 2 (two sort passes per keyword), PLUS
    len(regions) × max_region_trends (one sort pass per region keyword).
    """
    cfg = get_layer_config("layer1")

    niches = niches if niches is not None else cfg.get("niches", _DEFAULTS["niches"])
    max_trends = max_trends if max_trends is not None else cfg.get("max_trends", _DEFAULTS["max_trends"])
    max_seed_keywords = (
        max_seed_keywords if max_seed_keywords is not None
        else cfg.get("max_seed_keywords", _DEFAULTS["max_seed_keywords"])
    )
    max_products_per_keyword = (
        max_products_per_keyword if max_products_per_keyword is not None
        else cfg.get("max_products_per_keyword", _DEFAULTS["max_products_per_keyword"])
    )
    regions = regions if regions is not None else cfg.get("regions", _DEFAULTS["regions"])
    max_region_trends = (
        max_region_trends if max_region_trends is not None
        else cfg.get("max_region_trends", _DEFAULTS["max_region_trends"])
    )
    seen_pids_path = cfg.get("seen_pids_path", _DEFAULTS["seen_pids_path"])

    graph = build_graph()

    # Ensure output dir exists for seen_pids
    Path("layer1/output").mkdir(parents=True, exist_ok=True)

    initial_state: GraphState = {
        "niches":                   niches,
        "max_trends":               max_trends,
        "max_seed_keywords":        max_seed_keywords,
        "max_products_per_keyword": max_products_per_keyword,
        "regions":                  regions if regions is not None else REGION_KEYS,
        "max_region_trends":        max_region_trends,
        "trend_keywords":           [],
        "seed_keywords":            [],
        "trend_scores":             [],
        "region_trend_keywords":    {},
        "raw_products":             [],
        "scored_products":          [],
        "winning_products":         [],
        "report_path":              None,
        "errors":                   [],
        "seen_pids_path":           seen_pids_path,
    }

    print("\n🚀 Starting Layer 1 — Beauty & Skincare Market Intelligence Pipeline")
    print(f"   Niches: {initial_state['niches']}")
    print(f"   Max trend keywords: {max_trends}")
    print(f"   Max seed keywords: {max_seed_keywords if max_seed_keywords else 'unlimited (~250)'}")
    print(f"   Products per keyword (×2 passes): {max_products_per_keyword}")
    print(f"   World Trends regions: {initial_state['regions'] or 'disabled'}")
    print(f"   Max trend keywords per region: {max_region_trends}")
    print(f"   Seen PIDs cache: {seen_pids_path}\n")

    final_state = graph.invoke(initial_state)
    return final_state


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # All settings now come from config.json's "layer1" section — edit that
    # file to change niches / trend counts / regions / etc. instead of this
    # block. Call run_layer1(...) with explicit kwargs here only if you want
    # a one-off override that bypasses config.json for a single run.
    result = run_layer1()