# advertools-mcp

An MCP server that wraps the [advertools](https://github.com/eliasdabbas/advertools)
SEO toolkit so an AI client can run scalable, accurate SEO crawls, export
structured crawl data, and run a best-practice SEO audit as a separate
post-crawl process — without ever blocking and without dumping large files into
the model context.

It speaks **stdio** for local use and **streamable HTTP** for remote, from one
shared core implementation.

---

## Why the architecture looks like this

Two hard constraints shaped the design:

1. **Subprocess per crawl.** advertools' crawler is Scrapy on a Twisted reactor,
   and a Twisted reactor cannot be restarted inside a process. A long-lived
   server that called `adv.crawl()` in-process would crash on the *second*
   crawl with `ReactorNotRestartable`. So every crawl, header crawl, and image
   crawl runs in its **own short-lived worker subprocess**
   (`advertools_mcp/workers/crawl_worker.py`). The server process never imports
   Scrapy. The test `tests/test_sequential_crawls.py` proves two sequential
   crawls succeed in one server session.

2. **Never return raw crawl/audit files to the model.** Crawls and audits
   produce large artefacts. They are persisted under `data/crawls/`, and tools
   return a **compact summary plus an ID**. Query tools then pull only the
   slices the client asks for, reading parquet columnar slices rather than
   whole files.

```
client ──tool──▶ MCP server (always responsive)
                   │  start_crawl → register job, spawn subprocess, return job_id
                   ▼
            JobManager (asyncio, semaphore-capped)
                   │  one subprocess per crawl
                   ▼
        crawl_worker.py  ──advertools.crawl──▶  data/crawls/<job>/crawl.jl
                                               └▶ crawl.parquet (crawlytics)
                   ▲
   query/summary/analyse/export/audit tools read parquet slices only
```

## Tools

**Crawling** (async jobs — return a `job_id` immediately)
- `start_crawl` — discovery (spider) or list mode; full param surface incl.
  include/exclude regex, max_pages/depth, concurrency, delay, obey_robots,
  user_agent, CSS/XPath custom extraction, and a `custom_settings` passthrough.
- `start_header_crawl` — `crawl_headers` (HEAD only) for fast status sweeps.
- `start_image_crawl` — `crawl_images` for image discovery/metadata.
- `crawl_status`, `list_jobs`, `cancel_job` — job lifecycle.

**Results & analysis**
- `get_crawl_summary` — rows, status-code distribution, content types, depth, schema.
- `query_crawl` — paginated, filtered, column-projected rows from parquet.
- `analyse_links`, `analyse_redirects`, `analyse_images` — `crawlytics` analyses.
- `export_crawl_csv` — flat Internal/All-style CSV (one row per URL).

**robots.txt** — `parse_robots`, `test_robots`.
**XML sitemaps** — `fetch_sitemap` (recursive index, news, video; can seed a list crawl).
**Audit** — `run_audit`, `get_audit_summary`.

## Structured CSV export

`export_crawl_csv(job_id)` writes one flat CSV per crawl, modelled on Screaming
Frog's *Internal* tab with the *All* filter. List-type columns (links, images,
hreflang, structured-data types) are reduced to counts (plus a delimited string
where useful) so the file stays one row per URL. Fixed column order:

```
URL, Status Code, Status, Indexability, Indexability Reason, Title, Title Length,
Meta Description, Meta Description Length, H1, H1 Count, H2 Count, Meta Robots,
Canonical, Canonical Is Self, Word Count, Content-Type, Content-Encoding,
Response Time, Redirect URL, Redirect Type, Crawl Depth, Inlinks, Outlinks,
External Outlinks, Images, Images Missing Alt, Size (bytes), Hreflang Count,
Structured Data Types
```

The tool returns a summary and the file path — never the file contents inline.

## SEO audit (separate post-crawl process)

`run_audit(job_id)` runs only after a crawl exists. It never crawls; it analyses
the saved crawl plus already-fetched robots/sitemap data. The 90 checks are read
from `advertools_audit_check_catalogue.xlsx`, each tagged with a feasibility
tier (CRAWL, CRAWL+, RENDER, CWV, EXTERNAL).

Behaviour by tier:

| Tier | Default behaviour |
|------|-------------------|
| CRAWL, CRAWL+ | Run by default. CRAWL+ may HEAD-crawl assets, fetch JS/CSS bodies, or test URL variants, capped at `audit_url_sample` (200) URLs. |
| RENDER | Run only if `ADVTOOLS_ENABLE_RENDER=true`, else **Not assessed (requires rendering)**. |
| CWV | Run only if `ADVTOOLS_LIGHTHOUSE_API_KEY` is set. Where a check has a static-heuristic part, that part runs regardless and is labelled **Heuristic**. |
| EXTERNAL | Run only if GSC/backlink data is supplied, else **Not assessed**. |

**Hard rule:** a check that could not be evaluated is never recorded as a
pass/fail. The only states are: **Present**, **Not present**, **Not assessed**,
**Heuristic**. The test `tests/test_audit_not_assessed.py` proves every RENDER,
CWV (non-heuristic) and EXTERNAL check reports *Not assessed* when the optional
sources are off.

Output is an xlsx under `data/crawls/_audits/` with three sheets: **Checklist**
(one row per check), **Detail** (offending URLs per failed check), and
**Summary** (counts by status and tier + the audit configuration used). The full
file is never returned inline.

## Clarifying-confirmation behaviour

Every tool has a typed schema with validation. `start_crawl` will not silently
proceed on a high-impact config — it returns a structured `needs_confirmation`
response (echoing the resolved config) that you re-issue with `confirm=true`.
Triggers:

- a discovery crawl with **no** `max_pages` **and** no `max_depth` (open-ended),
- `obey_robots` turned **off**,
- high concurrency or zero delay against a **single host**,
- `allowed_domains` unset on a discovery crawl,
- (remote) a target host not on the domain allowlist.

## Local setup (stdio)

```bash
pip install -e .
```

MCP client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "advertools": {
      "command": "python",
      "args": ["-m", "advertools_mcp"],
      "env": {
        "ADVTOOLS_DATA_DIR": "/absolute/path/to/data/crawls"
      }
    }
  }
}
```

## Remote setup (streamable HTTP) + security model

```bash
ADVTOOLS_TRANSPORT=http \
ADVTOOLS_BEARER_TOKEN=$(openssl rand -hex 32) \
ADVTOOLS_DOMAIN_ALLOWLIST=example.com,shop.example.com \
ADVTOOLS_DATA_DIR=/data/crawls \
python -m advertools_mcp
```

Remote enforces, and **fails fast at startup** without:

- **Bearer auth** on every request (`Authorization: Bearer <token>`).
- A **non-empty domain allowlist** — prevents the server being used to crawl
  arbitrary third-party sites (SSRF/abuse). Targets are checked per request.
- **Per-client rate limits** and hard caps: `ADVTOOLS_MAX_PAGES`,
  `ADVTOOLS_MAX_JOB_RUNTIME`, `ADVTOOLS_MAX_CONCURRENT_JOBS`.

## Hosting recommendation

advertools needs a **long-lived process**, the ability to **spawn subprocesses**,
and a **writable filesystem** for `data/crawls`. That rules out serverless edge
runtimes such as Cloudflare Workers (no long-lived process, no subprocess
spawning, ephemeral FS).

- **Local:** run over stdio with the client config above.
- **Remote:** ship the multi-arch Docker image to an always-on container host —
  **Fly.io**, **Railway**, a small **VPS**, or a self-hosted lab VM exposed via a
  **Cloudflare Tunnel** — with a **persistent volume mounted at
  `/data/crawls`** so jobs and artefacts survive restarts. Two vCPU / 2–4 GB RAM
  is a comfortable starting point; crawls are the memory driver, so size to your
  `max_pages` and concurrency.

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t advertools-mcp:0.1 .
docker run -p 8000:8000 -v advertools-data:/data/crawls \
  -e ADVTOOLS_TRANSPORT=http -e ADVTOOLS_BEARER_TOKEN=... \
  -e ADVTOOLS_DOMAIN_ALLOWLIST=example.com advertools-mcp:0.1
```

## Configuration reference

| Env var | Default | Meaning |
|---------|---------|---------|
| `ADVTOOLS_DATA_DIR` | `./data/crawls` | Artefact + job store root |
| `ADVTOOLS_MAX_PAGES` | `3000` | `CLOSESPIDER_PAGECOUNT` safety cap |
| `ADVTOOLS_CONCURRENT_REQUESTS` | `6` | Default crawl concurrency |
| `ADVTOOLS_DOWNLOAD_DELAY` | `0.25` | Politeness delay (s) |
| `ADVTOOLS_OBEY_ROBOTS` | `true` | Default robots.txt obedience |
| `ADVTOOLS_USER_AGENT` | `intrepidbot-mcp/0.1 (+<contact>)` | Crawler UA |
| `ADVTOOLS_CONTACT_URL` | `https://www.intrepidonline.com` | Contact in UA (**flagged "to confirm"**) |
| `ADVTOOLS_CONTACT_URL_CONFIRMED` | `false` | Set true once the contact URL is verified |
| `ADVTOOLS_MAX_CONCURRENT_JOBS` | `2` | Running jobs before queueing |
| `ADVTOOLS_MAX_JOB_RUNTIME` | `3600` | Per-job runtime cap (s) |
| `ADVTOOLS_AUDIT_URL_SAMPLE` | `200` | Max URLs for any per-URL extra fetch |
| `ADVTOOLS_LIGHTHOUSE_API_KEY` | _(blank)_ | Enables CWV tier (PageSpeed Insights) |
| `ADVTOOLS_ENABLE_RENDER` | `false` | Enables RENDER tier |
| `ADVTOOLS_GSC_CREDENTIALS` | _(blank)_ | Enables EXTERNAL tier |
| `ADVTOOLS_TRANSPORT` | `stdio` | `stdio` or `http` |
| `ADVTOOLS_BEARER_TOKEN` | _(blank)_ | Required for remote |
| `ADVTOOLS_DOMAIN_ALLOWLIST` | _(empty)_ | Required non-empty for remote |

> **Note — `contact_url`.** The Inputs block flags `contact_url` as *to confirm*.
> The server uses `https://www.intrepidonline.com` as a placeholder and surfaces
> an advisory on every `start_crawl` until `ADVTOOLS_CONTACT_URL_CONFIRMED=true`.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

Proves: two sequential crawls succeed in one session; large crawls/audits return
summaries not raw data; the confirmation path fires on each trigger; and the
audit reports *Not assessed* (never a false pass) for every RENDER, CWV and
EXTERNAL check when optional sources are off.
