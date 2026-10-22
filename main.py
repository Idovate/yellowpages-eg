"""
Yellow Pages Egypt Scraper — Eigenlabs Probe.

Ported from the deterministic yp_scraper (browser.py + extractors.py + run.py +
nested_capture.py). The verified in-page JS extractors are kept VERBATIM — do
not rediscover the selectors (see CONTEXT.md / the original STRUCTURE.md).

Model: JSON in -> structured data out. One function, run(input, ctx). All
infra comes through ctx:
  - ctx.browser.new_context(proxy_endpoint=) -> (browser, context, page)   [Playwright]
  - ctx.proxy.pick() / ctx.proxy.record(endpoint, ok)
  - ctx.dataset.push(record)   emits one business record (dataset_schema)
  - ctx.log(msg)

Two modes:
  mode="category"  crawl /en/category/<slug> listing pages -> cards -> each
                   card's profile (+ nested branches). One record per profile.
  mode="profile"   capture a single chain: parent profile + all branches
                   (each branch gets its own deep detail, nested under parent).

Original hard rules preserved:
  * ONE browser tab, sequential, humane pacing (never parallelize).
  * ONE phone-popover open at a time -> phones captured by a sequential
    click->read sweep (bulk clicking silently loses numbers).
  * Stale-page audit: verify location.pathname carries the target profile id
    before extracting; refuse mismatches.
  * Never bypass a block. On a Cloudflare challenge we STOP (raise) — the
    platform handles retry/handoff; we do not add stealth/bypass.
  * YP chrome (footer phones, CustomerCare@Yellow.com.eg, widget +201224195585,
    share links, YP's own socials, yellowmedia ads) is filtered in-extractor.
"""
import hashlib
import json
import math
import random
import time

BASE = "https://yellowpages.com.eg"

# --- humane pacing (ported from browser.py; do not remove / parallelize) ------
MIN_DELAY = 1.5
MAX_DELAY = 3.0
SETTLE = 2.5
BREATHER_EVERY = 25
BREATHER_S = (30, 60)

MAX_PAGES_DEFAULT = 400


# =============================================================================
#  In-page JS extractors — VERBATIM from extractors.py (selectors verified live)
# =============================================================================

LISTING_PAGE = r"""
JSON.stringify((()=>{
  const txt=(e)=>(e&&e.innerText||'').replace(/\s+/g,' ').trim();
  const uniq=(x)=>[...new Set(x)];
  const wrap=document.querySelector('.companyCards');
  const cards={};
  if(wrap){
    for(const a of wrap.querySelectorAll('a[href*="?position="]')){
      const m=(a.getAttribute('href')||'').match(/\/profile\/([^/]+)\/(\d+)/);
      if(!m) continue; const id=m[2]; if(cards[id]) continue;
      let el=a;
      for(let i=0;i<8;i++){
        if(!el.parentElement || el.parentElement===wrap) break;
        el=el.parentElement;
      }
      const own=[...el.querySelectorAll('a[href*="/profile/'+m[1]+'/'+id+'"]')]
        .map(x=>txt(x)).filter(t=>t&&t.length>2&&!/More info|^Map$|^Phone/i.test(t));
      const raw=txt(el).slice(0,500);
      const addrM=raw.match(/More info\s+(.+?)(?:\s*See All Branches|\s*Categories:)/);
      const catsM=raw.match(/Categories:\s*(.+?)(?:\s*(?:Phone Number|Website|Email Us|Map|WhatsApp|Directions|Send us a message|Location|The phone)|$)/);
      const ratingM=raw.match(/\s(\d\.\d)\s/);
      const acts=[...el.querySelectorAll('a')].map(x=>x.getAttribute('href')||'');
      const popTyped=[];
      for(const btn of el.querySelectorAll('[aria-describedby]')){
        const pop=document.getElementById(btn.getAttribute('aria-describedby'));
        if(!pop) continue;
        for(const mm of pop.innerHTML.matchAll(/(Phone|Mobile|Fax|Hotline)\s*<a[^>]*href="tel:\s*([^"]+)"/gi))
          popTyped.push({type:mm[1].toLowerCase(), number:mm[2].trim()});
        for(const a of pop.querySelectorAll('a[href^="tel:"]')){
          const n=(a.getAttribute('href')||'').replace(/^tel:\s*/,'').trim();
          if(n&&!popTyped.some(p=>p.number===n)) popTyped.push({type:'unknown',number:n});
        }
      }
      const popTels=popTyped.map(p=>p.number);
      cards[id]={
        slug:m[1], id,
        name:own[0]||null,
        address:(addrM?addrM[1]:(own[1]||null)),
        categories:(catsM?catsM[1].split(',').map(s=>s.trim()).filter(Boolean):[]),
        rating:(ratingM?parseFloat(ratingM[1]):null),
        has_branches:/See All Branches/i.test(raw),
        phones:(popTyped.length?popTyped:popTels.map(n=>({type:'unknown',number:n}))),
        direct_tels:uniq(acts.filter(h=>h.startsWith('tel:')).map(h=>h.replace(/^tel:\s*/,''))),
        whatsapp:uniq(acts.filter(h=>/wa\.me|whatsapp/i.test(h)&&!/text=|201224195585/.test(h))),
        emails:uniq(acts.filter(h=>h.startsWith('mailto:')).map(h=>h.replace(/^mailto:/,'').split('?')[0])
          .filter(e=>!/@yellow\.com\.eg$/i.test(e))),
        website:uniq(acts.filter(h=>/^https?:/.test(h)&&
          !/yellowpages|yellowmedia|yellow\.com\.eg|google|facebook|instagram|tiktok|linkedin|twitter|x\.com|youtube|wa\.me|whatsapp/i.test(h))).slice(0,3),
        maps:uniq(acts.filter(h=>/maps|#map/i.test(h))).slice(0,3),
        _raw_text:raw
      };
    }
  }
  const out=Object.values(cards);
  const pager=[...document.querySelectorAll('a')].map(a=>a.getAttribute('href')||'')
    .filter(h=>/\/category\/[^/]+\/p\d+/.test(h));
  const last=Math.max(0,...pager.map(h=>+(h.match(/\/p(\d+)/)||[0,0])[1]));
  const header=(document.body.innerText.match(/Showing[^\n]{0,120}/i)||[null])[0];
  return {profiles:out, lastPage:last, header};
})())
"""

PROFILE_PAGE = r"""
JSON.stringify((()=>{
  const Q=(s)=>[...document.querySelectorAll(s)];
  const href=(a)=>a.getAttribute('href')||'';
  const txt=(e)=>(e&&e.innerText||'').replace(/\s+/g,' ').trim();
  const uniq=(x)=>[...new Set(x)];
  const name=txt(document.querySelector('h1'));

  const phones=[];
  for(const row of Q('.popover-phones')){
    const html=row.innerHTML;
    for(const m of html.matchAll(/(Phone|Mobile|Fax|Hotline)\s*<a[^>]*href="tel:\s*([^"]+)"/gi))
      phones.push({type:m[1].toLowerCase(), number:m[2].trim()});
    for(const a of row.querySelectorAll('a[href^="tel:"]')){
      const n=href(a).replace(/^tel:\s*/,'').trim();
      if(!phones.some(p=>p.number===n)) phones.push({type:'unknown', number:n});
    }
  }
  const whatsapp=uniq(Q('a[href*="wa.me"],a[href*="whatsapp"]').map(href))
    .filter(h=>!/text=/.test(h))
    .filter(h=>!/201224195585/.test(h));
  const allMails=uniq(Q('a[href^="mailto:"]').map(a=>href(a).replace(/^mailto:/,'').split('?')[0]));
  const emails=allMails.filter(e=>!/@yellow\.com\.eg$/i.test(e));

  const coords=uniq(Q('a[href*="maps"]').map(href)
    .map(h=>(h.match(/query=(-?\d+\.\d+),(-?\d+\.\d+)/)||h.match(/ll=(-?\d+\.\d+),(-?\d+\.\d+)/)||[]))
    .filter(m=>m.length).map(m=>m[1]+','+m[2]));
  const latAttr=(document.documentElement.outerHTML.match(/lat="(-?\d+\.\d+)"[^>]*lng="(-?\d+\.\d+)"/)||[]);

  const toc=uniq(Q('a[href^="#"]').map(a=>JSON.stringify({t:txt(a),id:href(a).slice(1)}))
    ).map(JSON.parse).filter(x=>x.t&&x.t.length<40&&x.id&&!/collapse|Mob/i.test(x.id));
  const sections={};
  for(const s of toc){
    if(/categor|branch|rate|review/i.test(s.id)) continue;
    const el=document.getElementById(s.id);
    if(el){const t=txt(el); if(t.length>15) sections[s.t]=t.slice(0,2500);}
  }

  const catRoot=document.getElementById('categories')||document;
  const cats=uniq([...catRoot.querySelectorAll('a[href*="/category/"]')]
    .map(a=>JSON.stringify({t:txt(a),h:href(a)}))).map(JSON.parse)
    .filter(c=>c.t&&c.t.length<60);

  const days=Q('.opening-hours-day').map(txt);
  let hours={};
  if(days.length){
    const tvals=(txt(document.querySelector('.days-hours')).match(/(\d{2}:\d{2}\s*-\s*\d{2}:\d{2}|Closed)/g)||[]);
    days.forEach((d,i)=>{hours[d]=tvals[i]||null});
  }

  const galRoot=document.querySelector('.gallery-video-images-div')||document;
  const images=uniq([...galRoot.querySelectorAll('img')]
    .map(i=>i.getAttribute('src')||i.getAttribute('data-src')||'')
    .filter(s=>/uploads|media|companies|content/i.test(s)&&!/\/static\/|closed-logo|sprite|\.svg/i.test(s))
    .map(s=>s.startsWith('//')?'https:'+s:s));
  const logo=(()=>{const i=document.querySelector('header img,[class*="logo" i] img');
    const s=i?(i.getAttribute('src')||''):''; return /closed-logo|static/.test(s)?null:(s.startsWith('//')?'https:'+s:s||null)})();

  const ratingTxt=(document.body.innerText.match(/([0-9.]+)\s*Rating\s*\/?\s*\(?(\d+)\)?\s*review/i)||[]);
  const rating_value=parseFloat(txt(document.querySelector('.rating-value')))||parseFloat(ratingTxt[1])||null;
  const rating_count=parseInt((txt(document.querySelector('.rating-total')).match(/\d+/)||[])[0])||parseInt(ratingTxt[2])||0;
  const histogram={};
  for(const m of txt(document.querySelector('.rating-analysis')).matchAll(/(\d)\s*-\s*(\d+)\s*Ratings/g))
    histogram[m[1]]=+m[2];
  const reviews=(()=>{
    const out=[], seenR=new Set();
    for(const nd of Q('.reviews-data .name-rating-div')){
      let row=nd; for(let i=0;i<4;i++){ if(row.parentElement) row=row.parentElement;
        if(txt(row).length>txt(nd).length+10) break; }
      const r={reviewer:txt(nd), text:txt(row).replace(txt(nd),'').trim().slice(0,600)};
      const k=r.reviewer+'|'+r.text.slice(0,80);
      if(r.text && !seenR.has(k)){ seenR.add(k); out.push(r); }
    }
    return out.slice(0,50);
  })();

  const YP_SOCIAL=/egypt_yellow_pages|EgyptYellowPages|egypt-yellow-pages|EgyYellowPages|YellowPages\.Egypt/i;
  const socials=uniq(Q('a').map(href)
    .filter(h=>/facebook\.com|instagram\.com|tiktok\.com|linkedin\.com|twitter\.com|x\.com|youtube\.com/i.test(h))
    .filter(h=>!/\/share|sharer\.php|shareArticle|intent\/tweet/i.test(h))
    .filter(h=>!YP_SOCIAL.test(h)));
  const website=uniq(Q('a').map(href).filter(h=>/^https?:/.test(h) &&
    !/yellowpages|yellowmedia|yellow\.com\.eg|google|facebook|instagram|tiktok|linkedin|twitter|x\.com|youtube|wa\.me|whatsapp|apple|play\.google/i.test(h))).slice(0,5);

  let branches=[];
  const bAnchor=document.getElementById('branches2')||document.querySelector('[id^="branches"]');
  if(bAnchor){
    const seenB={};
    for(const a of bAnchor.querySelectorAll('a[href*="/profile/"]')){
      const m=href(a).match(/\/profile\/([^/]+)\/(\d+)/);
      const t=txt(a);
      if(!m||!t||t.length<3) continue;
      if(seenB[m[2]]) continue;
      seenB[m[2]]={name:t.slice(0,200), profile_id:m[2],
                   url:'https://yellowpages.com.eg/en/profile/'+m[1]+'/'+m[2]};
    }
    branches=Object.values(seenB).slice(0,200);
  }

  const _hb=(()=>{const h=document.querySelector('h1');if(!h)return '';
    let el=h; for(let i=0;i<4;i++){ if(el.parentElement) el=el.parentElement; }
    return txt(el).slice(0,400)})();
  const address=(_hb.match(/(?:Share Like|Like)\s+(.+?)(?:\s+away|\s+\d+\.\d+\s*Rating|\s+Rating)/)||[])[1]||null;

  return {name, address, phones, whatsapp, emails, coords,
          lat:latAttr[1]||null, lng:latAttr[2]||null, logo, images:images.slice(0,80),
          categories:cats, socials, website, sections, hours,
          rating_value, rating_count, rating_histogram:histogram, reviews, branches,
          url:location.href, scraped_at:new Date().toISOString()};
})())
"""

# Read all currently-open phone popovers, labeled. Used after each sweep click.
_POPOVER_READ = r"""JSON.stringify((()=>{const out=[];
for(const row of document.querySelectorAll('.popover-phones')){
for(const m of row.innerHTML.matchAll(/(Phone|Mobile|Fax|Hotline)\s*<a[^>]*href="tel:\s*([^"]+)"/gi))
out.push({type:m[1].toLowerCase(),number:m[2].trim()});
for(const a of row.querySelectorAll('a[href^="tel:"]')){
const t=(a.getAttribute('href')||'').replace(/^tel:\s*/,'').trim();
if(t&&!out.some(p=>p.number===t))out.push({type:'unknown',number:t});}}
return out;})())"""


def _natural_key(profile_id):
    """sha256, never Python hash() (must be stable across runs)."""
    return hashlib.sha256(f"yellowpages|{profile_id}".encode()).hexdigest()


def _slim_branch_detail(det):
    """Branch-level de-noising: socials belong to the BRAND (parent) only, and
    the parent's branch list is the single canonical one."""
    if isinstance(det, dict):
        det.pop("socials", None)
        det.pop("branches", None)
    return det


def run(input, ctx):
    mode = input.get("mode")
    if mode not in ("category", "profile"):
        raise ValueError("input.mode must be 'category' or 'profile'")
    crawl_branches = input.get("crawl_branches", True)
    max_pages = input.get("max_pages") or MAX_PAGES_DEFAULT

    # Cloudflare gates yellowpages.com.eg; use an Egypt residential exit.
    proxy = ctx.proxy.pick()
    browser, context, page = ctx.browser.new_context(proxy_endpoint=proxy)

    nav = {"n": 0}
    counts = {"profiles": 0, "branches": 0, "failed": 0, "records": 0, "pages": 0}

    # ------------------------------------------------------------------ helpers
    def _eval(js):
        return page.evaluate(js)

    def _eval_json(js):
        """For extractors that return JSON.stringify(...) -> parse to Python."""
        raw = page.evaluate(js)
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw

    def _check_blocked():
        sig = _eval_json(
            "JSON.stringify({t:document.title.slice(0,60),"
            "b:document.body.innerText.slice(0,200)})")
        if isinstance(sig, dict):
            blob = (sig.get("t", "") + " " + sig.get("b", "")).lower()
            if ("attention required" in blob or "you have been blocked" in blob
                    or "just a moment" in blob):
                # NEVER bypass a block — stop and let the platform handle it.
                ctx.proxy.record(proxy, False)
                raise RuntimeError(f"cloudflare block: {sig.get('t', '')}")

    def _goto(url):
        nav["n"] += 1
        if nav["n"] % BREATHER_EVERY == 0:
            pause = random.uniform(*BREATHER_S)
            ctx.log(f"[pace] breather {pause:.0f}s after {nav['n']} navs")
            time.sleep(pause)
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        page.goto(url, timeout=120000)
        time.sleep(SETTLE)
        _check_blocked()

    def _sweep_profile_phones():
        """Profile pages have several phone-reveal buttons; the site keeps ONE
        popover open at a time, so sequentially click each header/contact-bar
        button (branch-section buttons excluded) and read the open popover."""
        sel = ("[...document.querySelectorAll("
               "'.profile-companyPhones,.call-us-click,[class*=\"companyPhones\"]')]"
               ".filter(b=>/phone/i.test(b.innerText||''))"
               ".filter(b=>!b.closest('[id^=\"branch\"]'))")
        try:
            n = int(_eval(f"(()=>{{return {sel}.length}})()"))
        except (TypeError, ValueError):
            return []
        phones = []
        for i in range(n):
            _eval(f"(()=>{{const b={sel}[{i}]; if(!b) return 'no-btn';"
                  "b.dispatchEvent(new MouseEvent('click',{bubbles:true,"
                  "cancelable:true,view:window})); return 'ok';})()")
            time.sleep(0.7)
            got = _eval_json(_POPOVER_READ)
            if isinstance(got, list):
                for p in got:
                    if not any(q["number"] == p["number"] and
                               (q["type"] == p["type"] or p["type"] == "unknown")
                               for q in phones):
                        phones.append(p)
        return phones

    def _sweep_card_phones():
        """Listing cards keep ONE popover open at a time too — sequential
        click->read sweep, mapping each popover back to its profile id."""
        try:
            n = int(_eval(
                "(()=>{const w=document.querySelector('.companyCards')||document;"
                "return [...w.querySelectorAll('.call-us-click,[class*=\"companyPhones\"]')]"
                ".filter(b=>/phone/i.test(b.innerText||'')).length})()"))
        except (TypeError, ValueError):
            return {}
        out = {}
        for i in range(n):
            r = _eval(
                "(()=>{const w=document.querySelector('.companyCards')||document;"
                "const btns=[...w.querySelectorAll('.call-us-click,[class*=\"companyPhones\"]')]"
                ".filter(b=>/phone/i.test(b.innerText||''));"
                f"const b=btns[{i}]; if(!b) return 'no-btn';"
                "let el=b, pid=null;"
                "for(let k=0;k<8&&el;k++){const a=el.querySelector&&el.querySelector('a[href*=\"?position=\"]');"
                "if(a){const m=(a.getAttribute('href')||'').match(/\\/profile\\/[^/]+\\/(\\d+)/);"
                "if(m){pid=m[1];break;}} el=el.parentElement;}"
                "b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));"
                "return pid||'no-pid';})()")
            time.sleep(0.7)
            phones = _eval_json(_POPOVER_READ)
            r = str(r)
            if r.isdigit() and isinstance(phones, list) and phones:
                out.setdefault(r, [])
                for p in phones:
                    if p not in out[r]:
                        out[r].append(p)
        return out

    def _extract_listing():
        d = None
        for _ in range(3):
            d = _eval_json(LISTING_PAGE)
            if isinstance(d, dict) and d.get("profiles"):
                break
            time.sleep(2.5)
        if not (isinstance(d, dict) and d.get("profiles")):
            return d if isinstance(d, dict) else None
        phones = _sweep_card_phones()
        for p in d["profiles"]:
            if p["id"] in phones:
                p["phones"] = phones[p["id"]]
        return d

    def _capture_profile(purl, do_phone_reveal=False):
        """One profile page -> full detail dict (or None). Stale-page audit:
        verify the browser landed on the target id before extracting."""
        target_id = purl.rstrip("/").split("/")[-1]
        landed = False
        for _attempt in range(3):
            _goto(purl)
            here = _eval("location.pathname")
            if isinstance(here, str) and target_id in here:
                landed = True
                break
            if isinstance(here, str) and here in ("/", ""):
                ctx.log(f"[dead-profile] {target_id} redirects to homepage")
                return None
            ctx.log(f"[nav-retry] wanted {target_id}, on {here}")
        if not landed:
            return None
        phones = _sweep_profile_phones() if do_phone_reveal else None
        d = _eval_json(PROFILE_PAGE)
        if isinstance(d, dict) and d.get("name"):
            if target_id not in (d.get("url") or ""):
                ctx.log(f"[stale-page] {target_id} extracted {d.get('url')}")
                return None
            if do_phone_reveal:
                d["phones"] = phones
            return d
        return None

    def _push_chain(pid, detail, card, from_category):
        """Nest branches under the parent, emit the parent record, and emit each
        branch as a flat (slimmed) record. Mirrors the original card->detail->
        branches[] contract. Returns nothing; updates counts."""
        # carry card-level phones into the detail when the profile exposed none
        if card:
            card_phones = card.get("phones", [])
            if card_phones and not detail.get("phones"):
                detail["phones"] = card_phones

        if crawl_branches:
            for b in detail.get("branches", []):
                bid = b.get("profile_id")
                bdet = _slim_branch_detail(
                    _capture_profile(b["url"], do_phone_reveal=True))
                b["detail"] = bdet
                if bid and bdet:
                    counts["branches"] += 1
                    ctx.dataset.push({
                        "profile_id": bid,
                        "natural_key": _natural_key(bid),
                        "from_category": from_category,
                        "branch_of": pid,
                        **bdet,
                    })
                    counts["records"] += 1

        ctx.dataset.push({
            "profile_id": pid,
            "natural_key": _natural_key(pid),
            "from_category": from_category,
            **detail,
        })
        counts["records"] += 1
        counts["profiles"] += 1

    # ----------------------------------------------------------------- dispatch
    try:
        if mode == "profile":
            purl = input.get("profile_url")
            if not purl:
                raise ValueError("mode='profile' requires input.profile_url")
            from_category = input.get("from_category", "")
            pid = purl.rstrip("/").split("/")[-1]
            ctx.log(f"[profile] capturing chain {purl}")
            det = _capture_profile(purl, do_phone_reveal=True)
            if not det:
                counts["failed"] += 1
                raise RuntimeError(f"failed to capture parent profile {purl}")
            ctx.log(f"[profile] {det.get('name')} | phones={det.get('phones')} | "
                    f"branches={len(det.get('branches', []))}")
            _push_chain(pid, det, None, from_category)
            input_ref = purl

        else:  # category
            slug = input.get("category")
            if not slug:
                raise ValueError("mode='category' requires input.category")
            from_category = input.get("from_category") or slug
            input_ref = slug

            # ---- walk listing pages, collecting every card (frontier in memory)
            cards = []
            page_n = 1
            _goto(f"{BASE}/en/category/{slug}")
            d = _extract_listing()
            if not isinstance(d, dict):
                raise RuntimeError(f"listing extractor failed on {slug} p1")
            pager_last = d.get("lastPage") or 1
            last = min(pager_last, max_pages)
            while True:
                for p in d.get("profiles", []):
                    p["_url"] = f"{BASE}/en/profile/{p['slug']}/{p['id']}"
                    cards.append(p)
                counts["pages"] += 1
                ctx.log(f"[crawl:{slug}] p{page_n}/{last}: "
                        f"{len(d.get('profiles', []))} cards")
                if page_n >= last:
                    break
                page_n += 1
                _goto(f"{BASE}/en/category/{slug}/p{page_n}")
                d = _extract_listing()
                if not isinstance(d, dict):
                    ctx.log(f"[crawl:{slug}] extractor failed on p{page_n}; "
                            "stopping pagination")
                    break

            ctx.log(f"[crawl:{slug}] {len(cards)} cards collected; "
                    "capturing profiles")

            # ---- capture each card's profile (+ nested branches), push records
            seen = set()
            for card in cards:
                pid = card["id"]
                if pid in seen:
                    continue
                seen.add(pid)
                det = _capture_profile(card["_url"], do_phone_reveal=False)
                if not det:
                    counts["failed"] += 1
                    continue
                _push_chain(pid, det, card, from_category)
                if counts["profiles"] % 10 == 0:
                    ctx.log(f"[profiles] {counts['profiles']} captured, "
                            f"{counts['branches']} branches")

        ctx.proxy.record(proxy, True)
        return {
            "source": "yellowpages.com.eg",
            "mode": mode,
            "input_ref": input_ref,
            "records_pushed": counts["records"],
            "profiles": counts["profiles"],
            "branches": counts["branches"],
            "pages_crawled": counts["pages"],
            "failed": counts["failed"],
        }
    finally:
        context.close()
