# FootageCredit: project brief for coding agents (Codex, Claude)

## What this is
A free, ad-supported website that searches public-domain and Creative Commons footage and photos
across open archives at once, and writes the attribution ("credits") block for a YouTube description
automatically. Target users: YouTube creators, especially faceless/documentary channels that need b-roll.

Monetization plan, in order:
1. Google AdSense (slots already in the template, switched on by `ADSENSE_CLIENT` in `build.py`).
2. Affiliate links to paid stock (Envato, Storyblocks, Adobe Stock): planned, not built yet.
3. Paid extras built on the credits feature (bulk ZIP download, saved projects): idea stage.

Traffic comes from SEO: each keyword in `keywords.txt` becomes a pre-rendered page
`/free/<slug>/` ("Free rocket launch footage and photos") with real results baked into the HTML.

## Architecture
Fully static. Hosted on GitHub Pages from `main:/docs`. No backend, no API keys in the browser.

- `build.py`: Python 3.12, stdlib only. Fetches results for every keyword (cached in `cache/` for 24h),
  renders all HTML pages into `docs/`, plus `sitemap.xml`, `robots.txt`, about and privacy pages.
  Config at the top: `SITE_NAME`, `BASE_URL`, `ADSENSE_CLIENT`.
- `app.js`: live search in the browser, license badges, Download button, credits drawer
  (stored in `localStorage` under `fc-credits`). On topic pages it reads baked results from
  `<script id="seed">` and makes the server-rendered cards interactive.
- `style.css`: single stylesheet, CSS variables on `:root`, dark mode via `prefers-color-scheme`, native nesting.
- `docs/` is GENERATED. Never edit it by hand; edit the sources and run `python3 build.py`.

The source adapters exist twice and must stay in sync: Python in `build.py`, JS in `app.js`
(`openverse`, `commons`, `nasa`, `archive`, and the `lic()` license classifier).

## Sources (all CORS-open, no key)
| Source | Media | Notes |
|---|---|---|
| Wikimedia Commons | photo, video | license from `extmetadata.LicenseShortName` |
| NASA Image and Video Library | photo, video | public domain; file URL built as `<base>/<id>~orig.mp4/jpg` because `collection.json` blocks CORS |
| Internet Archive | video | license often empty, shown as "check license"; file resolved via `/metadata/<id>` |
| Openverse | photo | rate-limits anonymous use hard; failures are tolerated |

Deliberately NOT used:
- **Coverr**: its API is free only for non-commercial use; an ad-funded site is commercial.
- **Pixabay** is live-search only, through the Cloudflare Worker in `worker/` (key in the Worker secret
  `PIXABAY_KEY`, 24h cache). Enabled when `PROXY_URL` in `build.py` is set. Not baked into topic pages,
  because Pixabay forbids permanent hotlinking of images. License badge code `free`: commercial OK, no credit.
- **YouTube / yt-dlp downloading**: violates YouTube terms and gets the site rejected by AdSense. Never add it.

## License badges (`lic()`)
- `cc0`, `pd`: commercial OK, no credit needed
- `by`, `by-sa`: commercial OK, credit required
- `nc`, `nd`: shown as non-commercial / credit required (ND also forbids editing, which footage always needs)
- anything unrecognised: "check license"

The site hosts no files; downloads go straight to the archive. Always tell users to verify on the source page.

## Commands
    python3 build.py             # fetch (respecting cache) and rebuild docs/
    python3 build.py --no-fetch  # rebuild templates from cache only
    python3 -m http.server -d docs 8000   # local preview at http://localhost:8000

## Rules
- UI and all site copy in English (global audience, higher ad rates).
- Keep it static and dependency-free unless there is a strong reason.
- Don't invent license facts; if a source doesn't state a license, show "check license".
- AdSense needs a privacy page (exists) and, in practice, a custom domain; a `github.io` subdomain usually isn't accepted.

## Status (2026-10-16)
- MVP built: home with live search, 40 topic pages, topics index, about, privacy, sitemap.
- Not yet: GitHub repo + Pages, custom domain, AdSense application, Google Search Console, affiliate block, more keywords.
