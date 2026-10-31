#!/usr/bin/env python3
"""Job agent.

1. Fetches jobs from free public APIs
2. Merges them into docs/jobs.json (the data behind the github.io page)
3. Posts NEW jobs that match your Discord filters (config.json) to a Discord webhook

Uses only the Python standard library, so there is nothing to pip install.

Usage:
    python3 fetch_jobs.py             # normal run (what GitHub Actions does)
    python3 fetch_jobs.py --dry-run   # print what would be sent, change nothing
    python3 fetch_jobs.py --force     # ignore per-source min_interval_hours
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
JOBS_PATH = ROOT / "docs" / "jobs.json"
SEEN_PATH = ROOT / "data" / "seen.json"
STATE_PATH = ROOT / "data" / "state.json"
UA = "job-agent/1.0 (personal job board)"


# ----------------------------------------------------------------- helpers
def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value):
    """Turn an epoch number or ISO string into an aware UTC datetime (or None)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        s = re.sub(r"\.\d+", "", str(value).strip()).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OverflowError, OSError):
        return None


TAG_RE = re.compile(r"<[^>]+>")


def clean(text, limit=300):
    """Strip HTML, collapse whitespace, truncate."""
    if not text:
        return ""
    text = re.sub(r"(?i)<(br|/p|/li|/div|/h\d)[^>]*>", " ", str(text))
    text = html.unescape(TAG_RE.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def pretty_type(value):
    if not value:
        return ""
    return str(value).replace("_", " ").replace("-", " ").strip().capitalize()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)


def get_json(url, retries=2, timeout=30):
    last = None
    for attempt in range(retries + 1):
        try:
            req = request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("%s -> %s" % (url, last))


def make_job(source, raw_id, title, company, url, **kw):
    """Normalise a job from any source into one shape. Returns None if unusable."""
    if raw_id in (None, "") or not title or not url:
        return None
    remote = bool(kw.get("remote"))
    location = clean(kw.get("location"), 120)
    dt = parse_dt(kw.get("posted"))
    tags = [clean(t, 40) for t in (kw.get("tags") or []) if t]
    return {
        "id": "%s:%s" % (source, raw_id),
        "source": source,
        "title": clean(title, 200),
        "company": clean(company, 120) or "Unknown company",
        "location": location or ("Remote" if remote else "Not specified"),
        "remote": remote,
        "type": kw.get("type") or "",
        "category": clean(kw.get("category"), 60),
        "tags": tags[:8],
        "salary": clean(kw.get("salary"), 80),
        "url": str(url),
        "posted": iso(dt) if dt else None,
        "summary": clean(kw.get("description"), 300),
    }


# ----------------------------------------------------------------- sources
# Each fetcher returns a list of normalised jobs. Parsing is defensive (.get
# everywhere) so a small change in an API's response doesn't crash the run.
def fetch_remotive(cfg):
    data = get_json("https://remotive.com/api/remote-jobs")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(make_job(
            "remotive", j.get("id"), j.get("title"), j.get("company_name"), j.get("url"),
            location=j.get("candidate_required_location"), remote=True,
            type=pretty_type(j.get("job_type")), category=j.get("category"),
            tags=j.get("tags"), salary=j.get("salary"),
            posted=j.get("publication_date"), description=j.get("description")))
    return jobs


def fetch_remoteok(cfg):
    data = get_json("https://remoteok.com/api")
    jobs = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict) or "id" not in j:  # first element is a legal notice
            continue
        salary = ""
        lo, hi = j.get("salary_min"), j.get("salary_max")
        if lo and hi:
            salary = "$%s - $%s" % (format(int(lo), ","), format(int(hi), ","))
        jobs.append(make_job(
            "remoteok", j.get("id"), j.get("position"), j.get("company"), j.get("url"),
            location=j.get("location"), remote=True, tags=j.get("tags"),
            salary=salary, posted=j.get("date") or j.get("epoch"),
            description=j.get("description")))
    return jobs


def fetch_arbeitnow(cfg):
    jobs = []
    for page in range(1, int(cfg.get("pages", 2)) + 1):
        data = get_json("https://www.arbeitnow.com/api/job-board-api?page=%d" % page)
        for j in data.get("data", []):
            types = j.get("job_types") or []
            jobs.append(make_job(
                "arbeitnow", j.get("slug"), j.get("title"), j.get("company_name"), j.get("url"),
                location=j.get("location"), remote=bool(j.get("remote")),
                type=pretty_type(types[0]) if types else "", tags=j.get("tags"),
                posted=j.get("created_at"), description=j.get("description")))
        time.sleep(1)
    return jobs


def fetch_themuse(cfg):
    jobs = []
    for page in range(1, int(cfg.get("pages", 3)) + 1):
        data = get_json("https://www.themuse.com/api/public/jobs?page=%d" % page)
        for j in data.get("results", []):
            locs = [l.get("name", "") for l in j.get("locations", [])]
            levels = [l.get("name", "") for l in j.get("levels", [])]
            cats = [c.get("name", "") for c in j.get("categories", [])]
            remote = any(("remote" in l.lower() or "flexible" in l.lower()) for l in locs)
            jobs.append(make_job(
                "themuse", j.get("id"), j.get("name"), (j.get("company") or {}).get("name"),
                (j.get("refs") or {}).get("landing_page"),
                location=", ".join(locs[:2]), remote=remote,
                category=cats[0] if cats else "", tags=levels + cats[1:],
                posted=j.get("publication_date"), description=j.get("contents")))
        if page >= int(data.get("page_count", page)):
            break
        time.sleep(1)
    return jobs


SOURCES = {
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "arbeitnow": fetch_arbeitnow,
    "themuse": fetch_themuse,
}


def run_sources(cfg, state, force):
    fetched = []
    for name, fn in SOURCES.items():
        scfg = cfg.get("sources", {}).get(name, {})
        if not scfg.get("enabled", True):
            continue
        last = parse_dt(state.get(name))
        min_gap = timedelta(hours=scfg.get("min_interval_hours", 1)) - timedelta(minutes=10)
        if not force and last and now_utc() - last < min_gap:
            print("[%s] skipped (fetched recently)" % name)
            continue
        try:
            jobs = [j for j in fn(scfg) if j]
            fetched.extend(jobs)
            state[name] = iso(now_utc())
            print("[%s] fetched %d jobs" % (name, len(jobs)))
        except Exception as exc:  # one broken source must not stop the others
            print("[%s] FAILED: %s" % (name, exc), file=sys.stderr)
    return fetched


# ----------------------------------------------------------------- filtering
def _has_word(text, word):
    word = word.strip().lower()
    if not word:
        return False
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text) is not None


def matches(job, f):
    """Discord filter from config.json -> 'discord'. Whole-word, case-insensitive."""
    blob = " ".join([job["title"], job["category"], " ".join(job["tags"])]).lower()
    inc = f.get("include_keywords") or []
    exc = f.get("exclude_keywords") or []
    if inc and not any(_has_word(blob, k) for k in inc):
        return False
    if any(_has_word(job["title"].lower(), k) for k in exc):
        return False
    if f.get("remote_only") and not job["remote"]:
        return False
    locs = [l.lower() for l in (f.get("locations") or []) if l.strip()]
    if locs and not any(l in job["location"].lower() for l in locs):
        return False
    return True


# ----------------------------------------------------------------- discord
def valid_webhook(url):
    return bool(url) and re.match(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$", url) is not None


def discord_send(webhook, payload):
    data = json.dumps(payload).encode("utf-8")
    for _ in range(3):
        req = request.Request(webhook, data=data, method="POST",
                              headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with request.urlopen(req, timeout=30):
                return True
        except error.HTTPError as exc:
            if exc.code == 429:
                try:
                    wait = float(json.loads(exc.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait, 30) + 0.5)
                continue
            print("Discord error HTTP %d" % exc.code, file=sys.stderr)
            return False
        except error.URLError as exc:
            print("Discord network error: %s" % exc, file=sys.stderr)
            time.sleep(2)
    return False


def job_embed(job):
    bits = [job["company"], job["location"]]
    if job["salary"]:
        bits.append(job["salary"])
    embed = {
        "title": job["title"][:250],
        "url": job["url"],
        "description": " · ".join(bits)[:200],
        "color": 0x5865F2,
        "footer": {"text": job["source"]},
    }
    if job.get("posted"):
        embed["timestamp"] = job["posted"]
    return embed


def post_jobs(webhook, jobs, total, dashboard_url):
    for i in range(0, len(jobs), 10):  # Discord allows 10 embeds per message
        chunk = jobs[i:i + 10]
        payload = {"username": "Job Agent", "embeds": [job_embed(j) for j in chunk]}
        if i == 0:
            text = "**%d new job%s** matching your filters" % (total, "" if total == 1 else "s")
            if total > len(jobs):
                text += " (showing the newest %d)" % len(jobs)
            if dashboard_url:
                text += "\nAll jobs & filters: <%s>" % dashboard_url
            payload["content"] = text
        if not discord_send(webhook, payload):
            return False
        time.sleep(1.2)
    return True


def dashboard_url_from_env(cfg):
    if cfg.get("dashboard_url"):
        return cfg["dashboard_url"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if name.lower() == owner.lower() + ".github.io":
            return "https://%s.github.io/" % owner.lower()
        return "https://%s.github.io/%s/" % (owner.lower(), name)
    return ""


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't post or save anything")
    ap.add_argument("--force", action="store_true", help="ignore min_interval_hours")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    dcfg = cfg.get("discord", {})
    keep_days = int(cfg.get("keep_days", 30))
    max_jobs = int(cfg.get("max_jobs", 3000))
    cutoff = now_utc() - timedelta(days=keep_days)

    state = load_json(STATE_PATH, {})
    seen = load_json(SEEN_PATH, {})
    store = {j["id"]: j for j in load_json(JOBS_PATH, {}).get("jobs", [])}

    fetched = run_sources(cfg, state, args.force)
    fetched = list({j["id"]: j for j in fetched}.values())  # same job twice in one run -> once

    # Merge into the stored list (jobs from skipped/failed sources are kept)
    now_s = iso(now_utc())
    fresh = []
    for job in fetched:
        posted_dt = parse_dt(job["posted"])
        if posted_dt and posted_dt < cutoff:
            continue  # too old to care about
        old = store.get(job["id"], {})
        job["posted"] = job["posted"] or old.get("posted") or now_s
        job["first_seen"] = old.get("first_seen") or now_s
        store[job["id"]] = job
        fresh.append(job)

    kept = [j for j in store.values() if (parse_dt(j["posted"]) or now_utc()) >= cutoff]
    kept.sort(key=lambda j: j["posted"], reverse=True)
    kept = kept[:max_jobs]

    # New = never seen before. Of those, the ones matching your Discord filter get posted.
    new_jobs = [j for j in fresh if j["id"] not in seen]
    to_notify = sorted((j for j in new_jobs if matches(j, dcfg)),
                       key=lambda j: j["posted"], reverse=True)
    batch = to_notify[: int(dcfg.get("max_per_run", 10))]
    print("%d fetched, %d new, %d match Discord filters, %d to post"
          % (len(fetched), len(new_jobs), len(to_notify), len(batch)))

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    dash = dashboard_url_from_env(cfg)
    posted_ok = True

    if args.dry_run:
        for j in batch:
            print("  would post: %s | %s | %s" % (j["title"], j["company"], j["location"]))
    elif webhook and not valid_webhook(webhook):
        print("DISCORD_WEBHOOK_URL doesn't look like a Discord webhook URL; not posting.", file=sys.stderr)
    elif webhook:
        if batch:
            posted_ok = post_jobs(webhook, batch, len(to_notify), dash)
        elif dcfg.get("post_when_empty"):
            msg = "No new matching jobs this hour. %d jobs on the dashboard." % len(kept)
            if dash:
                msg += "\n<%s>" % dash
            discord_send(webhook, {"username": "Job Agent", "content": msg})
    else:
        print("No DISCORD_WEBHOOK_URL set; skipping Discord.")

    if args.dry_run:
        return

    # If posting failed, leave those jobs unseen so they are retried next hour.
    skip_ids = set(j["id"] for j in to_notify) if not posted_ok else set()
    today = now_utc().strftime("%Y-%m-%d")
    for j in new_jobs:
        if j["id"] not in skip_ids:
            seen[j["id"]] = today
    old_cut = (now_utc() - timedelta(days=keep_days * 2)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= old_cut}

    save_json(JOBS_PATH, {"updated": now_s, "count": len(kept), "jobs": kept}, compact=True)
    save_json(SEEN_PATH, seen)
    save_json(STATE_PATH, state)
    print("Saved %d jobs." % len(kept))


if __name__ == "__main__":
    main()
