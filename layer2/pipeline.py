"""
Layer 2 Pipeline — Store & Listing
====================================
Graph:

  START
    ↓
  DuplicateFilter    (check cj_pid registry — skip already published)
    ↓
  ListingGenerator   (SEO title, description, tags, meta)
    ↓
  PriceOptimizer     (retail price, compare_at, margin)
    ↓
  ImageAgent         (download, bg removal, resize)
    ↓
  StorePublisher     (upload images, create product, publish)
    ↓
  END

Configuration:
    store_url / publish_immediately (and Layer 1's settings, when running
    the full automated pipeline) come from config.json by default — see
    config_loader.py. `python -m layer2.pipeline` picks these up with no
    extra flags needed.
"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END

from layer2.state import GraphState
from layer2.agents.listing_generator import listing_generator_node
from layer2.agents.price_optimizer import price_optimizer_node
from layer2.agents.image_agent import image_agent_node
from layer2.agents.store_publisher import store_publisher_node, _cj_pid_exists

load_dotenv()

from config_loader import get_layer_config


# ── Duplicate Filter Node ─────────────────────────────────────────────────────

def duplicate_filter_node(state: GraphState) -> dict:
    """
    First node in the pipeline.
    Uses _cj_pid_exists() which checks:
      1. Local registry (fast)
      2. TTL expiry (90 days)
      3. Shopify API verification (handles manual deletions)
    This prevents wasting LLM calls, image processing, and API calls
    on products that are truly already live on Shopify.
    """
    print("\n[DuplicateFilter] 🔍 Checking for already-published products...")
    all_products = state.get("winning_products", [])

    new_products = []
    skipped = []

    for p in all_products:
        cj_pid = p.get("cj_pid", "")
        if cj_pid and _cj_pid_exists(cj_pid):
            skipped.append(p.get("title", cj_pid))
        else:
            new_products.append(p)

    if skipped:
        print(f"[DuplicateFilter] ⏭️  Skipping {len(skipped)} already-published products:")
        for title in skipped:
            print(f"[DuplicateFilter]    - {title[:60]}")

    print(f"[DuplicateFilter] ✅ {len(new_products)} new products to process\n")

    return {"winning_products": new_products}


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(GraphState)

    graph.add_node("DuplicateFilter", duplicate_filter_node)
    graph.add_node("ListingGenerator", listing_generator_node)
    graph.add_node("PriceOptimizer", price_optimizer_node)
    graph.add_node("ImageAgent", image_agent_node)
    graph.add_node("StorePublisher", store_publisher_node)

    graph.set_entry_point("DuplicateFilter")
    graph.add_edge("DuplicateFilter", "ListingGenerator")
    graph.add_edge("ListingGenerator", "PriceOptimizer")
    graph.add_edge("PriceOptimizer", "ImageAgent")
    graph.add_edge("ImageAgent", "StorePublisher")
    graph.add_edge("StorePublisher", END)

    return graph.compile()


# ── Runner ────────────────────────────────────────────────────────────────────

def run_layer2(winning_products: list = None, layer1_json_path: str = None,
               publish_immediately: bool = None) -> dict:
    """
    Run Layer 2 pipeline.

    Args:
        winning_products:   list of product dicts (from Layer 1 state directly)
        layer1_json_path:   OR path to Layer 1's output JSON file
        publish_immediately: Whether to publish immediately or draft.
                              None (default) -> read from config.json's
                              "layer2.publish_immediately" (falls back to
                              True if config.json/that key is missing).

    store_url is read from config.json's "layer2.store_url"; if that's
    blank, falls back to the SHOPIFY_STORE_URL env var (.env), keeping the
    old behavior for anyone who hasn't migrated to config.json yet.

    Returns:
        Final GraphState with published_listings.
    """
    print("\n" + "=" * 65)
    print("  LAYER 2: Store & Listing Pipeline")
    print("=" * 65)

    cfg = get_layer_config("layer2")

    if publish_immediately is None:
        publish_immediately = cfg.get("publish_immediately", True)

    store_url = cfg.get("store_url") or os.getenv("SHOPIFY_STORE_URL", "")

    if winning_products is None and layer1_json_path:
        try:
            with open(layer1_json_path) as f:
                winning_products = json.load(f)
            print(f"✅ Loaded {len(winning_products)} products from {layer1_json_path}")
        except FileNotFoundError:
            print(f"❌ File not found: {layer1_json_path}")
            raise
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON in {layer1_json_path}: {e}")
            raise

    if not winning_products:
        raise ValueError("Provide either winning_products list or layer1_json_path")

    graph = build_graph()

    initial_state: GraphState = {
        "winning_products": winning_products,
        "store_url": store_url,
        "publish_immediately": publish_immediately,
        "listings": [],
        "priced_listings": [],
        "image_ready_listings": [],
        "published_listings": [],
        "failed_listings": [],
        "errors": [],
    }

    print(f"   Products to process:  {len(winning_products)}")
    print(f"   Store: {initial_state['store_url']}")
    print(f"   Publish immediately: {publish_immediately}\n")

    return graph.invoke(initial_state)


def run_full_automated_pipeline() -> dict:
    """
    Run Layer 1 and Layer 2 back-to-back automatically.
    Both layers pull ALL their settings from config.json — nothing to pass
    in here. This is the main entry point when running python -m layer2.pipeline

    Returns:
        Combined results from both layers
    """
    # Import Layer 1's run function and constants
    from layer1.pipeline import run_layer1, REGION_KEYS

    print("\n" + "=" * 65)
    print("  🚀 FULL AUTOMATED PIPELINE: Layer 1 → Layer 2")
    print("=" * 65)

    # ── PHASE 1: Layer 1 ──────────────────────────────────────────────────
    print("\n📊 PHASE 1: Market Intelligence")
    print("-" * 65)

    try:

        l1_state = run_layer1()

        # Extract winning products
        winning_products = l1_state.get("winning_products", [])

        if not winning_products:
            print("❌ Layer 1 found no winning products. Stopping.")
            return {"layer1": l1_state, "layer2": None}

        print(f"\n✅ Layer 1 complete: {len(winning_products)} winning products found")

        # Convert Pydantic models to dicts if needed
        if winning_products and hasattr(winning_products[0], 'model_dump'):
            winning_products = [p.model_dump() for p in winning_products]

    except Exception as e:
        print(f"❌ Layer 1 failed with error: {e}")
        raise

    # ── PHASE 2: Layer 2 ──────────────────────────────────────────────────
    print("\n📦 PHASE 2: Store & Listing")
    print("-" * 65)

    try:
        l2_state = run_layer2(winning_products=winning_products)

        print("\n" + "=" * 65)
        print("  ✅ FULL PIPELINE COMPLETE!")
        print("=" * 65)
        print(f"   Layer 1 - Products found: {len(winning_products)}")
        print(f"   Layer 2 - Published: {len(l2_state.get('published_listings', []))}")
        print(f"   Layer 2 - Failed: {len(l2_state.get('failed_listings', []))}")
        print("=" * 65 + "\n")

    except Exception as e:
        print(f"❌ Layer 2 failed with error: {e}")
        raise

    return {
        "layer1": l1_state,
        "layer2": l2_state
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Check if user passed a specific Layer 1 JSON file
    if len(sys.argv) > 1:
        json_path = sys.argv[1]
        print(f"📂 Processing specific Layer 1 output: {json_path}")
        run_layer2(layer1_json_path=json_path)
    else:
        # Default: Run full automated pipeline using config.json for both
        # layers' settings.
        print("🚀 Running full automated pipeline (Layer 1 + Layer 2)...")
        print("   Settings read from config.json (layer1 + layer2 sections)")
        print("   (To process existing JSON file: python -m layer2.pipeline path/to/file.json)")
        print()

        run_full_automated_pipeline()