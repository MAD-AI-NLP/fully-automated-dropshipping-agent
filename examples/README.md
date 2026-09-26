#  Fully Automated Dropshipping Agent

A 100% automated dropshipping business built with LangGraph, Claude/GPT-4o-mini,
Shopify, and CJ Dropshipping. Five agent layers handle everything from product
discovery to customer service — fully hands-free.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     PRIMEPICK AGENT                         │
├─────────────┬───────────────────────────────────────────────┤
│  Layer 1    │  Market Intelligence                          │
│             │  Google Trends → CJ Dropshipping → Scorer    │
├─────────────┼───────────────────────────────────────────────┤
│  Layer 2    │  Store & Listing                              │
│             │  SEO Copy → Price Optimizer → Shopify Publish │
├─────────────┼───────────────────────────────────────────────┤
│  Layer 3    │  Order Router & Fulfillment                   │
│             │  Webhook → CJ Order → Tracking → Fulfill      │
├─────────────┼───────────────────────────────────────────────┤
│  Layer 4    │  Customer Service                             │
│             │  Ticket Classifier → AI Reply → Email Send    │
├─────────────┼───────────────────────────────────────────────┤
│  Layer 5    │  Pricing & Analytics                          │
│             │  Dynamic Pricing → KPIs → Ad Copy → Alerts   │
└─────────────┴───────────────────────────────────────────────┘
```

---

## Project Structure

```
dropship_agent/
├── .env                          ← credentials (never commit this)
├── .env.example                  ← template
├── requirements.txt
├── README.md
│
├── layer1/                       ← Market Intelligence
│   ├── pipeline.py               ← entry point: python -m layer1.pipeline
│   ├── state.py                  ← Product + GraphState schemas
│   ├── agents/
│   │   └── nodes.py              ← TrendScout, SupplierFinder, Scorer
│   └── tools/
│       ├── google_trends.py      ← pytrends wrapper + fallback keywords
│       └── cj_api.py             ← CJ Dropshipping API wrapper
│
├── layer2/                       ← Store & Listing
│   ├── pipeline.py               ← entry point: python -m layer2.pipeline
│   ├── state.py                  ← ShopifyListing + GraphState schemas
│   ├── agents/
│   │   ├── listing_generator.py  ← SEO copywriting via GPT-4o-mini
│   │   ├── price_optimizer.py    ← margin rules + charm pricing
│   │   ├── image_agent.py        ← background removal + resize
│   │   └── store_publisher.py    ← Shopify Admin API publisher
│   └── output/
│       └── published_pids.json   ← deduplication registry (auto-generated)
│
├── layer3/                       ← Order Router & Fulfillment
│   ├── pipeline.py               ← process_order(webhook_payload)
│   ├── scheduler.py              ← ALL background jobs (L3 + L4 + L5)
│   ├── state.py                  ← ShopifyOrder, CJOrder, GraphState
│   ├── agents/
│   │   └── nodes.py              ← Validator, Placer, Tracker, Fulfiller, Exception
│   ├── tools/
│   │   ├── cj_orders.py          ← place_order, get_order_status
│   │   └── shopify_orders.py     ← fulfill_order, cancel_order
│   ├── webhooks/
│   │   └── server.py             ← FastAPI webhook server (ALL webhooks)
│   └── output/
│       └── pending_orders.json   ← orders awaiting tracking (auto-generated)
│
├── layer4/                       ← Customer Service
│   ├── pipeline.py               ← process_ticket(), process_abandoned_carts()
│   ├── state.py                  ← CustomerTicket, TicketResponse, GraphState
│   ├── agents/
│   │   └── nodes.py              ← Classifier, Responder, EmailSender, CartAgent
│   └── tools/
│       ├── email_tool.py         ← Gmail SMTP sender + templates
│       └── shopify_cs.py         ← order lookup, abandoned checkouts
│
└── layer5/                       ← Pricing & Analytics
    ├── pipeline.py               ← run_layer5(trigger)
    ├── state.py                  ← ProductMetrics, StoreKPIs, AdCopy
    ├── agents/
    │   └── nodes.py              ← MetricsCollector, Pricer, Reporter, Ads, Alerts
    ├── tools/
    │   ├── shopify_analytics.py  ← orders + products data fetching
    │   └── alerts.py             ← email alerts + daily report
    └── output/                   ← JSON reports (auto-generated)
```

---

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Mac/Linux

pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# OpenAI
OPENAI_API_KEY=sk-...

# CJ Dropshipping
# Get from: https://app.cjdropshipping.com → Settings → API Config
CJ_EMAIL=your@email.com
CJ_API_KEY=your_cj_api_key

# Shopify
# Get from: Shopify Admin → Settings → Apps → Develop apps → Install → Reveal token
SHOPIFY_STORE_URL=your-store.myshopify.com
SHOPIFY_API_KEY=your_client_id
SHOPIFY_API_SECRET=shpss_xxxxx
SHOPIFY_ACCESS_TOKEN=shpat_xxxxx

# Gmail (for customer emails + alerts)
# Get from: Google Account → Security → App Passwords
GMAIL_ADDRESS=your@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

# Optional — competitor price lookup
# SERPAPI_KEY=your_serpapi_key
```

---

## Running the System

### Layer 1 + 2 — Product Discovery & Publishing

```bash
# Run Layer 1 only (find products, save JSON)
python -m layer1.pipeline

# Run Layer 2 only (publish from a saved Layer 1 file)
python -m layer2.pipeline layer1/output/winning_products_TIMESTAMP.json

# Run Layer 1 + 2 together (full product pipeline)
python -m layer2.pipeline
```

### Layer 3, 4, 5 — Order Management & Automation

Requires 3 terminals running simultaneously:

```bash
# Terminal 1 — Webhook server (receives ALL Shopify events)
uvicorn layer3.webhooks.server:app --host 0.0.0.0 --port 8000

# Terminal 2 — Scheduler (background jobs every 6h/24h)
python -m layer3.scheduler

# Terminal 3 — ngrok tunnel (exposes local server to Shopify)
ngrok http 8000
```

### Layer 5 — Manual runs

```bash
# Full daily run (metrics + pricing + report + ads + alerts)
python -m layer5.pipeline daily

# Repricing only
python -m layer5.pipeline repricing

# Ad copy generation only
python -m layer5.pipeline ad_generation
```

---

## Shopify Webhook Setup

After starting the webhook server and ngrok, register these webhooks in:
**Shopify Admin → Settings → Notifications → Webhooks**

| Event | URL | Layer |
|---|---|---|
| Order creation | `https://YOUR-NGROK/webhooks/orders/create` | Layer 3 |
| Order cancellation | `https://YOUR-NGROK/webhooks/orders/cancelled` | Layer 3 |
| Checkout abandonment | `https://YOUR-NGROK/webhooks/checkouts/abandoned` | Layer 4 |

For customer support emails, forward emails to:
```
POST https://YOUR-NGROK/webhooks/customers/email
Body: {email, name, subject, body, order_number}
```

---

## How Each Layer Works

### Layer 1 — Market Intelligence
```
Google Trends (fallback: curated keyword list)
        ↓
TrendScout      → discovers trending search keywords
        ↓
SupplierFinder  → searches CJ Dropshipping API for matching products
        ↓
Scorer          → weighted score (trend 30% + margin 25% + orders 20%
                  + shipping 15% + rating 10%)
        ↓
LLM Filter      → GPT-4o-mini eliminates counterfeits, off-niche,
                  dangerous items + assigns Shopify collection
        ↓
Output: layer1/output/winning_products_TIMESTAMP.json
```

### Layer 2 — Store & Listing
```
DuplicateFilter  → checks registry + Shopify API (90d TTL)
        ↓
ListingGenerator → GPT-4o-mini writes SEO title, HTML description,
                   5 bullet points, 8-12 tags, meta title + description
        ↓
PriceOptimizer  → target 60% margin, charm pricing (.99),
                  compare_at price, optional competitor check
        ↓
ImageAgent      → downloads all CJ images, removes background (rembg),
                  adds white bg, resizes to 2048×2048
        ↓
StorePublisher  → creates Shopify product, uploads images,
                  sets prices, assigns collection, publishes immediately
        ↓
Registry        → saves cj_pid to published_pids.json (prevents duplicates)
```

### Layer 3 — Order Router & Fulfillment
```
Shopify webhook (new order)
        ↓
OrderValidator      → parses order, fetches CJ pid from metafields
        ↓
CJOrderPlacer       → places order on CJ Dropshipping API
        ↓
FulfillmentTracker  → polls CJ for tracking number (3 attempts)
        ↓
ShopifyFulfiller    → marks order fulfilled + sends tracking email
        ↓
ExceptionHandler    → auto-refund if cancelled, flag if delayed >20d
        ↓
Scheduler (every 6h) → retries tracking for pending orders
```

### Layer 4 — Customer Service
```
Customer email / contact form
        ↓
TicketClassifier  → GPT-4o-mini detects intent (tracking/refund/question),
                    sentiment, priority, language
        ↓
TicketResponder   → fetches order context from Shopify + Layer 3 tracking,
                    generates personalised reply
        ↓
EmailSender       → sends via Gmail SMTP
                    urgent tickets → escalation email to admin
        ↓
Scheduler (every 6h) → scans abandoned checkouts, sends recovery emails
```

### Layer 5 — Pricing & Analytics
```
Scheduler (every 24h)
        ↓
MetricsCollector  → fetches all products + 30d orders, computes
                    per-product revenue, profit, units sold
        ↓
DynamicPricer     → raises price if margin < 35%
                    +5% for best sellers, -10% for slow movers
                    max 20% change per cycle, charm pricing
        ↓
AnalyticsReporter → store KPIs: revenue, profit, avg margin,
                    best sellers, underperformers
                    saves JSON report to layer5/output/
        ↓
AdOptimizer       → GPT-4o-mini generates Facebook, Google,
                    Instagram ad copy for top 5 products
        ↓
AlertSystem       → critical alert if selling at loss
                    warning if margin < 35%
                    daily report email to admin
```

---

## Scheduler Cycles

The scheduler (`layer3/scheduler.py`) runs everything automatically:

| Frequency | Job | Layer |
|---|---|---|
| Every 6h | CJ tracking updates | 3 |
| Every 6h | Abandoned cart recovery emails | 4 |
| Every 24h | Dynamic repricing | 5 |
| Every 24h | Analytics report + alerts | 5 |

---

## Collections

Products are automatically assigned to one of four collections:

| Collection | Products |
|---|---|
| PrimePick \| Skincare Essentials | Cleansers, serums, moisturizers, masks, toners |
| PrimePick \| Beauty Tools | Gua sha, jade roller, LED mask, cleansing brush |
| PrimePick \| Accessories | Makeup brushes, bags, mirrors, organizers |
| PrimePick \| Hair & Self-Care | Hair clips, oils, silk pillowcase, bath bombs |

---

## Deduplication

Products are never published twice thanks to a three-layer check:

```
1. Local registry (layer2/output/published_pids.json) — fast lookup
2. TTL check — entries older than 90 days are eligible for refresh
3. Shopify API verification — if product was manually deleted, republish it
```

---

## Scaling

When ready to add more suppliers (AliExpress, Spocket, etc.):
1. Add a new tool in `layer1/tools/` (e.g. `aliexpress_api.py`)
2. Update `supplier_finder_node` in `layer1/agents/nodes.py` to call it
3. Everything else stays the same

When ready to move to production:
1. Deploy webhook server on Railway / Render / DigitalOcean
2. Replace ngrok with your real domain
3. Set up systemd service for the scheduler
4. Add `SERPAPI_KEY` for live competitor price checking

---

## Tech Stack

| Component | Technology |
|---|---|
| Agent orchestration | LangGraph |
| LLM | GPT-4o-mini (OpenAI) |
| Supplier API | CJ Dropshipping |
| Store | Shopify Admin REST API |
| Webhook server | FastAPI + uvicorn |
| Image processing | rembg + Pillow |
| Trend discovery | pytrends / curated fallback |
| Email | Gmail SMTP |
| Language | Python 3.11+ |
