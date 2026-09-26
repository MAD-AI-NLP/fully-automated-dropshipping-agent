# API & Tool Integrations

Every external integration actually present in Layers 1 and 2, and how each
is implemented.

## Google Trends, TikTok, Instagram (via Apify)

**File:** `layer1/tools/apify_trends.py`

- **Purpose:** source trend signal for TrendScout without relying on
  unofficial scraping.
- **Authentication:** Apify API token, read from `APIFY_API_TOKEN` with
  optional numbered fallbacks (`APIFY_API_TOKEN_1/2/3`), tried in order via
  `find_working_apify_token`.
- **Request/response flow:** each platform is a separate Apify actor run;
  results are normalized to a common `(keyword, score)` shape before being
  merged upstream in `TrendScout`.
- **Fallback behavior:** if one platform's actor call fails, the other two
  still proceed — trend data from any single source is treated as
  optional, not required.
- **Rate-limit/cost considerations:** per-country result caching (7 days
  for social trends, 3 days for Google Trends) exists specifically to
  limit Apify actor runs, which are billed per use.
- **Security considerations:** no PII is involved; the API token is read
  from environment variables only.

## CJ Dropshipping API

**File:** `layer1/tools/cj_api.py`, also used by `layer2/agents/image_agent.py`
(video resolution) and indirectly by pricing (`_calculate_retail_price`).

- **Purpose:** supplier product search, product detail, variant, and video
  data — this is the actual product catalog the whole system sources from.
- **Authentication:** email + API key (`CJ_EMAIL`, `CJ_API_KEY`) exchanged
  for a bearer token, cached for 23 hours to avoid re-authenticating on
  every call.
- **Request/response flow:** a shared, pooled `requests.Session` is reused
  across all calls — added specifically to fix a documented production
  issue where every product request was intermittently hitting a
  connection timeout under the default per-call connection behavior.
- **Error handling / rate limits:** retries distinguish network errors
  (2s/4s/8s backoff) from HTTP 429/5xx responses (5s/10s/20s backoff,
  honoring a `Retry-After` header when CJ provides one).
- **Fallback behavior:** none at the API-call level beyond retries — a
  keyword that exhausts retries is logged and skipped, and the run
  continues with the remaining keywords.
- **Notable detail:** CJ's video CDN requires a specific `Referer` header
  to serve content; `download_video` sets this explicitly, and unlike
  images, a failed video download has no URL fallback (see
  [layer2.md](layer2.md#node-4--imageagent)).
- **Security considerations:** credentials are environment variables only;
  no product or customer PII is handled by this integration (it is a
  supplier catalog API, not an order or customer API).

## Shopify Admin API (REST + GraphQL)

**File:** `layer2/agents/store_publisher.py`

- **Purpose:** create products, manage variants/options, attach media, and
  assign collections on the live store.
- **Authentication:** `SHOPIFY_STORE_URL` + `SHOPIFY_ACCESS_TOKEN`
  (custom-app access token).
- **REST usage:** product creation (title, description, tags, price,
  variants, options, images), collection lookup and assignment.
- **GraphQL usage:** video attachment specifically, since the REST Product
  API cannot carry video media. Done in three steps: `stagedUploadsCreate`
  (get a signed upload URL) → a multipart upload directly to that URL →
  `productCreateMedia` (attach the uploaded asset to the product), followed
  by `productReorderMedia` to move video to the front of the gallery.
- **Error handling:** the product-creation POST retries only on a
  connection that never succeeded — not on a read-timeout, since a
  timeout after the request was sent could mean Shopify already created
  the product, and a blind retry risks a duplicate. Video attachment
  failures are treated as non-fatal to the overall product publish.
- **Rate-limit considerations:** not explicitly rate-limit-aware in the
  code I reviewed beyond the general retry logic above — worth flagging
  as a improvement area if publish volume scales up (see
  [Future Improvements](../README.md#future-improvements)).
- **Security considerations:** the access token is a full store-admin
  credential; kept in environment variables only, never in `config.json`.

## SerpAPI (optional — competitor pricing)

**File:** `layer2/agents/price_optimizer.py`

- **Purpose:** an optional Google Shopping price lookup used to slightly
  adjust retail price relative to a competitor's listed price.
- **Authentication:** `SERPAPI_KEY`.
- **Fallback behavior:** if the key is unset, the check is silently
  skipped — pricing proceeds entirely from the cost-tiered/charm-pricing
  logic with no competitor adjustment. This is a genuine optional
  enhancement, not a required dependency.
- **Security considerations:** no PII; API key in environment variables.

## OpenAI API

Used by all four LLM calls documented in
[`llm-engineering.md`](llm-engineering.md). Authentication via
`OPENAI_API_KEY`; no other OpenAI-specific integration concerns beyond what's
covered there.
