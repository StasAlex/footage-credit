#!/usr/bin/env python3
"""Builds the static site into docs/ (GitHub Pages serves main:/docs).

    python3 build.py            # all keywords from keywords.txt
    python3 build.py --no-fetch # rebuild templates only, keep cached results

Every keyword gets its own pre-rendered page (docs/free/<slug>/) with real search results baked in,
so search engines see content; app.js then makes the same cards interactive.
Results are cached in cache/ for a day (Pixabay-style etiquette, and fewer requests to the archives).
"""
import html
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SITE_NAME = "FootageCredit"
BASE_URL = "https://stasalex.github.io/footage-credit"   # change when a custom domain is attached
PROXY_URL = ""                                            # deployed worker/ URL; enables Pixabay in live search
ADSENSE_CLIENT = ""                                       # "ca-pub-XXXXXXXX" once AdSense approves the site

ROOT = Path(__file__).parent
OUT, CACHE = ROOT / "docs", ROOT / "cache"
UA = "FootageCreditBuilder/0.1 (static site generator)"
q = urllib.parse.quote
esc = lambda s: html.escape(str(s or ""), quote=True)


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------- sources (mirror of app.js) ----------

def openverse(kw):
    d = get_json(f"https://api.openverse.org/v1/images/?q={q(kw)}&page_size=12&mature=false")
    out = []
    for h in d.get("results", []):
        lic = f"CC {h.get('license', '').upper()} {h.get('license_version', '')}".replace("CC CC0", "CC0").replace(
            "CC PDM", "Public Domain").strip()
        out.append(dict(src=f"Openverse / {h.get('source', '')}", kind="photo", title=h.get("title", ""),
                        author=h.get("creator", ""), license=lic, page=h.get("foreign_landing_url", ""),
                        thumb=h.get("thumbnail") or h.get("url", ""), file=h.get("url", "")))
    return out


def commons(kw):
    d = get_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search&gsrnamespace=6"
                 f"&gsrlimit=16&gsrsearch={q(kw)}&prop=imageinfo&iiprop=url|size|mime|extmetadata&iiurlwidth=480")
    out = []
    for p in (d.get("query", {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        mime, meta = ii.get("mime", ""), ii.get("extmetadata", {})
        video = mime.startswith("video")
        if not (mime.startswith("image") or video) or mime == "image/svg+xml":
            continue
        out.append(dict(src="Wikimedia Commons", kind="video" if video else "photo",
                        title=p.get("title", "").replace("File:", ""),
                        author=strip_tags(meta.get("Artist", {}).get("value", ""))[:120],
                        license=strip_tags(meta.get("LicenseShortName", {}).get("value", "")),
                        page=ii.get("descriptionurl", ""), thumb=ii.get("thumburl", ""), file=ii.get("url", "")))
    return out


def nasa(kw):
    d = get_json(f"https://images-api.nasa.gov/search?q={q(kw)}&media_type=image,video&page_size=12")
    out = []
    for it in d.get("collection", {}).get("items", [])[:12]:
        dd = (it.get("data") or [{}])[0]
        thumb = next((l["href"] for l in it.get("links", []) if l.get("rel") == "preview"), "")
        out.append(dict(src="NASA", kind="video" if dd.get("media_type") == "video" else "photo",
                        title=dd.get("title", ""), author=dd.get("center", "NASA"), license="Public Domain (NASA)",
                        page=f"https://images.nasa.gov/details/{dd.get('nasa_id')}", thumb=thumb,
                        file="nasa:" + it.get("href", "")))
    return out


def lic_from_url(u):
    if "publicdomain/zero" in u:
        return "CC0"
    if "publicdomain" in u:
        return "Public Domain"
    m = re.search(r"licenses/([a-z-]+)/([\d.]+)", u)
    return f"CC {m[1].upper()} {m[2]}" if m else u


def archive(kw):
    d = get_json("https://archive.org/advancedsearch.php?q=" + q(f"({kw}) AND mediatype:movies")
                 + "&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=year&fl[]=licenseurl&rows=10&output=json")
    out = []
    for h in d.get("response", {}).get("docs", []):
        ident, cr = h["identifier"], h.get("creator", "")
        out.append(dict(src="Internet Archive", kind="video",
                        title=f"{h.get('title', '')} ({h['year']})" if h.get("year") else h.get("title", ""),
                        author=", ".join(cr) if isinstance(cr, list) else cr,
                        license=lic_from_url(h["licenseurl"]) if h.get("licenseurl") else "",
                        page=f"https://archive.org/details/{ident}", thumb=f"https://archive.org/services/img/{ident}",
                        file="archive:" + ident))
    return out


def fetch(kw):
    cf = CACHE / f"{slug(kw)}.json"
    if cf.exists() and ("--no-fetch" in sys.argv or time.time() - cf.stat().st_mtime < 86400):
        return json.loads(cf.read_text())
    parts = []
    for fn in (commons, nasa, archive, openverse):
        try:
            parts.append(fn(kw))
        except Exception as e:
            print(f"  {kw}: {fn.__name__} failed: {e}")
            parts.append([])
    res = []                                   # interleave sources
    for i in range(max(map(len, parts), default=0)):
        res += [p[i] for p in parts if i < len(p) and p[i].get("thumb")]
    res = res[:24]
    CACHE.mkdir(exist_ok=True)
    cf.write_text(json.dumps(res, ensure_ascii=False))
    return res


# ---------- licence badges (mirror of app.js lic()) ----------

def lic(raw):
    s = (raw or "").lower()
    code = "unknown"
    if "pixabay" in s: code = "free"
    elif re.search(r"cc0|zero", s): code = "cc0"
    elif re.search(r"public.?domain|pdm|\bpd\b|no known copyright", s): code = "pd"
    elif "nc" in s: code = "nc"
    elif "nd" in s: code = "nd"
    elif re.search(r"by.?sa", s): code = "by-sa"
    elif re.search(r"\bby\b|cc.?by|attribution", s): code = "by"
    return code, code in ("free", "cc0", "pd", "by", "by-sa"), code in ("by", "by-sa", "nc", "nd")


def card(it, i):
    code, commercial, attribution = lic(it["license"])
    if code == "unknown":
        badge = '<span class="b warn">check license</span>'
    else:
        badge = (f'<span class="b {"ok" if commercial else "warn"}">{"commercial OK" if commercial else "non-commercial"}</span>'
                 + ('<span class="b">credit required</span>' if attribution else '<span class="b ok">no credit needed</span>'))
    video = '<span class="kind">▶ video</span>' if it["kind"] == "video" else ""
    return f"""<article class="card" data-i="{i}">
  <a class="thumb" href="{esc(it['page'])}" target="_blank" rel="noopener nofollow"><img loading="lazy" src="{esc(it['thumb'])}" alt="{esc(it['title'])}">{video}</a>
  <div class="meta">
    <h3 title="{esc(it['title'])}">{esc(it['title'] or 'Untitled')}</h3>
    <p class="by">{esc(it['author'] or 'Unknown author')} · {esc(it['src'])}</p>
    <p class="lic">{esc(it['license'] or 'Check license')}</p>
    <div class="badges">{badge}</div>
    <div class="act"><button class="dl" type="button">Download</button><button class="add" type="button">+ Credits</button></div>
  </div>
</article>"""


# ---------- page shell ----------

def page(title, desc, body, rel, path, extra_head=""):
    ads = (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={ADSENSE_CLIENT}" '
           'crossorigin="anonymous"></script>') if ADSENSE_CLIENT else ""
    proxy = f'<script>window.FC_PROXY = {json.dumps(PROXY_URL)};</script>' if PROXY_URL else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{BASE_URL}/{path}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<link rel="stylesheet" href="{rel}style.css">
{ads}{extra_head}
</head>
<body>
<header class="top"><div class="wrap">
  <a class="logo" href="{rel}">Footage<b>Credit</b></a>
  <nav><a href="{rel}free/">Browse topics</a><a href="{rel}about/">About</a></nav>
</div></header>
{body}
<footer><div class="wrap">
  <a href="{rel}about/">About</a><a href="{rel}privacy/">Privacy</a>
  <span>Results come from Wikimedia Commons, NASA, Internet Archive and Openverse. Always check the license on the source page.</span>
</div></footer>
<aside id="credits">
  <div class="panel">
    <h2>Your credits</h2>
    <p>Clips you download that need attribution land here automatically. Paste the block into your video description.</p>
    <ul id="credits-list"></ul>
    <textarea id="credits-text" readonly placeholder="Add clips with “+ Credits”"></textarea>
    <div class="row"><button id="copy" type="button">Copy</button><button id="clear" type="button">Clear</button></div>
  </div>
  <button id="credits-toggle" type="button">Credits (<span id="credits-n">0</span>)</button>
</aside>
{proxy}<script src="{rel}app.js"></script>
</body>
</html>
"""


SEARCH_FORM = """<form id="search" role="search">
  <input name="q" type="search" placeholder="Try: 1950s kitchen, rocket launch, steam train" value="{value}" required>
  <select name="kind" aria-label="Media type"><option value="any">Video + photo</option><option value="video">Video</option><option value="photo">Photo</option></select>
  <button type="submit">Search</button>
</form>
<ol class="how">
  <li><b>Search once.</b> We look through NASA, Wikimedia Commons, Internet Archive and Openverse at the same time.</li>
  <li><b>See what you're allowed to do.</b> Each clip is marked: OK for monetized videos, credit required, or check license.</li>
  <li><b>Download and copy credits.</b> Clips that need attribution go into your credits list. Paste it into your video description.</li>
</ol>"""

FILTER_BAR = """<div class="bar">
  <span id="count">{count}</span>
  <label>License <select id="lic"><option value="any">Any</option><option value="commercial">OK for monetized videos</option><option value="nocredit">No credit needed</option></select></label>
</div>"""

AD_SLOT = '<div class="ad">Advertisement</div>' if not ADSENSE_CLIENT else (
    f'<ins class="adsbygoogle" style="display:block" data-ad-client="{ADSENSE_CLIENT}" data-ad-format="auto" '
    'data-full-width-responsive="true"></ins><script>(adsbygoogle=window.adsbygoogle||[]).push({});</script>')


def write(path, text):
    p = OUT / path / "index.html" if path else OUT / "index.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def main():
    keywords = [l.strip() for l in (ROOT / "keywords.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    for f in ("app.js", "style.css"):
        shutil.copy(ROOT / f, OUT / f)

    with ThreadPoolExecutor(4) as ex:
        data = dict(zip(keywords, ex.map(fetch, keywords)))

    # topic pages
    for kw in keywords:
        res, s = data[kw], slug(kw)
        others = [k for k in keywords if k != kw]
        start = keywords.index(kw)
        related = (others[start:] + others[:start])[:12]
        rel_links = "".join(f'<a href="../{slug(k)}/">{esc(k)}</a>' for k in related)
        n_free = sum(1 for it in res if lic(it["license"])[1])
        body = f"""<main class="wrap">
<section class="hero">
  <h1>Free {esc(kw)} footage and photos</h1>
  <p>{len(res)} public-domain and Creative Commons results for “{esc(kw)}” from open archives, {n_free} of them cleared for monetized videos. Download a clip and its credit line is written for you.</p>
  {SEARCH_FORM.format(value=esc(kw))}
</section>
{AD_SLOT}
{FILTER_BAR.format(count=f"{len(res)} results")}
<div id="grid">{"".join(card(it, i) for i, it in enumerate(res))}</div>
<section class="related"><h2>Related topics</h2>{rel_links}</section>
</main>"""
        seed = f'<script type="application/json" id="seed">{json.dumps(res, ensure_ascii=False).replace("</", "<\\/")}</script>'
        write(f"free/{s}", page(f"Free {kw} stock footage & photos (public domain, CC) | {SITE_NAME}",
                                f"Free {kw} videos and photos from NASA, Wikimedia, Internet Archive and Openverse, with license badges and auto-generated credits for YouTube.",
                                body, "../../", f"free/{s}/", seed))

    # topics index
    links = "".join(f'<a href="{slug(k)}/">{esc(k)}</a>' for k in sorted(keywords))
    write("free", page(f"Browse free footage topics | {SITE_NAME}", "All topics with free public-domain and Creative Commons footage.",
                       f'<main class="wrap"><section class="hero"><h1>Browse topics</h1></section><section class="related">{links}</section>'
                       f'<div id="grid" hidden></div></main>', "../", "free/"))

    # home
    popular = "".join(f'<a href="free/{slug(k)}/">{esc(k)}</a>' for k in keywords[:24])
    home = f"""<main class="wrap">
<section class="hero">
  <h1>Free footage for YouTube, with the credits written for you</h1>
  <p>Search public-domain and Creative Commons video and photos from NASA, Wikimedia Commons, Internet Archive and Openverse in one place. Every result shows whether you can use it in a monetized video, and the credit block for your description builds itself as you download.</p>
  {SEARCH_FORM.format(value="")}
</section>
{AD_SLOT}
{FILTER_BAR.format(count="")}
<div id="grid"></div>
<section class="related"><h2>Popular topics</h2>{popular}</section>
</main>"""
    write("", page(f"{SITE_NAME}: free public-domain footage with auto credits",
                   "Search free public-domain and Creative Commons footage across NASA, Wikimedia, Internet Archive and Openverse. License badges and ready-to-paste YouTube credits.",
                   home, "", ""))

    for name, title, text in STATIC_PAGES:
        write(name, page(f"{title} | {SITE_NAME}", title, f'<main class="wrap prose"><h1>{title}</h1>{text}<div id="grid" hidden></div></main>', "../", f"{name}/"))

    urls = [""] + ["free/"] + [f"free/{slug(k)}/" for k in keywords] + [f"{n}/" for n, _, _ in STATIC_PAGES]
    (OUT / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                     + "".join(f"<url><loc>{BASE_URL}/{u}</loc></url>\n" for u in urls) + "</urlset>\n")
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n")
    (OUT / ".nojekyll").write_text("")
    print(f"built {len(keywords)} topic pages -> {OUT}")


STATIC_PAGES = [
    ("about", "About", """
<p>FootageCredit searches open archives (Wikimedia Commons, NASA Image and Video Library, Internet Archive and Openverse) and shows the results side by side with plain-language license badges.</p>
<p>Clips that need attribution are added to your credits list when you download them. Copy the block into your video description and you're covered.</p>
<p>We don't host any files. Every download comes straight from the original archive, and the license shown is the one the archive reports. Licenses on archives can be wrong or incomplete, so check the source page before publishing anything important.</p>"""),
    ("privacy", "Privacy policy", """
<p>FootageCredit has no accounts and stores no personal data on a server. Your credits list is kept only in your own browser (localStorage) and you can clear it at any time.</p>
<p>Searches are sent directly from your browser to the archives listed on the site; each archive's own privacy policy applies to those requests.</p>
<p>This site may show ads served by Google. Google and its partners use cookies to serve ads based on your prior visits to this and other websites. You can opt out of personalized advertising at <a href="https://adssettings.google.com" rel="nofollow">Google Ads Settings</a>. See also <a href="https://policies.google.com/technologies/partner-sites" rel="nofollow">how Google uses data from partner sites</a>.</p>"""),
]

if __name__ == "__main__":
    main()
