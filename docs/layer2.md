# Layer 2 — Store & Listing

Graph: `DuplicateFilter → ListingGenerator → PriceOptimizer → ImageAgent → StorePublisher`
(`layer2/pipeline.py`, `layer2/state.py`, `layer2/agents/`)

## State

**`ShopifyListing`** (Pydantic model): source fields carried over from
Layer 1's `Product`, LLM-generated fields (title, HTML description, bullet
points, tags, SEO title/description), pricing fields, processed-media
fields, publish-result fields, and computed properties (`all_tags`,
`margin_amount`, `margin_percentage`, `formatted_price`, `variant_count`).

**`GraphState`** (TypedDict): `winning_products` in; `listings` →
`priced_listings` → `image_ready_listings` as intermediates;
`published_listings` / `failed_listings` / `errors` out.

## Node 1 — DuplicateFilter

Filters `winning_products` against the publishing registry
(`_cj_pid_exists`, defined in `store_publisher.py` — see below)
**before** any LLM call or image download happens, so cost isn't spent on
products that are already live.

## Node 2 — ListingGenerator

One GPT-4o-mini call per product (temperature 0.7), producing structured
JSON: title, HTML description, 5 bullet points, tags, and SEO
title/description, against a prompt with explicit style constraints
(character limits, keyword placement, no ALL CAPS).

Tags are merged from the LLM's output and Layer 1's `shopify_tags`,
deduplicated, with `shopify_tags` preferred over LLM-invented tags when they
overlap.

**Failure handling:** per-product try/except — a single product's failure
is logged to `errors` and excluded from `listings`; it doesn't stop the
batch.

## Node 3 — PriceOptimizer

**Deterministic — no LLM.** Pricing is treated as arithmetic, not a
creative task.

Configuration (`config.json`'s `layer2.pricing`):

| Parameter | Default |
|---|---|
| `min_margin_pct` | 20% |
| `target_margin_pct` | 25% |
| `max_margin_pct` | 30% |
| `compare_at_multiplier` | 1.35× |
| `min_absolute_profit` | $5 |

**Pricing logic (`_calculate_price`)** prefers Layer 1's
`suggested_retail` (already tiered by cost band in `cj_api.py`) over
recomputing from a flat margin target. This fixed a real bug in an earlier
version: a flat-margin formula blew past its own 30% cap on cheap items —
a $1 item landed at a 49.7% margin. The flat-margin formula is now only a
fallback when no `suggested_retail` is present; a minimum-margin floor is
still enforced either way.

**Charm pricing (`_charm_price`)** applies `.99`-style psychological
pricing, with both a ceiling and a floor on the rounding (never drops more
than 8% below the true computed price).

**Competitor pricing** (optional): a SerpAPI Google Shopping lookup
(`_get_competitor_price`), silently skipped if `SERPAPI_KEY` is unset.
Undercuts by 5% if a competitor is cheaper; raises price slightly (capped)
if the competitor is significantly more expensive.

**Variant pricing** (`_price_variants`): every variant is priced
individually using its own CJ cost when available, else inheriting the
listing-level price — added specifically to prevent a `KeyError` further
downstream in StorePublisher for multi-variant products.

**Failure handling:** on error, still produces a listing priced at the
configured minimum margin (not an arbitrary hardcoded number), so a pricing
failure can't silently undercut the margin floor.

## Node 4 — ImageAgent

Downloads product images and any resolved videos to local disk for the
publish step. **Does not** currently do background removal, recompositing,
or resizing — an earlier `rembg`-based pipeline was removed (its
dependencies are also removed from `requirements.txt` in this repo, since
they're unused dead weight in the current implementation).

- Retries downloads up to 3 times with backoff, aware of 429/5xx responses.
- Uses a stable MD5-based cache filename — fixing a documented bug where
  Python's randomized string hashing caused unnecessary re-downloads every
  run.
- Videos are handled separately from images because CJ's video CDN requires
  a `Referer` header (`cj_api.download_video`). Unlike images — which fall
  back to the original CJ URL if a local download fails — videos have no
  such fallback, since hotlinking CJ's video URLs directly to customers
  isn't a viable production behavior.

## Node 5 — StorePublisher

The largest and most complex node (~1,300 lines). Responsible for turning a
priced, image-ready listing into a live Shopify product.

### Deduplication (`_cj_pid_exists`)

Three-layer check:
1. Not in the local registry → publish.
2. In the registry but past `registry_ttl_days` (120 days, from
   `config.json`) → eligible for a refresh.
3. In the registry and still fresh → verified against live Shopify
   (`_shopify_product_exists`). If Shopify 404s (e.g. the product was
   manually deleted), it's removed from the registry and republished.

### Product creation (`_publish_product`)

Builds the full Shopify REST payload:
- Title, description + bullet points, deduplicated tags with the
  collection name appended.
- Price / compare-at price — compare-at is intentionally left unset unless
  PriceOptimizer computed a real one, i.e. no fabricated "was" prices.
- **Variant/option derivation** — CJ supplies one combined string per
  variant (e.g. `"Badge Blue-S"`). The code:
  1. Splits on the first `-` (or ` - `) into two columns, but only commits
     to this if ≥60% of variants in the batch split cleanly — a single
     outlier previously collapsed an entire product into one option.
  2. Classifies each resulting column by its **content** (a color-word
     set, a size-word/regex pattern, or a pack/quantity pattern), not by
     position — which attribute comes first varies by product type.
  3. If the heuristic can't confidently label a column, a single
     GPT-4o-mini call (`_llm_classify_option_names`, temperature 0) names
     it — e.g. "Material" or "Scent". This call is deliberately scoped to
     *only* return two short labels; it never touches or reformats the
     actual variant values, so a bad response degrades to a generic label
     rather than corrupting variant data.
  4. Variant images already present in the general product gallery are
     reused rather than re-uploaded.

### Video attachment

REST product creation can't carry video, so videos are attached via the
GraphQL Admin API in three steps: `stagedUploadsCreate` → a multipart
upload to the returned signed URL → `productCreateMedia`, followed by
`productReorderMedia` to move the video to the front of the gallery. This
is explicitly best-effort — a failure here does not fail the overall
product publish.

### Collection assignment

Looks up the Shopify collection ID by title across both custom and smart
collections, caches the lookup, and handles the "already in collection"
422 response gracefully.

### Retry discipline

The product-creation POST only retries on a connection that never
succeeded — deliberately *not* on a read-timeout, since a timeout after the
request was sent means Shopify may have already created the product;
blindly retrying in that case risks a duplicate publish.
