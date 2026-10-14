#!/usr/bin/env python3
"""
pennews: zbiera wpisy z feedów RSS/Atom o piórach wiecznych, zapamiętuje co już widział
i generuje statyczną stronę (docs/index.html) oraz własny feed (docs/feed.xml).

Użycie:
    python pennews.py                          # pobierz, zaktualizuj dane, wygeneruj stronę
    python pennews.py --discover https://...   # znajdź adres feedu na danej stronie
    python pennews.py --config inna.json       # inny plik konfiguracji (np. do testów)
"""
import argparse
import hashlib
import html as htmllib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
UA = "pennews/1.0 (+personal feed reader)"
TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
WS_RE = re.compile(r"\s+")


# ---------- pobieranie ----------

def fetch(url: str, timeout: int) -> bytes:
    if url.startswith("file:"):  # tylko do testów lokalnych
        return Path(url[5:]).read_bytes()
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.content


def discover(url: str, timeout: int = 20) -> None:
    """Wypisz adresy feedów znalezione w <link rel="alternate"> na stronie."""
    page = fetch(url, timeout).decode("utf-8", errors="replace")
    found = set()
    for tag in re.findall(r"<link[^>]+>", page, re.I):
        if re.search(r"rel=[\"']alternate[\"']", tag, re.I) and re.search(r"(rss|atom)\+xml", tag, re.I):
            m = re.search(r"href=[\"']([^\"']+)[\"']", tag, re.I)
            if m:
                found.add(urljoin(url, htmllib.unescape(m.group(1))))
    if found:
        print("Znalezione feedy:")
        for f in sorted(found):
            print("  ", f)
    else:
        print("Nie znaleziono feedu w nagłówku strony. Spróbuj /feed, /rss, /blogs/news.atom (Shopify).")


# ---------- przetwarzanie wpisów ----------

def clean_text(raw: str, limit: int) -> str:
    text = htmllib.unescape(TAG_RE.sub(" ", raw or ""))
    text = WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def entry_image(e) -> str:
    for key in ("media_thumbnail", "media_content"):
        for m in e.get(key, []) or []:
            u = m.get("url")
            if u and u.startswith("http"):
                return u
    for enc in e.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href", "").startswith("http"):
            return enc["href"]
    blobs = [e.get("summary", "")] + [c.get("value", "") for c in e.get("content", []) or []]
    for b in blobs:
        m = IMG_RE.search(b or "")
        if m and m.group(1).startswith("http"):
            return m.group(1)
    return ""


def entry_date(e, now: datetime) -> datetime:
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return now
    d = datetime(*t[:6], tzinfo=timezone.utc)
    return min(d, now)  # nie ufaj datom z przyszłości


def build_matchers(cfg):
    kw = re.compile(r"\b(?:" + "|".join(cfg["release_keywords"]) + r")\b", re.I)
    ambiguous = {b.lower() for b in cfg.get("ambiguous_brands", [])}
    ctx = re.compile(r"\b(?:" + "|".join(map(re.escape, cfg.get("ambiguous_context_words", []))) + r")\b", re.I)
    brands = []
    for b in cfg["brands"]:
        brands.append((b, re.compile(r"(?<![\w-])" + re.escape(b) + r"(?![\w]|-\w)", re.I), b.lower() in ambiguous))
    ex_title = re.compile(r"\b(?:" + "|".join(cfg.get("exclude_title_keywords", ["$^"])) + r")\b", re.I)
    ex_text = re.compile(r"\b(?:" + "|".join(cfg.get("exclude_text_keywords", ["$^"])) + r")\b", re.I)
    return kw, brands, ctx, ex_title, ex_text


def evaluate(title: str, summary: str, matchers, only_releases: bool):
    """Zwraca (zachowaj, is_release, marki). W trybie only_releases: tylko premiery, bez wykluczonych."""
    kw, brands, ctx, ex_title, ex_text = matchers
    is_release, found = classify(f"{title} {summary}", matchers)
    excluded = bool(ex_title.search(title) or ex_text.search(f"{title} {summary}"))
    keep = (is_release and not excluded) if only_releases else True
    return keep, is_release and not excluded, found


def classify(text: str, matchers):
    kw, brands, ctx = matchers[:3]
    is_release = bool(kw.search(text))
    has_ctx = bool(ctx.search(text))
    found = []
    for name, rx, amb in brands:
        if rx.search(text) and (not amb or has_ctx):
            canon = "Nahvalur" if name == "Narwhal" else "HongDian" if name == "Hong Dian" else \
                    "YSTUDIO" if name == "Ystudio" else name
            if canon not in found:
                found.append(canon)
    return is_release, found


def uid(source: str, raw_id: str) -> str:
    return hashlib.sha1(f"{source}|{raw_id}".encode("utf-8")).hexdigest()[:16]


# ---------- stan (co już widzieliśmy) ----------

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"UWAGA: {path} jest uszkodzony, zaczynam od zera.", file=sys.stderr)
    return default


def run(cfg_path: Path, data_dir: Path, out_dir: Path) -> int:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    s = cfg["settings"]
    now = datetime.now(timezone.utc)
    matchers = build_matchers(cfg)

    entries_path = data_dir / "entries.json"
    first_run = not entries_path.exists()
    only = bool(s.get("only_releases", False))
    stored = {}
    for e in load_json(entries_path, []):
        keep, rel, brands = evaluate(e["title"], e.get("summary", ""), matchers, only)
        if keep:  # zmiana słów kluczowych działa wstecz na całe archiwum
            e["release"], e["brands"] = rel, brands
            stored[e["uid"]] = e
    status = []
    added = 0
    cutoff = now - timedelta(days=s["retention_days"])

    for feed in cfg["feeds"]:
        if not feed.get("enabled", True):
            continue
        name, url = feed["name"], feed["url"]
        try:
            parsed = feedparser.parse(fetch(url, s["request_timeout"]))
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"nie wygląda na poprawny feed ({parsed.bozo_exception})")
            count = 0
            for e in parsed.entries:
                link = e.get("link", "")
                if urlparse(link).scheme not in ("http", "https"):
                    continue
                u = uid(name, e.get("id") or link)
                count += 1
                if u in stored:
                    continue
                title = clean_text(e.get("title", ""), 200) or "(bez tytułu)"
                body = e.get("summary", "") or " ".join(c.get("value", "") for c in e.get("content", []) or [])
                summary = clean_text(body, s["max_summary_chars"])
                published = entry_date(e, now)
                if published < cutoff:
                    continue
                keep, is_release, brands = evaluate(title, summary, matchers, only)
                if not keep:
                    continue
                stored[u] = {
                    "uid": u,
                    "source": name,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "image": entry_image(e),
                    "published": published.isoformat(),
                    # przy pierwszym uruchomieniu nic nie świeci się jako "nowe"
                    "first_seen": (published if first_run else now).isoformat(),
                    "release": is_release,
                    "brands": brands,
                }
                added += 1
            status.append({"source": name, "url": url, "ok": True, "entries_in_feed": count})
            print(f"[ok]   {name}: {count} wpisów w feedzie")
        except Exception as ex:  # jedno zepsute źródło nie może zatrzymać reszty
            status.append({"source": name, "url": url, "ok": False, "error": str(ex)[:200]})
            print(f"[BŁĄD] {name}: {ex}", file=sys.stderr)

    kept = [e for e in stored.values() if datetime.fromisoformat(e["published"]) >= cutoff]
    kept.sort(key=lambda e: e["published"], reverse=True)

    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries_path.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    (data_dir / "status.json").write_text(
        json.dumps({"last_run": now.isoformat(), "sources": status}, ensure_ascii=False, indent=1), encoding="utf-8")

    render_html(kept, status, now, cfg, out_dir / "index.html")
    render_rss(kept, now, cfg, out_dir / "feed.xml")
    print(f"Nowych wpisów: {added}, w archiwum: {len(kept)}")
    if status and not any(x["ok"] for x in status):
        print("Żadne źródło nie odpowiedziało poprawnie.", file=sys.stderr)
        return 1
    return 0


# ---------- generowanie wyjścia ----------

PAGE = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="alternate" type="application/rss+xml" title="__TITLE__" href="feed.xml">
<style>
:root{--bg:#faf8f5;--card:#fff;--text:#1d1b19;--muted:#6d665e;--line:#e4ded6;--accent:#8c3b2a;--chip:#f0ebe3;--new:#2f7d4f}
@media (prefers-color-scheme:dark){:root{--bg:#161412;--card:#201d1a;--text:#ece7e0;--muted:#a39b91;--line:#34302b;--accent:#e08a6f;--chip:#2a2622;--new:#6cc08b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px;font-size:.9rem}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.bar button,.bar select,.bar input{font:inherit;font-size:.9rem;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--text)}
.bar button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.bar input{flex:1;min-width:140px;border-radius:8px}
.item{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.item img{width:96px;height:96px;object-fit:cover;border-radius:8px;flex:none;background:var(--chip)}
.item h2{font-size:1.05rem;margin:0 0 4px}
.item a{color:inherit;text-decoration:none}
.item a:hover h2{color:var(--accent);text-decoration:underline}
.meta{font-size:.8rem;color:var(--muted);margin-bottom:6px}
.p{margin:0 0 8px;font-size:.92rem}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:.75rem;background:var(--chip);border-radius:999px;padding:2px 10px}
.chip.new{background:var(--new);color:#fff}
.chip.rel{background:var(--accent);color:#fff}
.empty{color:var(--muted);text-align:center;padding:40px 0}
footer{margin-top:32px;font-size:.8rem;color:var(--muted)}
footer .bad{color:#c0392b}
@media (max-width:520px){.item img{width:64px;height:64px}}
</style>
</head>
<body>
<div class="wrap">
<h1>__TITLE__</h1>
<p class="sub">Ostatnia aktualizacja: <span id="upd"></span> · <a href="feed.xml" style="color:var(--accent)">własny feed RSS (premiery)</a></p>
<div class="bar">
  <button id="f-all" aria-pressed="true">Wszystkie premiery</button>
  <button id="f-rel" aria-pressed="false">Premiery i edycje</button>
  <button id="f-new" aria-pressed="false">Nowe (__NEWH__ h)</button>
  <select id="brand"><option value="">Wszystkie marki</option></select>
  <select id="src"><option value="">Wszystkie źródła</option></select>
  <input id="q" type="search" placeholder="Szukaj…">
</div>
<div id="list"></div>
<footer id="foot"></footer>
</div>
<script>
const DATA = __DATA__;
const STATUS = __STATUS__;
const NEW_HOURS = __NEWH__;
const ONLY_REL = __ONLYREL__;
const now = Date.now();
const $ = id => document.getElementById(id);
let relOnly = false, newOnly = false;

function safeUrl(u){ try{ const x = new URL(u); return (x.protocol==='http:'||x.protocol==='https:') ? x.href : '#'; }catch(e){ return '#'; } }
function isNew(e){ return (now - Date.parse(e.first_seen)) < NEW_HOURS*3600*1000; }
function el(tag, cls, text){ const n=document.createElement(tag); if(cls) n.className=cls; if(text!==undefined) n.textContent=text; return n; }

$('upd').textContent = new Date(STATUS.last_run).toLocaleString('pl-PL');
if(ONLY_REL){ $('f-rel').style.display='none'; }
const brands = [...new Set(DATA.flatMap(e=>e.brands))].sort((a,b)=>a.localeCompare(b));
const srcs = [...new Set(DATA.map(e=>e.source))].sort();
brands.forEach(b=>{ const o=el('option',null,b); o.value=b; $('brand').append(o); });
srcs.forEach(s=>{ const o=el('option',null,s); o.value=s; $('src').append(o); });

function render(){
  const q = $('q').value.trim().toLowerCase(), b = $('brand').value, s = $('src').value;
  const rows = DATA.filter(e =>
    (!relOnly || e.release) && (!newOnly || isNew(e)) && (!b || e.brands.includes(b)) && (!s || e.source===s) &&
    (!q || (e.title+' '+e.summary).toLowerCase().includes(q)));
  const box = $('list'); box.replaceChildren();
  if(!rows.length){ box.append(el('div','empty','Brak wpisów dla tych filtrów.')); return; }
  rows.forEach(e=>{
    const card = el('article','item');
    if(e.image){ const im=el('img'); im.src=safeUrl(e.image); im.alt=''; im.loading='lazy'; im.referrerPolicy='no-referrer'; card.append(im); }
    const body = el('div');
    const a = el('a'); a.href = safeUrl(e.link); a.target='_blank'; a.rel='noopener noreferrer'; a.append(el('h2',null,e.title));
    body.append(a);
    body.append(el('div','meta', e.source+' · '+new Date(e.published).toLocaleDateString('pl-PL')));
    if(e.summary) body.append(el('p','p',e.summary));
    const chips = el('div','chips');
    if(isNew(e)) chips.append(el('span','chip new','nowe'));
    if(e.release) chips.append(el('span','chip rel','premiera / edycja'));
    e.brands.forEach(x=>chips.append(el('span','chip',x)));
    body.append(chips); card.append(body); box.append(card);
  });
}
function setBtn(){ $('f-all').setAttribute('aria-pressed', !relOnly&&!newOnly); $('f-rel').setAttribute('aria-pressed', relOnly); $('f-new').setAttribute('aria-pressed', newOnly); }
$('f-all').onclick=()=>{relOnly=false;newOnly=false;setBtn();render();};
$('f-rel').onclick=()=>{relOnly=!relOnly;setBtn();render();};
$('f-new').onclick=()=>{newOnly=!newOnly;setBtn();render();};
['brand','src'].forEach(i=>$(i).onchange=render); $('q').oninput=render;

const bad = STATUS.sources.filter(s=>!s.ok), zero = STATUS.sources.filter(s=>s.ok && s.entries_in_feed===0);
const foot = $('foot');
foot.append(el('div',null,'Źródła: '+STATUS.sources.filter(s=>s.ok).length+' działa, '+bad.length+' z błędem.'));
bad.forEach(s=>foot.append(el('div','bad','⚠ '+s.source+': '+s.error)));
zero.forEach(s=>foot.append(el('div','bad','⚠ '+s.source+': feed jest pusty')));
render();
</script>
</body>
</html>
"""


def _js_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def render_html(entries, status, now, cfg, path: Path) -> None:
    s = cfg["settings"]
    out = (PAGE.replace("__TITLE__", htmllib.escape(s["site_title"]))
               .replace("__NEWH__", str(int(s["new_hours"])))
               .replace("__ONLYREL__", "true" if s.get("only_releases") else "false")
               .replace("__DATA__", _js_json(entries))
               .replace("__STATUS__", _js_json({"last_run": now.isoformat(), "sources": status})))
    path.write_text(out, encoding="utf-8")


def render_rss(entries, now, cfg, path: Path) -> None:
    items = []
    for e in [x for x in entries if x["release"]][:50]:
        d = datetime.fromisoformat(e["published"])
        cats = "".join(f"<category>{xml_escape(b)}</category>" for b in e["brands"])
        items.append(
            "<item>"
            f"<title>{xml_escape(e['source'] + ': ' + e['title'])}</title>"
            f"<link>{xml_escape(e['link'])}</link>"
            f"<guid isPermaLink=\"false\">{e['uid']}</guid>"
            f"<pubDate>{format_datetime(d)}</pubDate>"
            f"<description>{xml_escape(e['summary'])}</description>{cats}</item>")
    title = xml_escape(cfg["settings"]["site_title"])
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{title} (premiery i edycje)</title><link>.</link>"
        f"<description>Wpisy dopasowane do słów kluczowych premier i edycji limitowanych</description>"
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>{''.join(items)}</channel></rss>",
        encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--out-dir", default=str(ROOT / "docs"))
    ap.add_argument("--discover", metavar="URL")
    a = ap.parse_args()
    if a.discover:
        discover(a.discover)
        return 0
    return run(Path(a.config), Path(a.data_dir), Path(a.out_dir))


if __name__ == "__main__":
    sys.exit(main())
