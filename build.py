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
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SITE_NAME = "FootageCredit"
BASE_URL = "https://stasalex.github.io/footage-credit"   # change when a custom domain is attached
PROXY_URL = ""                                            # deployed worker/ URL; enables Pixabay in live search
MIN_RESULTS = 10                                          # topic pages with fewer results are not published
REPO_URL = "https://github.com/StasAlex/footage-credit"    # reports and takedown requests go to its issues
ADSENSE_CLIENT = ""                                       # "ca-pub-XXXXXXXX" once AdSense approves the site

ROOT = Path(__file__).parent
OUT, CACHE = ROOT / "docs", ROOT / "cache"
UA = "FootageCreditBuilder/0.2 (+https://github.com/StasAlex/footage-credit)"
q = urllib.parse.quote
# Cache-busting version for style.css and app.js, so browsers never mix new HTML with old assets.
import hashlib
ASSET_V = hashlib.md5((Path(__file__).parent / "style.css").read_bytes() + (Path(__file__).parent / "app.js").read_bytes()).hexdigest()[:8]
esc = lambda s: html.escape(str(s or ""), quote=True)
BLOCKED = {l.strip() for l in (ROOT / "blocklist.txt").read_text().splitlines() if l.strip() and not l.startswith("#")} \
    if (ROOT / "blocklist.txt").exists() else set()


def report_url(it):
    body = (f"Source page: {it['page']}\nTitle: {it['title']}\n\n"
            "Why should this result be removed? (for copyright claims: who owns the work, and a link to proof)\n")
    return f"{REPO_URL}/issues/new?labels=report&title={q('Report: ' + it['page'])}&body={q(body)}"


def get_json(url, timeout=40):
    """GET JSON, backing off and retrying when the archive rate-limits us (HTTP 429)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for wait in (5, 15, 45, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait is None:
                raise
            time.sleep(wait)


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------- sources (mirror of app.js) ----------

OPENVERSE_FAILS = [0]


def openverse(kw):
    if OPENVERSE_FAILS[0] >= 3:                # Openverse often stalls for anonymous clients; stop asking after 3 failures
        return []
    try:
        d = get_json(f"https://api.openverse.org/v1/images/?q={q(kw)}&page_size=12&mature=false", timeout=10)
    except Exception:
        OPENVERSE_FAILS[0] += 1
        raise
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


# Internet Archive: only items marked public domain (Prelinger and PD/CC0 licence URLs), most downloaded first.
ARCHIVE_PD = ' AND (collection:prelinger OR licenseurl:"http://creativecommons.org/licenses/publicdomain/" OR licenseurl:"http://creativecommons.org/publicdomain/mark/1.0/" OR licenseurl:"http://creativecommons.org/publicdomain/zero/1.0/")'


def archive_lic(h):
    """Only Prelinger items are trusted as public domain; elsewhere the PD mark is the uploader's own claim."""
    if "prelinger" in (h.get("collection") or []):
        return "Public Domain (Prelinger Archives)"
    return "Unverified: uploader marked it public domain" if h.get("licenseurl") else ""


def archive(kw):
    def ask(expr):
        return get_json("https://archive.org/advancedsearch.php?q=" + q(f"{expr} AND mediatype:movies" + ARCHIVE_PD)
                        + "&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=year&fl[]=licenseurl&fl[]=collection"
                        + "&rows=12&sort[]=downloads+desc&output=json")
    # Title/subject matches are far more relevant; fall back to full text when they are too few.
    d = ask(f"(title:({kw}) OR subject:({kw}))")
    if len(d.get("response", {}).get("docs", [])) < 5:
        d = ask(f"({kw})")
    out = []
    for h in d.get("response", {}).get("docs", []):
        ident, cr = h["identifier"], h.get("creator", "")
        out.append(dict(src="Internet Archive", kind="video",
                        title=f"{h.get('title', '')} ({h['year']})" if h.get("year") else h.get("title", ""),
                        author=", ".join(cr) if isinstance(cr, list) else cr,
                        license=archive_lic(h),
                        page=f"https://archive.org/details/{ident}", thumb=f"https://archive.org/services/img/{ident}",
                        file="archive:" + ident))
    return out


def fetch(kw):
    cf = CACHE / f"{slug(kw)}.json"
    if cf.exists() and ("--no-fetch" in sys.argv or time.time() - cf.stat().st_mtime < 86400):
        return json.loads(cf.read_text())
    parts, failed = [], False
    for fn in (commons, nasa, archive, openverse):
        try:
            parts.append(fn(kw))
        except Exception as e:
            print(f"  {kw}: {fn.__name__} failed: {e}")
            failed |= fn is not openverse
            parts.append([])
    res = []                                   # interleave sources
    for i in range(max(map(len, parts), default=0)):
        res += [p[i] for p in parts if i < len(p) and p[i].get("thumb")]
    res = res[:24]
    if not failed:                             # don't cache incomplete results, retry them next build
        CACHE.mkdir(exist_ok=True)
        cf.write_text(json.dumps(res, ensure_ascii=False))
    return res


# ---------- licence badges (mirror of app.js lic()) ----------

def lic(raw):
    s = (raw or "").lower()
    code = "unknown"
    if "unverified" in s: code = "unknown"
    elif "pixabay" in s: code = "free"
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
    <a class="report" href="{esc(report_url(it))}" target="_blank" rel="noopener nofollow">Report</a>
    <div class="act"><button class="dl" type="button"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg><span>Download</span></button><button class="add" type="button" title="Adds this clip's author, source and license to your credit list. Paste the list into your YouTube description."><span class="plus">+</span><span>Credit line</span></button></div>
  </div>
</article>"""


# ---------- page shell ----------

def page(title, desc, body, rel, path, extra_head=""):
    ads = (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={ADSENSE_CLIENT}" '
           'crossorigin="anonymous"></script>') if ADSENSE_CLIENT else ""
    proxy = (f'<script>window.FC_PROXY = {json.dumps(PROXY_URL)}; window.FC_REPO = {json.dumps(REPO_URL)}; '
             f'window.FC_BLOCK = {json.dumps(sorted(BLOCKED))};</script>')
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
<script>try{{var t=localStorage.getItem("fc-theme");if(t)document.documentElement.dataset.theme=t}}catch(e){{}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/tom-select@2.4.3/dist/css/tom-select.min.css">
<link rel="stylesheet" href="{rel}style.css?v={ASSET_V}">
{ads}{extra_head}
</head>
<body>
<div class="glow" aria-hidden="true"><div class="icons"></div></div>
<header class="top"><div class="wrap">
  <a class="logo" href="{rel}">Footage<b>Credit</b></a>
  <nav><a href="{rel}free/">Browse topics</a><a href="{rel}about/">About</a>
    <button id="theme" type="button" aria-label="Switch light or dark theme" title="Light / dark theme">
      <svg class="sun" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
      <svg class="moon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    </button>
  </nav>
</div></header>
{body}
<footer><div class="wrap">
  <a href="{rel}about/">About</a><a href="{rel}terms/">Terms</a><a href="{rel}privacy/">Privacy</a><a href="{rel}report/">Report content</a>
  <span>We host no files. License badges are automatic: check the license on the source page before you publish.</span>
</div></footer>
<aside id="credits">
  <div class="panel">
    <h2>Your credit list</h2>
    <p>Many free clips may only be used if you name the author. This list writes that text for you: copy it and paste it into your YouTube video description.</p>
    <p>Clips that need a credit are added when you click <b>Download</b>. Use <b>+ Credit line</b> to add any other clip by hand.</p>
    <ul id="credits-list"></ul>
    <textarea id="credits-text" readonly placeholder="Example:
Footage credits:
• &quot;Ocean waves&quot; by Jane Doe (Wikimedia Commons), CC BY 4.0. https://…"></textarea>
    <div class="row"><button id="copy" type="button">Copy</button><button id="clear" type="button">Clear</button></div>
  </div>
  <p id="toast" role="status" hidden></p>
  <button id="credits-toggle" type="button">Credit list (<span id="credits-n">0</span>)</button>
</aside>
{proxy}<script src="https://cdn.jsdelivr.net/npm/tom-select@2.4.3/dist/js/tom-select.complete.min.js"></script>
<script src="{rel}app.js?v={ASSET_V}"></script>
</body>
</html>
"""


SEARCH_FORM = """<form id="search" role="search">
  <svg class="ico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
  <input name="q" type="search" placeholder="Search archival footage: newsreel, 1950s kitchen, apollo 11…" value="{value}" aria-label="Search footage" required>
  <select name="kind" aria-label="Media type"><option value="any">All media</option><option value="video">Video</option><option value="photo">Photo</option></select>
  <button type="submit">Search</button>
</form>
<ol class="how">
  <li><span class="n">1</span><h3>Search</h3><p>Type a topic, like “steam train” or “1950s kitchen”, and press Search. We check the Internet Archive, NASA, Wikimedia Commons and Openverse at once.</p></li>
  <li><span class="n">2</span><h3>Check the badge</h3><p>Green “commercial OK” means you can use it in a monetized video. “Credit required” means you must name the author. Orange means open the source page and check the license yourself.</p></li>
  <li><span class="n">3</span><h3>Download and copy credits</h3><p>Click Download. If the clip needs a credit, it’s added to your Credit list (bottom right). Open it, click Copy and paste it into your video description.</p></li>
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

    with ThreadPoolExecutor(3) as ex:
        data = dict(zip(keywords, ex.map(fetch, keywords)))

    # Thin pages hurt the whole site in search, so topics with few results are skipped.
    keywords = [k for k in keywords if len([it for it in data[k] if it["page"] not in BLOCKED]) >= MIN_RESULTS]

    # topic pages
    for kw in keywords:
        res, s = [it for it in data[kw] if it["page"] not in BLOCKED], slug(kw)
        others = [k for k in keywords if k != kw]
        start = keywords.index(kw)
        related = (others[start:] + others[:start])[:12]
        rel_links = "".join(f'<a href="../{slug(k)}/">{esc(k)}</a>' for k in related)
        n_free = sum(1 for it in res if lic(it["license"])[1])
        body = f"""<main class="wrap">
<section class="hero">
  <h1>Public domain {esc(kw)} footage and photos</h1>
  <p>{len(res)} archival and public-domain results for “{esc(kw)}” from the Internet Archive, NASA, Wikimedia Commons and Openverse, {n_free} of them cleared for monetized videos. Download a clip and its credit line is written for you.</p>
  {SEARCH_FORM.format(value=esc(kw))}
</section>
{AD_SLOT}
{FILTER_BAR.format(count=f"{len(res)} results")}
<div id="grid">{"".join(card(it, i) for i, it in enumerate(res))}</div>
<section class="related"><h2>Related topics</h2>{rel_links}</section>
</main>"""
        seed = f'<script type="application/json" id="seed">{json.dumps(res, ensure_ascii=False).replace("</", "<\\/")}</script>'
        write(f"free/{s}", page(f"{kw.capitalize()} footage, public domain & free | {SITE_NAME}",
                                f"Public domain and archival {kw} footage and photos from the Internet Archive, NASA and Wikimedia Commons. License badges and ready-to-paste YouTube credits.",
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
  <h1>Public domain archival footage for your videos</h1>
  <p>Old newsreels, educational and industrial films, home movies, NASA missions and historic photos from the Internet Archive, NASA, Wikimedia Commons and Openverse, in one search. Every result shows whether you can use it in a monetized video, and the credit block for your description writes itself as you download.</p>
  {SEARCH_FORM.format(value="")}
</section>
{AD_SLOT}
{FILTER_BAR.format(count="")}
<div id="grid"></div>
<section class="related"><h2>Popular topics</h2>{popular}</section>
</main>"""
    write("", page(f"{SITE_NAME}: public domain archival footage with auto credits",
                   "Search public domain archival footage, newsreels, vintage films and NASA video in one place. License badges and ready-to-paste YouTube credits.",
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
    ("terms", "Terms of use", """
<p>FootageCredit is a search tool. It does not host, sell or license any files. Results, thumbnails and downloads come directly from third-party archives (Wikimedia Commons, NASA Image and Video Library, Internet Archive, Openverse, Pixabay), and each archive's own terms apply.</p>
<h2>License badges are not legal advice</h2>
<p>Badges such as "commercial OK" or "no credit needed" are generated automatically from the license text the archive reports. Archives are often edited by volunteers, and that information can be wrong, incomplete or out of date. A file may also have been uploaded by someone who did not own it.</p>
<p>You are solely responsible for checking the license on the source page and for how you use any file you find here, including in monetized videos. Some uses need extra permission even when the license allows reuse, for example recognizable people, trademarks, logos or artwork shown in the footage.</p>
<h2>No warranty</h2>
<p>The site and the generated credits are provided "as is", without warranty of any kind. To the fullest extent permitted by law, FootageCredit is not liable for any claim, loss or damage arising from your use of the site or of any file found through it.</p>
<h2>Removal requests</h2>
<p>If a result infringes your rights, see <a href="../report/">Report content</a>. We remove reported results from our pages.</p>
<p>We may change these terms at any time. Continued use of the site means you accept the current version.</p>"""),
    ("report", "Report content", f"""
<p>Every result on FootageCredit links to a file hosted by a third-party archive. We can remove a result from our pages, but the file itself stays on the archive until the archive removes it.</p>
<h2>How to report</h2>
<p>Use the <b>Report</b> link under any result, or <a href="{REPO_URL}/issues/new?labels=report&title=Report" rel="nofollow">open a report</a> directly. Include the source page link and why it should be removed. For copyright claims, say who owns the work and link to proof of ownership.</p>
<p>We review reports and remove valid ones from search results and topic pages, usually within a few days.</p>
<h2>Removing the file at the source</h2>
<p>To take the file down everywhere, also contact the archive that hosts it: <a href="https://commons.wikimedia.org/wiki/Commons:Contact_us" rel="nofollow">Wikimedia Commons</a>, <a href="https://help.archive.org/help/how-do-i-request-to-remove-something-from-archive-org/" rel="nofollow">Internet Archive</a>, <a href="https://pixabay.com/service/contact/" rel="nofollow">Pixabay</a>, <a href="https://docs.openverse.org/" rel="nofollow">Openverse</a> (removes it from its index; the original host keeps the file).</p>"""),
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
