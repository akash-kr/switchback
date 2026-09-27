## What does switchback do?

**switchback turns any URL into clean Markdown for LLMs, RAG pipelines and AI agents.** Give it a list of pages. For each one it tries the cheapest method first (a free API mirror, then plain HTTP), and it only starts a stealth browser when a site blocks the cheap attempt.

You're charged only for pages it actually delivers:

- **$1.00 per 1,000 pages** fetched over HTTP
- **$3.00 per 1,000 pages** that needed the stealth browser
- **$0 for pages that fail.** Blocked, empty or dead pages still get a row explaining why, free.

It's built on [switchback](https://github.com/akash-kr/switchback), an open-source (MIT) Python scraper. Every line of the scraping logic is public.

## Why use it?

- 💸 **Pay for results, not attempts.** A page that fails costs nothing. You never pay to find out a site is blocking you.
- 🧗 **Cheapest method first.** Most pages come back over plain HTTP at the lower price. Browser pricing applies only to pages that genuinely need a browser.
- 🧠 **Remembers each site.** The actor saves which method worked on each site to your account. On the next run it goes straight to that method and skips the attempts it knows will fail.
- 🧾 **Every row explains itself.** Each result shows which method won, how long it took, and for failures, which methods were tried and why each one failed.
- 🧹 **Clean output.** Boilerplate, navigation and ads are stripped, ready to chunk and embed.

## How to convert URLs to Markdown

1. Add your pages to **Start URLs** (paste them, upload a file, or link a text file).
2. Pick an **Output format**. `markdown` is the default.
3. Click **Start**. Results appear in the **Output** tab as each page finishes.
4. Download them as JSON, CSV or Excel, or read them over the [Apify API](https://docs.apify.com/api/v2).

## Input

```json
{
  "startUrls": [
    { "url": "https://en.wikipedia.org/wiki/Web_scraping" },
    { "url": "https://www.pcmag.com/news" }
  ],
  "outputFormat": "markdown",
  "rememberSites": true
}
```

| Field | What it does |
|---|---|
| `startUrls` | Pages to convert. Each URL is scraped on its own, cheapest method first. |
| `outputFormat` | `markdown` (default) · `markdown_trimmed` (extra ad/nav lines removed) · `html` (raw HTML as fetched) · `html_selectors` (cleaned HTML, not converted) |
| `rememberSites` | Save which method worked on each site to the `switchback-memory` key-value store in your account. Default `true`. |

## Output

One dataset row per URL, whether it succeeded or failed:

```json
{
  "url": "https://www.pcmag.com/news",
  "ok": true,
  "markdown": "# Latest Tech News\n\n…",
  "tier": "tier_2",
  "outcome": "ok",
  "errorClass": "",
  "statusCode": null,
  "latencyMs": 1455,
  "attempts": [
    { "tier": "tier_1", "outcome": "not_applicable" },
    { "tier": "tier_2", "outcome": "ok" }
  ]
}
```

A failed page looks like this, and costs nothing:

```json
{
  "url": "https://www.glassdoor.com/Career/index.htm",
  "ok": false,
  "markdown": null,
  "tier": null,
  "outcome": "all_failed",
  "errorClass": "botwall",
  "attempts": [
    { "tier": "tier_1", "outcome": "not_applicable" },
    { "tier": "tier_2", "outcome": "botwall" },
    { "tier": "tier_3", "outcome": "timeout" },
    { "tier": "tier_4", "outcome": "botwall" }
  ]
}
```

The Output tab has two views: **Overview** (every page with its Markdown) and **Why pages failed** (just the failures, with their reasons).

## How it gets through: the cascade

Each URL walks up this ladder and stops at the first method that returns real content:

| Tier | Method | You pay |
|---|---|---|
| tier_1 | Direct APIs and mirrors (arXiv, Wikipedia, Europe PMC) | $1.00 / 1,000 |
| tier_2 | Plain HTTP with browser-grade TLS fingerprints, including PDFs | $1.00 / 1,000 |
| tier_3 | Cloudflare / anti-bot challenge solver | $1.00 / 1,000 |
| tier_4 | Stealth headless Chromium | $3.00 / 1,000 |

A page only counts as a success if it passes a quality check. Cloudflare, DataDome, Akamai and PerimeterX block pages, unrendered video placeholders and navigation-only shells are all rejected, so you don't pay for a block page dressed up as content.

## Pricing

This actor uses pay-per-event pricing, so you pay only for what you get:

| Event | Price | When |
|---|---|---|
| Page via HTTP | $0.001 | a page is delivered by tier_1, tier_2 or tier_3 |
| Page via stealth browser | $0.003 | a page needed tier_4 |
| Failed page | **$0** | nothing usable came back |
| Actor start | $0.00005 | once per run (Apify's standard start fee) |

**Example:** 10,000 pages, of which 8,500 came back over HTTP, 1,000 needed the browser, and 500 were blocked:
8,500 × $0.001 + 1,000 × $0.003 + 500 × $0 = **$11.50**.

Set a **Maximum cost per run** in the run options and the actor stops cleanly before it goes over. It checks the budget before each page, so it never scrapes a page it can't bill.

## FAQ

**Can it scrape Cloudflare-protected pages?**
Often, yes. tier_3 solves many Cloudflare JavaScript challenges, and tier_4 is a stealth browser. Some sites block data-center IPs outright, and no browser trick gets past that. Those pages come back as `ok: false` with the reason, and you aren't charged.

**Why did a URL fail instantly on a second run?**
When a URL fails the same way repeatedly, switchback skips it for 24 hours instead of spending time on it again. The row says `url_excluded`. It's free, and the URL is tried again after 24 hours. Set `rememberSites` to `false` to always start fresh.

**Where is the per-site memory stored? Can I delete it?**
In a key-value store named `switchback-memory` in **your** Apify account, under Storage. Nobody else can see it. Delete it at any time to reset what the actor has learned.

**Does it handle PDFs?**
Yes. tier_2 extracts the text of PDF links.

**Can I use it with LangChain, LlamaIndex or my own code?**
Yes. Call it through the [Apify API](https://docs.apify.com/api/v2) or client libraries and read the dataset rows. The `markdown` field is ready to chunk and embed.

```python
from apify_client import ApifyClient

client = ApifyClient("<YOUR_APIFY_TOKEN>")
run = client.actor("<username>/switchback-url-to-markdown").call(
    run_input={"startUrls": [{"url": "https://en.wikipedia.org/wiki/Web_scraping"}]}
)
for row in client.dataset(run["defaultDatasetId"]).iterate_items():
    if row["ok"]:
        print(row["url"], len(row["markdown"]))
```

**Can I run it myself instead?**
Yes. It's open source: `pip install switchback`. The self-hosted library also has tiers this actor doesn't include (Camoufox, a residential-proxy browser, Firecrawl). See the [GitHub repo](https://github.com/akash-kr/switchback).

**Is web scraping legal?**
Scraping publicly available data is generally allowed, but you're responsible for following each site's terms of service, `robots.txt`, rate limits and applicable law, including personal-data rules like GDPR. This actor is for lawful collection of public pages. It isn't a tool for getting around logins, paywalls or access controls you aren't authorized to bypass.

## Feedback

Found a site it can't scrape, or want a feature? Open an issue in the **Issues** tab. I read every one. You can also report it on [GitHub](https://github.com/akash-kr/switchback/issues).
