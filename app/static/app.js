/* Campkins Batch Uploader interface.
   Plain JS on purpose: a state object and a screen switch, no framework.
   Everything rendered from server data goes through esc() because sheet
   cells and scraped alt text are untrusted input. */

"use strict";

const state = {
  runId: null,
  rows: [],
  problems: [],
  scraping: false,
  rowIndex: null,
  product: null,   // {row, images, extracted, total_rows}
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("is-bad", bad);
  el.classList.remove("is-hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("is-hidden"), 4500);
}

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", headers: {} };
  if (options.form) {
    opts.body = options.form;
  } else if (options.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, opts);
  let data = {};
  try { data = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    throw new Error(data.detail || data.error || `Request failed (${response.status})`);
  }
  return data;
}

function slugify(value) {
  return String(value || "").toLowerCase().normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^\w\s-]/g, "").trim()
    .replace(/[-\s_]+/g, "-")
    .replace(/^-+|-+$/g, "") || "product";
}

function kb(bytes) {
  if (!bytes) return "0 KB";
  return bytes >= 1024 * 1024
    ? (bytes / 1048576).toFixed(1) + " MB"
    : Math.round(bytes / 1024) + " KB";
}

/* --- screens ----------------------------------------------------------- */

const SCREENS = ["home", "run", "review", "confirm", "settings"];

function show(name) {
  SCREENS.forEach((s) => $(`screen-${s}`).classList.toggle("is-hidden", s !== name));
  if (name !== "run") stopPolling();
  window.scrollTo(0, 0);
}

/* --- header ------------------------------------------------------------ */

async function loadStatus() {
  try {
    const settings = await api("/api/settings");
    $("site-label").textContent = settings.site_url || "Site not set up. Open Settings.";
  } catch (err) {
    $("site-label").textContent = "Could not reach the local server.";
  }
}

async function testConnection(logEl) {
  const dot = $("conn-dot");
  dot.className = "dot";
  try {
    const result = await api("/api/connection");
    const good = result.wordpress && result.woocommerce;
    dot.classList.add(good ? "is-good" : "is-bad");
    const lines = [
      `WordPress media:  ${result.wordpress ? "OK" : "FAILED"}`,
      `WooCommerce:      ${result.woocommerce ? "OK" : "FAILED"}`,
      ...result.errors,
    ];
    if (logEl) {
      logEl.textContent = lines.join("\n");
      logEl.className = `log ${good ? "is-good" : "is-bad"}`;
    } else {
      toast(good ? "Both connections OK." : lines.join(" "), !good);
    }
  } catch (err) {
    dot.classList.add("is-bad");
    toast(err.message, true);
  }
}

/* --- home -------------------------------------------------------------- */

async function showHome() {
  show("home");
  try {
    const data = await api("/api/runs");
    const list = $("runs-list");
    if (!data.runs.length) {
      list.innerHTML = '<p class="muted">No runs yet.</p>';
      return;
    }
    list.innerHTML = data.runs.map((run) => {
      const date = new Date(run.created_at * 1000).toLocaleString("en-GB");
      const counts = run.counts || {};
      const done = counts.uploaded || 0;
      return `<div class="run-item">
        <div class="run-item__name">
          <div>${esc(run.sheet_name || "Run " + run.id)}</div>
          <div class="mono muted">${esc(date)} &middot; ${run.total_rows} products
            &middot; ${done} uploaded</div>
        </div>
        <button class="btn" data-open-run="${run.id}">Open</button>
      </div>`;
    }).join("");
    list.querySelectorAll("[data-open-run]").forEach((btn) => {
      btn.addEventListener("click", () => openRun(Number(btn.dataset.openRun)));
    });
  } catch (err) {
    toast(err.message, true);
  }
}

async function startRun() {
  const input = $("sheet-file");
  if (!input.files.length) {
    toast("Choose a CSV or Excel file first.", true);
    return;
  }
  const button = $("start-run");
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span>Reading sheet';
  try {
    const form = new FormData();
    form.append("file", input.files[0]);
    const data = await api("/api/run", { method: "POST", form });
    state.problems = data.problems || [];
    await api(`/api/run/${data.run_id}/scrape`, { method: "POST" });
    openRun(data.run_id);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Start run";
  }
}

/* --- run overview ------------------------------------------------------ */

async function openRun(runId) {
  state.runId = runId;
  show("run");
  try {
    const data = await api(`/api/run/${runId}`);
    state.rows = data.rows;
    state.scraping = data.scraping;
    $("run-title").textContent = data.run.sheet_name || `Run ${runId}`;
    $("run-dir").textContent = data.run.dir;
    $("batch-log").classList.add("is-hidden");
    const problemsEl = $("run-problems");
    if (state.problems.length) {
      problemsEl.innerHTML = state.problems.map((p) => `<p>${esc(p)}</p>`).join("");
      problemsEl.classList.remove("is-hidden");
    } else {
      problemsEl.classList.add("is-hidden");
    }
    renderRunRows();
    if (state.scraping || state.rows.some((r) => ["pending", "scraping"].includes(r.status))) {
      startPolling();
    }
  } catch (err) {
    toast(err.message, true);
  }
}

const STATUS_LABELS = {
  pending: "Waiting", scraping: "Scraping…", scraped: "Ready to review",
  failed: "Failed", reviewed: "Reviewed", uploaded: "Uploaded",
  upload_failed: "Upload failed", skipped: "Skipped",
};

function renderRunRows() {
  const body = $("run-rows");
  body.innerHTML = state.rows.map((row) => {
    const label = STATUS_LABELS[row.status] || row.status;
    const reviewable = ["scraped", "reviewed", "uploaded", "upload_failed", "skipped"].includes(row.status);
    const actions = [];
    if (reviewable) {
      actions.push(`<button class="btn btn--ghost" data-review="${row.row_index}">Review</button>`);
    }
    if (row.status === "failed"
        || (row.status === "scraped" && !row.image_count)) {
      actions.push(`<button class="btn btn--ghost" data-rescrape="${row.row_index}">Retry</button>`);
    }
    if (row.status === "uploaded" && row.edit_url) {
      actions.push(`<a class="btn btn--ghost" href="${esc(row.edit_url)}" target="_blank" rel="noopener">WP admin</a>`);
    }
    const error = row.error
      ? `<div class="mono muted" title="${esc(row.error)}">${esc(row.error.slice(0, 80))}</div>` : "";
    const spin = row.status === "scraping" ? '<span class="spin"></span>' : "";
    const ready = row.ready && !["uploaded", "skipped"].includes(row.status)
      ? ' <span class="chip chip--ready">Ready</span>' : "";
    return `<tr>
      <td class="mono">${row.row_index}</td>
      <td>${esc(row.name)}${error}</td>
      <td>${spin}<span class="chip chip--${esc(row.status)}">${esc(label)}</span>${ready}</td>
      <td class="mono">${row.image_count || 0}${row.kept_count ? ` (${row.kept_count} kept)` : ""}</td>
      <td>${actions.join(" ")}</td>
    </tr>`;
  }).join("");

  body.querySelectorAll("[data-review]").forEach((btn) => {
    btn.addEventListener("click", () => openReview(Number(btn.dataset.review)));
  });
  body.querySelectorAll("[data-rescrape]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/run/${state.runId}/row/${btn.dataset.rescrape}/rescrape`,
          { method: "POST" });
        startPolling();
      } catch (err) { toast(err.message, true); }
    });
  });

  const total = state.rows.length;
  const doneCount = state.rows.filter((r) => !["pending", "scraping"].includes(r.status)).length;
  $("run-progress").textContent = state.scraping
    ? `Scraping ${doneCount}/${total}…` : `${total} products`;

  const readyCount = state.rows.filter((r) =>
    r.ready && !["uploaded", "skipped"].includes(r.status)).length;
  const readyBtn = $("upload-ready-btn");
  readyBtn.classList.toggle("is-hidden", !readyCount);
  readyBtn.textContent = `Upload all ready (${readyCount})`;
}

async function uploadAllReady() {
  const button = $("upload-ready-btn");
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span>Uploading';
  try {
    const result = await api(`/api/run/${state.runId}/upload-ready`,
      { method: "POST" });
    await openRun(state.runId);
    const lines = result.results.map((r) => {
      if (!r.ok) return `${r.row_index}. ${r.name}  ->  FAILED: ${r.error}`;
      let line = `${r.row_index}. ${r.name}  ->  draft ${r.product_id}`;
      (r.warnings || []).forEach((w) => { line += `\n    Note: ${w}`; });
      return line;
    });
    if (!lines.length) lines.push("No products are marked ready.");
    const good = result.results.length && result.results.every((r) => r.ok);
    const log = $("batch-log");
    log.textContent = lines.join("\n");
    log.className = `log ${good ? "is-good" : "is-bad"}`;
    log.classList.remove("is-hidden");
    const done = result.results.filter((r) => r.ok).length;
    toast(`${done} of ${result.attempted} uploaded as drafts.`, !good);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    renderRunRows();
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      const data = await api(`/api/run/${state.runId}/progress`);
      state.scraping = data.scraping;
      // Merge status fields; the full row objects come from openRun.
      data.rows.forEach((update) => {
        const row = state.rows.find((r) => r.row_index === update.row_index);
        if (row) Object.assign(row, update);
      });
      renderRunRows();
      if (!data.scraping && !data.rows.some((r) => ["pending", "scraping"].includes(r.status))) {
        stopPolling();
        openRun(state.runId); // Pick up edit URLs and counts in full.
      }
    } catch (_) { /* transient; keep polling */ }
  }, 1500);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/* --- review ------------------------------------------------------------ */

async function openReview(rowIndex) {
  try {
    const data = await api(`/api/run/${state.runId}/product/${rowIndex}`);
    state.rowIndex = rowIndex;
    state.product = data;
    data.images.forEach((img) => {
      img.keep = !!img.keep;
      img.custom_name = img.custom_name || "";
      img.suffix = img.suffix || "";
      img.alt_custom = !!img.alt_custom;
      // A saved custom name means the switch was on when it was saved.
      img.use_custom = !!img.custom_name;
    });
    show("review");

    const row = data.row;
    $("review-counter").textContent = `Product ${rowIndex} of ${data.total_rows}`;
    $("review-name").textContent = row.name;
    $("review-url").innerHTML =
      `<a href="${esc(row.url)}" target="_blank" rel="noopener">${esc(row.url)}</a>`;
    $("review-prev").disabled = rowIndex <= 1;
    $("review-next").disabled = rowIndex >= data.total_rows;
    $("suffix").value = row.suffix || "";
    $("editor").innerHTML = row.description_html || "";
    renderExtracted(data.extracted);

    const failures = data.failures || [];
    const failEl = $("review-failures");
    if (failures.length) {
      failEl.textContent = `${failures.length} image(s) could not be downloaded from the page.`;
      failEl.classList.remove("is-hidden");
    } else {
      failEl.classList.add("is-hidden");
    }
    renderGrid();
  } catch (err) {
    toast(err.message, true);
  }
}

function keptImages() {
  return state.product.images.filter((i) => i.keep)
    .sort((a, b) => (a.position || 999) - (b.position || 999));
}

/* Image naming.

   prefix   the "Filename string" field, or the product name when empty
   suffix   per image: the zero-padded position unless the user typed one
   custom   per image, behind a switch: the whole filename, typed by hand

   filename  custom ? custom.webp : prefix-suffix.webp   (all slugified)
   alt text  custom ? custom : "prefix suffix"           (unless hand-set) */

function filenamePrefix() {
  return $("suffix").value.trim() || state.product.row.name;
}

function autoSuffix(position, total) {
  const width = Math.max(2, String(total).length);
  return String(position).padStart(width, "0");
}

function effectiveSuffix(img, total) {
  return img.suffix || autoSuffix(img.position, total);
}

function computeName(img, total) {
  if (img.use_custom) {
    return `${slugify(img.custom_name)}.webp`;
  }
  return `${slugify(filenamePrefix())}-${slugify(effectiveSuffix(img, total))}.webp`;
}

function computeAlt(img, total) {
  if (img.use_custom) return img.custom_name.trim();
  return `${filenamePrefix()} ${effectiveSuffix(img, total)}`;
}

function renumber() {
  const kept = keptImages();
  kept.forEach((img, i) => {
    img.position = i + 1;
    img.filename = computeName(img, kept.length);
    if (!img.alt_custom) img.alt_text = computeAlt(img, kept.length);
  });
}

function renderGrid() {
  renumber();
  const grid = $("review-grid");
  const kept = keptImages();
  const row = state.product.row;

  grid.innerHTML = state.product.images.map((img, index) => {
    const isThumb = img.keep && row.thumbnail_sha === img.sha256;
    const badge = img.keep
      ? `<span class="frame__no">${String(img.position).padStart(2, "0")}</span>` : "";
    const thumbTag = isThumb ? '<span class="frame__thumbtag">THUMBNAIL</span>' : "";
    const fields = img.keep ? `
      <div class="frame__fields">
        <label class="switch" title="Type the whole filename instead of a suffix">
          <input type="checkbox" data-custom-toggle="${index}" ${img.use_custom ? "checked" : ""}>
          <span class="switch__track"></span>
          <span class="switch__label">Custom filename</span>
        </label>
        <input class="field" data-custom-name="${index}" value="${esc(img.custom_name)}"
               placeholder="Custom filename" title="Whole filename, without .webp"
               ${img.use_custom ? "" : "hidden"}>
        <input class="field" data-suffix="${index}"
               value="${esc(effectiveSuffix(img, kept.length))}"
               placeholder="Suffix" title="Suffix after the filename string"
               ${img.use_custom ? "hidden" : ""}>
        <input class="field" data-alt="${index}" value="${esc(img.alt_text)}"
               placeholder="Alt text" title="Alt text">
        <div class="frame__name mono muted" data-name="${index}">${esc(img.filename)}</div>
        <label class="frame__radio">
          <input type="radio" name="thumb" data-thumb="${index}"
                 ${isThumb ? "checked" : ""}> Use as thumbnail
        </label>
      </div>` : "";
    return `<article class="frame ${img.keep ? "is-keep" : "is-drop"}">
      ${badge}${thumbTag}
      <img class="frame__img" src="${esc(img.url)}" loading="lazy"
           data-toggle="${index}" alt="">
      <div class="frame__meta">
        <span>${kb(img.orig_bytes)} src</span>
        <span>${img.width}&times;${img.height} &middot; ${kb(img.webp_bytes)}</span>
      </div>
      ${fields}
    </article>`;
  }).join("");

  $("kept-count").textContent = `${kept.length} of ${state.product.images.length} kept`;
  $("filename-preview").textContent = kept.length
    ? `${kept[0].filename}${kept.length > 1 ? ` … ${kept[kept.length - 1].filename}` : ""}`
    : "No images kept yet.";

  grid.querySelectorAll("[data-toggle]").forEach((el) => {
    el.addEventListener("click", () => {
      const img = state.product.images[Number(el.dataset.toggle)];
      img.keep = !img.keep;
      if (img.keep) {
        const max = Math.max(0, ...state.product.images.filter((i) => i.keep && i !== img)
          .map((i) => i.position || 0));
        img.position = max + 1;
        if (!state.product.row.thumbnail_sha) {
          state.product.row.thumbnail_sha = img.sha256; // First pick leads.
        }
      } else {
        img.position = 0;
        if (state.product.row.thumbnail_sha === img.sha256) {
          state.product.row.thumbnail_sha = "";
        }
      }
      renderGrid();
    });
  });
  // Typing in a card must not redraw the grid, or the field loses focus.
  // Only the derived name and alt text on that card are refreshed.
  const refreshCard = (index) => {
    renumber();
    const img = state.product.images[index];
    const nameEl = grid.querySelector(`[data-name="${index}"]`);
    const altEl = grid.querySelector(`[data-alt="${index}"]`);
    if (nameEl) nameEl.textContent = img.filename;
    if (altEl && !img.alt_custom) altEl.value = img.alt_text;
    const kept = keptImages();
    $("filename-preview").textContent =
      `${kept[0].filename}${kept.length > 1 ? ` … ${kept[kept.length - 1].filename}` : ""}`;
  };
  grid.querySelectorAll("[data-custom-toggle]").forEach((el) => {
    el.addEventListener("change", () => {
      const img = state.product.images[Number(el.dataset.customToggle)];
      img.use_custom = el.checked;
      renderGrid();
      if (img.use_custom) {
        const box = grid.querySelector(`[data-custom-name="${el.dataset.customToggle}"]`);
        if (box) box.focus();
      }
    });
  });
  grid.querySelectorAll("[data-custom-name]").forEach((el) => {
    el.addEventListener("input", () => {
      const index = Number(el.dataset.customName);
      state.product.images[index].custom_name = el.value;
      refreshCard(index);
    });
  });
  grid.querySelectorAll("[data-suffix]").forEach((el) => {
    el.addEventListener("input", () => {
      const index = Number(el.dataset.suffix);
      state.product.images[index].suffix = el.value.trim();
      refreshCard(index);
    });
    // An emptied suffix goes back to the position number.
    el.addEventListener("blur", () => {
      const index = Number(el.dataset.suffix);
      const img = state.product.images[index];
      if (!img.suffix) el.value = effectiveSuffix(img, keptImages().length);
    });
  });
  grid.querySelectorAll("[data-alt]").forEach((el) => {
    el.addEventListener("input", () => {
      const index = Number(el.dataset.alt);
      const img = state.product.images[index];
      img.alt_text = el.value;
      // Clearing the box hands the alt text back to the automatic rule.
      img.alt_custom = el.value.trim() !== "";
      if (!img.alt_custom) refreshCard(index);
    });
  });
  grid.querySelectorAll("[data-thumb]").forEach((el) => {
    el.addEventListener("change", () => {
      state.product.row.thumbnail_sha =
        state.product.images[Number(el.dataset.thumb)].sha256;
      renderGrid();
    });
  });
}

function renderExtracted(extracted) {
  const panel = $("extracted-panel");
  const blocks = [
    ["Structured data", extracted.json_ld],
    ["Main content", extracted.main_block],
    ["Meta description", extracted.meta],
  ].filter(([, text]) => text && text.trim());
  if (!blocks.length) {
    panel.innerHTML = '<p class="muted">Nothing usable was extracted. Paste '
      + "the description in, or write one.</p>";
    return;
  }
  // The main block arrives as HTML so its headings and lists survive;
  // the other two are plain text. Both are shown and inserted as the
  // same cleaned HTML the editor will hold.
  const asHtml = blocks.map(([, text]) => toEditorHtml(text));
  panel.innerHTML = blocks.map(([label], i) => `
    <div class="extract-block">
      <div class="extract-block__head">
        <span class="mono muted">${esc(label)}</span>
        <button class="btn btn--tool" data-insert="${i}">Insert</button>
      </div>
      <div class="extract-block__text" data-text="${i}">${asHtml[i]}</div>
    </div>`).join("");
  panel.querySelectorAll("[data-insert]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("editor").innerHTML += asHtml[Number(btn.dataset.insert)];
    });
  });
}

/* Plain text becomes paragraphs; lines that start with a bullet or a
   number become a list. Mirrors _text_to_html on the server. */
const BULLET = /^(?:[-*•▪●–]|\d+[.)])\s+/;

function textToHtml(text) {
  const out = [];
  let inList = false;
  for (const raw of text.split(/\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const m = BULLET.exec(line);
    if (m) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${esc(line.slice(m[0].length))}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    out.push(`<p>${esc(line)}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("");
}

function toEditorHtml(text) {
  if (/<[a-z][^>]*>/i.test(text)) {
    const template = document.createElement("template");
    template.innerHTML = text;
    return cleanFragment(template.content, SCRAPE_REMAP);
  }
  return textToHtml(text);
}

/* Whitelist paste: manufacturer pages arrive as span soup with inline
   styles and tracking pixels. Same rule as the server applies again. */
const PASTE_ALLOWED = new Set(["P", "BR", "UL", "OL", "LI", "STRONG", "EM",
  "B", "I", "H1", "H2", "H3", "H4", "H5", "H6", "A",
  "TABLE", "THEAD", "TBODY", "TFOOT", "TR", "TH", "TD"]);
const PASTE_DROP = new Set(["SCRIPT", "STYLE", "IFRAME", "NOSCRIPT", "SVG",
  "IMG", "VIDEO", "FORM", "BUTTON", "SELECT", "HEAD", "TITLE"]);
// Applied to scraped copy only: a supplier page's h1 becomes h2, since
// the shop page already has an h1. Headings typed or pasted stay as
// they are. Same rule as SCRAPE_REMAP on the server.
const SCRAPE_REMAP = { H1: "H2" };
// Dropped containers whose text would otherwise run into the next one.
const PASTE_BLOCK = new Set(["DIV", "SECTION", "ARTICLE", "ASIDE", "HEADER",
  "FOOTER", "MAIN", "FIGURE", "FIGCAPTION", "BLOCKQUOTE", "DL", "DT", "DD",
  "PRE", "ADDRESS", "DETAILS", "SUMMARY", "NAV"]);
const BLOCK_SELECTOR = "p,ul,ol,li,h1,h2,h3,h4,h5,h6,table,div,section,"
  + "article,blockquote,dl,pre";

function cleanFragment(parent, remap = {}) {
  let out = "";
  for (const node of parent.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) {
      out += esc(node.textContent);
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = remap[node.tagName] || node.tagName;
      if (PASTE_DROP.has(tag)) continue;
      const inner = cleanFragment(node, remap);
      if (!PASTE_ALLOWED.has(tag)) {
        // A container holding only inline content is one paragraph.
        const wrap = PASTE_BLOCK.has(tag) && inner.trim()
          && !node.querySelector(BLOCK_SELECTOR);
        out += wrap ? `<p>${inner}</p>` : inner;
        continue;
      }
      if (tag === "BR") { out += "<br>"; continue; }
      if (tag === "A") {
        const href = node.getAttribute("href") || "";
        out += /^https?:\/\//.test(href)
          ? `<a href="${esc(href)}">${inner}</a>` : inner;
        continue;
      }
      const t = tag.toLowerCase();
      out += `<${t}>${inner}</${t}>`;
    }
  }
  return out;
}

/* Editor toolbar. Buttons carry data-cmd (an execCommand name) and an
   optional data-value; the style select applies formatBlock. Links use a
   small inline box instead of a browser prompt, so the selection is kept
   across the click and restored before the command runs. */
let savedRange = null;

function saveRange() {
  const sel = window.getSelection();
  if (sel.rangeCount && $("editor").contains(sel.anchorNode)) {
    savedRange = sel.getRangeAt(0).cloneRange();
  }
}

function restoreRange() {
  $("editor").focus();
  if (!savedRange) return;
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(savedRange);
}

function linkAtSelection() {
  const sel = window.getSelection();
  let node = sel.rangeCount ? sel.anchorNode : null;
  while (node && node !== $("editor")) {
    if (node.nodeType === Node.ELEMENT_NODE && node.tagName === "A") return node;
    node = node.parentNode;
  }
  return null;
}

function currentBlockTag() {
  const sel = window.getSelection();
  let node = sel.rangeCount ? sel.anchorNode : null;
  while (node && node !== $("editor")) {
    if (node.nodeType === Node.ELEMENT_NODE
        && /^(P|H[1-6]|LI)$/.test(node.tagName)) {
      return node.tagName === "LI" ? "P" : node.tagName;
    }
    node = node.parentNode;
  }
  return "P";
}

function applyLink() {
  let href = $("link-url").value.trim();
  if (!href) return;
  // The sanitiser drops anything that is not http(s); adding the scheme
  // here saves the link from vanishing silently on save.
  if (!/^https?:\/\//i.test(href)) href = `https://${href}`;
  restoreRange();
  const existing = linkAtSelection();
  const sel = window.getSelection();
  if (existing) {
    existing.setAttribute("href", href);
  } else if (!sel.rangeCount || sel.isCollapsed) {
    // Nothing selected: insert the address itself as the link text.
    document.execCommand("insertHTML", false,
      `<a href="${esc(href)}">${esc(href)}</a>`);
  } else {
    document.execCommand("createLink", false, href);
  }
  closeLinkBox();
}

function removeLink() {
  restoreRange();
  const existing = linkAtSelection();
  if (existing) {
    // unlink needs the whole anchor selected to remove it cleanly.
    const range = document.createRange();
    range.selectNodeContents(existing);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  document.execCommand("unlink", false, null);
  closeLinkBox();
}

function openLinkBox() {
  saveRange();
  const existing = linkAtSelection();
  $("link-url").value = existing ? existing.getAttribute("href") || "" : "";
  $("link-box").hidden = false;
  $("link-url").focus();
}

function closeLinkBox() {
  $("link-box").hidden = true;
}

function wireToolbar() {
  const editor = $("editor");
  // Without this, Enter after a heading starts a div rather than a
  // paragraph, and the cleaner has to guess at the structure later.
  document.execCommand("defaultParagraphSeparator", false, "p");
  document.querySelectorAll("[data-cmd]").forEach((btn) => {
    // mousedown, not click: by click time the editor has lost focus and
    // with it the selection the command should apply to.
    btn.addEventListener("mousedown", (event) => event.preventDefault());
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      editor.focus();
      document.execCommand(btn.dataset.cmd, false, btn.dataset.value || null);
      $("block-style").value = currentBlockTag();
    });
  });
  const style = $("block-style");
  style.addEventListener("mousedown", saveRange);
  style.addEventListener("change", () => {
    restoreRange();
    document.execCommand("formatBlock", false, `<${style.value}>`);
  });
  const syncStyle = () => { style.value = currentBlockTag(); };
  editor.addEventListener("keyup", syncStyle);
  editor.addEventListener("mouseup", syncStyle);

  $("link-open").addEventListener("mousedown", (event) => event.preventDefault());
  $("link-open").addEventListener("click", (event) => {
    event.preventDefault();
    openLinkBox();
  });
  $("link-apply").addEventListener("click", applyLink);
  $("link-remove").addEventListener("click", removeLink);
  $("link-cancel").addEventListener("click", closeLinkBox);
  $("link-url").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); applyLink(); }
    if (event.key === "Escape") closeLinkBox();
  });
}

function wirePaste() {
  $("editor").addEventListener("paste", (event) => {
    event.preventDefault();
    const html = event.clipboardData.getData("text/html");
    let clean;
    if (html) {
      const template = document.createElement("template");
      template.innerHTML = html;
      clean = cleanFragment(template.content);
    } else {
      clean = textToHtml(event.clipboardData.getData("text/plain"));
    }
    document.execCommand("insertHTML", false, clean);
  });
}

function collectSelection() {
  return {
    suffix: $("suffix").value.trim(),
    description_html: $("editor").innerHTML,
    thumbnail_sha: state.product.row.thumbnail_sha || "",
    images: state.product.images.map((img) => ({
      sha256: img.sha256,
      keep: img.keep,
      position: img.position || 0,
      filename: img.keep ? img.filename : "",
      alt_text: img.keep ? img.alt_text : "",
      custom_name: img.keep && img.use_custom ? img.custom_name.trim() : "",
      suffix: img.keep ? img.suffix : "",
      alt_custom: img.keep && img.alt_custom,
    })),
  };
}

async function saveSelection(continueOn) {
  const kept = keptImages();
  if (kept.some((i) => i.use_custom && !i.custom_name.trim())) {
    toast("A custom filename is switched on but empty. Type a name or switch it off.", true);
    return;
  }
  const names = kept.map((i) => i.filename);
  if (new Set(names).size !== names.length) {
    toast("Two kept images share a filename. Make them unique.", true);
    return;
  }
  try {
    await api(`/api/run/${state.runId}/product/${state.rowIndex}/selection`,
      { method: "POST", body: collectSelection() });
    if (continueOn) {
      openConfirm(state.rowIndex);
    } else {
      toast("Saved.");
    }
  } catch (err) {
    toast(err.message, true);
  }
}

/* --- confirmation ------------------------------------------------------ */

async function openConfirm(rowIndex) {
  try {
    const data = await api(`/api/run/${state.runId}/product/${rowIndex}`);
    state.rowIndex = rowIndex;
    state.product = data;
    show("confirm");
    const row = data.row;
    $("confirm-counter").textContent = `Product ${rowIndex} of ${data.total_rows}`;
    $("confirm-name-label").textContent = row.name;
    $("confirm-name").value = row.name;
    $("confirm-slug").value = row.slug || "";
    $("confirm-sku").value = row.sku || "";
    $("confirm-price").value = row.cost || "";
    $("confirm-ean").value = row.ean || "";
    $("confirm-brand").value = row.brand || "";
    $("confirm-categories").value = row.categories || "";
    $("confirm-weight").value = row.weight || "";
    $("confirm-dimensions").value = row.dimensions || "";
    $("meta-title").value = row.meta_title || "";
    $("meta-desc").value = row.meta_description || "";
    updateCounters();
    $("confirm-desc").innerHTML = row.description_html || "<p>(no description)</p>";
    $("exists-box").classList.add("is-hidden");
    $("upload-log").classList.add("is-hidden");
    renderReadyButton();

    const kept = data.images.filter((i) => i.keep)
      .sort((a, b) => (a.position || 999) - (b.position || 999));
    $("confirm-thumbs").innerHTML = kept.map((img) => `
      <figure class="${row.thumbnail_sha === img.sha256 ? "is-thumb" : ""}">
        <img src="${esc(img.url)}" alt="">
        <figcaption title="${esc(img.filename)}">${esc(img.filename)}</figcaption>
      </figure>`).join("")
      || '<p class="muted">No images kept.</p>';
  } catch (err) {
    toast(err.message, true);
  }
}

function updateCounters() {
  const title = $("meta-title").value.length;
  const desc = $("meta-desc").value.length;
  const titleEl = $("meta-title-count");
  const descEl = $("meta-desc-count");
  titleEl.textContent = `${title}/60`;
  descEl.textContent = `${desc}/155`;
  titleEl.classList.toggle("is-over", title > 60);
  descEl.classList.toggle("is-over", desc > 155);
}

async function generateMeta() {
  const button = $("gen-meta");
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span>Generating';
  try {
    const result = await api(
      `/api/run/${state.runId}/product/${state.rowIndex}/meta`,
      { method: "POST" });
    $("meta-title").value = result.title;
    $("meta-desc").value = result.meta_description;
    updateCounters();
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Generate with AI";
  }
}

function collectConfirmFields() {
  return {
    name: $("confirm-name").value,
    slug: $("confirm-slug").value,
    sku: $("confirm-sku").value,
    price: $("confirm-price").value,
    ean: $("confirm-ean").value,
    brand: $("confirm-brand").value,
    categories: $("confirm-categories").value,
    weight: $("confirm-weight").value,
    dimensions: $("confirm-dimensions").value,
    meta_title: $("meta-title").value,
    meta_description: $("meta-desc").value,
  };
}

function renderReadyButton() {
  const marked = !!state.product.row.ready;
  const button = $("ready-btn");
  button.textContent = marked ? "Ready ✓ (click to unmark)"
    : "Mark ready to upload";
  button.classList.toggle("btn--ghost", marked);
}

async function toggleReady() {
  const button = $("ready-btn");
  const makeReady = !state.product.row.ready;
  button.disabled = true;
  try {
    await api(`/api/run/${state.runId}/product/${state.rowIndex}/ready`,
      { method: "POST",
        body: { ...collectConfirmFields(), ready: makeReady } });
    state.product.row.ready = makeReady ? 1 : 0;
    const row = state.rows.find((r) => r.row_index === state.rowIndex);
    if (row) row.ready = state.product.row.ready;
    renderReadyButton();
    toast(makeReady
      ? "Marked ready. Upload it with the others from the run overview."
      : "Unmarked.");
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
}

async function doUpload(mode) {
  const button = $("upload-btn");
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span>Uploading';
  $("exists-box").classList.add("is-hidden");
  try {
    const result = await api(
      `/api/run/${state.runId}/product/${state.rowIndex}/upload`,
      { method: "POST", body: { ...collectConfirmFields(), mode } });
    if (result.exists) {
      const product = result.product;
      $("exists-text").textContent =
        `A product with this SKU already exists: "${product.name}" `
        + `(${product.status}). Choose what to do.`;
      $("exists-open").href = product.edit_url;
      $("exists-box").classList.remove("is-hidden");
      return;
    }
    if (result.skipped) {
      toast("Product skipped.");
      openRun(state.runId);
      return;
    }
    const log = $("upload-log");
    const lines = (result.uploaded || []).map((u) =>
      `${u.filename}  ->  media ${u.id}${u.reused ? " (reused)" : ""}`);
    lines.push("", `Product ${result.product.id} created as a draft.`,
      result.product.edit_url);
    (result.product.warnings || []).forEach((w) => lines.push(`Note: ${w}`));
    log.textContent = lines.join("\n");
    log.className = "log is-good";
    log.classList.remove("is-hidden");
    toast("Uploaded. The product is a draft.");
    const row = state.rows.find((r) => r.row_index === state.rowIndex);
    if (row) { row.status = "uploaded"; row.edit_url = result.product.edit_url; }
  } catch (err) {
    const log = $("upload-log");
    log.textContent = err.message;
    log.className = "log is-bad";
    log.classList.remove("is-hidden");
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Upload to Campkins";
  }
}

/* --- settings ----------------------------------------------------------- */

const SETTING_IDS = ["site_url", "wp_user", "wp_app_password", "woo_key",
  "woo_secret", "output_root", "llm_model", "llm_api_key",
  "llm_system_prompt", "seo_title_key", "seo_desc_key",
  "store_weight_unit", "store_dimension_unit",
  "proxy_provider", "proxy_username", "proxy_password"];

// Provider list from the backend: key, label, field labels and hint.
let proxyProviders = [];

async function showSettings() {
  show("settings");
  try {
    const settings = await api("/api/settings");
    // Options must exist before the select's value is set below.
    fillProxyProviders(settings.proxy_providers);
    SETTING_IDS.forEach((key) => { $(`s-${key}`).value = settings[key] || ""; });
    if (!settings.llm_model) {
      $("s-llm_model").placeholder = settings.default_model || "";
    }
    applyProxyProvider();
  } catch (err) {
    toast(err.message, true);
  }
}

function fillProxyProviders(providers) {
  proxyProviders = providers || [];
  const select = $("s-proxy_provider");
  select.innerHTML = "";
  proxyProviders.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.key;
    option.textContent = provider.label;
    select.appendChild(option);
  });
}

function applyProxyProvider() {
  const select = $("s-proxy_provider");
  const provider = proxyProviders.find((p) => p.key === select.value)
    || proxyProviders[0];
  if (!provider) return;
  select.value = provider.key;
  $("proxy-username-text").textContent = provider.username_label;
  $("s-proxy_username").placeholder = provider.username_placeholder || "";
  $("proxy-password-text").textContent = provider.password_label;
  $("proxy-hint").textContent = provider.hint || "";
  $("proxy-username-lbl").classList.toggle("is-hidden", !provider.needs_credentials);
  $("proxy-password-lbl").classList.toggle("is-hidden", !provider.needs_credentials);
  $("proxy-log").className = "log is-hidden";
}

async function testProxy() {
  const button = $("proxy-test");
  const logEl = $("proxy-log");
  const key = $("s-proxy_provider").value;
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span>Testing';
  try {
    const result = await api("/api/proxy/check", { method: "POST", body: {
      proxy_provider: key,
      proxy_username: $("s-proxy_username").value,
      proxy_password: $("s-proxy_password").value,
    } });
    let lines;
    if (!result.ok) {
      lines = [`${result.provider}: FAILED`, result.error];
    } else if (key === "none") {
      lines = ["No proxy.", `Your IP is ${result.ip}.`];
    } else {
      lines = [`${result.provider}: OK`, `The proxy IP is ${result.ip}.`];
    }
    logEl.textContent = lines.join("\n");
    logEl.className = `log ${result.ok ? "is-good" : "is-bad"}`;
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Test proxy";
  }
}

async function saveSettings() {
  const updates = {};
  SETTING_IDS.forEach((key) => { updates[key] = $(`s-${key}`).value; });
  try {
    await api("/api/settings", { method: "POST", body: updates });
    toast("Settings saved.");
    loadStatus();
  } catch (err) {
    toast(err.message, true);
  }
}

async function downloadSettingsJson() {
  try {
    // Export what is saved, not what is typed, so the file always matches
    // the settings the app is actually running with.
    const settings = await api("/api/settings");
    const data = {};
    SETTING_IDS.forEach((key) => { data[key] = settings[key] || ""; });
    const blob = new Blob([JSON.stringify(data, null, 2)],
      { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "campkins-settings.json";
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (err) {
    toast(err.message, true);
  }
}

async function importSettingsJson(file) {
  let updates;
  try {
    updates = JSON.parse(await file.text());
  } catch {
    toast("That file is not valid JSON.", true);
    return;
  }
  if (!updates || typeof updates !== "object" || Array.isArray(updates)) {
    toast("Expected a JSON object of settings.", true);
    return;
  }
  try {
    await api("/api/settings", { method: "POST", body: updates });
    toast("Settings loaded and saved.");
    loadStatus();
    showSettings();
  } catch (err) {
    toast(err.message, true);
  }
}

/* --- boot --------------------------------------------------------------- */

function wire() {
  $("nav-home").addEventListener("click", showHome);
  $("nav-settings").addEventListener("click", showSettings);
  $("nav-conn").addEventListener("click", () => testConnection(null));
  $("start-run").addEventListener("click", startRun);
  $("run-back").addEventListener("click", showHome);

  $("review-back").addEventListener("click", () => openRun(state.runId));
  $("review-prev").addEventListener("click", () => openReview(state.rowIndex - 1));
  $("review-next").addEventListener("click", () => openReview(state.rowIndex + 1));
  $("drop-all").addEventListener("click", () => {
    state.product.images.forEach((img) => { img.keep = false; img.position = 0; });
    state.product.row.thumbnail_sha = "";
    renderGrid();
  });
  $("suffix").addEventListener("input", renderGrid);
  $("save-selection").addEventListener("click", () => saveSelection(false));
  $("save-continue").addEventListener("click", () => saveSelection(true));
  wireToolbar();
  wirePaste();

  $("confirm-back").addEventListener("click", () => openReview(state.rowIndex));
  $("gen-meta").addEventListener("click", generateMeta);
  $("meta-title").addEventListener("input", updateCounters);
  $("meta-desc").addEventListener("input", updateCounters);
  $("upload-btn").addEventListener("click", () => doUpload("create"));
  $("ready-btn").addEventListener("click", toggleReady);
  $("upload-ready-btn").addEventListener("click", uploadAllReady);
  $("exists-skip").addEventListener("click", () => doUpload("skip"));
  $("exists-replace").addEventListener("click", () => doUpload("replace_images"));

  $("settings-save").addEventListener("click", saveSettings);
  $("settings-test").addEventListener("click", () => testConnection($("settings-log")));
  $("s-proxy_provider").addEventListener("change", applyProxyProvider);
  $("proxy-test").addEventListener("click", testProxy);
  $("settings-export").addEventListener("click", downloadSettingsJson);
  $("settings-import").addEventListener("click", () => $("settings-file").click());
  $("settings-file").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) importSettingsJson(file);
    event.target.value = "";
  });
}

wire();
loadStatus();
showHome();
