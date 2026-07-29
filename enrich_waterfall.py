"""
Stage 4: waterfall for every business LocalPipe left without an email.

  AI Ark  -> people search by domain (NO seniority filter -- filtering on it
             returned 0/10 while unfiltered returned 3/10), rank candidates by
             seniority, then /v2/people/export/single for the email (1 credit
             only when found).
  Prospeo -> if still no email: enrich-person using the best known name
             (LocalPipe's owner_name preferred, else AI Ark's) + company website,
             or a LinkedIn URL when that's all we have. Free on no-match.
  else    -> left empty for the user to handle.

Every AI Ark request carries a browser User-Agent: the default Python UA is
rejected by Cloudflare with "error code: 1010" (HTTP 403), which looks exactly
like an auth failure but is not.

Run:  python enrich_waterfall.py --limit 10     # sample
      python enrich_waterfall.py                # all remaining
Out:  waterfall_progress.jsonl   appended per row, resume-safe
      waterfall_results.csv      parsed results
      waterfall.log
"""
import csv, json, os, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
AIARK = "https://api.ai-ark.com/api/developer-portal"
PROSPEO = "https://api.prospeo.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Region selection mirrors scrape.py; US filenames stay unsuffixed.
PREFIXES = {"us": "general-contractors", "ca": "general-contractors-canada"}
REGION = "us"
if "--region" in sys.argv:
    REGION = sys.argv[sys.argv.index("--region") + 1].lower()
if REGION not in PREFIXES:
    sys.exit(f"Unknown --region '{REGION}'. Available: {', '.join(PREFIXES)}")
PREFIX = PREFIXES[REGION]
SUF = "" if REGION == "us" else f"-{REGION}"

IN_CSV = os.path.join(HERE, f"{PREFIX}.csv")
LP_CSV = os.path.join(HERE, f"localpipe_results{SUF}.csv")
PROG = os.path.join(HERE, f"waterfall_progress{SUF}.jsonl")
OUT_CSV = os.path.join(HERE, f"waterfall_results{SUF}.csv")
LOG = os.path.join(HERE, f"waterfall{SUF}.log")

CONCURRENCY = 4        # AI Ark allows 5 req/s, 300/min -- stay under
MIN_INTERVAL = 0.25

# Best decision-maker first. AI Ark returns whoever it has for small firms, so
# this ranks rather than filters.
SENIORITY_RANK = ["owner", "founder", "c_suite", "partner", "vp", "head",
                  "director", "manager", "senior", "entry", "unpaid", "training"]

_lock = threading.Lock()
_last = [0.0]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env():
    env = {}
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        sys.exit("No .env file found.\n"
                 "  1. cp .env.example .env\n"
                 "  2. paste your API keys into .env\n"
                 "See the README for where to get each key.")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def throttle():
    with _lock:
        wait = MIN_INTERVAL - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def req(url, headers, payload=None, method="POST", timeout=90, attempt=0):
    """Returns (code, body). code 0 == transient network error."""
    throttle()
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, headers=headers,
                               method=method if data else "GET")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        if e.code == 429 and attempt < 6:
            time.sleep(2 * (attempt + 1))
            return req(url, headers, payload, method, timeout, attempt + 1)
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        if attempt < 4:
            time.sleep(2 * (attempt + 1))
            return req(url, headers, payload, method, timeout, attempt + 1)
        return 0, {"_net_error": f"{type(e).__name__}: {e}"}


def rank_person(p):
    sen = ((p.get("department") or {}).get("seniority") or "").lower()
    try:
        return SENIORITY_RANK.index(sen)
    except ValueError:
        return len(SENIORITY_RANK)


def aiark_lookup(domain, H):
    """Search people at this domain, pick the most senior, fetch their email."""
    code, d = req(f"{AIARK}/v1/people", H,
                  {"account": {"domain": {"any": {"include": [domain]}}},
                   "page": 0, "size": 5})
    if code != 200 or not isinstance(d, dict):
        return {"aiark_error": f"search HTTP {code}"}
    people = d.get("content") or []
    if not people:
        return {"aiark_people": 0}

    best = sorted(people, key=rank_person)[0]
    prof = best.get("profile") or {}
    out = {
        "aiark_people": d.get("totalElements", len(people)),
        "aiark_name": prof.get("full_name") or "",
        "aiark_first": prof.get("first_name") or "",
        "aiark_last": prof.get("last_name") or "",
        "aiark_linkedin": (best.get("link") or {}).get("linkedin") or "",
        "aiark_seniority": (best.get("department") or {}).get("seniority") or "",
    }

    pid = best.get("id")
    if not pid:
        return out
    code, e = req(f"{AIARK}/v2/people/export/single", H, {"id": pid})
    if code == 200 and isinstance(e, dict):
        # v2 wraps in {status, error, data}; email at data.email.value
        data = e.get("data") or e
        em = data.get("email") or {}
        addr = em.get("value") or em.get("address") or ""
        if not addr:
            outp = em.get("output") or []
            if outp and isinstance(outp, list):
                addr = (outp[0] or {}).get("address", "")
        if addr:
            out["aiark_email"] = addr
    return out


def prospeo_lookup(first, last, domain, linkedin, key):
    """1 credit only when an email is found; no charge on NO_MATCH."""
    H = {"Content-Type": "application/json", "X-KEY": key, "User-Agent": UA}
    extra = {}
    if first and last and domain:
        data = {"first_name": first, "last_name": last, "company_website": domain}
    elif linkedin:
        data = {"linkedin_url": linkedin}
    elif domain:
        # No name anywhere. Search Prospeo by company website for a person_id,
        # then enrich that. Costs 1 credit only if the search returns someone;
        # NO_RESULTS is free.
        code, b = req(f"{PROSPEO}/search-person", H,
                      {"page": 1, "filters": {"company": {"websites": {"include": [domain]}}}})
        if code != 200 or not isinstance(b, dict) or b.get("error"):
            ec = b.get("error_code") if isinstance(b, dict) else code
            return {"prospeo_error": f"search {ec}"}
        results = b.get("results") or []
        if not results:
            return {"prospeo_error": "search NO_RESULTS"}
        person = results[0].get("person") or {}
        ppid = person.get("person_id") or person.get("id")
        if not ppid:
            return {"prospeo_error": "search returned no person_id"}
        extra = {"prospeo_searched_name": person.get("full_name", "")}
        data = {"person_id": ppid}
    else:
        return {"prospeo_skipped": "no name, no linkedin, no domain"}

    code, b = req(f"{PROSPEO}/enrich-person", H, {"data": data})
    if code != 200 or not isinstance(b, dict):
        err = b.get("error_code") if isinstance(b, dict) else str(b)[:60]
        return {**extra, "prospeo_error": f"HTTP {code} {err}"}
    if b.get("error"):
        return {**extra, "prospeo_error": b.get("error_code", "unknown")}
    person = b.get("person") or {}
    em = person.get("email") or {}
    addr = em.get("email") or ""
    # Prospeo masks unrevealed addresses with '*' -- those are not usable.
    if addr and "*" in addr:
        return {**extra, "prospeo_masked": addr, "prospeo_status": em.get("status", "")}
    return {**extra, "prospeo_email": addr, "prospeo_status": em.get("status", ""),
            "prospeo_name": person.get("full_name", "")}


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    open(LOG, "a").close()
    env = load_env()
    H_ark = {"Content-Type": "application/json", "X-TOKEN": env["AIARK_KEY"],
             "User-Agent": UA, "Accept": "*/*"}
    pkey = env["PROSPEO_KEY"]

    with open(IN_CSV, encoding="utf-8-sig") as f:
        biz = list(csv.DictReader(f))
    lp = {}
    with open(LP_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            lp[r["place_id"]] = r

    done = set()
    if os.path.exists(PROG):
        with open(PROG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        done.add(json.loads(line)["place_id"])
                    except Exception:
                        continue

    targets = []
    for b in biz:
        e = lp.get(b["place_id"], {})
        if (e.get("owner_email") or "").strip() or (e.get("business_email") or "").strip():
            continue                      # LocalPipe already delivered
        if b["place_id"] in done:
            continue                      # already processed by a previous run
        targets.append(b)
    if limit:
        targets = targets[:limit]

    log(f"Waterfall targets: {len(targets)} "
        f"({len(done)} already processed, concurrency {CONCURRENCY})")

    counter = [0]

    def work(b):
        pid = b["place_id"]
        e = lp.get(pid, {})
        rec = {"place_id": pid, "business_name": b["business_name"],
               "domain": b["domain"], "lp_owner_name": e.get("owner_name", "")}

        rec.update(aiark_lookup(b["domain"], H_ark))

        email = rec.get("aiark_email", "")
        source = "aiark" if email else ""

        if not email:
            # Prefer LocalPipe's name (it verified the business), else AI Ark's.
            lp_name = (e.get("owner_name") or "").strip()
            first = (e.get("owner_first_name") or "").strip()
            last = (e.get("owner_last_name") or "").strip()
            if not (first and last) and lp_name and len(lp_name.split()) >= 2:
                first, last = lp_name.split()[0], lp_name.split()[-1]
            if not (first and last):
                first, last = rec.get("aiark_first", ""), rec.get("aiark_last", "")
            rec.update(prospeo_lookup(first, last, b["domain"],
                                      rec.get("aiark_linkedin", ""), pkey))
            if rec.get("prospeo_email"):
                email = rec["prospeo_email"]
                source = "prospeo"

        rec["final_email"] = email
        rec["final_source"] = source
        with _lock:
            with open(PROG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            counter[0] += 1
            n = counter[0]
        if n % 10 == 0 or email:
            log(f"  [{n}/{len(targets)}] {b['business_name'][:30]:<30} "
                f"{source or '-':<8} {email or '-'}")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        list(ex.map(work, targets))

    # ---------- write results ----------
    recs = []
    with open(PROG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    cols = ["place_id", "business_name", "domain", "lp_owner_name",
            "aiark_people", "aiark_name", "aiark_seniority", "aiark_linkedin",
            "aiark_email", "prospeo_email", "prospeo_status", "prospeo_error",
            "final_email", "final_source"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            w.writerow({c: r.get(c, "") for c in cols})

    n_ark = sum(1 for r in recs if r.get("final_source") == "aiark")
    n_pro = sum(1 for r in recs if r.get("final_source") == "prospeo")
    n_any = sum(1 for r in recs if r.get("final_email"))
    log(f"RESULT  processed {len(recs)}  emails {n_any}  "
        f"(aiark {n_ark}, prospeo {n_pro})  still empty {len(recs) - n_any}")
    log(f"Wrote {OUT_CSV}")
    log("DONE")


if __name__ == "__main__":
    main()
