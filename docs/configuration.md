# Configuration

Two layers of configuration are kept deliberately separate.

## `.env` — secrets only

Never committed. See [`.env.example`](../.env.example) for the full list
with explanatory comments. Covers: OpenAI, CJ Dropshipping, Apify, Shopify,
optional SerpAPI, and deployment settings (admin trigger token, persistent
disk path).

## `config.json` — everything else

Safe to commit; contains no credentials. Loaded via `config_loader.py`,
which caches the parsed file and exposes `get_layer_config("layer1")` /
`get_layer_config("layer2")`.

### `layer1`

| Key | Purpose |
|---|---|
| `niches` | The store's product niches, used to scope trend/seed keyword generation |
| `max_trends` | Cap on aggregated global trend categories per run |
| `max_seed_keywords` | Cap on the randomized seed-keyword fallback list |
| `max_products_per_keyword` | Cap on CJ products fetched per keyword |
| `regions` | Which of the defined regions to run region-specific trend discovery for |
| `max_region_trends` | Cap on aggregated trend categories per region |
| `seen_pids_path` | Path to the cross-run product-dedup cache |
| `cj_price_multiplier` | Base retail markup multiplier (tiered further in `cj_api.py`) |
| `region_definitions` | Per-region geo code, TikTok country code, and Shopify tag — defines the 6 default regions (US, Europe, Korea, Japan, Latin America, Africa) but is fully extensible |
| `seed_categories` | Per-collection keyword seed lists used when live trend data is thin |
| `lifestyle_keywords` | Aesthetic/lifestyle search terms mixed into seed generation |

### `layer2`

| Key | Default | Purpose |
|---|---|---|
| `store_url` | *(blank — set via `.env` instead)* | Legacy/unused field; the real store URL comes from `SHOPIFY_STORE_URL` |
| `publish_immediately` | `true` | Whether StorePublisher sets new products to `active` immediately |
| `products_per_publish_run` | 15 | How many products from the current batch to publish per scheduler tick |
| `publish_frequency_hours` | 72 | Minimum time between publish batches |
| `empty_batch_retry_hours` | 6 | How soon to retry Layer 1 if a run produced an empty product batch |
| `registry_ttl_days` | 120 | How long a published product is trusted as "still live" before StorePublisher re-verifies it against Shopify |
| `pricing.min_margin_pct` | 20% | Absolute floor margin, enforced regardless of other pricing logic |
| `pricing.target_margin_pct` | 25% | Used only in the flat-margin fallback formula |
| `pricing.max_margin_pct` | 30% | Cap used only in the flat-margin fallback formula |
| `pricing.compare_at_multiplier` | 1.35× | Multiplier applied to price to produce a compare-at (strikethrough) price, when one is set at all |
| `pricing.min_absolute_profit` | $5 | Minimum dollar profit enforced alongside the percentage floor |

## Environment variable reference

See [`.env.example`](../.env.example) — every variable listed there is one
actually read by the Layer 1/2 code in this repository; nothing is included
speculatively.
