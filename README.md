# EWAI-topic-digest

`ewai-digest` is a Python CLI that finds a trending, **actionable** AI topic for
the [European Women in AI (EWAI)](https://europeanwomeninai.org) community, does
a deep dive, and drafts a short WhatsApp message with a concrete thing a member
can try — reframed through EWAI's mission and brand voice (Purposeful, Warm,
Courageous).

## Pipeline

Each stage is its own function (in `ewai_digest.py`) so you can swap sources or
tune the weighting later, and each stage prints its output so you can watch the
run before it goes on a schedule.

1. **`fetch_insider_feeds()`** — latest content from 4 AI-industry sources:
   The Rundown AI (RSS), MIT Technology Review – AI (RSS), The Batch by
   DeepLearning.AI (scraped), and AlphaSignal (scraped, login-free page).
2. **`fetch_mainstream_signal()`** — 3–4 targeted searches via the Anthropic
   API `web_search` tool, matched to EWAI's real focus areas: AI + investing/
   finance, AI + jobs/careers, AI tools for professionals, AI regulation/policy.
3. **`cross_reference(insider, mainstream)`** — a Claude call that finds topics
   in **both** sets (strongest), **mainstream-only** (still relevant to a
   non-technical audience), and **insider-only** (deprioritized unless major
   safety/regulatory news), then picks the most *actionable* winner.
4. **`deep_dive(topic)`** — Claude + web search pull details from multiple
   sources on the winning topic.
5. **`generate_whatsapp_message(research)`** — reframes the topic through EWAI's
   mission and voice: topic → why it matters → one concrete action step, short
   enough for WhatsApp.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # or run `ant auth login`
```

## Run

```bash
python3 ewai_digest.py
```

The pipeline prints each stage as it goes and ends with the final WhatsApp
message.

## Configuration

Everything tunable lives in the config block near the top of `ewai_digest.py`:

- `RSS_FEEDS` / `SCRAPE_SOURCES` — swap or add insider sources.
- `MAINSTREAM_QUERIES` — the focus-area searches for stage 2.
- `SELECTION_WEIGHTING` — the rules stage 3 uses to pick a topic.
- `EWAI_MISSION` / `EWAI_VOICE` — the framing injected into every Claude prompt.
- `MODEL` — defaults to `claude-sonnet-5` (good quality, ~half the price of
  Opus). Override with the `EWAI_MODEL` env var — the tool automatically picks
  the right web-search tool version and thinking config for the model you set:
  - higher quality: `export EWAI_MODEL=claude-opus-4-8`
  - cheapest: `export EWAI_MODEL=claude-haiku-4-5`

## Cost & billing

Claude API usage is **billed separately from a Claude.ai Pro/Max subscription**
— a Pro plan does not include API access. You need an API key from the
[Anthropic Console](https://console.anthropic.com) with a payment method or
prepurchased credits. Usage is pay-as-you-go by token count, so a single digest
run is cheap (a handful of model calls plus a few web searches), but not free.

To cap what you can be charged, set a spending limit in the Console:

1. Sign in at <https://console.anthropic.com>.
2. Open **Settings → Billing** (also look under **Limits / Usage limits**).
3. Set a **monthly spend limit** (and, if offered, usage-alert thresholds).

Once reached, the API stops serving requests for the rest of the period, so the
tool can't run up an unexpected bill. The cheaper default model above also keeps
per-run cost down.

## Notes

- RSS/Atom parsing uses the Python standard library; scraping is intentionally
  generic (headlines/links) so it degrades gracefully when those sites change
  their markup. If a source can't be reached the run logs it and continues.
- Network egress must reach the source sites and `api.anthropic.com`. Some
  sandboxed environments restrict outbound hosts — run it where those sites are
  reachable.
