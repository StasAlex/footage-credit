// Cloudflare Worker: keyless proxy for keyed stock APIs (Pixabay), so the static site never exposes the key.
// GET /?src=pixabay&q=ocean&kind=video|photo|any  ->  [{src, kind, title, author, license, page, thumb, file}]

const ALLOWED = ["https://stasalex.github.io", "http://localhost:8000"];
const DAY = 86400;

async function pixabay(q, kind, key) {
  const enc = encodeURIComponent;
  const jobs = [];
  if (kind !== "photo") jobs.push(fetch(`https://pixabay.com/api/videos/?key=${key}&q=${enc(q)}&per_page=30&safesearch=true`)
    .then((r) => r.json()).then((d) => (d.hits || []).map((h) => ({
      src: "Pixabay", kind: "video", title: h.tags || "", author: h.user || "", license: "Pixabay Content License",
      page: h.pageURL, thumb: h.videos?.tiny?.thumbnail || h.videos?.small?.thumbnail || "",
      file: h.videos?.large?.url || h.videos?.medium?.url || h.videos?.small?.url || h.pageURL,
    }))));
  if (kind !== "video") jobs.push(fetch(`https://pixabay.com/api/?key=${key}&q=${enc(q)}&per_page=30&image_type=photo&safesearch=true`)
    .then((r) => r.json()).then((d) => (d.hits || []).map((h) => ({
      src: "Pixabay", kind: "photo", title: h.tags || "", author: h.user || "", license: "Pixabay Content License",
      page: h.pageURL, thumb: h.webformatURL, file: h.largeImageURL || h.webformatURL,
    }))));
  const parts = await Promise.all(jobs);
  // Drop items Pixabay users tagged as AI-generated.
  return parts.flat().filter((it) => !/\bai generated\b/i.test(it.title));
}

export default {
  async fetch(req, env, ctx) {
    const origin = req.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOWED.includes(origin) ? origin : ALLOWED[0],
      "Vary": "Origin",
      "Content-Type": "application/json",
    };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });

    const url = new URL(req.url);
    const q = (url.searchParams.get("q") || "").trim().slice(0, 100);
    const kind = url.searchParams.get("kind") || "any";
    if (url.searchParams.get("src") !== "pixabay" || !q) return new Response("[]", { status: 400, headers: cors });

    // Pixabay asks API users to cache results for 24h.
    const cacheKey = new Request(`https://cache.local/pixabay?q=${encodeURIComponent(q.toLowerCase())}&kind=${kind}`);
    const cache = caches.default;
    const hit = await cache.match(cacheKey);
    if (hit) return new Response(hit.body, { headers: cors });

    try {
      const body = JSON.stringify(await pixabay(q, kind, env.PIXABAY_KEY));
      ctx.waitUntil(cache.put(cacheKey, new Response(body, { headers: { "Cache-Control": `max-age=${DAY}` } })));
      return new Response(body, { headers: cors });
    } catch {
      return new Response("[]", { status: 502, headers: cors });
    }
  },
};
