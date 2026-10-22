# CONTEXT.md — Yellow Pages Egypt Scraper (Eigenlabs Probe)

> Rich context so a future AI session can extend this probe **cold**. This is a
> faithful port of a deterministic standalone scraper (`browser.py` +
> `extractors.py` + `run.py` + `nested_capture.py`, originally driven by the
> gstack headed-browse daemon with a SQLite frontier) onto the Eigenlabs probe
> model. The verified in-page JS extractors were kept **verbatim** — do not
> rediscover the selectors.

---

## 1. What it extracts and the exact source

- **Source:** `https://yellowpages.com.eg` (Yellow Pages Egypt), English pages.
- **Unit:** one business **profile**. A profile may have **branches**, each of
  which is its own profile page; branches are captured deeply and nested under
  the parent (card → detail → branches[], each branch with its own detail).
- **Fields per record** (see `.probe/dataset_schema.json`): `name`, `address`,
  `phones[{type,number}]` (type ∈ phone/mobile/fax/hotline/unknown), `whatsapp[]`,
  `emails[]`, `coords[]` (`"lat,lng"`), `lat`, `lng`, `logo`, `images[]` (CDN
  URLs, capture-only), `categories[{t,h}]`, `socials[]`, `website[]`,
  `sections{}` (About Us / Company Description / Search Words / …), `hours{}`,
  `rating_value`, `rating_count`, `rating_histogram{}`, `reviews[{reviewer,text}]`,
  `branches[{name,profile_id,url,detail}]`, `url`, `scraped_at`. Probe-added:
  `profile_id`, `natural_key` (sha256), `from_category`, and (branch records
  only) `branch_of`.

---

## 2. Input / output contract

**Input** (`.probe/input_schema.json`):
```json
{ "mode": "category" | "profile",
  "category": "<slug>",                 // mode=category, e.g. "gas-stations"
  "profile_url": "<full profile url>",  // mode=profile
  "from_category": "<provenance label>",// optional
  "crawl_branches": true,               // optional (default true)
  "max_pages": 400 }                    // optional category cap
```

**Return** (summary, `.probe/output_schema.json`): `source`, `mode`,
`input_ref`, `records_pushed`, `profiles`, `branches`, `pages_crawled`,
`failed`.

**Per-record emission:** every profile is pushed via `ctx.dataset.push(record)`
(`.probe/dataset_schema.json`). Parent records carry nested `branches[].detail`;
each branch is **also** pushed as a flat record (`branch_of` set,
`socials`/`branches` stripped — "branch-level de-noising").

---

## 3. Capabilities declared and WHY

| capability | value | why |
| --- | --- | --- |
| `browser` | `true` | Content is client-rendered; phones only appear after clicking a popover. Uses `ctx.browser.new_context(proxy_endpoint=)` (Playwright sync API returning `(browser, context, page)`). |
| `proxy` | `residential-in` | Egypt-only, Cloudflare-gated site → needs an Egypt residential exit. `ctx.proxy.pick()`, report health with `ctx.proxy.record(ep, ok)`. |
| `captcha` | `none` | We **never** solve/bypass the Cloudflare challenge. On a block we raise; the platform handles retry/handoff. |
| `otp` / `auth` | `false` / `none` | Public pages, no login. |
| `concurrency` | `1` | One browser tab, sequential navigation. Two concurrent scrapes corrupt each other's navigations. |
| `cache_ttl` | `7d` | Listings change slowly. |

> **Porting note on the original's "headed browser only" rule.** The source
> README notes Cloudflare blocked curl/httpx **and headless browsers**, so it
> required a *headed* real browser. On the platform we rely on
> `ctx.browser` + a residential Egypt proxy to get past Cloudflare. If the
> platform's browser is headless and Cloudflare blocks it, `_check_blocked`
> will raise `cloudflare block: …` — that is the **expected, honest** failure
> (we do not add stealth). If this happens in the sandbox, that's the first
> thing to flag.

---

## 4. Extraction flow (step by step)

All navigation goes through `_goto(url)`: humane pacing (1.5–3.0 s jitter per
nav, a 30–60 s breather every 25 navs), `page.goto`, a 2.5 s settle, then
`_check_blocked()`. In-page JS is run with `page.evaluate`; extractors return
`JSON.stringify(...)`, parsed by `_eval_json`.

### mode = "category"
1. `_goto(/en/category/<slug>)`, then `_extract_listing()`:
   - `LISTING_PAGE` JS parses `.companyCards` → cards (`id`, `slug`, `name`,
     `address`, `categories`, `rating`, `has_branches`, action links) and reads
     the pager `lastPage`. Retries up to 3× on empty render.
   - `_sweep_card_phones()` — **sequential** click→read over each card's phone
     button, mapping each `.popover-phones` back to its profile id.
2. Paginate `/en/category/<slug>/p<N>` up to `min(pager_last, max_pages)`,
   collecting every card in memory (the frontier).
3. For each card: `_capture_profile(url, do_phone_reveal=False)` (listing cards
   already carry phones), backfill card phones if the profile exposed none,
   then `_push_chain(...)`.

### mode = "profile"
1. `_capture_profile(profile_url, do_phone_reveal=True)` for the parent.
2. `_push_chain(...)`: for each branch, `_capture_profile(branch_url,
   do_phone_reveal=True)` → `_slim_branch_detail` → nest under parent + push
   flat; then push the parent record.

### `_capture_profile(purl, do_phone_reveal)`
- Up to 3 nav attempts; **stale-page audit**: require `target_id` in
  `location.pathname`; a `/` redirect ⇒ dead profile (return None).
- If `do_phone_reveal`: `_sweep_profile_phones()` — sequential click→read over
  header/contact-bar phone buttons, **excluding** branch-section buttons
  (`!b.closest('[id^="branch"]')`).
- Run `PROFILE_PAGE` JS; second stale check (`target_id` in extracted `url`).

### Key selectors / endpoints (verified live 2026-09-11; see source STRUCTURE.md)
- Category listing: `/en/category/<slug>[/p<N>]`, cards in `.companyCards`,
  card links carry `?position=…&categoryId=…`, pager `/p<N>`.
- Profile: `h1` name; header block address; `.popover-phones` labeled tel rows;
  `a[href*="maps"]?query=<lat>,<lng>` + `lat="…" lng="…"` attrs; TOC `a[href^="#"]`
  drives `sections`; `#categories`; `.opening-hours-day` + `.days-hours` → hours;
  `.gallery-video-images-div`/`.photos-gallery` images; `.rating-value`,
  `.rating-total`, `.rating-analysis`, `.reviews-data .name-rating-div`;
  `#branches2` → branch profile links.

---

## 5. How to test

**Canonical verification chain** (mirrors the original README's regression check):
```json
{ "mode": "profile",
  "profile_url": "https://yellowpages.com.eg/en/profile/emarat-misr/145405",
  "from_category": "gas-stations" }
```
Expected (from `emarat_misr_FULL.json`):
- Parent `Emarat Misr`, mobile `+20100-1058-175`, coords `29.986084,31.308653`,
  **16 branches**, each with its own coords/detail.
- Branch `145954` has phones incl. `+202-3539-1400` and its own coords
  `30.061689,31.0298`.
- Branch records carry **no** `socials` and **no** nested `branches`; branch
  `website` IS kept (can differ per branch).
- `records_pushed` = 1 parent + 16 branches = **17**; `profiles` = 1,
  `branches` = 16.

**Small category smoke test:**
```json
{ "mode": "category", "category": "gas-stations", "max_pages": 1 }
```
Watch `ctx.log` stream: `[crawl:gas-stations] p1/… : N cards`, then
`[profiles] …`. Confirm each pushed record matches `dataset_schema.json`.

Sandbox-test in the Eigenlabs console and watch it run live.

---

## 6. Known edge cases / failure modes

- **Cloudflare block** → `_check_blocked` raises `cloudflare block: …`. Do not
  add bypass; this is the honest failure. Retry via a different proxy exit.
- **Phone popovers** — one open at a time; always use the sequential sweeps.
  Bulk clicking loses numbers.
- **Stale / lagging navigation** — the `location.pathname` audit + the
  extracted-`url` audit reject mismatches; a `/` redirect is a dead profile.
- **Late render** — listing extractor retries 3× on empty `profiles`.
- **Chrome leak detector** — if identical contact data shows up on *every*
  record (e.g. the 8 footer phones, `CustomerCare@Yellow.com.eg`, widget
  `+201224195585`), a filter regressed; fix the extractor, don't ship it.
- **Big categories** — all cards for a category are held in memory before
  profile capture; `max_pages` caps the walk. For very large categories or
  resumable runs, consider chunking by `max_pages`/page-range, or persisting a
  frontier via `ctx.storage.kv_set/kv_get` (not currently used — a single run
  is treated as one unit of work).

---

## 7. What changed from the standalone scraper

| standalone | probe |
| --- | --- |
| `browser.py` → gstack `browse` daemon via `subprocess`, `--headed` | `ctx.browser.new_context()` (Playwright) + `ctx.proxy` |
| `browser.goto` / `browser.js` | `page.goto` / `page.evaluate` (via `_goto` / `_eval[_json]`) |
| `store.py` → SQLite frontier + `yp_records.jsonl` + `yp_all.csv` | `ctx.dataset.push(record)` per profile; platform owns persistence |
| `run.py` stages harvest/match/crawl/export | `run(input, ctx)` with `category` / `profile` modes (harvest/match were one-time category-selection steps — out of scope for a per-call probe; pass a category slug directly) |
| `nested_capture.py` | `mode="profile"` |
| `browser.Blocked` → `handoff_and_exit` (sys.exit 3) | `raise RuntimeError("cloudflare block: …")` — platform handles retry/handoff |
| humane pacing in `browser.goto` | preserved verbatim in `_goto` |
| sequential phone sweeps, stale-page audit, chrome filters, sha256 key | preserved verbatim |

The only behavioral narrowing: `harvest` (building the full category index) and
`match` (keyword relevance flagging) were pre-crawl category **selection**
stages that populated a local DB. A probe takes a concrete category slug (or
profile URL) as input, so those stages are not part of per-call extraction. The
seed keyword/category lists live in the source repo's `config.json` /
`relevant_categories.json` if a category-discovery probe is ever wanted.

---

## 8. Shipping checklist

1. `git init && git add -A && git commit -m "eigenlabs probe"`, push to GitHub.
2. Eigenlabs console → **Onboard** → **Connect GitHub** → authorize + pick repo.
3. Agent runs conformance; if it flags gaps, paste its fix prompt back, fix,
   push, re-sync. Then sandbox-test (the canonical chain in §5) and launch.
