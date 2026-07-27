"""
Stage 1 + 2: Scrape "general contractors" across 32 US cities via ScraperTech,
dedup, filter, derive chain/multi-location flags, emit <=500 rows.

Run:  python scrape.py
Out:  raw/<city>.json          one file per city (audit trail)
      general-contractors.csv  final <=500 rows, enrichment-ready
      scrape.log               progress + failures
"""
import csv, json, os, re, sys, time, urllib.request, urllib.error
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
LOG = os.path.join(HERE, "scrape.log")

KEYWORD      = "general contractors"
TARGET_TOTAL = 500     # hard cap on final rows
PER_CITY     = 25      # request ceiling per city; ~800 raw before filtering
WORKERS      = 4       # conservative: ScraperTech rate limits are undocumented
TIMEOUT      = 300

# Longitudes below are WEST -> stored NEGATIVE. Getting this wrong silently
# scrapes the wrong hemisphere and returns plausible-looking garbage.
CITIES = [
    ("New York City, NY",     40.7143, -74.0060), ("Los Angeles, CA",   34.0522, -118.2437),
    ("Brooklyn, NY",          40.6501, -73.9496), ("Chicago, IL",       41.8500, -87.6500),
    ("Queens, NY",            40.6815, -73.8365), ("Houston, TX",       29.7633, -95.3633),
    ("Phoenix, AZ",           33.4484, -112.0740),("Philadelphia, PA",  39.9524, -75.1636),
    ("San Antonio, TX",       29.4241, -98.4936), ("Manhattan, NY",     40.7834, -73.9663),
    ("San Diego, CA",         32.7157, -117.1647),("The Bronx, NY",     40.8499, -73.8664),
    ("Dallas, TX",            32.7831, -96.8067), ("Jacksonville, FL",  30.3322, -81.6556),
    ("Fort Worth, TX",        32.7254, -97.3208), ("San Jose, CA",      37.3394, -121.8950),
    ("Austin, TX",            30.2672, -97.7431), ("Columbus, OH",      39.9612, -82.9988),
    ("Charlotte, NC",         35.2271, -80.8431), ("Indianapolis, IN",  39.7684, -86.1580),
    ("San Francisco, CA",     37.7749, -122.4194),("Seattle, WA",       47.6062, -122.3321),
    ("Denver, CO",            39.7392, -104.9847),("Washington, DC",    38.8951, -77.0364),
    ("Nashville, TN",         36.1659, -86.7844), ("Oklahoma City, OK", 35.4676, -97.5164),
    ("El Paso, TX",           31.7587, -106.4869),("Boston, MA",        42.3584, -71.0598),
    ("Portland, OR",          45.5234, -122.6762),("Detroit, MI",       42.3314, -83.0457),
    ("Las Vegas, NV",         36.1750, -115.1372),("New South Memphis, TN", 35.0868, -90.0568),
]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env():
    env = {}
    with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def mcp_call(url, tool, arguments, timeout=TIMEOUT):
    """One MCP tools/call over streamable HTTP. Returns parsed inner payload."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        outer = json.loads(resp.read().decode())
    if "error" in outer:
        raise RuntimeError(f"MCP error: {outer['error']}")
    return json.loads(outer["result"]["content"][0]["text"])


# ---------- normalization helpers (used for chain grouping) ----------

# Hosts where many UNRELATED businesses share one registrable domain. Grouping
# on these would falsely merge distinct companies into a fake "chain", so they
# are never grouped -- each row on one of these stays a singleton.
SHARED_HOSTS = {
    "facebook.com", "m.facebook.com", "sites.google.com", "business.site",
    "wixsite.com", "wix.com", "godaddysites.com", "weebly.com", "squarespace.com",
    "blogspot.com", "wordpress.com", "myshopify.com", "linktr.ee", "yelp.com",
    "instagram.com", "nextdoor.com", "angi.com", "houzz.com", "thumbtack.com",
}

SUFFIXES = {
    "llc", "l.l.c", "inc", "inc.", "incorporated", "corp", "corp.", "corporation",
    "co", "co.", "company", "ltd", "ltd.", "limited", "lp", "llp", "pllc", "pc",
    "the", "and", "&",
}


def norm_name(name):
    """Lowercase, drop branch qualifiers, punctuation and legal suffixes."""
    if not name:
        return ""
    n = name.lower()
    n = re.split(r"\s+[-–—#]\s*", n)[0]        # "Acme - Downtown" / "Acme #3" -> "acme"
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    tokens = [t for t in n.split() if t and t not in SUFFIXES]
    return " ".join(tokens)


def root_domain(website):
    """Registrable-ish domain. US-only list, so a simple netloc strip is adequate."""
    if not website:
        return ""
    w = website.strip().lower()
    w = re.sub(r"^https?://", "", w)
    w = w.split("/")[0].split("?")[0].split("#")[0]
    if w.startswith("www."):
        w = w[4:]
    return w if "." in w else ""


# ---------- stage 1: scrape ----------

def scrape_city(url, city, lat, lng):
    try:
        data = mcp_call(url, "search_maps", {
            "query": KEYWORD,
            "lat": str(lat), "lng": str(lng),
            "limit": str(PER_CITY), "country": "us", "lang": "en",
        })
    except Exception as e:                      # network / MCP / parse failure
        log(f"  FAIL  {city}: {type(e).__name__}: {e}")
        return city, []

    if data.get("status") != "ok":
        log(f"  FAIL  {city}: status={data.get('status')}")
        return city, []

    rows = data.get("data") or []
    os.makedirs(RAW, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", city)
    with open(os.path.join(RAW, f"{safe}.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    log(f"  ok    {city}: {len(rows)} results")
    return city, rows


def main():
    open(LOG, "w").close()
    env = load_env()
    key = env.get("SCRAPERTECH_KEY")
    if not key:
        sys.exit("SCRAPERTECH_KEY missing from .env")
    url = f"https://mcp.scraper.tech/{key}"

    log(f"Scraping '{KEYWORD}' across {len(CITIES)} cities "
        f"(limit {PER_CITY}/city, {WORKERS} workers)")

    results = []
    if "--reuse-raw" in sys.argv:
        # Rebuild from cached raw/*.json -- costs ZERO API requests. Use this
        # when only the flagging/filtering logic changed, not the scrape.
        log("--reuse-raw: loading cached raw/ files, no API calls")
        for city, _, _ in CITIES:
            safe = re.sub(r"[^A-Za-z0-9]+", "_", city)
            path = os.path.join(RAW, f"{safe}.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    results.append((city, json.load(f)))
            else:
                log(f"  MISS  {city}: no cached file")
                results.append((city, []))
    else:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = [ex.submit(scrape_city, url, c, la, ln) for c, la, ln in CITIES]
            for fut in futures:
                results.append(fut.result())

    raw_total = sum(len(r) for _, r in results)
    failed = [c for c, r in results if not r]
    log(f"Raw results: {raw_total}   cities returning data: "
        f"{len(CITIES) - len(failed)}/{len(CITIES)}")
    if failed:
        log(f"Cities with no data: {', '.join(failed)}")

    # ---------- dedup by place_id, keep first city that saw it ----------
    by_city = OrderedDict()
    seen = set()
    dupes = 0
    for city, rows in results:
        keep = []
        for r in rows:
            pid = r.get("place_id")
            if not pid or pid in seen:
                dupes += 1
                continue
            seen.add(pid)
            r["_city"] = city
            keep.append(r)
        if keep:
            by_city[city] = keep
    deduped = sum(len(v) for v in by_city.values())
    log(f"After place_id dedup: {deduped}  (removed {dupes} dupes/blank ids)")

    # ---------- filter: closed businesses and rows with no website ----------
    dropped_closed = dropped_nosite = 0
    for city, rows in by_city.items():
        keep = []
        for r in rows:
            if r.get("is_permanently_closed") or r.get("is_temporarily_closed"):
                dropped_closed += 1
                continue
            if not root_domain(r.get("website")):
                dropped_nosite += 1
                continue
            keep.append(r)
        by_city[city] = keep
    filtered = sum(len(v) for v in by_city.values())
    log(f"After filter: {filtered}  (dropped {dropped_closed} closed, "
        f"{dropped_nosite} without a usable website)")

    # ---------- stage 2: chain flags, computed on the FULL filtered set ----------
    # Done BEFORE truncation so location counts use every location we saw,
    # not just the 500 that survive the cap.
    # Key = root domain ALONE. Evidence for dropping the name from the key:
    # turnerconstruction.com carried both "turner" and "turner construction",
    # splitting one 7-location brand into a 5 and a 2. Same for hoffmancorp.com,
    # topnotchbuilders1.com, grandeurhillsgroup.com.
    # Guards: no domain -> singleton; shared site-builder host -> singleton.
    groups = defaultdict(list)
    for rows in by_city.values():
        for r in rows:
            dom = root_domain(r.get("website"))
            r["_key"] = dom if dom and dom not in SHARED_HOSTS else None
            if r["_key"]:
                groups[r["_key"]].append(r)

    for rows in by_city.values():
        for r in rows:
            k = r.get("_key")
            n = len({x.get("place_id") for x in groups[k]}) if k else 1
            r["_location_count"] = n
            r["_is_multi"] = "yes" if n >= 2 else "no"
            r["_chain_cities"] = "; ".join(sorted({x["_city"] for x in groups[k]})) if k else ""

    multi = sum(1 for v in by_city.values() for r in v if r["_is_multi"] == "yes")
    log(f"Multi-location flagged: {multi} rows across "
        f"{len([k for k, v in groups.items() if len({x['place_id'] for x in v}) >= 2])} brands")

    # ---------- round-robin truncate to TARGET_TOTAL (even city spread) ----------
    final, i = [], 0
    pools = list(by_city.values())
    while len(final) < TARGET_TOTAL and any(len(p) > i for p in pools):
        for p in pools:
            if i < len(p):
                final.append(p[i])
                if len(final) >= TARGET_TOTAL:
                    break
        i += 1
    log(f"Final rows after round-robin cap: {len(final)}")

    # ---------- write CSV ----------
    out = os.path.join(HERE, "general-contractors.csv")
    cols = ["business_name", "business_location", "business_website", "domain",
            "phone", "full_address", "city", "state", "rating", "review_count",
            "types", "is_claimed", "verified", "location_count", "is_multi_location",
            "chain_cities", "place_id", "business_id", "latitude", "longitude"]
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in final:
            w.writerow({
                "business_name": r.get("name") or "",
                "business_location": r.get("_city") or "",
                "business_website": r.get("website") or "",
                "domain": root_domain(r.get("website")),
                "phone": r.get("phone_number") or "",
                "full_address": r.get("full_address") or "",
                "city": r.get("city") or "",
                "state": r.get("state") or "",
                "rating": r.get("rating") if r.get("rating") is not None else "",
                "review_count": r.get("review_count") if r.get("review_count") is not None else "",
                "types": "; ".join(r.get("types") or []),
                "is_claimed": r.get("is_claimed"),
                "verified": r.get("verified"),
                "location_count": r["_location_count"],
                "is_multi_location": r["_is_multi"],
                "chain_cities": r["_chain_cities"],
                "place_id": r.get("place_id") or "",
                "business_id": r.get("business_id") or "",
                "latitude": r.get("latitude") if r.get("latitude") is not None else "",
                "longitude": r.get("longitude") if r.get("longitude") is not None else "",
            })

    # ---------- assertions (success criteria) ----------
    pids = [r.get("place_id") for r in final]
    problems = []
    if len(set(pids)) != len(pids):
        problems.append(f"duplicate place_ids: {len(pids) - len(set(pids))}")
    if len(final) > TARGET_TOTAL:
        problems.append(f"row count {len(final)} exceeds cap {TARGET_TOTAL}")
    if any(not root_domain(r.get("website")) for r in final):
        problems.append("rows without a website survived the filter")
    log(f"Cities represented in final CSV: "
        f"{len({r['_city'] for r in final})}/{len(CITIES)}")
    log("ASSERTIONS: " + ("PASS" if not problems else "FAIL -> " + "; ".join(problems)))
    log(f"Wrote {out}")
    log("DONE")


if __name__ == "__main__":
    main()
