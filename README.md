# AI Radar

A daily AI-news digest built as a data pipeline. It ingests ~14 sources, deduplicates
them, scores every item against a personal interest profile with an LLM, and delivers
8–12 items to Discord each morning.

Roughly 200 items surface across those sources every day. You want to read ten. No SQL
rule can decide which ten — "is this interesting to *me*?" is not expressible as a
`WHERE` clause. That judgement is what the LLM layer is for, and everything else in the
pipeline exists to make that judgement cheap, accurate, and repeatable.

Runs on GitHub Actions. No server. Under $2/month, and $0 on the default backend.

---

## Why a pipeline and not a web app

The obvious version of this project is a feed with a nice UI. That version fails, because
the hard part isn't display — it's **source curation and relevance filtering**. A dashboard
nobody opens is worse than a Discord message read over coffee.

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
                Discord        Gmail SMTP     static HTML
               (primary)        (mirror)      → GitHub Pages
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
| Delivery | **Discord, Gmail, GitHub Pages** | Three renderers over one table. No domain, no deliverability problems, no server. |
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
| `deliver/discord.py` | Primary channel. Webhook POST, rich embeds coloured by category, 2000-char chunking. |
| `deliver/telegram.py` | Second renderer. MarkdownV2 escaping and 4096-char chunking. |
| `tests/test_discord.py`, `tests/test_telegram.py` | Guard the formatting, because a bad message means no digest rather than a loud error. |
| `settings.py` | Loads `.env` from the repo root, with real environment variables taking precedence. |
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
| Reddit (r/LocalLLaMA) | Served by the RSS resource, not a bespoke module — the public `/top/.rss` endpoint needs no credentials. |
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
| `fct_items` | Joins enrichment and computes `final_score`. Materialized as a table, not incremental: the decay term changes every row's score on every run. |
| `fct_daily_digest` | Today's top N, threshold-filtered, ledger-excluded, and capped per source and category so one busy topic cannot take the whole digest. |
| `macros/portable.sql` | `json_get` and `epoch_start`, which differ between DuckDB and BigQuery. Isolating them here keeps the models readable and the prod switch cheap. |
| `scripts/init_warehouse.py` | Creates the tables Python owns — `raw.enrichments` and `raw.sent_items` — which dbt reads but does not build. |
| `models/intermediate/_unit_tests.yml` | dbt unit tests that run the dedupe against fabricated collisions. |

The `unique` test on `int_items_dedup.url_hash` is the duplicate alarm. Fix
`canonicalize()` until it passes — never loosen the test. Every duplicate that escapes
later becomes a new case in `tests/test_normalize.py`.

**But `unique` alone is not enough.** On a warehouse that happens to contain no
cross-source duplicates it passes without exercising one line of the merge logic — which
was exactly the state this project was in when the model was written. The unit tests
supply the collisions instead: one item seen by three sources must collapse to a single
row carrying the earliest timestamp, the maximum engagement, and the highest-weighted
source's metadata.

The incremental key is `fetched_at`, not `published_at`. Filtering on `published_at`
would miss late corroboration — a paper published two days ago that reaches Hacker News
today has an unchanged `published_at`, so it would fall outside the window and never pick
up the points or the higher source count.

---

## Phase 3 — Enrichment

The LLM's job is **triage**, not summarization. It scores ~60 candidates against your
profile so you read ten. The summary is a side effect.

| Component | What it does |
|---|---|
| `config/profile.yaml` | Your interests, written honestly. Goes into the prompt verbatim and drives every score. |
| `enrich/backends/base.py` | The `EnrichmentBackend` interface both providers implement. |
| `enrich/backends/glm.py` | GLM via its Anthropic-compatible endpoint. Free tier, bounded concurrency. |
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

**Why the provider is swappable.** The choice depends on data you do not have on day one:
how often a backend returns malformed JSON, and whether its scores match your judgement.
`EnrichmentResult.failure_rate` is reported on every run. Under ~5% is fine; over ~10% and
`ENRICH_BACKEND=claude` costs about $1.20 a month, which is worth more than a weekend
debugging a flaky free tier.

GLM is the default for one specific reason: z.ai exposes an **Anthropic-compatible
endpoint**, so both backends share the same `anthropic` SDK and there are no extra
dependencies. Gemini or Groq would each mean another SDK and another response shape.

**Nothing raises for a single bad item.** A model that returns prose, markdown fences, or
a score of 99 degrades to a skipped item — never a crashed run, and never a row that
fails a dbt test downstream. Out-of-range scores are clamped rather than rejected, because
an 11 would fail `accepted_range` and take the whole build with it.

---

## Phase 4 — Ship

| Component | What it does |
|---|---|
| `scripts/run_daily.py` | Orchestrates the six steps and stops hard if the duplicate test fails. |
| `deliver/digest.py` | `build_digest()` — queries the digest once and returns structured items. Every channel consumes this; no channel writes its own SQL. |
| `warehouse.py` | One interface over DuckDB and BigQuery, so Python takes the same code path in CI as locally. |
| `raw.sent_items` | Ledger of every delivered item, excluded from future digests. |
| `.github/workflows/daily.yml` | Cron at 05:00 UTC, `workflow_dispatch` for testing, Discord alert on failure. |
| `.github/workflows/ci.yml` | Builds the entire warehouse from empty DuckDB on every push — no network, no API spend. |

**Why BigQuery is not optional.** The Actions runner is destroyed after every run, so a
DuckDB file on its disk would vanish and each day would start from an empty warehouse:
refetching everything and resending yesterday's digest. dlt stores its incremental cursor
*in the destination* (`_dlt_pipeline_state`), so a fresh runner reads the bookmark from
BigQuery and resumes exactly where the last run stopped.

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

**Slack** is planned as a fourth renderer, deliberately built last — an incoming webhook and
Block Kit formatting, once the pipeline itself is finished.

Three channels were considered and rejected:

- **Microsoft Teams** — Microsoft disabled Office 365 Connectors, the classic Teams incoming
  webhook, during 18–22 May 2026. The replacement is a Power Automate *Workflows* webhook,
  which needs a work or school M365 account, is configured as a flow rather than a copied
  URL, and posts under the generic Flow bot identity with no custom name or icon.
- **Outlook / Microsoft 365 email** — Microsoft permanently disabled Basic Auth for SMTP AUTH
  on 1 March 2026, and Outlook.com personal accounts cannot enable it at all. App passwords no
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

Pure top-N by score does not produce a digest — it produces whatever the loudest corner
of the internet did yesterday. A real run returned **9 of 12 items about one model
release, all from one subreddit**: individually well-scored, collectively useless,
because reading item 9 taught you nothing item 1 had not.

So the top-N cut is preceded by three caps, applied **in sequence**:

| Cap | Default | The failure it prevents |
|---|---|---|
| `digest_max_per_source` | 3 | One noisy feed dominating |
| `digest_max_per_topic` | 2 | One hot subject dominating, even across sources |
| `digest_max_per_category` | 4 | A busy arXiv day burying every tooling item |

The source cap alone was not enough. It cut the Qwen block from 9 items to 3, but 3 of
the 5 delivered items were still the same model release. `topic_key` normalises
`Qwen`, `Qwen3.8-27B` and `Qwen3.8-Flash-Next` to one key by taking the leading
alphabetic run of the first entity — exact matching could not group those, which is why
entity clustering looked useless at first glance.

**Order matters, and getting it wrong was a real bug.** Ranking the caps together let an
item a later cap would discard still consume an earlier cap's slot: a fourth Reddit
tooling post held the last tooling slot and pushed out the only GitHub item of the day.

**Tier-1 backfill.** On a thin day the tight caps can leave four items from a pool of
seventeen. Rather than ship four, the digest tops up from what the caps excluded, still
under a looser topic ceiling. Diversity is a preference, not an absolute — someone who
wanted four items would not have asked for ten. Every tier-0 item outranks every
backfilled one regardless of score.

All of it is dbt vars, so calibration needs no SQL edit:

```bash
uv run dbt build --profiles-dir . --vars '{digest_max_per_topic: 1}'
```

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
│   └── sources/               # rss, hackernews, github
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
uv run python scripts/init_warehouse.py   # create Python-owned tables (idempotent)
uv run python -m ingest.pipeline          # fetch feeds into ai_radar.duckdb
uv run python -m deliver.discord          # render a message (prints if no webhook set)
```

Transformations run from the `transform/` directory:

```bash
cd transform
uv run dbt deps  --profiles-dir .    # once, installs dbt_utils
uv run dbt build --profiles-dir .    # models + data tests + unit tests
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
| `DISCORD_WEBHOOK_URL` | A private Discord server you own — Channel → Integrations → Webhooks |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional. @BotFather, then `/getUpdates` for the chat ID |
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
- **Reddit's JSON API is effectively closed, but its RSS is not.** The `.json` endpoints
  return 403, and OAuth access now needs manual approval under the Responsible Builder
  Policy, which rejects most hobby projects. The public `/top/.rss?t=day` endpoint needs
  neither — and ranking by the day's top posts is a better filter than the upvote
  threshold the API would have given. It rate-limits hard, so fetch it once a day and
  send a descriptive User-Agent.
- **dlt keys its local state by pipeline name, not by destination.** Sharing one name
  across dev and prod meant a local DuckDB run's cursor was reused for BigQuery: dlt
  concluded it had already fetched everything and loaded a single row. A fresh CI runner
  has no local state and would never hit this, which is exactly why it was worth fixing —
  it misleads only during local testing, when you are deciding whether prod works.
- **Dialect splits Python can hit too.** `cast(x as varchar)` is valid DuckDB and a 400
  on BigQuery; `SELECT * EXCLUDE` is DuckDB where BigQuery wants `EXCEPT`; `r'...'` is a
  BigQuery raw string that DuckDB reads as a type name. `text_type` and `hours_ago()`
  live on the `Warehouse` class and the models use `QUALIFY`, which both engines accept.
- **Naive datetimes silently break the dlt cursor.** `_parse_date` always returns tz-aware
  UTC, using `timegm` rather than `mktime`, because feedparser's `*_parsed` struct_times are
  already UTC.

---

## Known risks

| Risk | Mitigation |
|---|---|
| Feed URLs rot | Weekly validation script; treat a 7-day silence as an alert, not a quiet week |
| Reddit closes its RSS too | Fail soft — it is one source of fourteen |
| Duplicates escape canonicalization | dbt `unique` test as a hard gate; every escape becomes a test case |
| The LLM returns malformed JSON | Try/except per item, skip on failure, log the rate — that rate decides which backend to use |
| Actions skips a scheduled run | The 26-hour window makes the next run self-healing |
| Relevance scores don't match your taste | Phase 6 calibration; the `reason` field exists for exactly this |
| **You stop reading it after three weeks** | One message. Ruthless threshold. Kill sources that never land an item. |

That last row is the one that matters. Everything above — Telegram over a dashboard, 12
items over 50, one message over a feed — exists to protect against it.
