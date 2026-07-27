# Local Business Enrichment Pipeline

Scrape local businesses from Google Maps, then find the **owner's name and email** through a four-source waterfall — falling through to the next provider only when the previous one comes up empty.

Built for cold outbound lead lists. Pure Python standard library: **no `pip install` required.**

```
ScraperTech  ->  chain flags  ->  LocalPipe  ->  AI Ark  ->  Prospeo  ->  blank
  (scrape)       (free, local)     (primary)    (fallback)  (fallback)
```

## Measured results

Real run: **500 US general contractors across 32 cities.**

| Outcome | Rows | Share |
|---|---|---|
| Email found | **284** | 57% |
| Owner name but no email | 123 | 25% |
| Nothing found | 93 | 19% |

**Emails by source:** LocalPipe owner 114 (verified) · LocalPipe business 88 (generic, unverified) · AI Ark 73 · Prospeo 9.

The waterfall recovered **82 emails from the 298 LocalPipe missed**, cutting dead rows from 171 to 93. LocalPipe carries the bulk; the fallbacks are worth running but are not the main engine.

## Setup

```bash
git clone <this-repo>
cd local-business-enrichment-pipeline
cp .env.example .env      # then paste your own keys into .env
```

Requires **Python 3.8+**. Nothing else — no dependencies.

You need at minimum a ScraperTech key and a LocalPipe key. AI Ark and Prospeo are optional; without them stage 4 is skipped and rows stay in the "no email" tier.

## Usage

```bash
# Stage 1+2 -- scrape and flag chains  (~32 API requests)
python scrape.py
python scrape.py --reuse-raw          # rebuild from cache, ZERO API calls

# Stage 3 -- owner name + email
python enrich_localpipe.py --limit 5  # sample first, always
python enrich_localpipe.py            # full run; skips rows already done

# Stage 4 -- waterfall over whatever stage 3 missed
python enrich_waterfall.py --limit 10
python enrich_waterfall.py

# Exports -> your Downloads folder
python split_results.py               # merged file, all sources
python export_localpipe_only.py       # LocalPipe-only cut
```

**Always run the `--limit` sample first.** Stages 3 and 4 spend credits; the sample shows you the hit rate before you commit to the full list.

## Configuring your search

Edit the constants at the top of `scrape.py`:

```python
KEYWORD      = "general contractors"   # what to search
TARGET_TOTAL = 500                     # hard cap on final rows
PER_CITY     = 25                      # request ceiling per city
CITIES = [("Austin, TX", 30.2672, -97.7431), ...]   # name, lat, lng
```

⚠️ **Western longitudes must be negative.** `97.7431° W` is `-97.7431`. Getting this wrong silently scrapes the wrong hemisphere and returns plausible-looking garbage.

## Output

Two CSVs land in your Downloads folder. Both sort by tier, then location, then name, and are UTF-8-with-BOM so Excel opens them cleanly.

**`general-contractors-enriched.csv`** — 27 columns, everything merged. Key fields:

| Column | Meaning |
|---|---|
| `result_tier` | `1_EMAIL_FOUND` / `2_NAME_ONLY_NO_EMAIL` / `3_EMPTY` / `4_NOT_ENRICHED_YET` |
| `best_email` | the address to use — owner email preferred, else generic business email |
| `email_source` | `localpipe_owner` / `localpipe_business` / `aiark` / `prospeo` |
| `email_domain_match` | `no` = address is on a different domain than the website — **QA these** |
| `location_count`, `is_multi_location`, `chain_cities` | chain / franchise detection |

**`general-contractors-localpipe-stage3.csv`** — 31 columns, LocalPipe only, no waterfall data.

## How chain detection works (free, no API)

Group businesses by **root domain**, count distinct `place_id`s. Two guards matter:

1. **Group by domain, never by name.** "Turner" and "Turner Construction Company" share `turnerconstruction.com`; keying on the name splits one 7-location brand into a 5 and a 2.
2. **Blocklist shared hosts.** A business whose website is `sites.google.com` or `facebook.com` must never be grouped or looked up by that domain — you'll match employees of the *host*. This is not hypothetical: an early run returned `mikejohnston@google.com` for a Los Angeles renovation contractor.

Correctly identified Turner, DPR, Gilbane, Suffolk, JE Dunn, and Mortenson in the test run. Counts reflect **only the cities you scraped**, not nationwide totals.

## Provider gotchas (learned the hard way)

**ScraperTech** — `limit` is a ceiling, not a page size: one request returns everything Google has for that query. `offset` paging is unreliable (13% overlap with page 1), so always dedup by `place_id`. Billed per request, not per result.

**LocalPipe** — asynchronous: submit, then poll. The generic mailbox field is `business_email`, **never** `general_email`. Misses come back as the literal string `"not found"`, never null. Business emails are returned **unverified** on credit plans — run them through a verifier before sending. Re-submitting an already-enriched business hits your own cache instead of re-charging.

**AI Ark** — Cloudflare blocks Python's default User-Agent with a `403 / error code: 1010` that looks exactly like a bad API key. Send a browser User-Agent. Filtering by seniority destroys recall on small businesses (0/10 domains vs 3/10 unfiltered) — search unfiltered and rank afterward.

**Prospeo** — needs a name, so it cannot start from a bare domain; search for a `person_id` first, then enrich. Masked addresses contain `*` and are unusable. `free_enrichment: true` means no credit was charged.

**Both AI Ark and Prospeo have thin coverage of small local businesses** — roughly 30% of small US contractors had any person on file. They are LinkedIn-shaped B2B databases; sole proprietors are largely absent.

## Design notes

- **Resumable.** Every script skips work already completed, so a crash or timeout costs nothing. `scrape.py --reuse-raw` rebuilds exports from cached JSON with zero API calls.
- **Job IDs persist immediately.** Each accepted job is appended to disk the instant the API returns it. An early version wrote them only after all submissions finished — a mid-run disconnect orphaned ~495 already-accepted (and already-charged) jobs.
- **Bounded concurrency + 429 retry.** A 429 means the call was *not* accepted; without a retry that record is silently lost. Concurrency 8 is proven stable.
- **Assertions on every stage.** Row counts, duplicate keys, and tier consistency are checked before anything is written, and failures print loudly.

## Costs

| Provider | Model |
|---|---|
| ScraperTech | per request — 32 requests scraped 800 businesses |
| LocalPipe | ~2 credits per email, charged only when found |
| AI Ark | per credit, charged only when found |
| Prospeo | 1 credit per success, free on no-match |

## Before you send

- Verify the generic `info@`-style addresses — LocalPipe returns them unverified.
- Review rows where `email_domain_match` is `no`. Most are free-mail (gmail/yahoo) which is normal for small contractors; the rest are usually legitimate alternate corporate domains, but some are wrong matches.
- Dedupe by email — multi-location chains repeat the same contact across rows.
