# AI Radar

A daily AI-news digest built as a data pipeline. It ingests ~14 sources, deduplicates
them, scores every item against a personal interest profile with an LLM, and delivers
8–12 items to Telegram each morning.

Roughly 200 items surface across those sources every day. You want to read ten. No SQL
rule can decide which ten — "is this interesting to *me*?" is not expressible as a
`WHERE` clause. That judgement is what the LLM layer is for, and everything else in the
pipeline exists to make that judgement cheap, accurate, and repeatable.

Runs on GitHub Actions. No server. Under $2/month, and $0 on the default backend.

---

## Why a pipeline and not a web app

The obvious version of this project is a feed with a nice UI. That version fails, because
the hard part isn't display — it's **source curation and relevance filtering**. A dashboard
nobody opens is worse than a Telegram message read over coffee.

So: pipeline first, delivery second, UI maybe never. Every design choice below defends
against the one real risk, which is that you stop reading it after three weeks.

---

## Architecture

```
14 sources ─dlt→ raw.items ─dbt→ stg_items → int_items_dedup
                                                   │
                                    ┌──────────────┘
                                    ▼
                          enrich (GLM | Claude) → raw.enrichments
                                    │
                                    ▼
                          dbt → fct_items → fct_daily_digest
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
               Telegram        Gmail SMTP     static HTML
              (primary)         (mirror)      → GitHub Pages
                    └───────────────┴───────────────┘
                                    │
                                    ▼
                          raw.sent_items ledger

Orchestration: GitHub Actions, cron 05:00 UTC
```

---

## Stack

| Layer | Choice | Why this one |
|---|---|---|
| Ingestion | **dlt** | Handles incremental state, schema evolution and retries. Hand-rolled `requests` means more code and worse state handling. |
| Dev warehouse | **DuckDB** | One file, zero setup. Delete it and rebuild in 30 seconds, which is what keeps the dev loop fast. |
| Prod warehouse | **BigQuery** | Free tier is generous at this volume. Partitioned by date, clustered by source. |
| Transformation | **dbt-core** | The dedupe logic belongs in SQL with tests and lineage, not in Python. |
| Enrichment | **GLM or Claude Haiku 4.5** | Swappable behind one interface. GLM is free; Haiku is ~$1.20/month with the Batch API. |
| Delivery | **Telegram, Gmail, GitHub Pages** | Three renderers over one table. No domain, no deliverability problems, no server. |
| Orchestration | **GitHub Actions** | Cron, secrets and logs are all built in and free for public repos. |
| Config | **YAML** | Sources and the interest profile change often. Hardcode them and you stop editing them. |

**Deliberately absent:** Airflow, Kafka, Docker orchestration. A daily batch job with six
steps does not need a scheduler that comes with its own database. Adding one is the most
common way a project like this dies.

---

## Phase 0 — Walking skeleton

One feed into DuckDB and out to a message. No dbt, no LLM. The goal is proving the whole
loop end to end before adding a single piece of complexity.

| Component | What it does |
|---|---|
| `ingest/normalize.py` | `canonicalize()`, `url_hash()` and `strip_html()`. The most important 100 lines in the project. |
| `tests/test_normalize.py` | Pins canonicalization behaviour across arXiv variants, tracking params, ports, fragments and query ordering. |
| `ingest/sources/rss.py` | Generic RSS/Atom dlt resource, parameterised by URL. |
| `deliver/telegram.py` | MarkdownV2 escaping, 4096-char chunking, stdout fallback when no token is set. |
| `tests/test_telegram.py` | Guards the escaping, because one missed character silently drops the whole digest. |
| `.gitignore` | Hardened for `*.duckdb`, `.env` and service-account JSON before anything exists to leak. |

---

## Phase 1 — Real ingestion

All sources, fail-soft, with a verified incremental cursor.

| Component | What it does |
|---|---|
| `config/sources.yaml` | Every feed with a trust weight and category hint. Adding a source is one entry, not new code. |
| `ingest/pipeline.py` | Dispatch table from source type to resource factory, plus the fail-soft loop and `RunReport`. |
| `ingest/sources/hackernews.py` | Algolia search API. Free, no key, and carries engagement signal (points, comments). |
| `ingest/sources/github.py` | Repository search for new, fast-growing LLM/RAG/MLOps repos. |
| `ingest/sources/reddit.py` | r/LocalLLaMA via OAuth client-credentials. Unauthenticated Reddit returns 403. |
| `scripts/validate_feeds.py` | Fetches every configured feed and reports entry counts. Run weekly; feed URLs rot. |

**Fail-soft is the central rule.** One dead feed must never cost you the digest. Failures
are collected and surfaced in the message footer rather than aborting the run.

---

## Phase 2 — Transformation

Where duplicates die. This is the real work of the project.

| Model | What it does |
|---|---|
| `stg_items` | Normalises timestamps, trims titles, guards against future dates and epoch-0 parse bugs. |
| `int_items_dedup` | One row per canonical URL. Keeps the earliest sighting but the maximum engagement, so a paper that hits arXiv then Hacker News gets the arXiv timestamp and the HN points. |
| `fct_items` | Joins enrichment and computes `final_score`. |
| `fct_daily_digest` | Today's top N, one row per item, filtered by threshold and excluded against the sent-items ledger. |

The `unique` test on `int_items_dedup.url_hash` is the duplicate alarm. It will fail the
first time. Fix `canonicalize()` until it passes — never loosen the test. Every duplicate
that escapes later becomes a new case in `tests/test_normalize.py`.

---

## Phase 3 — Enrichment

The LLM's job is **triage**, not summarization. It scores ~60 candidates against your
profile so you read ten. The summary is a side effect.

| Component | What it does |
|---|---|
| `config/profile.yaml` | Your interests, written honestly. Goes into the prompt verbatim and drives every score. |
| `enrich/backends/base.py` | The `EnrichmentBackend` interface both providers implement. |
| `enrich/backends/glm.py` | GLM via its Anthropic-compatible endpoint. Free, bounded concurrency. |
| `enrich/backends/claude.py` | Claude Haiku 4.5 via the Batch API — 50% off, results keyed by `custom_id`. |
| `enrich/prompts.py` | The triage prompt and its JSON contract. |
| `enrich/run.py` | Selects unenriched items in the window, enriches, writes back to `raw.enrichments`. |

Each item returns `summary`, `category`, `entities`, `relevance_score` and `reason`.

Scoring is **relevance to you**, not global importance: a major funding round scores 2, a
small dbt-adjacent tooling release scores 8. The `reason` field costs almost nothing and is
how you debug bad rankings — when junk appears in the digest, read the reasons and you will
usually find your profile was vague, not that the model misjudged.

Enrichment writes to a separate table joined on `url_hash`, so prompts can be rewritten and
re-run without touching raw data. It runs *after* dedupe, so you never pay three times to
score the same paper that appeared on arXiv, HN and Reddit.

---

## Phase 4 — Ship

| Component | What it does |
|---|---|
| `scripts/run_daily.py` | Orchestrates the six steps in order and stops hard if the duplicate test fails. |
| `deliver/digest.py` | `build_digest()` — queries the digest table once and returns structured items. Every channel consumes this; no channel writes its own SQL. |
| `raw.sent_items` | Ledger of every delivered item, excluded from future digests. |
| BigQuery target | Partitioned on `DATE(published_at)`, clustered on `source_name`. |
| `.github/workflows/daily.yml` | Cron at 05:00 UTC, `workflow_dispatch` for testing, Telegram alert on failure. |

The run sequence:

```
1. ingest.pipeline.run()                   fail soft per source
2. dbt run --select staging intermediate
3. dbt test --select int_items_dedup       HARD FAIL on duplicates
4. enrich.run.main()
5. dbt run --select marts
6. deliver.telegram.send() + ledger write
```

Step 3 failing hard is intentional. A duplicate bug should stop the pipeline, not ship a
broken digest.

Two things about Actions cron: it is **UTC only and ignores daylight saving**, and it is
**not punctual** — scheduled runs routinely fire 5–20 minutes late. Both are fine for a
digest. The 26-hour lookback window means a skipped run self-heals the next day.

**Quiet days still send.** If fewer than three items clear the threshold, the message goes
out anyway with the count and the top sub-threshold items. Silence is indistinguishable
from a crashed job; "quiet day, 2 items" is information.

---

## Phase 5 — Email and web archive

Both channels reuse `build_digest()`. Neither touches the pipeline.

| Component | What it does |
|---|---|
| `deliver/email_smtp.py` | Gmail over SMTP with an app password. Multipart plain-text and HTML. |
| `deliver/site.py` | Renders `index.html` plus one page per day from `fct_items`. |
| `deliver/templates/` | Shared Jinja templates, so the email and the site never drift apart. |
| `gh-pages` branch | Published by the daily workflow. Never sleeps, no credentials, permanent URL. |

Two channels were considered and rejected:

- **Outlook / Microsoft 365** — Microsoft permanently disabled Basic Auth for SMTP AUTH on
  1 March 2026, and Outlook.com personal accounts cannot enable it at all. App passwords no
  longer work there. OAuth2 only, which is far too much machinery for a cron job.
- **Streamlit Community Cloud** — free-tier apps sleep after 12 hours without traffic. A
  once-daily digest site would be asleep almost every time anyone opened it, and it would
  need a second copy of the GCP key in Streamlit's secret store. Streamlit becomes the right
  answer later, when interactive search over the archive needs a running server.

The site is deliberately static: the build has the credentials, the published page must not.

---

## Phase 6 — Calibration

No new code. This is the step everyone skips and the one that decides whether you are still
reading the digest in month three.

| Activity | Why it matters |
|---|---|
| Log which items you actually click | The only ground truth about whether ranking works. |
| Retune `profile.yaml` | Vague profiles produce vague scores. This is the highest-leverage file. |
| Retune source weights and the decay constant | Too much decay and you miss a good paper posted at 6pm; too little and stale items crowd out today. |
| Add the category diversity cap | Stops a busy arXiv day producing 12 papers and no tooling news. |
| Prune sources that never land an item | Every dead source is noise in the footer and cost in the run. |

---

## Ranking

`final_score` combines four factors:

| Factor | Range | Purpose |
|---|---|---|
| `relevance_score` | 0–10 | The LLM's judgement against your profile. The main signal. |
| `source_weight` | 0.6–1.5 | Trust prior. A lab's release blog outranks an aggregator. |
| `corroboration` | 1.0–1.3 | Independent sources surfacing the same item is evidence. |
| Recency decay | e^(−0.30·days) | Keeps the digest about today. Roughly −26% per day. |

Source weight is denormalised onto every row on purpose, so changing a weight later does
not rewrite history.

---

## Repo layout

```
ai-radar/
├── IMPLEMENTATION.md          # the build plan of record
├── config/
│   ├── sources.yaml           # feeds, weights, category hints
│   └── profile.yaml           # interest profile — drives all ranking
├── ingest/
│   ├── normalize.py           # canonicalize() + url_hash()
│   ├── pipeline.py            # dispatch table, fail-soft loop
│   └── sources/               # rss, hackernews, github, reddit
├── enrich/
│   ├── backends/              # base, glm, claude
│   ├── prompts.py
│   └── run.py
├── deliver/
│   ├── digest.py              # shared by every channel
│   ├── telegram.py
│   ├── email_smtp.py
│   └── site.py
├── transform/                 # dbt project, duckdb dev + bigquery prod
│   └── models/{staging,intermediate,marts}/
├── scripts/{run_daily,validate_feeds}.py
├── tests/
└── .github/workflows/
```

---

## Running it

```bash
uv sync                              # install dependencies
uv run pytest                        # run the test suite
uv run python -m ingest.pipeline     # fetch feeds into ai_radar.duckdb
uv run python -m deliver.telegram    # render a message (prints if no token set)
```

Inspect what landed:

```bash
duckdb ai_radar.duckdb -c "select source_name, count(*) from raw.items group by 1"
```

The warehouse is one gitignored file. Delete it and re-run to rebuild from scratch.

---

## Configuration

`config/sources.yaml` defines every feed:

```yaml
- name: simon_willison
  type: rss
  url: https://simonwillison.net/atom/everything/
  weight: 1.3
  category_hint: tooling
```

`weight` multiplies the LLM relevance score, so a high-signal, low-volume source outranks a
noisy, high-volume one. `type` selects the resource factory; unknown types are skipped with a
warning rather than failing the run, so config can grow ahead of code.

---

## Secrets

Copy `.env.example` to `.env`. Nothing here is required to run ingestion — missing
credentials degrade gracefully rather than crashing.

| Secret | Where it comes from |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | @BotFather, then `/getUpdates` for the chat ID |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | reddit.com/prefs/apps, "script" app |
| `GLM_API_KEY` | z.ai |
| `ANTHROPIC_API_KEY` | console.anthropic.com, only if using the Claude backend |
| `GCP_SA_KEY` | Service account JSON — BigQuery Data Editor + Job User |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | Google account with 2FA on, then App Passwords |

Never commit `.env`, the DuckDB file, or the service-account JSON. All three are gitignored.

---

## Testing

```bash
uv run pytest
```

The suite covers URL canonicalization and Telegram escaping — the two places where a silent
bug costs you the digest rather than raising an error.

Verifying the incremental cursor is manual and worth doing by hand, because this is where
generated code looks correct and quietly isn't:

```bash
uv run python -m ingest.pipeline   # note the row count
uv run python -m ingest.pipeline   # should add ~0 rows
```

---

## Things that will surprise you

- **arXiv does not publish on weekends.** The feed returns a valid document with zero items
  and a `<skipDays>Saturday,Sunday</skipDays>` declaration. Treating an empty feed as a
  failure would mark arXiv broken two days out of every seven.
- **dlt keys incremental state by resource name.** If every feed shared one resource name,
  arXiv's few hundred daily entries would push the shared cursor forward and starve a feed
  that publishes weekly. Each feed gets its own resource name writing into one shared table.
- **Anthropic publishes no RSS feed.** Every candidate path 404s. Their announcements arrive
  via Hacker News and Simon Willison instead.
- **Unauthenticated Reddit is dead.** The `.json` endpoints return 403 even with a
  descriptive User-Agent. OAuth is now mandatory.
- **Naive datetimes silently break the dlt cursor.** `_parse_date` always returns tz-aware
  UTC, using `timegm` rather than `mktime`, because feedparser's `*_parsed` struct_times are
  already UTC.

---

## Known risks

| Risk | Mitigation |
|---|---|
| Feed URLs rot | Weekly validation script; treat a 7-day silence as an alert, not a quiet week |
| Reddit tightens access further | Fail soft — it is one source of fourteen |
| Duplicates escape canonicalization | dbt `unique` test as a hard gate; every escape becomes a test case |
| The LLM returns malformed JSON | Try/except per item, skip on failure, log the rate — that rate decides which backend to use |
| Actions skips a scheduled run | The 26-hour window makes the next run self-healing |
| Relevance scores don't match your taste | Phase 6 calibration; the `reason` field exists for exactly this |
| **You stop reading it after three weeks** | One message. Ruthless threshold. Kill sources that never land an item. |

That last row is the one that matters. Everything above — Telegram over a dashboard, 12
items over 50, one message over a feed — exists to protect against it.
