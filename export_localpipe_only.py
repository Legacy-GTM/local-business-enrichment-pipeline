"""
Deliverable: everything through stage 3 ONLY -- ScraperTech scrape + chain flags
+ LocalPipe enrichment. Deliberately excludes AI Ark / Prospeo (stage 4) so this
file shows exactly what LocalPipe alone produced.

  Downloads/general-contractors-localpipe-stage3.csv

lp_tier:
  1_EMAIL_FOUND          LocalPipe returned an email (owner preferred, else business)
  2_NAME_ONLY_NO_EMAIL   LocalPipe found the owner's name but no email
  3_EMPTY                LocalPipe found nothing
  4_NOT_ENRICHED_YET     not submitted to LocalPipe
"""
import csv, os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
OUT = os.path.join(DOWNLOADS, "general-contractors-localpipe-stage3.csv")

SRC_LIST = os.path.join(HERE, "general-contractors.csv")
SRC_LP = os.path.join(HERE, "localpipe_results.csv")

COLS = ["lp_tier", "business_name", "business_location", "business_website",
        "domain", "phone", "full_address", "city", "state", "rating", "review_count",
        "types", "is_claimed", "verified",
        "location_count", "is_multi_location", "chain_cities",
        "owner_name", "owner_first_name", "owner_last_name",
        "owner_email", "business_email", "best_email", "email_source",
        "email_provider", "owner_role", "lp_status",
        "place_id", "business_id", "latitude", "longitude"]


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    os.makedirs(DOWNLOADS, exist_ok=True)
    businesses = read(SRC_LIST)
    lp = {r["place_id"]: r for r in read(SRC_LP)}
    print(f"Source: {len(businesses)} businesses | LocalPipe rows {len(lp)}")

    out = []
    for b in businesses:
        e = lp.get(b["place_id"])
        owner_email = ((e or {}).get("owner_email") or "").strip()
        biz_email = ((e or {}).get("business_email") or "").strip()
        owner_name = ((e or {}).get("owner_name") or "").strip()

        if owner_email:
            best, src = owner_email, "localpipe_owner"
        elif biz_email:
            best, src = biz_email, "localpipe_business"
        else:
            best, src = "", ""

        if not e:
            tier = "4_NOT_ENRICHED_YET"
        elif best:
            tier = "1_EMAIL_FOUND"
        elif owner_name:
            tier = "2_NAME_ONLY_NO_EMAIL"
        else:
            tier = "3_EMPTY"

        row = {c: b.get(c, "") for c in COLS if c in b}
        row.update({
            "lp_tier": tier,
            "owner_name": owner_name,
            "owner_first_name": ((e or {}).get("owner_first_name") or "").strip(),
            "owner_last_name": ((e or {}).get("owner_last_name") or "").strip(),
            "owner_email": owner_email,
            "business_email": biz_email,
            "best_email": best,
            "email_source": src,
            "email_provider": ((e or {}).get("email_provider") or "").strip(),
            "owner_role": ((e or {}).get("owner_role") or "").strip(),
            "lp_status": ((e or {}).get("lp_status") or "not_enriched_yet").strip(),
        })
        out.append(row)

    out.sort(key=lambda r: (r["lp_tier"], r["business_location"], r["business_name"]))

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in out:
            w.writerow({c: r.get(c, "") for c in COLS})

    tiers = Counter(r["lp_tier"] for r in out)
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
        if r["lp_tier"] == "1_EMAIL_FOUND" and not r["best_email"]:
            problems.append("tier 1 without email"); break
        if r["lp_tier"] == "2_NAME_ONLY_NO_EMAIL" and (r["best_email"] or not r["owner_name"]):
            problems.append("tier 2 mislabelled"); break
        if r["lp_tier"] == "3_EMPTY" and (r["best_email"] or r["owner_name"]):
            problems.append("tier 3 with data"); break
    # this file must contain NO stage-4 data
    if any(k.startswith(("aiark", "prospeo")) for k in COLS):
        problems.append("stage-4 column leaked into the LocalPipe-only file")
    print("\nASSERTIONS: " + ("PASS" if not problems else "FAIL -> " + "; ".join(problems)))


if __name__ == "__main__":
    main()
