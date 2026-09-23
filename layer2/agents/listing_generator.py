"""
Listing Generator Node
======================
Takes raw products from Layer 1 and generates:
  - SEO-optimized Shopify title
  - Full HTML product description
  - 5 bullet points (key benefits)
  - Tags (for Shopify collections + search)
  - SEO meta title + meta description

Uses GPT-4o-mini for cost efficiency (same as your MDS pipeline).
One LLM call per product with structured JSON output.
"""

import os
import json
from typing import List
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from layer2.state import GraphState, ShopifyListing
from dotenv import load_dotenv
load_dotenv()


_llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0.7,       # slightly creative for marketing copy
    openai_api_key=os.getenv("OPENAI_API_KEY"),
)

SYSTEM_PROMPT = """You are an expert Shopify copywriter and SEO specialist.
Your job is to write compelling, conversion-optimized product listings.

Rules:
- Title: 60-80 chars, include primary keyword naturally, no ALL CAPS
- Description: HTML format, 150-250 words, focus on benefits not features,
  include primary keyword 2-3 times naturally, end with a call to action
- Bullet points: exactly 5, start each with an emoji, benefit-focused
- Tags: 8-12 lowercase tags, mix of product type / use case / audience
- SEO title: 50-60 chars, keyword-first
- SEO description: 150-160 chars, include keyword, action-oriented

Return ONLY valid JSON, no markdown, no explanation.
"""

LISTING_TEMPLATE = """{
  "shopify_title": "...",
  "shopify_description": "<p>...</p>",
  "bullet_points": ["-  ...", "-  ...", "-  ...", "-  ...", "-  ..."],
  "tags": ["tag1", "tag2", ...],
  "seo_title": "...",
  "seo_description": "..."
}"""


def listing_generator_node(state: GraphState) -> dict:
    """
    Generates SEO listing content for each winning product.
    Runs LLM calls with a small batch delay to stay within rate limits.
    """
    print("\n[ListingGenerator] ✍️  Generating SEO listings...")
    errors = list(state.get("errors", []))
    winning_products = state.get("winning_products", [])

    if not winning_products:
        errors.append("ListingGenerator: No winning products received from Layer 1.")
        return {"listings": [], "errors": errors}

    listings: List[ShopifyListing] = []

    for i, product in enumerate(winning_products):
        print(f"[ListingGenerator]   [{i+1}/{len(winning_products)}] {product.get('title', 'Unknown')[:50]}...")

        try:
            listing = _generate_listing(product)
            listings.append(listing)
        except Exception as e:
            error_msg = f"ListingGenerator failed for '{product.get('title', '?')}': {e}"
            print(f"[ListingGenerator] ❌ {error_msg}")
            errors.append(error_msg)

    print(f"[ListingGenerator] ✅ Generated {len(listings)} listings")
    return {"listings": listings, "errors": errors}


def _generate_listing(product: dict) -> ShopifyListing:
    """Generate a full listing for a single product via LLM."""

    prompt = f"""Create a Shopify product listing for this item:

Product name: {product.get('title', '')}
Category: {product.get('category', 'General')}
Key trend keyword: {product.get('trend_keyword', '')}
Cost price: ${product.get('supplier_price', 0):.2f}
Shipping: ~{product.get('shipping_days', 14)} days

Return this exact JSON structure:
{LISTING_TEMPLATE}"""

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    response = _llm.invoke(messages)
    content = response.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    data = json.loads(content)

    # ── Extract tags from multiple sources ──────────────────────────────
    # Get tags from LLM-generated content
    llm_tags = data.get("tags", [])

    # Get shopify_tags from the original product (if they exist)
    shopify_tags = product.get("shopify_tags", [])

    # Combine both, preferring shopify_tags if they exist
    # This way, manual tags from the JSON are preserved
    if shopify_tags:
        final_tags = shopify_tags
        # Optionally add LLM-generated tags that aren't already present
        for tag in llm_tags:
            if tag not in final_tags:
                final_tags.append(tag)
    else:
        final_tags = llm_tags

    # ── Create the ShopifyListing with ALL fields ──────────────────────
    return ShopifyListing(
        # Source (from Layer 1)
        cj_pid=product.get("cj_pid", ""),
        supplier_price=float(product.get("supplier_price", 0)),
        suggested_retail=float(product.get("suggested_retail", 0) or 0),  # ← ADD THIS
        original_title=product.get("title", ""),
        image_urls=(
            product.get("image_urls")
            or ([product.get("image_url")] if product.get("image_url") else [])
        ),
        video_urls=product.get("video_urls") or [],
        variants=product.get("variants") or [],
        trend_keyword=product.get("trend_keyword", ""),
        category=product.get("category", ""),
        collection=product.get("collection", ""),

        # NEW: Carry over sub_collection and related fields
        sub_collection=product.get("sub_collection", ""),
        subcategory=product.get("subcategory", product.get("sub_collection", "")),

        # Generated content
        shopify_title=data.get("shopify_title", product.get("title", "")),
        shopify_description=data.get("shopify_description", ""),
        bullet_points=data.get("bullet_points", []),
        tags=final_tags,  # Combined tags
        shopify_tags=shopify_tags if shopify_tags else final_tags,  # Explicit field
        seo_title=data.get("seo_title", ""),
        seo_description=data.get("seo_description", ""),

        # NEW: Additional metadata
        cj_product_url=product.get("cj_product_url", ""),
        discovery_date=product.get("discovery_date", ""),
        source=product.get("source", ""),
        region=product.get("region", ""),
        region_label=product.get("region_label", ""),
    )