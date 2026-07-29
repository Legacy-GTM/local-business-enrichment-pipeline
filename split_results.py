"""
Build ONE deliverable CSV in Downloads: every business, enriched and not,
merging LocalPipe (stage 3) with the AI Ark -> Prospeo waterfall (stage 4).

  Downloads/general-contractors-enriched.csv

result_tier, in file order:
  1_EMAIL_FOUND          an email was found by any source
  2_NAME_ONLY_NO_EMAIL   a decision-maker name is known but no email
  3_EMPTY                nothing found -- fill these yourself
  4_NOT_ENRICHED_YET     not processed (present so no row is ever dropped)

email_source: localpipe_owner | localpipe_business | aiark | prospeo
"""
import csv, os, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
# Region selection mirrors scrape.py; US filenames stay unsuffixed.
PREFIXES = {"us": "general-contractors", "ca": "general-contractors-canada"}
REGION = "us"
if "--region" in sys.argv:
    REGION = sys.argv[sys.argv.index("--region") + 1].lower()
if REGION not in PREFIXES:
    sys.exit(f"Unknown --region '{REGION}'. Available: {', '.join(PREFIXES)}")
PREFIX = PREFIXES[REGION]
SUF = "" if REGION == "us" else f"-{REGION}"

OUT = os.path.join(DOWNLOADS, f"{PREFIX}-enriched.csv")

SRC_LIST = os.path.join(HERE, f"{PREFIX}.csv")
SRC_LP = os.path.join(HERE, f"localpipe_results{SUF}.csv")
SRC_WF = os.path.join(HERE, f"waterfall_results{SUF}.csv")

# A business whose "website" is a shared host (facebook.com, sites.google.com,
# instagram.com...) must NEVER be used as a company-domain lookup key: AI Ark and
# Prospeo then return employees of the HOST. That is how Polar Home Renovation
# (sites.google.com) came back as mikejohnston@google.com. Purge those.
SHARED_HOSTS = {
    "facebook.com", "m.facebook.com", "sites.google.com", "google.com",
    "business.site", "wixsite.com", "wix.com", "godaddysites.com", "weebly.com",
    "squarespace.com", "blogspot.com", "wordpress.com", "myshopify.com",
    "linktr.ee", "yelp.com", "instagram.com", "nextdoor.com", "angi.com",
    "houzz.com", "thumbtack.com",
}

COLS = ["result_tier", "business_name", "business_location", "business_website",
        "domain", "phone", "full_address", "city", "state", "rating", "review_count",
        "location_count", "is_multi_location", "chain_cities",
        "contact_name", "contact_linkedin",
        "best_email", "email_source", "email_domain_match", "email_provider",
        "owner_name", "owner_email", "business_email",
        "aiark_name", "aiark_email", "prospeo_email", "place_id"]


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    os.makedirs(DOWNLOADS, exist_ok=True)
    businesses = read(SRC_LIST)
    lp = {r["place_id"]: r for r in read(SRC_LP)}
    wf = {r["place_id"]: r for r in read(SRC_WF)}
    print(f"Source: {len(businesses)} businesses | LocalPipe {len(lp)} | waterfall {len(wf)}")

    out = []
    for b in businesses:
        e = lp.get(b["place_id"], {})
        w = wf.get(b["place_id"], {})

        owner_email = (e.get("owner_email") or "").strip()
        biz_email = (e.get("business_email") or "").strip()
        ark_email = (w.get("aiark_email") or "").strip()
        pro_email = (w.get("prospeo_email") or "").strip()

        # Discard stage-4 emails sourced from a shared-host domain (see above).
        if (b.get("domain") or "").lower() in SHARED_HOSTS:
            ark_email = pro_email = ""

        # Precedence: verified owner email > AI Ark > Prospeo > generic business
        if owner_email:
            best, src = owner_email, "localpipe_owner"
        elif ark_email:
            best, src = ark_email, "aiark"
        elif pro_email:
            best, src = pro_email, "prospeo"
        elif biz_email:
            best, src = biz_email, "localpipe_business"
        else:
            best, src = "", ""

        owner_name = (e.get("owner_name") or "").strip()
        ark_name = (w.get("aiark_name") or "").strip()
        contact = owner_name or ark_name

        if not e and not w:
            tier = "4_NOT_ENRICHED_YET"
        elif best:
            tier = "1_EMAIL_FOUND"
        elif contact:
            tier = "2_NAME_ONLY_NO_EMAIL"
        else:
            tier = "3_EMPTY"

        row = {c: b.get(c, "") for c in COLS if c in b}
        row.update({
            "result_tier": tier,
            "contact_name": contact,
            "contact_linkedin": (w.get("aiark_linkedin") or "").strip(),
            "best_email": best,
            "email_source": src,
            # QA flag: "no" means the address sits on a different domain than the
            # business site. Often a legitimate alternate corporate domain
            # (tcco.com for Turner), sometimes a wrong match -- eyeball before sending.
            "email_domain_match": ("" if not best else
                                   ("yes" if best.split("@")[-1].lower()
                                    == (b.get("domain") or "").lower() else "no")),
            "email_provider": (e.get("email_provider") or "").strip(),
            "owner_name": owner_name,
            "owner_email": owner_email,
            "business_email": biz_email,
            "aiark_name": ark_name,
            "aiark_email": ark_email,
            "prospeo_email": pro_email,
        })
        out.append(row)

    out.sort(key=lambda r: (r["result_tier"], r["business_location"], r["business_name"]))

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w_ = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w_.writeheader()
        for r in out:
            w_.writerow({c: r.get(c, "") for c in COLS})

    tiers = Counter(r["result_tier"] for r in out)
    srcs = Counter(r["email_source"] for r in out if r["email_source"])
    print(f"\nWrote {OUT}\n")
    for t in sorted(tiers):
        print(f"  {t:<24} {tiers[t]:>4}")
    print("\n  emails by source:")
    for s, n in srcs.most_common():
        print(f"    {s:<22} {n:>4}")

    problems = []
    if len(out) != len(businesses):
        problems.append(f"{len(out)} out vs {len(businesses)} in")
    if len({r['place_id'] for r in out}) != len(out):
        problems.append("duplicate place_id")
    for r in out:
        if r["result_tier"] == "1_EMAIL_FOUND" and not r["best_email"]:
            problems.append("tier 1 without email"); break
        if r["result_tier"] == "3_EMPTY" and (r["best_email"] or r["contact_name"]):
            problems.append("tier 3 with data"); break
        if r["best_email"] and "*" in r["best_email"]:
            problems.append("masked email leaked into best_email"); break
    print("\nASSERTIONS: " + ("PASS" if not problems else "FAIL -> " + "; ".join(problems)))


if __name__ == "__main__":
    main()
