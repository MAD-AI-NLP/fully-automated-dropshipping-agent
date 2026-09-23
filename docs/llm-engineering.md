# LLM Engineering

Four LLM calls exist in the system — all GPT-4o-mini, all via
`langchain-openai`. None currently use OpenAI's function-calling/structured-
output mode; all parse a JSON response after stripping Markdown code fences.
That's documented honestly here as a current-implementation characteristic,
not glossed over — see [Limitations](../README.md#limitations) for the
improvement this implies.

| Call | Location | Temperature | Purpose |
|---|---|---|---|
| Trend category aggregation | `layer1/agents/nodes.py::_aggregate_trend_categories` | 0.2 | Collapse noisy branded/hashtag trend queries into canonical on-niche categories |
| Product elimination + collection assignment | `layer1/agents/nodes.py::scorer_node` | 0.2 | Remove rule-violating products; assign collection/sub-collection |
| Listing copy generation | `layer2/agents/listing_generator.py::_generate_listing` | 0.7 | Write SEO title/description/bullets/tags/meta |
| Variant option naming | `layer2/agents/store_publisher.py::_llm_classify_option_names` | 0 | Name ambiguous variant option columns only |

---

## 1. Trend category aggregation

**Model:** GPT-4o-mini, temperature 0.2
**Input:** a merged list of raw `(keyword, score)` pairs from Google Trends,
TikTok, and Instagram — many of them branded, long-tail, or hashtag-style.
**Output:** a JSON array, `[{"category": "...", "score": ...}]`.

**Why an LLM here:** the input is genuinely unstructured natural-language
noise (`"fjallraven kanken style mini backpack purse"`,
`"#guashaskincare"`) that needs to be reduced to a clean generic product
type — a task that's a poor fit for regex/keyword matching alone, but one
an LLM handles well when given a tight, example-driven prompt.

**Prompt structure (system message, paraphrased structure — see source for
exact wording):** defines the store's exact niche and 5 collections
up front, gives two explicit steps (extract the generic product category;
aggressively merge duplicates and sum their scores), gives worked
include/exclude examples (a branded backpack query → `"mini backpack"`; a
skincare hashtag → excluded outright), and explicitly permits returning
fewer categories than requested — or none — rather than stretching to fill
a quota with an off-niche result.

**Validation:** the response is parsed as JSON; a malformed or missing
response returns `([], [])` rather than raising, since trend data is
explicitly treated as a bonus signal, never worth failing the whole
pipeline over.

**Error handling:** 3 retries with exponential backoff (2s, 4s) on LLM
failure; if all retries are exhausted, a non-LLM regex-based fallback
(`_fallback_aggregate_trends`) produces a (rougher) result instead of
returning nothing.

**What could go wrong:** the model could over- or under-aggregate (merging
two genuinely distinct product types, or failing to merge near-duplicates),
or could hallucinate a category not actually present in the input — there's
no explicit validation that a returned category traces back to real input
keywords beyond score-plausibility.

---

## 2. Product elimination + collection assignment

**Model:** GPT-4o-mini, temperature 0.2, batched 5 products per call.
**Input:** a numbered list of candidate products (title, cost, margin,
orders, shipping time, source, trend keyword) plus the full list of valid
collections and their sub-collections.
**Output:** a JSON array, `[{"index": 1, "collection": "...", "sub_collection": "..."}]`.

**Why an LLM here:** whether a product fits the store's niche and brand
safety bar is a judgment call — the prompt lists specific disqualifying
categories (luxury counterfeits, medical claims, religious iconography,
seating furniture, industrial storage, men's-targeted items) as well as
things to explicitly *not* eliminate for (generic/unbranded products, low
order counts on new arrivals, secular symbols) — this is a nuanced
classification task, not a lookup.

**Why the LLM doesn't own every decision here:** it is deliberately scoped
to elimination and categorization only. It never sees or sets the
numeric composite score, never decides pricing, and — critically — its
elimination decision is not trusted as a final answer: see
[Deterministic backstops](layer1.md#deterministic-backstops-around-the-llm).

**Validation:**
- A returned `collection` not in the real 5-collection list is cleared
  (logged, not treated as fatal) rather than creating a phantom collection.
- A returned `sub_collection` that doesn't actually belong to the assigned
  `collection` is likewise cleared.
- Every kept product is re-checked against the men's-product and
  beauty-consumable deterministic filters *after* the LLM call, regardless
  of what the LLM decided.

**Error handling:** on an exception (bad JSON, API error, etc.) for a
batch, the fallback keeps the top 3 scorers *from that batch* by
deterministic score — but still requires them to clear the minimum score
floor and pass the men's/beauty-consumable checks. This specifically fixes
an earlier version that kept the top 3 unconditionally, which could
reintroduce low-quality products whenever the LLM call happened to fail.

**What could go wrong:** the model could be inconsistent across batches
(the same borderline product might be kept in one batch's context and
eliminated in another's) since each batch of 5 is evaluated independently
with no cross-batch memory.

---

## 3. Listing copy generation

**Model:** GPT-4o-mini, temperature 0.7 (notably higher than the other
three calls — appropriate here since this is the one genuinely creative
task in the system; the others are classification/labeling).
**Input:** product name, category, trend keyword, cost price, shipping
estimate.
**Output:** a JSON object — `shopify_title`, `shopify_description` (HTML),
`bullet_points` (exactly 5), `tags`, `seo_title`, `seo_description`.

**Prompt structure:** a fixed system prompt specifying hard constraints per
field (title 60-80 chars, description 150-250 words with the keyword
appearing 2-3 times, exactly 5 emoji-led bullet points, 8-12 lowercase
tags, SEO title/description length bounds) plus a literal JSON template the
model is shown to fill in.

**Why an LLM here:** this is the one task in the system that's genuinely
generative rather than classificatory — there's no deterministic way to
produce persuasive, SEO-appropriate marketing copy.

**Validation:** minimal beyond the JSON parse itself — there's no
programmatic check that the description actually falls in the 150-250 word
range or that exactly 5 bullets were returned; the constraints live entirely
in the prompt, not in code. This is called out here explicitly as a real
gap rather than an implemented safeguard — see
[Future Improvements](../README.md#future-improvements).

**Error handling:** per-product try/except in `listing_generator_node` — a
failure excludes that product from the batch rather than stopping it.

**What could go wrong:** the model could miss a length or count constraint
silently (nothing downstream checks tag count or bullet count), or produce
generic copy that doesn't meaningfully use the trend keyword.

---

## 4. Variant option naming

**Model:** GPT-4o-mini, temperature 0 (the only call using zero temperature
— appropriate since this is a narrow labeling task where consistency
matters more than variety).
**Input:** up to 8 sample values from each of two variant-option columns
(e.g. `["Red", "Blue", "Green"]` and `["S", "M", "L"]`).
**Output:** `{"option1_name": "...", "option2_name": "..."}`.

**Exact prompt (from source):**
> "You are labeling two columns of an e-commerce product's variant
> options... Pick a short (1-2 word) label for EACH column describing what
> it represents (e.g. 'Color', 'Size', 'Material', 'Style', 'Pack',
> 'Scent', 'Length'). The two labels must be different from each other."

**Why an LLM here:** most variant columns are classified by a fast,
deterministic content check (a fixed color-word set, a size regex, a
pack/quantity pattern) — the LLM is only invoked when that heuristic can't
confidently label a column, e.g. an unusual attribute like "Scent" or a
product-specific descriptor the heuristic doesn't recognize.

**Why this is the safest possible use of an LLM in the whole system:** the
call is deliberately constrained to return *only* two short labels — it
never sees, reformats, or has any path to alter the actual variant values
or prices. A malformed, empty, or duplicate-label response is rejected and
the function returns `None`, and callers are required to have a non-LLM
fallback ready. This makes it structurally impossible for this specific
call to corrupt real product data, even in a total failure mode.

**Validation:** rejects the response if either label is empty or if the
two labels are identical (case-insensitive).

**Error handling:** any exception (network, malformed JSON, missing API
key) returns `None`; the caller falls back to a generic label
(e.g. "Option 1" / "Option 2").
