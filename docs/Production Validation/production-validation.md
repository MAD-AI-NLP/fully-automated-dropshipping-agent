# Production Validation

This document records representative production behavior observed while running the public Layers 1 and 2 pipeline.

The purpose is to provide operational evidence for the system architecture described in the main README, while keeping private infrastructure, credentials, store information, and provider-specific operational details out of the public repository.

> **Scope:** Layers 1 and 2 only  
> **Evidence:** Sanitized production logs from real pipeline executions  
> **Privacy:** No credentials, private store identifiers, or private client data are included

---

## 1. What Was Validated

The production runs exercised the following parts of the system:

### Layer 1 — Market Intelligence

```text
Trend sources
    ↓
TrendScout
    ↓
LLM-based trend normalization/filtering
    ↓
SupplierFinder
    ↓
Product deduplication
    ↓
Scorer
    ↓
winning_products.json
```

### Layer 2 — Store & Listing

```text
winning_products.json
    ↓
DuplicateFilter
    ↓
ListingGenerator
    ↓
PriceOptimizer
    ↓
ImageAgent
    ↓
StorePublisher
```

The production logs demonstrate actual execution of the pipeline rather than only local/unit-level behavior.

---

## 2. Observed Production Behavior

### Trend collection and caching

The system was observed using cached trend data when a valid cache was available.

Examples from production execution included regional trend processing for:

- Europe
- South Korea
- Japan
- Latin America

Cached Google Trends and social-trend data were reused when available, reducing unnecessary external requests.

The logs also demonstrate fallback behavior when cached social-trend data was unavailable or stale.

---

## 3. LLM-Based Trend Filtering

TrendScout combines raw trend signals and sends them through an LLM filtering step to produce normalized product categories.

A representative successful execution produced normalized keywords such as:

```text
dress
earrings
massage gun
necklace
sneakers
wallet
jump rope
yoga mat
foam roller
shoes
```

This demonstrates the intended separation between external trend signals and the normalized product categories consumed by downstream processing.

---

## 4. External API Resilience

Production execution encountered external API failures.

One observed case involved an HTTP `429` response from the supplier API.

The system retried the request and subsequently handled the provider response without crashing the complete Layer 1 execution.

Representative behavior:

```text
External API request
        ↓
HTTP 429
        ↓
Retry
        ↓
Provider response
        ↓
No products returned
        ↓
Pipeline continues
```

This validates that external service failures are treated as operational conditions rather than automatically terminating the complete pipeline.

---

## 5. LLM Output Failure Handling

The production logs also captured an LLM JSON parsing failure.

In one run, the model returned JSON-like content containing inline comments, which caused the JSON parser to reject the response.

The system recorded the error as non-fatal and completed the Layer 1 run.

This is an important production observation because LLM output cannot be assumed to be perfectly schema-compliant.

### Observed failure

```text
LLM response
    ↓
Expected JSON
    ↓
Unexpected comments in response
    ↓
JSON parsing failure
    ↓
Error recorded
    ↓
Pipeline continues
```

### Engineering implication

This behavior identifies a concrete improvement opportunity:

- use schema-constrained/structured output where supported
- validate model responses before downstream processing
- add regression tests for malformed LLM responses
- define an explicit fallback strategy

This is documented as an observed production failure, not as a claim that the current implementation completely solves malformed LLM output.

---

## 6. Pipeline Completion

Despite non-fatal failures, the production logs show successful completion of Layer 1 runs.

Representative executions completed in approximately:

```text
~20–22 minutes
```

The observed logs include runs of approximately:

- 20:42
- 21:15
- 21:21
- 21:23
- 21:42

These timings are observations from the supplied production log and should not be interpreted as a guaranteed performance benchmark.

Runtime varies according to external API latency, cache state, number of regions, LLM calls, and other runtime conditions.

---

## 7. Output Artifact

After Layer 1 execution, the pipeline writes:

```text
layer1/output/winning_products.json
```

This file acts as the handoff artifact between Layer 1 and Layer 2.

The production logs explicitly record the creation of this output and the handoff to the publishing stage.

This creates a clear contract between the two pipeline layers:

```text
Layer 1
   │
   │ winning_products.json
   ▼
Layer 2
```

---

## 8. Production Failure Philosophy

The system distinguishes between failures that should terminate a workflow and failures that can be isolated.

Examples observed in production include:

| Failure | Observed behavior |
|---|---|
| External API rate limit | Retry |
| Provider temporarily unavailable | Continue/fail gracefully |
| LLM malformed JSON | Record non-fatal error |
| No supplier products returned | Continue with empty result |
| Cache available | Reuse cached data |
| Cache stale/missing | Attempt fresh collection |

This behavior is intentional: an individual external dependency or region should not necessarily invalidate the entire scheduled execution.

---

## 9. What These Logs Demonstrate

The production evidence supports the following engineering characteristics:

- real execution of the pipeline
- integration with external data providers
- cached-data reuse
- multi-region processing
- LLM-based normalization/filtering
- deterministic downstream processing
- external API retry handling
- non-fatal error handling
- persistent output artifacts
- observable execution timing
- graceful handling of empty supplier results

The logs should be considered **operational evidence**, not a formal benchmark.

No claim of accuracy, profitability, conversion rate, or business performance is made from these logs alone.

---

## 10. Known Limitations

The production evidence also exposed areas for future engineering work:

### Structured LLM output

The observed JSON parsing failure demonstrates that free-form model output can violate the expected format.

**Planned improvement:** migrate affected calls toward schema-constrained output and automated validation.

### Automated evaluation

The current logs provide operational evidence but are not a complete evaluation framework.

Future work should measure:

```text
LLM structured-output validity
Product filtering quality
Duplicate detection
Pipeline success rate
Retry rate
Latency
Token usage
LLM cost
External API failure rate
```

### Performance benchmarking

The observed 20–22 minute execution times are useful operational observations, but a controlled benchmark has not been performed.

A future benchmark should control:

- number of regions
- cache state
- number of products
- external API conditions
- model configuration

---

## 11. Reproducibility

The public repository does not include private production credentials, private store configuration, or proprietary production datasets.

The sanitized log included in:

```text
docs/examples/sanitized-production-run.log
```

is provided solely to demonstrate representative system behavior.

It is not intended to reproduce the original production environment exactly.

---

## 12. Evidence Policy

Only behavior directly observable in production logs is described as observed behavior.

No production metric is presented as a guaranteed system property unless it is explicitly supported by the implementation and/or measured across a defined benchmark.

This distinction is intentional:

> **Production evidence is evidence of execution — not automatically evidence of quality.**

---

## Conclusion

The production logs demonstrate that the system has been operated as a real multi-stage AI pipeline with external dependencies, LLM calls, caching, retry behavior, failure handling, and persistent outputs.

They also provide concrete evidence for future engineering improvements, particularly structured LLM output and automated evaluation.

The sanitized production log is included as a compact operational trace alongside the architecture and implementation documentation.