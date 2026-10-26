"""Funding Radar scraper.

Reads sources.yaml, collects funding / program opportunities from official
Canadian funder websites and writes docs/data/opportunities.json, which the
static page in docs/index.html renders.

Run:  python scraper/scrape.py            (all sources)
      python scraper/scrape.py nserc cfi  (only these source ids, for testing)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, urlsplit, urlunsplit, parse_qsl

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.yaml"
DATA_FILE = ROOT / "docs" / "data" / "opportunities.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; FundingRadar/1.0; weekly opportunity digest for "
    "Canadian tech-transfer offices; +https://springboardatlantic.ca)"
)
REQUEST_DELAY = 1.5          # seconds between requests to the same host
MAX_DETAIL_FETCHES = 25      # per source per run (only for newly seen items)
DROP_INACTIVE_AFTER_DAYS = 45

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-CA,en;q=0.8"})
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def allowed_by_robots(url: str) -> bool:
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=15)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                _robots[base] = rp
            else:
                _robots[base] = None
        except requests.RequestException:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def fetch(url: str) -> requests.Response:
    if not allowed_by_robots(url):
        raise RuntimeError("disallowed by robots.txt")
    host = urlparse(url).netloc
    wait = REQUEST_DELAY - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            _last_hit[host] = time.time()
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            if r.encoding and r.encoding.lower() == "iso-8859-1" and "charset" not in r.headers.get("content-type", "").lower():
                r.encoding = r.apparent_encoding
            return r
        except requests.RequestException as e:
            last_err = e
            if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code < 500:
                break
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(short_error(last_err))


def short_error(e: Exception | None) -> str:
    if e is None:
        return "unknown error"
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    name = type(e).__name__
    if "ConnectionReset" in str(e) or "Connection aborted" in str(e):
        return "connection refused by site (possible bot protection)"
    return name


# --------------------------------------------------------------------------- #
# Text helpers: cleaning, deadlines, categories
# --------------------------------------------------------------------------- #
def clean(text: str | None, limit: int | None = None) -> str:
    text = " ".join((text or "").split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
DATE_RE = re.compile(
    rf"(\d{{4}}-\d{{2}}-\d{{2}}|{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}\s+{MONTHS},?\s+\d{{4}})",
    re.I,
)
DEADLINE_CUE = re.compile(
    r"deadline|due date|closing date|closes|close on|apply by|submit by|submission|intake|"
    r"application(?:s)? (?:due|must)|registration|letter of intent|LOI|notice of intent",
    re.I,
)


def find_deadline(text: str) -> str | None:
    """Return the earliest upcoming date (ISO) that appears near a deadline cue."""
    today = date.today()
    found: list[date] = []
    for cue in DEADLINE_CUE.finditer(text):
        window = text[cue.start(): cue.end() + 160]
        for m in DATE_RE.finditer(window):
            try:
                d = dateparser.parse(m.group(1), fuzzy=True).date()
            except (ValueError, OverflowError):
                continue
            if today - timedelta(days=1) <= d <= today + timedelta(days=730):
                found.append(d)
    return min(found).isoformat() if found else None


CATEGORY_RULES: dict[str, str] = {
    "Research": r"\bresearch\b|discovery|operating grant|project grant|team grant|chair|scientist|investigator",
    "Commercialization": r"commerciali|technology transfer|tech transfer|intellectual property|\bIP\b|proof[- ]of[- ]concept|prototype|market[- ]ready|patent|licens",
    "Industry partnership": r"industr|partner|alliance|collaborat|applied research|co-develop|consorti|cluster",
    "Startups & spin-offs": r"start-?up|spin-?off|spin-?out|entrepreneur|venture|accelerat|incubat|founder|early-stage compan",
    "SME & business": r"\bSMEs?\b|small and medium|small business|businesses|compan(?:y|ies)|firms?\b|enterprise|innovators?\b",
    "Talent & internships": r"intern|student|graduate|post-?doc|fellowship|scholarship|work placement|co-?op\b|work-integrated|young (?:graduate|people)|youth|trainee|talent",
    "Infrastructure & equipment": r"infrastructure|equipment|facilit(?:y|ies)|instrument",
    "International": r"international|global|export|horizon europe|bilateral|\bEU\b",
    "Clean tech & climate": r"clean ?tech|climate|net[- ]zero|green|energy transition|emission|sustainab",
    "Health & life sciences": r"health|medical|clinical|biomedical|genomic|life science|biotech|disease",
    "Ocean": r"ocean|marine|fisher|aquacultur|blue economy",
    "AI & digital": r"artificial intelligence|\bAI\b|machine learning|digital|quantum|cyber|data science",
    "Defence & security": r"defence|defense|security|dual-use|military",
}
AUDIENCE_RULES: dict[str, str] = {
    "Universities": r"universit|faculty|professor|researcher|post-?doc|graduate student",
    "Colleges & polytechnics": r"college|polytechnic|c[ée]gep|CCI\b|applied research",
    "Industry & SMEs": r"\bSMEs?\b|small and medium|business|compan(?:y|ies)|firms?\b|enterprise|industry|innovators?\b",
    "Students & trainees": r"student|intern|post-?doc|fellowship|scholarship|trainee|graduate",
    "Not-for-profits": r"not-for-profit|non-profit|community organi",
}


def tag(text: str, rules: dict[str, str], defaults: list[str] | None = None) -> list[str]:
    tags = [name for name, pat in rules.items() if re.search(pat, text, re.I)]
    for d in defaults or []:
        if d not in tags:
            tags.insert(0, d)
    return tags


def detect_status(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(now closed|competition closed|status:?\s*closed|applications are closed|closed)\b", t) and not re.search(r"\bopen\b", t):
        return "closed"
    if re.search(r"coming soon|upcoming|forthcoming|anticipated launch|opens? (?:on|in)\b", t):
        return "upcoming"
    if re.search(r"\b(open|now accepting|accepting applications|apply now|call for (?:proposals|applications)|continuous intake|ongoing)\b", t):
        return "open"
    return "unknown"


def item_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")))
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


# --------------------------------------------------------------------------- #
# Extractors – each returns a list of raw dicts {title, url, description, extra_text}
# --------------------------------------------------------------------------- #
def extract_rss(src: dict) -> list[dict]:
    r = fetch(src["url"])
    soup = BeautifulSoup(r.content, "xml")
    out = []
    for it in soup.find_all(["item", "entry"]):
        title = clean(it.find("title").get_text() if it.find("title") else "")
        link_el = it.find("link")
        link = ""
        if link_el is not None:
            link = link_el.get("href") or link_el.get_text(strip=True)
        if not link and it.find("guid"):
            link = it.find("guid").get_text(strip=True)
        desc_el = it.find("description") or it.find("summary") or it.find("content")
        desc_html = desc_el.get_text() if desc_el else ""
        desc_text = clean(BeautifulSoup(desc_html, "lxml").get_text(" "))
        pub = it.find("pubDate") or it.find("updated") or it.find("published")
        out.append({"title": title, "url": link, "description": desc_text, "extra_text": desc_text,
                    "published": pub.get_text(strip=True) if pub else None})
    return out


def extract_gcnews(src: dict) -> list[dict]:
    """Government of Canada news API (canada.ca News Centre)."""
    params = {"sort": "publishedDate", "orderBy": "desc", "pick": src.get("pick", 300), "format": "json"}
    since = (date.today() - timedelta(days=src.get("days", 60))).isoformat()
    url = src["url"] + "?" + urlencode(params) + f"&publishedDate%3E={since}"
    r = fetch(url)
    entries = r.json().get("feed", {}).get("entry", [])
    keep = re.compile(src.get("title_include", "."), re.I)
    require = re.compile(src.get("title_require", "."), re.I)
    out = []
    for e in entries:
        text = f"{e.get('title', '')} {e.get('teaser', '')}"
        if not (keep.search(text) and require.search(text)):
            continue
        out.append({"title": clean(e.get("title")), "url": e.get("link"),
                    "description": clean(e.get("teaser")), "extra_text": text,
                    "published": e.get("publishedDate")})
    return out


def extract_wpjson(src: dict) -> list[dict]:
    """WordPress REST API listing (e.g. /wp-json/wp/v2/program?per_page=100)."""
    out = []
    for e in fetch(src["url"]).json():
        content = BeautifulSoup((e.get("excerpt") or {}).get("rendered") or (e.get("content") or {}).get("rendered") or "", "lxml")
        first = next((clean(p.get_text(" ")) for p in content.find_all("p") if len(clean(p.get_text(" "))) > 40), "")
        full = clean(BeautifulSoup((e.get("content") or {}).get("rendered") or "", "lxml").get_text(" "))
        out.append({"title": clean(BeautifulSoup(e["title"]["rendered"], "lxml").get_text()), "url": e["link"],
                    "description": first or full[:400], "extra_text": full[:5000], "published": e.get("modified")})
    return out


def extract_sshrc(src: dict) -> list[dict]:
    """SSHRC publishes its opportunity list as a JavaScript module; convert it to JSON."""
    r = fetch(src["url"])
    t = r.content.decode("utf-8-sig").strip()
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    t = re.sub(r"^\s*//.*$", "", t, flags=re.M)
    t = re.sub(r"^\s*export default\s*", "", t).rstrip().rstrip(";")
    t = re.sub(r"^(\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', t, flags=re.M)
    t = re.sub(r",(\s*[}\]])", r"\1", t)
    today = date.today().isoformat()
    out = []
    for o in json.loads(t)["opportunities"]:
        upcoming = sorted((c for c in o.get("competitions", []) if (c.get("deadline") or "")[:10] >= today),
                          key=lambda c: c["deadline"])
        nxt = upcoming[0] if upcoming else None
        amount = o.get("amount") or {}
        money = ""
        if amount.get("min"):
            money = f" Value: ${amount['min']:,}" + (f" – ${amount['max']:,}" if amount.get("max") else "") + \
                    ((amount.get("note") or {}).get("en") or "") + "."
        out.append({
            "title": o["title"]["en"],
            "url": urljoin(r.url, ((nxt or {}).get("link") or o["link"])["en"]),
            "description": clean(o["desc"]["en"] + money),
            "extra_text": " ".join(o.get("applicants", {}).get("en", {}).get("applicant", [])) + " " + (o.get("type") or {}).get("en", ""),
            "deadline": nxt["deadline"][:10] if nxt else None,
            "status": "open" if nxt else None,
        })
    return out


def page_urls(src: dict) -> list[str]:
    urls = src.get("urls") or [src["url"]]
    pag = src.get("paginate")
    if not pag:
        return urls
    out = []
    for u in urls:
        out.append(u)
        for n in range(1, pag.get("max", 3) + 1):
            parts = urlsplit(u)
            q = dict(parse_qsl(parts.query))
            q[pag.get("param", "page")] = str(n)
            out.append(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), "")))
    return out


def extract_html(src: dict) -> list[dict]:
    include = re.compile(src["include"], re.I) if src.get("include") else None
    exclude = re.compile(src["exclude"], re.I) if src.get("exclude") else None
    title_exclude = re.compile(
        src.get("title_exclude", r"^(learn more|read more|more|more info(rmation)?|details|link|apply|apply now|click here|here|view|"
                                 r"download|pdf|français|english|top|share|load more( results)?|see all|view all)\W*$"),
        re.I)
    link_sel = src.get("link", "a[href], gcds-link[href]")
    out, seen = [], set()
    for page in page_urls(src):
        try:
            r = fetch(page)
        except RuntimeError:
            if page == (src.get("urls") or [src["url"]])[0]:
                raise
            break  # pagination ran past the end
        soup = BeautifulSoup(r.text, "lxml")
        for junk in soup.select("header, footer, nav, script, style, noscript, #wb-bnr, #wb-sm, .gc-subway, aside.gc-subway"):
            junk.decompose()
        scope = soup.select_one(src.get("scope", "main")) or soup.body or soup
        new_on_page = 0
        containers = scope.select(src["item"]) if src.get("item") else None
        candidates = []
        if containers is not None:
            for c in containers:
                a = c.select_one(link_sel) if not (c.name in ("a", "gcds-link") and c.get("href")) else c
                if a is None or not a.get("href"):
                    continue
                t_el = c.select_one(src["title"]) if src.get("title") else None
                title = clean(t_el.get_text(" ")) if t_el else link_title(a, title_exclude)
                if not title:
                    continue
                d_el = c.select_one(src["desc"]) if src.get("desc") else None
                desc = clean(d_el.get_text(" "), 400) if d_el else clean(c.get_text(" ").replace(title, "", 1), 400)
                candidates.append((title, urljoin(r.url, a["href"]), desc, clean(c.get_text(" "))))
        else:
            for a in scope.select(link_sel):
                title = link_title(a, title_exclude)
                if not title:
                    continue
                card = card_of(a)
                ctx = clean(card.get_text(" ")) if card else title
                desc = clean(ctx.replace(title, "", 1), 400) if ctx != title else ""
                candidates.append((title, urljoin(r.url, a["href"]), desc, ctx))
        for title, url, desc, ctx in candidates:
            url = url.split("#")[0]
            if not url.startswith("http") or normalize_url(url) == normalize_url(r.url):
                continue
            key = normalize_url(url)
            if key in seen:
                # A second, shorter link to the same page is usually the clean title.
                prev = next(o for o in out if normalize_url(o["url"]) == key)
                if 4 <= len(title) < len(prev["title"]) and prev["title"].startswith(title):
                    prev["title"] = title
                continue
            if include and not include.search(url):
                continue
            if exclude and (exclude.search(url) or exclude.search(title)):
                continue
            seen.add(key)
            new_on_page += 1
            out.append({"title": title, "url": url, "description": desc, "extra_text": ctx})
        if src.get("paginate") and new_on_page == 0 and page not in (src.get("urls") or [src["url"]]):
            break
    return out


HEADINGS = ["h1", "h2", "h3", "h4", "h5"]


def card_of(a):
    """The smallest ancestor that looks like a card/list entry for this link."""
    node = a
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("main", "body"):
            return None
        if node.name in ("li", "article", "tr") or node.find(HEADINGS):
            return node
    return None


def link_title(a, title_exclude: re.Pattern) -> str:
    """Pick a real title for a link: a heading inside it, its text, or the card's heading."""
    h = a.find(HEADINGS + ["figcaption"]) or a.find(class_=re.compile(r"^h[1-5]$|title|heading"))
    title = clean((h or a).get_text(" "))
    if not title_exclude.match(title) and len(title) >= 4 and not title.upper().startswith("STATUS"):
        return title
    if a.get("title") and len(a["title"]) > 4:
        return clean(a["title"])
    card = card_of(a)
    h = card.find(HEADINGS) if card else None
    return clean(h.get_text(" ")) if h else ""


def extract_page(src: dict) -> list[dict]:
    """A single program page tracked as one evergreen item."""
    r = fetch(src["url"])
    soup = BeautifulSoup(r.text, "lxml")
    title = src.get("program") or clean(soup.find("h1").get_text(" ") if soup.find("h1") else src["name"])
    desc = meta_description(soup) or first_paragraph(soup)
    main = soup.select_one("main") or soup.body or soup
    for junk in main.select("script, style, nav, footer"):
        junk.decompose()
    body = clean(main.get_text(" "))
    return [{"title": title, "url": r.url, "description": desc, "extra_text": body,
             "content_hash": hashlib.sha1(body.encode()).hexdigest()}]


EXTRACTORS = {"rss": extract_rss, "gcnews": extract_gcnews, "html": extract_html, "page": extract_page,
              "wpjson": extract_wpjson, "sshrc": extract_sshrc}


def meta_description(soup: BeautifulSoup) -> str:
    for attrs in ({"name": "description"}, {"property": "og:description"}, {"name": "dcterms.description"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content") and len(m["content"].strip()) > 30:
            return clean(m["content"], 400)
    return ""


def first_paragraph(soup: BeautifulSoup) -> str:
    main = soup.select_one("main") or soup.body or soup
    for p in main.find_all("p"):
        t = clean(p.get_text(" "))
        if len(t) > 60:
            return clean(t, 400)
    return ""


BAD_TITLE = re.compile(r"^(learn more|read more|discover|explore)\b|@\dx|\.(png|jpe?g|svg|webp)$|^[\w-]*_[\w-]*$", re.I)


def enrich_from_detail(raw: dict, prefer_h1: bool = False) -> None:
    """Fetch the item's own page for a better title, description and a deadline."""
    try:
        r = fetch(raw["url"])
    except RuntimeError:
        return
    if "html" not in r.headers.get("content-type", "html"):
        return
    soup = BeautifulSoup(r.text, "lxml")
    h1 = soup.find("h1")
    h1 = clean(h1.get_text(" ")) if h1 else ""
    if not h1:  # fall back to the page title minus the " | Site name" suffix
        og = soup.find("meta", attrs={"property": "og:title"})
        h1 = re.split(r"\s+[|–—-]\s+", clean(og["content"] if og and og.get("content") else (soup.title.string if soup.title else "")))[0]
    if 4 <= len(h1) <= 150 and (prefer_h1 or BAD_TITLE.search(raw["title"]) or raw["title"].startswith(h1 + " ")):
        raw["title"] = h1
    desc = meta_description(soup) or first_paragraph(soup)
    if desc and (len(desc) > len(raw.get("description") or "") or not raw.get("description")):
        raw["description"] = desc
    main = soup.select_one("main") or soup.body or soup
    for junk in main.select("script, style, nav, footer, header"):
        junk.decompose()
    raw["extra_text"] = (raw.get("extra_text") or "") + " " + clean(main.get_text(" "))[:20000]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def load_previous() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"items": [], "meta": {}}


def run(only: set[str] | None = None) -> None:
    config = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = [s for s in config["sources"] if s.get("enabled", True)]
    prev = load_previous()
    prev_items = {it["id"]: it for it in prev.get("items", [])}
    today = date.today().isoformat()
    baseline = prev.get("meta", {}).get("baseline") or today

    items: dict[str, dict] = {}
    statuses = []
    for src in sources:
        if only and src["id"] not in only:
            continue
        started = time.time()
        status = {"id": src["id"], "name": src["name"], "url": src.get("home") or src["url"],
                  "region": src.get("region", "National"), "ok": True, "count": 0, "error": None}
        try:
            raws = EXTRACTORS[src.get("type", "html")](src)
            title_include = re.compile(src["title_include"], re.I) if src.get("title_include") and src.get("type") != "gcnews" else None
            details_left = MAX_DETAIL_FETCHES
            for raw in raws:
                if not raw.get("url") or not raw.get("title"):
                    continue
                if title_include and not title_include.search(raw["title"] + " " + raw.get("description", "")):
                    continue
                iid = item_id(raw["url"])
                old = prev_items.get(iid)
                if src.get("detail") and details_left > 0 and (old is None or not old.get("description")):
                    enrich_from_detail(raw, src.get("title_from_detail", False))
                    details_left -= 1
                elif old:
                    raw["title"] = old["title"]  # keep the cleaned-up title from the first visit
                if raw["title"].isupper():
                    raw["title"] = raw["title"].title()
                items[iid] = build_item(iid, raw, src, old, today)
            status["count"] = sum(1 for it in items.values() if it["source_id"] == src["id"])
            if status["count"] == 0:
                status["ok"] = False
                status["error"] = "no items found – page layout may have changed"
        except Exception as e:  # keep going: one broken site must not stop the run
            status["ok"] = False
            status["error"] = str(e)[:200] or type(e).__name__
        status["seconds"] = round(time.time() - started, 1)
        statuses.append(status)
        flag = "ok " if status["ok"] else "ERR"
        print(f"[{flag}] {src['id']:<22} {status['count']:>4} items  {status['error'] or ''}", flush=True)

    # Carry forward items from sources that failed this run (so a site outage
    # doesn't wipe them) and recently-inactive items, marking them inactive.
    failed = {s["id"] for s in statuses if not s["ok"]}
    ran = {s["id"] for s in statuses}
    cutoff = (date.today() - timedelta(days=DROP_INACTIVE_AFTER_DAYS)).isoformat()
    for iid, old in prev_items.items():
        if iid in items:
            continue
        if old["source_id"] not in ran or old["source_id"] in failed:
            items[iid] = old
        elif old.get("last_seen", today) >= cutoff:
            items[iid] = {**old, "active": False}
    # Keep status of sources not run this time (partial runs).
    for s in prev.get("sources", []):
        if s["id"] not in ran:
            statuses.append(s)

    known_ids = {s["id"] for s in config["sources"]}
    out_items = sorted((it for it in items.values() if it["source_id"] in known_ids),
                       key=lambda it: (it["first_seen"], it["title"]), reverse=True)
    payload = {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "baseline": baseline,
            "item_count": sum(1 for it in out_items if it["active"]),
        },
        "sources": sorted(statuses, key=lambda s: s["name"].lower()),
        "items": out_items,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for s in statuses if s["ok"])
    print(f"\nWrote {len(out_items)} items ({payload['meta']['item_count']} active) "
          f"from {ok}/{len(statuses)} healthy sources -> {DATA_FILE.relative_to(ROOT)}")


def build_item(iid: str, raw: dict, src: dict, old: dict | None, today: str) -> dict:
    text = f"{raw['title']} {raw.get('description', '')} {raw.get('extra_text', '')}"
    tag_text = f"{raw['title']} {raw.get('description', '')}"
    deadline = raw.get("deadline") or find_deadline(text) or (old or {}).get("deadline")
    if deadline and deadline < today:
        deadline = None
    status = raw.get("status") or detect_status(f"{raw['title']} {raw.get('extra_text', '')[:600]}")
    if status == "unknown" and src.get("default_status"):
        status = src["default_status"]
    updated = (old or {}).get("updated")
    if old and raw.get("content_hash") and old.get("content_hash") and raw["content_hash"] != old["content_hash"]:
        updated = today
    return {
        "id": iid,
        "title": raw["title"],
        "url": raw["url"],
        "description": clean(raw.get("description"), 420),
        "funder": src.get("funder", src["name"]),
        "source_id": src["id"],
        "source_name": src["name"],
        "region": src.get("region", "National"),
        "kind": src.get("kind", "Opportunity"),
        "categories": tag(tag_text, CATEGORY_RULES, src.get("categories")),
        "audience": tag(tag_text, AUDIENCE_RULES, src.get("audience")),
        "deadline": deadline,
        "status": status,
        "published": raw.get("published"),
        "content_hash": raw.get("content_hash"),
        "first_seen": (old or {}).get("first_seen", today),
        "last_seen": today,
        "updated": updated,
        "active": True,
    }


if __name__ == "__main__":
    run(set(sys.argv[1:]) or None)
