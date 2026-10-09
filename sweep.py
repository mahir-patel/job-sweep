#!/usr/bin/env python3
"""Canada Summer 2027 internship sweep.

Pulls postings from (1) the SimplifyJobs Summer 2027 internship list and
(2) public Greenhouse / Lever / Ashby job boards listed in config.json,
keeps Canadian tech internships, and sends a phone push (ntfy) for anything
not seen before. Standard library only.

Usage:
  NTFY_TOPIC=your-secret-topic python sweep.py      # real run
  python sweep.py --dry-run                         # print only, no state, no push
"""
import datetime
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE = ROOT / "seen.json"
MATCHES_MD = ROOT / "MATCHES.md"
DRY = "--dry-run" in sys.argv
UA = "job-sweep/1.0 (personal job alerts)"

SIMPLIFY_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "Summer2027-Internships/dev/.github/scripts/listings.json"
)
ALLOWED_CATS = {
    "AI/ML/Data", "Software", "Quant", "Software Engineering",
    "Data Science, AI & Machine Learning", "Quantitative Finance",
}

PROV = r"(?:ON|BC|AB|QC|MB|SK|NS|NB|NL|PE|PEI)"
CITIES = [
    "toronto", "vancouver", "montreal", "montréal", "ottawa", "waterloo",
    "calgary", "edmonton", "kitchener", "mississauga", "burnaby", "halifax",
    "winnipeg", "quebec", "québec", "markham", "richmond hill", "oakville",
    "kanata", "saskatoon", "regina", "fredericton", "moncton", "guelph",
    "brampton", "vaughan", "gatineau", "sherbrooke", "laval", "victoria, bc",
    "london, on", "hamilton, on", "surrey, bc",
]
ROLE_RE = re.compile(
    r"\b(software|swe|sde|developer|development|engineer(?:ing)?|machine learning|"
    r"ml|ai|artificial intelligence|data|analyst|analytics|quant\w*|security|"
    r"cyber\w*|automation|systems?|devops|sre|cloud|backend|back-end|frontend|"
    r"front-end|full[- ]?stack|platform|infrastructure|research|algorithm\w*|"
    r"trading|qa|test(?:ing)?|network\w*|embedded|firmware|computer vision|nlp|llm)\b",
    re.I,
)
INTERN_RE = re.compile(
    r"\b(intern|interns|internship|co-?op|student|summer analyst)\b", re.I
)
EXCLUDE_RE = re.compile(
    r"\b(marketing|sales|recruit\w*|human resources|accounting|legal|mechanical|"
    r"civil|chemical|nurs\w*|business development|customer success|"
    r"financial advisor|PhD)\b",
    re.I,
)


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def is_canada(loc):
    if not loc:
        return False
    if "canada" in loc.lower():
        return True
    if re.search(r",\s*" + PROV + r"\b", loc):
        return True
    low = loc.lower()
    return any(c in low for c in CITIES)


def year_ok(title):
    """Reject titles that clearly name another year or a non-summer term."""
    if re.search(r"\b(fall|winter|autumn|spring)\b", title, re.I) and not re.search(
        r"summer", title, re.I
    ):
        return False
    if "2027" in title:
        return True
    if re.search(r"\b20(?:2[0-6]|28)\b", title):
        return False
    return True


def to_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def norm_url(u):
    return re.sub(r"[?#].*$", "", (u or "").strip().lower()).rstrip("/")


# ---------------------------------------------------------------- sources
def fetch_simplify():
    out = []
    for x in http_get(SIMPLIFY_URL, timeout=90):
        if not (x.get("active") and x.get("is_visible", True)):
            continue
        locs = [l for l in x.get("locations", []) if is_canada(l)]
        if not locs:
            continue
        title = x.get("title", "")
        terms = x.get("terms") or []
        summer27 = "Summer 2027" in terms
        # No term listed = unclear: keep it (unless the title names another term/year).
        unclear = all(t in ("N/A", "") for t in terms) and year_ok(title)
        if not (summer27 or unclear):
            continue
        if not summer27 and "2027" not in title:
            title += "  (term unclear)"
        if EXCLUDE_RE.search(title):
            continue
        if not (x.get("category") in ALLOWED_CATS or ROLE_RE.search(title)):
            continue
        spons = x.get("sponsorship") or ""
        flag = spons if ("Not" in spons or "Requires" in spons) else ""
        out.append({
            "key": "simplify:" + str(x.get("id")),
            "company": x.get("company_name", "?"),
            "title": title,
            "locs": locs,
            "url": x.get("url", ""),
            "posted": to_ts(x.get("date_posted")),
            "flag": flag,
            "source": "Simplify list",
        })
    return out


def fetch_board(kind, slug):
    """Return (jobs, error_string_or_None) for one company job board."""
    try:
        rows = []
        if kind == "greenhouse":
            d = http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            for j in d.get("jobs", []):
                rows.append((str(j["id"]), j["title"], [(j.get("location") or {}).get("name", "")],
                             j["absolute_url"], to_ts(j.get("first_published") or j.get("updated_at"))))
        elif kind == "lever":
            d = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            for j in d:
                c = j.get("categories") or {}
                locs = c.get("allLocations") or [c.get("location", "")]
                rows.append((j["id"], j["text"], locs, j["hostedUrl"], to_ts(j.get("createdAt"))))
        elif kind == "ashby":
            d = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            for j in d.get("jobs", []):
                locs = [j.get("location", "")] + [
                    s.get("location", "") for s in (j.get("secondaryLocations") or [])
                ]
                rows.append((j["id"], j["title"], locs, j["jobUrl"], to_ts(j.get("publishedAt"))))
        else:
            return [], f"unknown kind {kind}"
    except Exception as e:  # board missing, blocked, timeout...
        return [], f"{kind}/{slug}: {e}"

    names = CONFIG.get("names", {})
    jobs = []
    for jid, title, locs, url, ts in rows:
        if not INTERN_RE.search(title) or not ROLE_RE.search(title):
            continue
        if EXCLUDE_RE.search(title) or not year_ok(title):
            continue
        ca = [l for l in locs if is_canada(l)]
        if not ca:
            continue
        jobs.append({
            "key": f"{kind}:{slug}:{jid}",
            "company": names.get(slug, slug.replace("-", " ").title()),
            "title": title + ("" if "2027" in title else "  (year not stated)"),
            "locs": ca,
            "url": url,
            "posted": ts,
            "flag": "",
            "source": kind,
        })
    return jobs, None


def fetch_ats():
    tasks = [(k, s) for k in ("greenhouse", "lever", "ashby") for s in CONFIG.get(k, [])]
    jobs, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for j, err in ex.map(lambda t: fetch_board(*t), tasks):
            jobs.extend(j)
            if err:
                errors.append(err)
    return jobs, errors, len(tasks)


# ---------------------------------------------------------------- output
def ago(ts):
    if not ts:
        return "posted time unknown"
    m = int((datetime.datetime.now().timestamp() - ts) / 60)
    if m < 90:
        return f"posted {max(m, 0)} min ago"
    if m < 60 * 36:
        return f"posted {m // 60} h ago"
    return f"posted {m // 1440} d ago"


def ntfy(title, message, click=None, priority=4):
    topic = os.environ.get("NTFY_TOPIC")
    if DRY or not topic:
        print(f"[notify] {title}\n         {message}\n         {click}")
        return
    body = {"topic": topic, "title": title[:120], "message": message,
            "priority": priority, "tags": ["briefcase"]}
    if click:
        body["click"] = click
    if os.environ.get("NTFY_EMAIL"):
        body["email"] = os.environ["NTFY_EMAIL"]
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def describe(j):
    parts = [", ".join(j["locs"][:3]), ago(j["posted"])]
    if j["flag"]:
        parts.append("Note: " + j["flag"])
    return " | ".join(parts)


def write_matches(jobs):
    jobs = sorted(jobs, key=lambda j: j["posted"] or 0, reverse=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Current matches ({len(jobs)})", "", f"Updated {now}", "",
             "| Company | Role | Location | Posted | Note | Apply |", "|---|---|---|---|---|---|"]
    for j in jobs:
        esc = lambda s: s.replace("|", "/")
        lines.append(f"| {esc(j['company'])} | {esc(j['title'])} | {esc(', '.join(j['locs'][:2]))} "
                     f"| {ago(j['posted'])} | {esc(j['flag'])} | [link]({j['url']}) |")
    MATCHES_MD.write_text("\n".join(lines) + "\n")


def main():
    jobs, errors, n_boards = [], [], 0
    try:
        jobs += fetch_simplify()
    except Exception as e:
        errors.append(f"simplify list: {e}")
    ats, ats_err, n_boards = fetch_ats()
    jobs += ats
    errors += ats_err

    # de-duplicate (same posting can come from the list and the company board)
    uniq, urls = {}, set()
    for j in jobs:
        u = norm_url(j["url"])
        if j["key"] in uniq or (u and u in urls):
            continue
        uniq[j["key"]] = j
        urls.add(u)
    jobs = list(uniq.values())

    print(f"matches={len(jobs)} boards_checked={n_boards} board_errors={len(errors)}")
    for e in errors[:40]:
        print("  source error:", e[:160])
    if not jobs and errors:
        sys.exit("No data from any source")

    first_run = not STATE.exists()
    seen = set(json.loads(STATE.read_text())["seen"]) if not first_run else set()
    new = [j for j in jobs if j["key"] not in seen and norm_url(j["url"]) not in seen]
    new.sort(key=lambda j: j["posted"] or 0, reverse=True)

    repo = os.environ.get("GITHUB_REPOSITORY")
    list_url = f"https://github.com/{repo}/blob/main/MATCHES.md" if repo else None

    if first_run:
        ntfy("Job sweep is live",
             f"{len(jobs)} Canadian Summer 2027 matches already posted. Full list in MATCHES.md. "
             "From now on you only get alerts for new postings.",
             click=list_url, priority=3)
    elif new:
        for j in new[:5]:
            ntfy(f"NEW: {j['company']} - {j['title']}", describe(j), click=j["url"], priority=4)
        if len(new) > 5:
            rest = "\n".join(f"{j['company']}: {j['title']}" for j in new[5:15])
            ntfy(f"+{len(new) - 5} more new postings", rest, click=list_url, priority=3)
    print(f"new={len(new)}")

    if not DRY:
        seen.update(j["key"] for j in jobs)
        seen.update(norm_url(j["url"]) for j in jobs if j["url"])
        STATE.write_text(json.dumps({"seen": sorted(seen)}, indent=0))
        write_matches(jobs)
    else:
        for j in sorted(jobs, key=lambda j: j["posted"] or 0, reverse=True)[:8]:
            print(f"  {j['company']} | {j['title']} | {', '.join(j['locs'][:2])} | {ago(j['posted'])}")


if __name__ == "__main__":
    main()
