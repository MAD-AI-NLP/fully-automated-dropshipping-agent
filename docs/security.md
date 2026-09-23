# Security

## Credential handling

- All API keys and access tokens live in environment variables only
  (`.env`, gitignored). `config.json` contains no credentials and is safe
  to commit.
- `.gitignore` excludes `.env` and any `.env.*` variant, while explicitly
  allowing `.env.example` through.
- The Shopify access token is a full store-admin credential — treated with
  the same care as the OpenAI/CJ/Apify keys, kept out of `config.json` and
  out of logs.

## Production access control

- `server.py` exposes a manual `/trigger` endpoint for forcing an
  out-of-schedule pipeline run. It requires a matching bearer token
  (`ADMIN_TRIGGER_TOKEN`) in the request — an unauthenticated request is
  rejected.
- The `/status` and health-check endpoints are read-only and expose no
  credentials or customer data — only pipeline run state (last run time,
  batch size, next scheduled run).

## What this repository intentionally excludes

- **Layers 3–5** of the underlying system (order fulfillment/webhooks,
  AI-driven customer service, dynamic pricing & analytics) — built for the
  same client, but these touch live order and customer data and remain
  private.
- **Runtime state**: trend caches, the seen-product dedup cache, the
  publish registry, and any real `winning_products.json` output. These are
  gitignored and were never included in this repository — everything
  shown here is source code, not a data export.
- **The real store's brand name and Shopify domain.** Both are replaced
  throughout this repository and its documentation with a placeholder
  (`Aurelle`) — see the scope note at the top of the
  [README](../README.md).

## Sanitization performed before publishing

For transparency, this is what changed between the original client
codebase and this public repository:

- Renamed `env.example` → `.env.example` and `gitignore` → `.gitignore`
  (their original filenames were missing the leading dot, which meant git
  would not have actually respected them).
- Removed a stray accidental text fragment from the original
  `env.example`.
- Rebuilt `.env.example` to include only variables Layers 1–2 actually
  read — dropped several unrelated variables (a secondary LLM provider key,
  an email/app-password pair, extra Shopify app credentials) that belong to
  the private Layers 3–5 and would otherwise have implied functionality
  that isn't in this repository.
- Trimmed `requirements.txt` to dependencies actually imported by the code
  in this repository — dropped leftover dependencies (`pytrends`, `rembg`,
  `scikit-image`, and related image-processing libraries) tied to earlier,
  since-removed implementations that the current code no longer uses.
- Replaced every occurrence of the real store's brand name and hardcoded
  Shopify `vendor` field with a placeholder (`Aurelle`).

No API keys, tokens, passwords, or other credentials from the original
codebase are present anywhere in this repository.
