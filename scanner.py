#!/usr/bin/env python3
"""
Micro Center prebuilt flip scanner.

1. Loads Micro Center search results per store (headless Chromium).
2. Extracts every desktop tile (name, price, open-box price, stock).
3. Opens each in-stock product page to CONFIRM price + stock at that store.
   Anything that can't be confirmed is dropped.
4. Parses specs from the product name, values the part-out, computes margin after tax.
5. Writes data/latest.json, data/history/<date>.json, report.html, summary.md
   and sends a push (ntfy) for anything >= min_profit_alert.
"""
import asyncio, json, os, re, sys, datetime as dt, random, urllib.request
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
DATA = ROOT / "data"; (DATA / "history").mkdir(parents=True, exist_ok=True)
DEBUG = ROOT / "debug"; DEBUG.mkdir(exist_ok=True)

MC = "https://www.microcenter.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# ---------- page extraction (runs in the browser) ----------
# Finds every /product/<id>/ link, climbs to the largest ancestor that contains
# only that one product id = the tile. Doesn't depend on Micro Center's CSS classes.
TILE_JS = r"""
() => {
  const idOf = h => { const m = (h||'').match(/\/product\/(\d+)\//); return m ? m[1] : null; };
  const out = {};
  for (const a of document.querySelectorAll('a[href*="/product/"]')) {
    const id = idOf(a.getAttribute('href')); if (!id) continue;
    let el = a, tile = a;
    for (let i = 0; i < 12; i++) {
      el = el.parentElement; if (!el || el === document.body) break;
      const ids = new Set([...el.querySelectorAll('a[href*="/product/"]')].map(x => idOf(x.getAttribute('href'))).filter(Boolean));
      if (ids.size > 1) break;
      tile = el;
    }
    const name = (a.getAttribute('data-name') || a.getAttribute('title') || a.innerText || '').trim();
    const prev = out[id];
    const rec = { id, url: a.href.split('?')[0], name,
                  data_price: a.getAttribute('data-price'), text: tile.innerText || '' };
    if (!prev || rec.name.length > prev.name.length || rec.text.length > prev.text.length) {
      out[id] = { ...prev, ...rec,
                  name: (prev && prev.name.length > rec.name.length) ? prev.name : rec.name,
                  text: (prev && prev.text.length > rec.text.length) ? prev.text : rec.text };
    }
  }
  return Object.values(out);
}
"""

MONEY = r"\$\s?([\d,]+\.\d{2})"

def money(s):
    return float(s.replace(",", "")) if s else None

def parse_stock(text):
    """Return (count, store_name_or_None). 0 means not available for pickup."""
    t = " ".join(text.upper().split())
    m = re.search(r"(?<!\d)(\d+)(\+?)\s*IN STOCK(?:\s*AT\s*(?:THE\s*)?([A-Z .'-]+?)\s*STORE)?", t)
    if m:
        n = int(m.group(1)); store = (m.group(3) or "").title().strip() or None
        return (max(n, 25) if m.group(2) else n), store
    if re.search(r"SOLD OUT|OUT OF STOCK|NOT AVAILABLE|UNAVAILABLE|SHIPPING ONLY|NOT CARRIED", t):
        return 0, None
    if "LIMITED AVAILABILITY" in t:
        return 1, None
    return 0, None

def parse_price(text, data_price=None):
    """Return (price, open_box_price). Ignores 'save $', 'was $', 'open box' amounts for the main price."""
    open_box = None
    m = re.search(r"open[- ]box[^$]{0,40}" + MONEY, text, re.I)
    if m: open_box = money(m.group(1))
    if data_price:
        try:
            p = float(data_price)
            if p > 100: return p, open_box
        except ValueError:
            pass
    cands = []
    for m in re.finditer(MONEY, text):
        pre = text[max(0, m.start() - 30):m.start()].lower()
        if re.search(r"(save|savings|was|off|reg\.?|regular|original|open[- ]box[^$\n]*|from)\s*:?\s*$", pre):
            continue
        post = text[m.end():m.end() + 12].lower()
        if re.match(r"\s*(/\s*mo|per month|off)", post):
            continue
        v = money(m.group(1))
        if v and v >= 300: cands.append(v)
    return (min(cands) if cands else None), open_box

# ---------- spec parsing ----------
GPU_PATTERNS = [
    ("RTX 5090", r"5090"), ("RTX 5080", r"5080"), ("RTX 5070 Ti", r"5070\s*Ti"), ("RTX 5070", r"5070(?!\s*Ti)"),
    ("RTX 5060 Ti", r"5060\s*Ti"), ("RTX 5060", r"5060(?!\s*Ti)"),
    ("RTX 4090", r"4090"), ("RTX 4080 SUPER", r"4080\s*SUPER"), ("RTX 4080", r"4080(?!\s*SUPER)"),
    ("RTX 4070 Ti SUPER", r"4070\s*Ti\s*SUPER"),
    ("RX 9070 XT", r"9070\s*XT"), ("RX 9070", r"RX\s*9070(?!\s*XT)"), ("RX 9060 XT", r"9060\s*XT"),
    ("RX 7900 XTX", r"7900\s*XTX"),
]
CPU_PATTERNS = sorted(CFG["part_resale"]["cpu"].keys(), key=len, reverse=True)

def parse_specs(name):
    s = {"gpu": None, "cpu": None, "ram_gb": None, "ram_type": None, "ssd_tb": None}
    for label, pat in GPU_PATTERNS:
        if re.search(pat, name, re.I): s["gpu"] = label; break
    for c in CPU_PATTERNS:
        if re.search(r"(?<![\dA-Z])" + re.escape(c) + r"(?![\dA-Z])", name, re.I): s["cpu"] = c; break
    m = re.search(r"(\d{2,3})\s*GB\s*(?:\w+\s+){0,2}?(DDR[45])", name, re.I)
    if m: s["ram_gb"], s["ram_type"] = int(m.group(1)), m.group(2).upper()
    else:
        m = re.search(r"(\d{2,3})\s*GB\s*(?:RAM|Memory)", name, re.I)
        if m: s["ram_gb"], s["ram_type"] = int(m.group(1)), "DDR5"
    m = re.search(r"(\d+(?:\.\d+)?)\s*TB\s*(?:\w+\s+){0,3}?(?:SSD|NVMe|M\.2|Solid)", name, re.I)
    if m: s["ssd_tb"] = float(m.group(1))
    else:
        m = re.search(r"(\d{3,4})\s*GB\s*(?:\w+\s+){0,3}?(?:SSD|NVMe|M\.2|Solid)", name, re.I)
        if m: s["ssd_tb"] = int(m.group(1)) / 1000
    return s

def is_desktop(name):
    n = name.lower()
    if re.search(r"laptop|notebook|monitor|graphics card|video card|barebone|keyboard|controller|headset|handheld", n):
        return False
    return bool(re.search(r"gaming pc|gaming desktop|desktop|tower|\bpc\b", n))

def value_build(name, specs):
    pr = CFG["part_resale"]
    parts = {}
    parts["gpu"] = pr["gpu"].get(specs["gpu"], 0) if specs["gpu"] else 0
    parts["cpu"] = pr["cpu"].get(specs["cpu"], 0) if specs["cpu"] else 0
    if specs["ram_gb"]:
        per = pr["ram_per_gb_ddr5"] if specs["ram_type"] != "DDR4" else pr["ram_per_gb_ddr4"]
        parts["ram"] = round(specs["ram_gb"] * per)
    else:
        parts["ram"] = 0
    parts["ssd"] = round((specs["ssd_tb"] or 0) * pr["ssd_per_tb"])
    prop = any(b.lower() in name.lower() for b in pr["proprietary_brands"])
    parts["board_psu_case_cooler"] = pr["board_psu_case_cooler"]["proprietary" if prop else "standard"]
    missing = [k for k in ("gpu", "cpu", "ram", "ssd") if not parts[k]]
    return parts, sum(parts.values()), prop, missing

# ---------- scraping ----------
async def new_context(browser, store_id):
    ctx = await browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1400, "height": 1000})
    await ctx.add_cookies([
        {"name": "storeSelected", "value": store_id, "domain": ".microcenter.com", "path": "/"},
        {"name": "myStore", "value": "true", "domain": ".microcenter.com", "path": "/"},
    ])
    return ctx

async def goto(page, url, tag):
    for attempt in range(3):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500 + random.randint(0, 1500))
            status = resp.status if resp else None
            if status and status >= 400:
                (DEBUG / f"{tag}.html").write_text(await page.content())
                print(f"  ! HTTP {status} on {url}")
                if status in (403, 429): await page.wait_for_timeout(8000 * (attempt + 1)); continue
                return None
            return status
        except Exception as e:
            print(f"  ! {type(e).__name__} on {url}: {e}")
            await page.wait_for_timeout(4000)
    return None

async def scrape_store(browser, store_id):
    store = CFG["stores"][store_id]
    ctx = await new_context(browser, store_id)
    page = await ctx.new_page()
    found = {}
    blocked_pages = 0
    for q in CFG["search_queries"]:
        for pg in range(1, CFG["max_pages_per_query"] + 1):
            url = (f"{MC}/search/search_results.aspx?Ntt={quote_plus(q)}&Ntk=all&sortby=match"
                   f"&rpp=96&page={pg}&storeid={store_id}&myStore=true")
            st = await goto(page, url, f"search_{store_id}_{q.replace(' ','_')}_{pg}")
            if st is None: blocked_pages += 1; break
            tiles = await page.evaluate(TILE_JS)
            if pg == 1 and not tiles:
                (DEBUG / f"empty_{store_id}_{q.replace(' ','_')}.html").write_text(await page.content())
            new = 0
            for t in tiles:
                if t["id"] in found or not is_desktop(t["name"]): continue
                found[t["id"]] = t; new += 1
            print(f"  [{store['name']}] '{q}' p{pg}: {len(tiles)} tiles, {new} new desktops")
            if new == 0: break
    await ctx.close()
    return found, blocked_pages

async def verify(browser, store_id, item):
    """Open the product page for this store; return (price, stock, open_box) or None if not confirmable."""
    ctx = await new_context(browser, store_id)
    page = await ctx.new_page()
    try:
        st = await goto(page, f"{item['url']}?storeid={store_id}", f"product_{store_id}_{item['id']}")
        if st is None: return None
        info = await page.evaluate(r"""() => {
          const pick = s => { const e = document.querySelector(s); return e ? (e.getAttribute('content') || e.innerText || '').trim() : null; };
          const ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent);
          return {
            title: pick('h1') || document.title,
            price_meta: pick('[itemprop="price"]') || pick('meta[property="product:price:amount"]'),
            inventory: pick('#pnlInventory') || pick('.inventory') || pick('[class*="inventory"]') || '',
            body: document.body.innerText.slice(0, 20000),
            ld
          };
        }""")
        price = None
        for blob in info["ld"]:
            m = re.search(r'"price"\s*:\s*"?([\d.]+)', blob or "")
            if m: price = float(m.group(1)); break
        if not price and info["price_meta"]:
            try: price = float(re.sub(r"[^\d.]", "", info["price_meta"]))
            except ValueError: price = None
        body = info["body"]
        if not price:
            price, _ = parse_price(body)
        _, open_box = parse_price(body)
        stock, at_store = parse_stock(info["inventory"] or body)
        if stock <= 0 and info["inventory"]:
            stock, at_store = parse_stock(body)
        if at_store and at_store.lower() != CFG["stores"][store_id]["name"].lower():
            return None   # page is showing a different store -> can't confirm
        return {"price": price, "stock": stock, "open_box": open_box, "title": info["title"]}
    finally:
        await ctx.close()

# ---------- reporting ----------
TIERS = ["RTX 5090", "RTX 4090", "RTX 5080", "RTX 4080 SUPER", "RTX 4080", "RTX 5070 Ti", "RX 9070 XT",
         "RTX 4070 Ti SUPER", "RX 7900 XTX", "RTX 5070", "RX 9070", "RTX 5060 Ti", "RX 9060 XT", "RTX 5060", None]

def fmt(v): return "—" if v is None else f"${v:,.0f}"

def write_reports(rows, meta, diff):
    thr = CFG["min_profit_alert"]
    # summary.md (shown on the GitHub Actions run page)
    md = [f"# Micro Center flip scan — {meta['run_at']}",
          f"Confirmed in-stock listings: **{len(rows)}** · Dropped (not confirmable / out of stock): **{meta['dropped']}**",
          ""]
    for s in meta["blocked"]:
        md.append(f"> ⚠️ {s} — Micro Center blocked or errored on some pages; results for that store may be incomplete.")
    hits = [r for r in rows if r["margin"] >= thr]
    md += ["", f"## ≥ ${thr} margin ({len(hits)})", ""]
    md += table_md(sorted(hits, key=lambda r: -r["margin"])) if hits else ["Nothing clears the bar today."]
    if diff["new"] or diff["drops"]:
        md += ["", "## Changes since last run", ""]
        for r in diff["new"]: md.append(f"- NEW: [{r['name'][:90]}]({r['url']}) — {fmt(r['price'])} @ {r['store']}")
        for r, old in diff["drops"]: md.append(f"- PRICE DROP: [{r['name'][:90]}]({r['url']}) — {fmt(old)} → {fmt(r['price'])} @ {r['store']}")
    for store_id, st in CFG["stores"].items():
        srows = [r for r in rows if r["store_id"] == store_id]
        md += ["", f"## {st['name']} ({len(srows)} in stock, tax {st['tax']*100:.2f}%)"]
        for tier in TIERS:
            trows = sorted([r for r in srows if r["gpu"] == tier], key=lambda r: -r["margin"])
            if not trows: continue
            md += ["", f"### {tier or 'GPU not parsed'}", ""] + table_md(trows)
    (ROOT / "summary.md").write_text("\n".join(md))

    # report.html (self-contained, sortable)
    html = HTML_TMPL.replace("__DATA__", json.dumps({"rows": rows, "meta": meta, "threshold": thr}))
    (ROOT / "report.html").write_text(html)

def table_md(rows):
    out = ["| Margin | Price | Open box | Stock | Store | PC | Part-out | Notes |",
           "|---:|---:|---:|---:|---|---|---:|---|"]
    for r in rows:
        notes = []
        if r["proprietary"]: notes.append("proprietary board/PSU")
        if r["missing"]: notes.append("unpriced: " + ",".join(r["missing"]))
        out.append(f"| **{fmt(r['margin'])}** | {fmt(r['price'])} | {fmt(r['open_box'])} | {r['stock']} | {r['store']} "
                   f"| [{r['name'][:80]}]({r['url']}) | {fmt(r['partout'])} | {'; '.join(notes)} |")
    return out

HTML_TMPL = (ROOT / "report_template.html").read_text() if (ROOT / "report_template.html").exists() else "<pre>__DATA__</pre>"

def notify(hits):
    topic = os.environ.get("NTFY_TOPIC") or CFG.get("ntfy_topic")
    if not topic or not hits: return
    lines = [f"+{fmt(r['margin'])}  {r['store']}  {fmt(r['price'])}  {r['cpu'] or '?'} / {r['gpu'] or '?'}  ({r['stock']} in stock)"
             for r in sorted(hits, key=lambda r: -r["margin"])[:8]]
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data="\n".join(lines).encode(),
                                 headers={"Title": f"{len(hits)} Micro Center flip(s) over ${CFG['min_profit_alert']}",
                                          "Click": hits[0]["url"], "Priority": "high"})
    try: urllib.request.urlopen(req, timeout=15); print("push sent")
    except Exception as e: print("push failed:", e)

# ---------- main ----------
async def main():
    run_at = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4))).strftime("%Y-%m-%d %H:%M ET")
    rows, dropped, blocked = [], 0, []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        for store_id, st in CFG["stores"].items():
            print(f"== {st['name']} ({store_id})")
            found, nblocked = await scrape_store(browser, store_id)
            if nblocked: blocked.append(f"{st['name']}: {nblocked} search page(s) failed")
            cands = []
            for t in found.values():
                price, ob = parse_price(t["text"], t.get("data_price"))
                stock, at_store = parse_stock(t["text"])
                if stock <= 0 or not price: dropped += 1; continue
                if at_store and at_store.lower() != st["name"].lower():
                    dropped += 1; print(f"  x wrong store ({at_store}): {t['name'][:60]}"); continue
                cands.append((t, price, ob, stock))
            print(f"  {len(cands)} in-stock candidates from search tiles")
            for t, price, ob, stock in cands:
                if CFG.get("verify_product_pages", True):
                    v = await verify(browser, store_id, t)
                    if not v or not v["price"] or v["stock"] <= 0:
                        dropped += 1; print(f"  x dropped (not confirmed): {t['name'][:70]}"); continue
                    price, stock, ob = v["price"], v["stock"], v["open_box"] or ob
                specs = parse_specs(t["name"])
                parts, partout, prop, missing = value_build(t["name"], specs)
                cost = round(price * (1 + st["tax"]), 2)
                rows.append({"id": t["id"], "store_id": store_id, "store": st["name"], "name": t["name"],
                             "url": t["url"], "price": price, "open_box": ob, "stock": stock,
                             **specs, "parts": parts, "partout": partout, "proprietary": prop,
                             "missing": missing, "cost_after_tax": cost, "margin": round(partout - cost),
                             "margin_open_box": round(partout - ob * (1 + st["tax"])) if ob else None})
        await browser.close()

    prev_path = DATA / "latest.json"
    prev = {(r["store_id"], r["id"]): r for r in json.loads(prev_path.read_text())["rows"]} if prev_path.exists() else {}
    diff = {"new": [], "drops": []}
    for r in rows:
        o = prev.get((r["store_id"], r["id"]))
        if prev and not o: diff["new"].append(r)
        elif o and r["price"] < o["price"] - 1: diff["drops"].append((r, o["price"]))

    meta = {"run_at": run_at, "dropped": dropped, "blocked": blocked,
            "stores": CFG["stores"], "part_resale": CFG["part_resale"]}
    out = {"meta": meta, "rows": rows}
    prev_path.write_text(json.dumps(out, indent=1))
    (DATA / "history" / f"{dt.date.today().isoformat()}.json").write_text(json.dumps(out))
    write_reports(rows, meta, diff)
    hits = [r for r in rows if r["margin"] >= CFG["min_profit_alert"]]
    notify(hits)
    print(f"\nDone: {len(rows)} confirmed in-stock, {len(hits)} over threshold, {dropped} dropped.")
    if not rows and blocked:
        print("NO DATA — Micro Center appears to be blocking this runner. See debug/ for the pages it returned.")
        sys.exit(2)

if __name__ == "__main__":
    asyncio.run(main())
