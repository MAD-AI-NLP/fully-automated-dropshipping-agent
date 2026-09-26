# Layer 1 — Market Intelligence

Graph: `TrendScout → SupplierFinder → Scorer → Reporter`
(`layer1/pipeline.py`, `layer1/state.py`, `layer1/agents/nodes.py`)

## State

**`Product`** (Pydantic model) carries everything discovered/computed about a
candidate product: title, CJ product ID, category, supplier cost, retail
price, margin %, shipping days, rating, order count, trend keyword/score,
image/video/variant data, CJ source URL, the final composite `score`,
assigned `collection`/`sub_collection`, a `source` tag (`trend` / `seed` /
`new_arrival` / `region_trend`), `discovery_date`, computed `shopify_tags`,
and `region`/`region_label` for geography-specific finds.

**`GraphState`** (TypedDict) accumulates: inputs (niches, trend/region/seed
caps), intermediate keyword sets, raw and scored product lists, the final
`winning_products` list, a `report_path`, and a shared `errors` list.

## Node 1 — TrendScout

**Purpose:** produce a clean set of on-niche product categories worth
sourcing, from noisy multi-platform trend data.

**Steps:**
1. Builds a randomized, capped seed-keyword list (`build_random_seed_list`)
   as a baseline even when trend data is thin.
2. Fetches Google Trends via an Apify actor (`get_trending_keywords`).
3. Fetches TikTok and Instagram trends via two more Apify actors
   (`get_social_trending_keywords`).
4. Merges all three, then runs an LLM aggregation pass
   (`_aggregate_trend_categories`) that collapses noisy, branded, or
   hashtag-style queries into a fixed number of canonical product categories
   with summed relevance scores.
5. Repeats steps 2–4 per configured region (`_fetch_region_trends`) across
   up to 6 world-trend regions (US, Europe, Korea, Japan, Latin America,
   Africa by default — configurable via `config.json`'s
   `region_definitions`).

**Returns:** `trend_keywords`, `seed_keywords`, `trend_scores`,
`region_trend_keywords`, plus any `errors`.

**Design notes:**
- Google Trends is sourced via a paid Apify actor rather than `pytrends`.
  This was a deliberate migration — `pytrends` (an unofficial scraper) was
  hitting hard rate limits (HTTP 429) in production.
- Social trends are cached per-country for 7 days; Google Trends per-geo for
  3 days — both are real caches tied to protecting Apify usage/cost, not
  incidental.
- LLM aggregation retries up to 3 times with exponential backoff (2s → 4s),
  and falls back to a non-LLM regex-based aggregation
  (`_fallback_aggregate_trends`) if the LLM path is exhausted — the pipeline
  degrades rather than failing outright.

## Node 2 — SupplierFinder

**Purpose:** turn trend/seed keywords into real, sourceable products.

**Steps:**
1. For every keyword, searches CJ Dropshipping twice: sorted by `orders`
   (proven sellers) and by `date` (new arrivals) — tagging each result's
   `source` field accordingly (also `region_trend` for region-specific
   keywords).
2. Applies three deterministic blocklists at the product level (see
   [Deterministic backstops](#deterministic-backstops-around-the-llm)
   below).
3. Deduplicates against a cross-run cache (`seen_pids.json`, capped at 5,000
   entries) so repeat runs surface fresh inventory instead of the same
   products.
4. Builds Shopify tags per product (`_build_shopify_tags`).

**Returns:** `raw_products`, plus any `errors`.

## Node 3 — Scorer

**Purpose:** rank products and cut the list down to genuinely worth-selling
candidates.

**Phase 1 — deterministic composite score** (`_compute_score`), a weighted
sum of normalized signals:

| Signal | Weight |
|---|---|
| Trend score | 30% |
| Margin % | 25% |
| Order count | 20% |
| Shipping days | 15% |
| Rating | 10% |

A source bonus is then added on top of the normalized score:

| Source | Bonus |
|---|---|
| `new_arrival` | +8 |
| `trend` | +5 |
| `region_trend` | +5 |
| `seed` | +0 |

The bonus exists so fresh, no-history products can compete against
established sellers that have accumulated orders/ratings over time.

**Phase 2 — LLM elimination + collection assignment**, batched (5 products
per call) through GPT-4o-mini: removes products violating a defined rule set
(counterfeits, medical claims, religious iconography, seating furniture,
industrial storage, men's-targeted items, off-niche categories), and assigns
each surviving product to exactly one of 5 collections (`Fashion`, `Shoes`,
`Jewelry`, `Accessories`, `Wellness`) plus a sub-collection within it. The
code guards against the LLM inventing a collection name or mismatching a
sub-collection to the wrong collection.

**Phase 3 — quality floor.** A hard-coded `MIN_WINNING_SCORE = 60.0` is
applied *after* the LLM step, in code. This was added because the LLM
elimination step only removes rule violations — it never enforced a
positive quality bar, so a batch of legitimately on-niche but low-quality
products could otherwise pass through untouched.

**Returns:** `scored_products`, `winning_products` (final ranked, deduped,
floor-filtered list), plus any `errors`.

## Node 4 — Reporter

Writes `winning_products.json` — the sole contract with Layer 2 — and prints
a console summary broken down by collection, source mix, and region.

## Deterministic backstops around the LLM

A consistent pattern across this entire layer: the LLM is asked to avoid
off-niche, beauty-consumable, or men's-marketed categories, but that
instruction is never trusted alone. The same three checks
(`_is_off_niche`, `_is_beauty_consumable`, `_is_mens_product`) are applied:

1. To the LLM's own trend-category output (TrendScout).
2. To every raw product from CJ, before scoring (SupplierFinder).
3. As a post-LLM safety net after collection assignment (Scorer).

This is a repeated, intentional architectural choice — "LLM proposes,
deterministic code disposes" — not a one-off check.

## Tools

### `layer1/tools/apify_trends.py`

Unifies Google Trends, TikTok, and Instagram behind one interface, entirely
via Apify actors (not raw scraping). Handles:
- Actor-schema drift (the code documents two prior breaking changes and how
  they were diagnosed).
- Multi-token fallback for the Apify API key
  (`APIFY_API_TOKEN_1/2/3` tried in order after the default).
- A shared safe-category keyword filter across all three trend sources.
- Per-country weekly caching.

### `layer1/tools/cj_api.py`

CJ Dropshipping REST client. Handles:
- Token caching (23-hour TTL).
- A shared, pooled `requests.Session` — added specifically to fix a
  documented production issue where every product request was hitting a
  connection timeout.
- Retry-with-backoff that distinguishes network errors (2s/4s/8s) from
  HTTP 429/5xx (5s/10s/20s, honoring `Retry-After` when present).
- Tiered, cost-based retail-price markup (`_calculate_retail_price`) — a
  flat multiplier underprices cheap items relative to fixed transaction
  costs, so the multiplier varies by price band.
- Variant fetching and deduplication.
- A dedicated video URL resolver, since CJ's video CDN requires a specific
  `Referer` header to serve content.
