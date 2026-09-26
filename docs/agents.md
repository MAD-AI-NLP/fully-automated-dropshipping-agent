# Agent Reference

Structured purpose/input/output/failure-mode breakdown for every node in
both graphs. For narrative explanation, see [`layer1.md`](layer1.md) and
[`layer2.md`](layer2.md).

---

## Layer 1

### TrendScout
- **Purpose:** produce clean, on-niche product categories from noisy
  multi-platform trend signals.
- **Inputs:** niches, trend/region/seed caps (`GraphState`).
- **Processing:** fetch Google Trends + TikTok + Instagram (via Apify) →
  LLM-aggregate into canonical categories → repeat per configured region.
- **Outputs:** `trend_keywords`, `seed_keywords`, `trend_scores`,
  `region_trend_keywords`.
- **Tools used:** `apify_trends.py`.
- **LLM used:** GPT-4o-mini, temperature 0.2 (category aggregation).
- **Failure cases:** Apify actor failure per source (logged, other sources
  still used); LLM aggregation failure after 3 retries falls back to a
  non-LLM regex aggregation.
- **Dependencies:** none upstream (first node).

### SupplierFinder
- **Purpose:** turn keywords into real, sourceable products.
- **Inputs:** `trend_keywords`, `seed_keywords`, `region_trend_keywords`.
- **Processing:** dual-sort CJ search (orders + date) per keyword →
  deterministic niche/brand-safety filtering → cross-run dedup.
- **Outputs:** `raw_products`.
- **Tools used:** `cj_api.py`.
- **LLM used:** none (fully deterministic).
- **Failure cases:** CJ API errors per keyword (logged, other keywords
  still processed).
- **Dependencies:** TrendScout.

### Scorer
- **Purpose:** rank and filter products down to a worth-selling shortlist.
- **Inputs:** `raw_products`.
- **Processing:** deterministic weighted composite score → LLM elimination
  + collection assignment (batched, 5/call) → hard-coded minimum-score
  floor.
- **Outputs:** `scored_products`, `winning_products`.
- **Tools used:** none.
- **LLM used:** GPT-4o-mini, temperature 0.2 (elimination + categorization).
- **Failure cases:** on an LLM batch failure, falls back to the top 3
  scorers from that batch by deterministic score — but still requires them
  to clear `MIN_WINNING_SCORE` and pass the men's/beauty-consumable
  backstops, specifically fixing an earlier version that kept the top 3
  unconditionally regardless of quality.
- **Dependencies:** SupplierFinder.

### Reporter
- **Purpose:** persist the Layer 1 → Layer 2 contract and summarize the run.
- **Inputs:** `winning_products`.
- **Processing:** write JSON, print console summary.
- **Outputs:** `winning_products.json` on disk, `report_path`.
- **Tools used:** none.
- **LLM used:** none.
- **Failure cases:** filesystem write failure (not specifically
  instrumented beyond the shared `errors` list).
- **Dependencies:** Scorer.

---

## Layer 2

### DuplicateFilter
- **Purpose:** avoid spending LLM/API cost on products already live.
- **Inputs:** `winning_products` (from `winning_products.json`).
- **Processing:** checks each product against the publishing registry
  (`_cj_pid_exists`, defined in `store_publisher.py`).
- **Outputs:** filtered product list.
- **Tools used:** publishing registry (`layer2/output/published_pids.json`).
- **LLM used:** none.
- **Failure cases:** registry read failure would affect the whole batch;
  not separately handled from other node-level errors.
- **Dependencies:** none upstream (first Layer 2 node).

### ListingGenerator
- **Purpose:** generate SEO-ready listing copy.
- **Inputs:** filtered products.
- **Processing:** one GPT-4o-mini call per product; merges LLM tags with
  Layer 1's `shopify_tags`.
- **Outputs:** `listings`.
- **Tools used:** none.
- **LLM used:** GPT-4o-mini, temperature 0.7.
- **Failure cases:** per-product try/except — failures logged and excluded,
  batch continues.
- **Dependencies:** DuplicateFilter.

### PriceOptimizer
- **Purpose:** compute retail price, compare-at price, and margin.
- **Inputs:** `listings`.
- **Processing:** cost-tiered pricing from Layer 1's `suggested_retail` →
  charm pricing → optional competitor-price adjustment → per-variant
  pricing.
- **Outputs:** `priced_listings`.
- **Tools used:** SerpAPI (optional).
- **LLM used:** none — deterministic by design.
- **Failure cases:** on error, falls back to the configured minimum margin
  rather than an arbitrary price.
- **Dependencies:** ListingGenerator.

### ImageAgent
- **Purpose:** prepare media for publishing.
- **Inputs:** `priced_listings`.
- **Processing:** download images (MD5-cached) and resolved videos
  (`Referer`-header-aware) to local disk.
- **Outputs:** `image_ready_listings`.
- **Tools used:** `cj_api.py` (video resolution).
- **LLM used:** none.
- **Failure cases:** image download failure falls back to the original CJ
  URL; video download failure has no fallback (video is simply omitted).
- **Dependencies:** PriceOptimizer.

### StorePublisher
- **Purpose:** publish the finished listing to Shopify.
- **Inputs:** `image_ready_listings`.
- **Processing:** three-layer dedup check → build REST payload (title,
  price, variants/options) → create product → attach video via GraphQL →
  assign to collection.
- **Outputs:** `published_listings`, `failed_listings`.
- **Tools used:** Shopify Admin REST API, Shopify GraphQL Admin API.
- **LLM used:** GPT-4o-mini, temperature 0 — narrowly scoped to naming
  ambiguous variant option columns only.
- **Failure cases:** connection failures retried; read-timeouts after the
  request was sent are *not* retried (duplicate-publish risk); video
  attachment failures are non-fatal to the overall product publish.
- **Dependencies:** ImageAgent.
