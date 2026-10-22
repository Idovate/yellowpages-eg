# Yellow Pages Egypt Scraper — Eigenlabs Probe

Scrapes business listings from **yellowpages.com.eg** and returns structured
records (name, address, labeled phones, WhatsApp, email, coordinates, hours,
categories, gallery image URLs, ratings/reviews, socials, website, and nested
branches). Ported from a deterministic stdlib scraper onto the Eigenlabs probe
model: **JSON in → structured data out**, one `run(input, ctx)` function, all
infra (browser, proxy) provided through `ctx`.

## Two modes

| mode | input | what it does |
| --- | --- | --- |
| `category` | `{ "mode": "category", "category": "gas-stations" }` | Walk every listing page of a category → each card's profile → nested branches. One record per profile. |
| `profile` | `{ "mode": "profile", "profile_url": "https://yellowpages.com.eg/en/profile/emarat-misr/145405" }` | Capture one chain: the parent profile + all branches, each with its own detail nested under the parent. |

Optional: `from_category` (provenance label), `crawl_branches` (default `true`),
`max_pages` (category cap, default 400).

## Output

- `run()` returns a **summary** (counts): `records_pushed`, `profiles`,
  `branches`, `pages_crawled`, `failed` — see `.probe/output_schema.json`.
- Each business is emitted via `ctx.dataset.push(record)` — see
  `.probe/dataset_schema.json`. Parent records carry a nested `branches[]`
  (each branch has its own `.detail`); branches are **also** emitted as flat
  records (`branch_of` = parent id, socials/branches stripped).

## Capabilities (and why)

| capability | value | why |
| --- | --- | --- |
| `browser` | `true` | Cloudflare blocks plain HTTP clients; the data is rendered client-side and phones require clicking popovers. Uses `ctx.browser.new_context()` (Playwright). |
| `proxy` | `residential-in` | Site is Egypt-only and Cloudflare-gated; needs an Egypt residential exit. `ctx.proxy.pick()`. |
| `captcha` | `none` | We never solve/bypass the Cloudflare challenge — on a block we **stop** (raise) and let the platform retry/handoff. |
| `concurrency` | `1` | One browser tab; sequential navigation with humane pacing. Never parallelize. |

## Preserved invariants (don't regress)

- **One phone-popover open at a time** → phones captured by a sequential
  click→read sweep (`_sweep_profile_phones`, `_sweep_card_phones`). Bulk
  clicking silently loses numbers.
- **Stale-page audit** → `_capture_profile` verifies `location.pathname`
  carries the target profile id before extracting; refuses mismatches; treats a
  homepage redirect as a dead profile.
- **Never bypass a block** → `_check_blocked` raises on "Attention Required" /
  "you have been blocked" / "Just a moment". No stealth.
- **YP chrome filtered in-extractor** → footer phones, `CustomerCare@Yellow.com.eg`,
  widget `+201224195585`, share links, YP's own socials, yellowmedia ads.
- **`natural_key` = sha256**, never Python `hash()`.

## The one hard rule

No raw network/browser/db imports (`requests`, `httpx`, `urllib`, `socket`,
`playwright`, `selenium`, `sqlite3`, `boto3`, …). Everything goes through `ctx`.
`requirements.txt` is empty.

See **CONTEXT.md** for the full system context, extraction flow, test plan, and
edge cases.
