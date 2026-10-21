// FootageCredit: search free footage across open archives and build a ready-to-paste credits block.
// Open sources are queried directly; keyed ones (Pixabay) go through a tiny proxy set in window.FC_PROXY.

const $ = (s, el = document) => el.querySelector(s);
const enc = encodeURIComponent;

// ---------- license normalisation ----------
// Returns {code, commercial, attribution, label}
function lic(raw) {
  const s = (raw || "").toLowerCase();
  let code = "unknown";
  if (/unverified/.test(s)) code = "unknown";
  else if (/pixabay/.test(s)) code = "free";
  else if (/cc0|zero/.test(s)) code = "cc0";
  else if (/public.?domain|pdm|\bpd\b|no known copyright/.test(s)) code = "pd";
  else if (/nc/.test(s)) code = "nc";
  else if (/nd/.test(s)) code = "nd";
  else if (/by.?sa/.test(s)) code = "by-sa";
  else if (/\bby\b|cc.?by|attribution/.test(s)) code = "by";
  const commercial = ["free", "cc0", "pd", "by", "by-sa"].includes(code);
  const attribution = ["by", "by-sa", "nc", "nd"].includes(code);
  return { code, commercial, attribution, label: raw || "Check license" };
}

const strip = (h) => { const d = document.createElement("div"); d.innerHTML = h || ""; return d.textContent.trim(); };

// ---------- sources ----------
// Internet Archive: only items marked public domain (Prelinger and PD/CC0 licence URLs). Mirror of build.py.
const ARCHIVE_PD = ' AND (collection:prelinger OR licenseurl:"http://creativecommons.org/licenses/publicdomain/" OR licenseurl:"http://creativecommons.org/publicdomain/mark/1.0/" OR licenseurl:"http://creativecommons.org/publicdomain/zero/1.0/")';
const SOURCES = {
  async pixabay(q, kind) {
    if (!window.FC_PROXY) return [];
    const r = await fetch(`${window.FC_PROXY}?src=pixabay&q=${enc(q)}&kind=${kind}`);
    if (!r.ok) throw new Error("pixabay");
    return r.json();
  },
  async openverse(q, kind) {
    if (kind === "video") return [];
    const d = await (await fetch(`https://api.openverse.org/v1/images/?q=${enc(q)}&page_size=30&mature=false`)).json();
    return (d.results || []).map((h) => ({
      src: `Openverse / ${h.source || ""}`, kind: "photo", title: h.title || "", author: h.creator || "",
      license: `CC ${(h.license || "").toUpperCase()} ${h.license_version || ""}`.replace("CC CC0", "CC0").replace("CC PDM", "Public Domain").trim(),
      page: h.foreign_landing_url || "", thumb: h.thumbnail || h.url, file: h.url,
    }));
  },
  async commons(q, kind) {
    const ft = { video: " filetype:video", photo: " filetype:bitmap" }[kind] || "";
    const d = await (await fetch("https://commons.wikimedia.org/w/api.php?action=query&format=json&origin=*&generator=search&gsrnamespace=6"
      + `&gsrlimit=30&gsrsearch=${enc(q + ft)}&prop=imageinfo&iiprop=url|size|mime|extmetadata&iiurlwidth=480`)).json();
    return Object.values(d.query?.pages || {}).flatMap((p) => {
      const ii = (p.imageinfo || [{}])[0], m = ii.extmetadata || {}, mime = ii.mime || "";
      const video = mime.startsWith("video");
      if (!(mime.startsWith("image") || video) || mime === "image/svg+xml") return [];
      return [{
        src: "Wikimedia Commons", kind: video ? "video" : "photo", title: (p.title || "").replace("File:", ""),
        author: strip(m.Artist?.value).slice(0, 120), license: strip(m.LicenseShortName?.value),
        page: ii.descriptionurl || "", thumb: ii.thumburl || "", file: ii.url || "",
      }];
    });
  },
  async nasa(q, kind) {
    const mt = { video: "video", photo: "image" }[kind] || "image,video";
    const d = await (await fetch(`https://images-api.nasa.gov/search?q=${enc(q)}&media_type=${mt}&page_size=30`)).json();
    return (d.collection?.items || []).slice(0, 30).map((it) => {
      const dd = (it.data || [{}])[0];
      return {
        src: "NASA", kind: dd.media_type === "video" ? "video" : "photo", title: dd.title || "", author: dd.center || "NASA",
        license: "Public Domain (NASA)", page: `https://images.nasa.gov/details/${dd.nasa_id}`,
        thumb: (it.links || []).find((l) => l.rel === "preview")?.href || "", file: "nasa:" + it.href,
      };
    });
  },
  async archive(q, kind) {
    if (kind === "photo") return [];
    const ask = async (expr) => (await fetch("https://archive.org/advancedsearch.php?q=" + enc(`${expr} AND mediatype:movies` + ARCHIVE_PD)
      + "&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=year&fl[]=licenseurl&fl[]=collection&rows=24&sort[]=downloads+desc&output=json")).json();
    // Title/subject matches are far more relevant; fall back to full text when they are too few.
    let d = await ask(`(title:(${q}) OR subject:(${q}))`);
    if ((d.response?.docs || []).length < 5) d = await ask(`(${q})`);
    return (d.response?.docs || []).map((h) => ({
      src: "Internet Archive", kind: "video", title: `${h.title || ""}${h.year ? ` (${h.year})` : ""}`,
      author: [].concat(h.creator || "").join(", "),
      license: archiveLic(h), page: `https://archive.org/details/${h.identifier}`,
      thumb: `https://archive.org/services/img/${h.identifier}`, file: "archive:" + h.identifier,
    }));
  },
};

// Only Prelinger items are trusted as public domain; elsewhere the PD mark is the uploader's own claim.
function archiveLic(h) {
  if ([].concat(h.collection || []).includes("prelinger")) return "Public Domain (Prelinger Archives)";
  return h.licenseurl ? "Unverified: uploader marked it public domain" : "";
}

// Resolve the real file URL for sources that only give an index.
async function resolveFile(it) {
  if (it.file.startsWith("nasa:")) {
    // NASA's collection.json blocks CORS, but the files follow a fixed naming pattern.
    const base = it.file.slice(5).replace(/collection\.json$/, "");
    const id = base.split("/").filter(Boolean).pop();
    return it.kind === "video" ? `${base}${enc(id)}~orig.mp4` : `${base}${enc(id)}~orig.jpg`;
  }
  if (it.file.startsWith("archive:")) {
    const id = it.file.slice(8);
    const d = await (await fetch(`https://archive.org/metadata/${id}`)).json();
    const f = (d.files || []).filter((f) => /\.(mp4|webm|ogv|mpeg4)$/i.test(f.name))
      .sort((a, b) => (+b.size || 0) - (+a.size || 0))[0];
    return f ? `https://archive.org/download/${id}/${enc(f.name)}` : it.page;
  }
  return it.file;
}

// ---------- state ----------
let results = [];
const store = {
  get() { try { return JSON.parse(localStorage.getItem("fc-credits") || "[]"); } catch { return []; } },
  set(v) { try { localStorage.setItem("fc-credits", JSON.stringify(v)); } catch {} },
};
let credits = store.get();

// ---------- rendering ----------
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function card(it, i) {
  const L = lic(it.license);
  const badge = L.code === "unknown" ? `<span class="b warn">check license</span>`
    : `<span class="b ${L.commercial ? "ok" : "warn"}">${L.commercial ? "commercial OK" : "non-commercial"}</span>`
      + (L.attribution ? `<span class="b">credit required</span>` : `<span class="b ok">no credit needed</span>`);
  const added = credits.some((c) => c.page === it.page);
  const report = `${window.FC_REPO}/issues/new?labels=report&title=${enc("Report: " + it.page)}&body=${enc(`Source page: ${it.page}\nTitle: ${it.title}\n\nWhy should this result be removed? (for copyright claims: who owns the work, and a link to proof)\n`)}`;
  return `<article class="card" data-i="${i}">
  <a class="thumb" href="${esc(it.page)}" target="_blank" rel="noopener nofollow">
    <img loading="lazy" src="${esc(it.thumb)}" alt="${esc(it.title)}">${it.kind === "video" ? '<span class="kind">▶ video</span>' : ""}
  </a>
  <div class="meta">
    <h3 title="${esc(it.title)}">${esc(it.title || "Untitled")}</h3>
    <p class="by">${esc(it.author || "Unknown author")} · ${esc(it.src)}</p>
    <p class="lic">${esc(L.label)}</p>
    <div class="badges">${badge}</div>
    <a class="report" href="${esc(report)}" target="_blank" rel="noopener nofollow">Report</a>
    <div class="act">
      <button class="dl" type="button"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg><span>Download</span></button>
      <button class="add${added ? " on" : ""}" type="button" title="Adds this clip's author, source and license to your credit list. Paste the list into your YouTube description.">${added ? "✓ In credit list" : '<span class="plus">+</span><span>Credit line</span>'}</button>
    </div>
  </div>
</article>`;
}

function filtered() {
  const f = $("#lic")?.value || "any";
  return results.filter((it) => {
    const L = lic(it.license);
    return f === "any" || (f === "commercial" && L.commercial) || (f === "nocredit" && L.commercial && !L.attribution);
  });
}

function render() {
  const list = filtered();
  $("#grid").innerHTML = list.map((it) => card(it, results.indexOf(it))).join("")
    || `<p class="empty">Nothing matches these filters.</p>`;
  $("#count").textContent = `${list.length} results`;
}

function creditsText() {
  if (!credits.length) return "";
  return "Footage credits:\n" + credits.map((c) => {
    const L = lic(c.license);
    const by = c.author ? ` by ${c.author}` : "";
    return `• "${c.title || "Untitled"}"${by} (${c.src}), ${L.label}. ${c.page}`;
  }).join("\n");
}

function renderCredits() {
  $("#credits-n").textContent = credits.length;
  $("#credits-text").value = creditsText();
  $("#credits-list").innerHTML = credits.map((c, i) =>
    `<li><span>${esc(c.title || "Untitled")}</span><button type="button" data-rm="${i}" aria-label="Remove">✕</button></li>`).join("");
}

// Short notice above the credit list button.
let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 4000);
}

// ---------- search ----------
async function search(q, kind) {
  $("#grid").innerHTML = `<p class="empty">Searching…</p>`;
  $("#count").textContent = "";
  const errs = [];
  const parts = await Promise.all(Object.entries(SOURCES).map(([name, fn]) =>
    fn(q, kind).catch((e) => { errs.push(name); return []; })));
  // Interleave sources so one archive does not dominate the first screen.
  results = [];
  for (let i = 0; parts.some((p) => p[i]); i++) parts.forEach((p) => p[i] && results.push(p[i]));
  const blocked = new Set(window.FC_BLOCK || []);
  results = results.filter((it) => it.thumb && !blocked.has(it.page));
  render();
  if (errs.length) $("#count").textContent += ` · unavailable: ${errs.join(", ")}`;
}

// ---------- theme toggle ----------
function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("fc-theme", root.dataset.theme); } catch {}
}

// Film/camera icon field that eases after the cursor, plus a tilt on the hovered card.
function initGlow() {
  const g = $(".glow");
  if (!g || !matchMedia("(hover: hover)").matches || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let tx = innerWidth / 2, ty = innerHeight / 2, x = tx, y = ty, running = false, last = 0;
  const tick = (t) => {
    const k = 1 - Math.exp(-(last ? Math.min(t - last, 50) : 16) / 140);   // frame-rate independent easing
    last = t;
    x += (tx - x) * k; y += (ty - y) * k;
    g.style.setProperty("--mx", x + "px"); g.style.setProperty("--my", y + "px");
    running = Math.abs(tx - x) > 0.5 || Math.abs(ty - y) > 0.5;
    if (running) requestAnimationFrame(tick); else last = 0;
  };
  addEventListener("pointermove", (e) => {
    tx = e.clientX; ty = e.clientY; g.classList.add("on");
    if (!running) { running = true; requestAnimationFrame(tick); }
    const card = e.target.closest?.(".card");
    if (card) {                                // gentle 3D tilt towards the cursor
      const r = card.getBoundingClientRect();
      card.style.setProperty("--ry", ((e.clientX - r.left) / r.width - 0.5) * 6 + "deg");
      card.style.setProperty("--rx", (0.5 - (e.clientY - r.top) / r.height) * 6 + "deg");
    }
  }, { passive: true });
  document.addEventListener("pointerleave", () => g.classList.remove("on"));
  document.addEventListener("pointerout", (e) => {
    const card = e.target.closest?.(".card");
    if (card && !card.contains(e.relatedTarget)) { card.style.removeProperty("--rx"); card.style.removeProperty("--ry"); }
  });
}

// ---------- wiring ----------
document.addEventListener("DOMContentLoaded", () => {
  $("#theme")?.addEventListener("click", toggleTheme);
  initGlow();

  const seed = $("#seed");
  if (seed) { results = JSON.parse(seed.textContent); }

  const form = $("#search");
  const params = new URLSearchParams(location.search);
  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = form.q.value.trim();
    if (!q) return;
    const kind = form.kind.value;
    history.replaceState(null, "", `?q=${enc(q)}&kind=${kind}`);
    search(q, kind);
  });
  if (params.get("q") && form) {
    form.q.value = params.get("q");
    form.kind.value = params.get("kind") || "any";
    search(form.q.value, form.kind.value);
  }

  $("#lic")?.addEventListener("change", render);

  $("#grid").addEventListener("click", async (e) => {
    const cardEl = e.target.closest(".card");
    if (!cardEl) return;
    const it = results[+cardEl.dataset.i];
    if (e.target.closest(".add")) {
      const at = credits.findIndex((c) => c.page === it.page);
      at >= 0 ? credits.splice(at, 1) : credits.push(it);
      store.set(credits); renderCredits(); render();
      if (at < 0) toast("Credit line added. Open the credit list to copy it into your video description.");
    }
    if (e.target.closest(".dl")) {
      const btn = e.target.closest(".dl");
      btn.classList.add("busy");
      try { window.open(await resolveFile(it), "_blank", "noopener"); }
      catch { window.open(it.page, "_blank", "noopener"); }
      btn.classList.remove("busy");
      if (lic(it.license).attribution && !credits.some((c) => c.page === it.page)) {
        credits.push(it); store.set(credits); renderCredits(); render();
        toast("This clip needs the author named, so its credit line was added to your list.");
      }
    }
  });

  $("#credits-list").addEventListener("click", (e) => {
    const i = e.target.dataset.rm;
    if (i === undefined) return;
    credits.splice(+i, 1); store.set(credits); renderCredits(); render();
  });
  $("#copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(creditsText()); $("#copy").textContent = "Copied!"; }
    catch { $("#credits-text").select(); }
    setTimeout(() => ($("#copy").textContent = "Copy"), 1500);
  });
  $("#clear").addEventListener("click", () => { credits = []; store.set(credits); renderCredits(); render(); });
  $("#credits-toggle").addEventListener("click", () => $("#credits").classList.toggle("open"));

  renderCredits();
  if (results.length) render();

  // Styled dropdowns; the native selects stay in sync, so existing change handlers keep working.
  if (window.TomSelect) {
    document.querySelectorAll("#search select, #lic").forEach((el) =>
      new TomSelect(el, { controlInput: null, allowEmptyOption: true, maxOptions: null }));
  }
});
