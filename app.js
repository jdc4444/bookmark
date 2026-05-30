// Bookmark Atlas — vanilla JS frontend.
// Layout: Pinboard-style tag sidebar + Tweetbot-style tweet feed.

// Resolve /api/* paths against the dev server's routing AND GitHub Pages'
// static-files-only filesystem. Dev server has handlers at /api/<thing>;
// Pages serves the publish-script's files at /api/<thing>.json (plus the
// longform reports live under /api/longform-report/, not /api/longform/).
// Try the bare path first; on 404, rewrite and retry.
async function apiFetch(path, init) {
  const r = await fetch(path, init);
  if (r.status !== 404) return r;
  // Split off any query string — Pages can't honour it (no router), but
  // the static file is still at <path>.json. Frontend filters client-side.
  const qIdx = path.indexOf("?");
  const base = qIdx >= 0 ? path.slice(0, qIdx) : path;
  let alt = "";
  const reportM = base.match(/^\/api\/longform\/(companies(?:%2F|\/)([^/]+))$/);
  if (reportM) {
    alt = `/api/longform-report/companies/${decodeURIComponent(reportM[2])}.json`;
  } else if (!base.endsWith(".json")) {
    alt = base + ".json";
  }
  if (alt && alt !== path) {
    const r2 = await fetch(alt, init);
    if (r2.ok || r2.status !== 404) return r2;
  }
  return r;
}

const els = {
  search: document.getElementById("searchBox"),
  sort: document.getElementById("sortSelect"),
  mediaFilter: document.getElementById("mediaFilter"),
  archiveMeta: null, // removed from header
  mastheadStamp: document.getElementById("mastheadStamp"),
  mastheadRefresh: document.getElementById("mastheadRefresh"),
  articlesToggle: document.getElementById("articlesToggle"),
  indexBtn: document.getElementById("indexBtn"),
  postsBtn: document.getElementById("postsBtn"),
  indexView: document.getElementById("indexView"),
  mastheadWord: document.getElementById("mastheadWord"),
  articlesView: document.getElementById("articlesView"),
  articleDetail: document.getElementById("articleDetail"),
  feed: document.getElementById("feed"),
  tagSearch: document.getElementById("tagSearch"),
  tagList: document.getElementById("tagList"),
  tagModeBtn: document.getElementById("tagModeBtn"),
  multiToggleBtn: document.getElementById("multiToggleBtn"),
  cards: document.getElementById("cards"),
  matchCount: document.getElementById("matchCount"),
  activeFilters: document.getElementById("activeFilters"),
  feedFooter: document.getElementById("feedFooter"),
  modal: document.getElementById("modal"),
  modalBody: document.getElementById("modalBody"),
  modalClose: document.getElementById("modalClose"),
  articlePane: document.getElementById("articlePane"),
  articlePaneBody: document.getElementById("articlePaneBody"),
  longformToggle: document.getElementById("longformToggle"),
  longformView: document.getElementById("longformView"),
  longformToc: document.getElementById("longformToc"),
  longformMain: document.getElementById("longformMain"),
  longformTocBar: document.getElementById("longformTocBar"),
  longformTocBarNum: document.getElementById("longformTocBarNum"),
  longformTocBarTitle: document.getElementById("longformTocBarTitle"),
  longformMobileBarPicker: document.getElementById("longformMobileBarPicker"),
  longformOverview: document.getElementById("longformOverview"),
  lfOverviewBody: document.getElementById("lfOverviewBody"),
  lfOverviewSort: document.getElementById("lfOverviewSort"),
  lfOverviewCount: document.getElementById("lfOverviewCount"),
};

const PANE_BREAKPOINT = "(min-width: 1280px)";
function paneAvailable() { return window.matchMedia(PANE_BREAKPOINT).matches; }

const state = {
  query: "",
  sort: "bookmarked-recent",
  mediaFilter: "",
  articlesMode: false,
  articles: [], // cached list from /api/articles
  selectedTags: new Set(),
  tagMode: "any", // any | all (only meaningful in multi-select)
  multiSelect: false, // false = one tag at a time; true = many
  tagCounts: {},
  articleIds: new Set(),
  staleArticleIds: new Set(),
  articleQueueIds: new Set(),
  articleQueue: { remaining_count: 0, total_count: 0, status: "idle" },
  inFlightArticleGen: new Set(),
  totals: { total: 0, with_media: 0, with_ocr: 0 },
  rows: [],
  loadToken: 0,
  tagFilterText: "",
};

// ---------- helpers ----------

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatTimeAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!t) return "";
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d`;
  if (diff < 86400 * 365) return `${Math.floor(diff / (86400 * 30))}mo`;
  return `${Math.floor(diff / (86400 * 365))}y`;
}

function formatNum(n) {
  n = Number(n) || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function articleIsWriting(bookmarkId) {
  return state.inFlightArticleGen.has(String(bookmarkId));
}

function articleIsQueued(bookmarkId) {
  return state.articleQueueIds.has(String(bookmarkId));
}

function articleWritingButtonHtml() {
  return `<button class="article-generate article-writing-pulse" type="button" disabled aria-disabled="true">
    <iconify-icon class="article-icon" icon="material-symbols-light:article-outline-sharp" aria-hidden="true"></iconify-icon>
    Writing Article
  </button>`;
}

function articleQueuedButtonHtml() {
  return `<button class="article-generate article-queued-label" type="button" disabled aria-disabled="true">
    <iconify-icon class="article-icon" icon="material-symbols-light:article-outline-sharp" aria-hidden="true"></iconify-icon>
    Article Queued
  </button>`;
}

function articleEmptyHtml(bookmarkId) {
  if (articleIsWriting(bookmarkId)) {
    return `<div class="article-empty article-empty-writing">${articleWritingButtonHtml()}</div>`;
  }
  if (articleIsQueued(bookmarkId)) {
    return `<div class="article-empty article-empty-queued">${articleQueuedButtonHtml()}</div>`;
  }
  return `<div class="article-empty">
    <button class="article-generate" type="button">
      <iconify-icon class="article-icon" icon="material-symbols-light:article-outline-sharp" aria-hidden="true"></iconify-icon>
      Write Article
    </button>
  </div>`;
}

function updateArticleWritingIndicators(bookmarkId) {
  const id = String(bookmarkId);
  const writing = articleIsWriting(id);
  const queued = articleIsQueued(id);
  const hasArticle = state.articleIds.has(id);
  const busy = writing || (queued && !hasArticle);
  document.querySelectorAll(`.card[data-id="${CSS.escape(id)}"]`).forEach((card) => {
    card.classList.toggle("has-article", hasArticle);
    card.classList.toggle("article-writing", writing);
    card.classList.toggle("article-queued", queued && !hasArticle);
    const action = card.querySelector(".card-article-action");
    if (!action) return;
    action.classList.toggle("article-writing-pulse", writing);
    action.classList.toggle("has-article", hasArticle);
    action.classList.toggle("article-queued", queued && !hasArticle);
    action.disabled = busy;
    action.setAttribute("aria-disabled", busy ? "true" : "false");
    if (writing) {
      action.title = "Writing Article";
      action.setAttribute("aria-label", "Writing Article");
    } else if (queued && !hasArticle) {
      action.title = "Article Queued";
      action.setAttribute("aria-label", "Article Queued");
    } else if (hasArticle) {
      action.title = "Open editorial brief";
      action.setAttribute("aria-label", "Open editorial brief");
    } else {
      action.title = "Generate editorial brief";
      action.setAttribute("aria-label", "Generate editorial brief");
    }
    const icon = action.querySelector(".article-icon");
    if (icon) icon.classList.toggle("article-writing-pulse", writing);
  });

  document.querySelectorAll(`.article-slot[data-bookmark-id="${CSS.escape(id)}"]`).forEach((slot) => {
    const block = slot.querySelector(".article-block");
    if (!writing) {
      slot.querySelectorAll(".article-writing-status").forEach((el) => el.remove());
      if (!block && (slot.querySelector(".article-empty-writing") || slot.querySelector(".article-empty-queued"))) {
        slot.innerHTML = articleEmptyHtml(id);
      }
      return;
    }
    if (block) {
      const actions = block.querySelector(".article-source-actions") || block.querySelector(".article-font-controls");
      if (actions && !actions.querySelector(".article-writing-status")) {
        actions.insertAdjacentHTML("afterbegin", `<button type="button" class="article-writing-status article-writing-pulse" disabled aria-disabled="true">Writing article…</button>`);
      }
      return;
    }
    slot.innerHTML = articleEmptyHtml(id);
  });
}

function markArticleWriting(bookmarkId) {
  state.inFlightArticleGen.add(String(bookmarkId));
  updateArticleWritingIndicators(bookmarkId);
}

function unmarkArticleWriting(bookmarkId) {
  state.inFlightArticleGen.delete(String(bookmarkId));
  updateArticleWritingIndicators(bookmarkId);
}

// ---------- data fetch ----------

async function fetchStats() {
  const res = await apiFetch("/api/stats");
  if (!res.ok) return null;
  return res.json();
}

async function fetchBookmarks() {
  const params = new URLSearchParams();
  if (state.query) params.set("query", state.query);
  if (state.sort) params.set("sort", state.sort);
  if (state.mediaFilter) params.set("media", state.mediaFilter);
  if (state.selectedTags.size) {
    params.set("tags", [...state.selectedTags].join(","));
    params.set("tag_mode", state.tagMode);
  }
  params.set("limit", "500");
  const res = await apiFetch(`/api/bookmarks?${params}`);
  if (!res.ok) return null;
  return res.json();
}

// ---------- rendering ----------

function renderStats(stats) {
  if (!stats) return;
  document.title = "Bookmark";
  const queue = stats.article_queue || state.articleQueue || {};
  const remaining = Number(queue.remaining_count || 0);
  if (els.mastheadStamp && remaining > 0) {
    const noun = remaining === 1 ? "Article" : "Articles";
    const stalled = queue.status === "stalled" || queue.status === "unknown";
    els.mastheadStamp.classList.add("refreshing");
    els.mastheadStamp.textContent = stalled
      ? `Article Queue Stalled: ${remaining} Left`
      : `Writing ${remaining} ${noun}...`;
    els.mastheadStamp.title = `${remaining} queued article${remaining === 1 ? "" : "s"} left`;
    return;
  }
  if (els.mastheadStamp && stats.generated_at) {
    els.mastheadStamp.classList.remove("refreshing");
    els.mastheadStamp.textContent = formatStamp(stats.generated_at);
    els.mastheadStamp.title = new Date(stats.generated_at).toLocaleString();
  }
}

// Hour-precision stamp for keyart panels: drop minutes for a calmer, less
// twitchy look. "May 5 · 2 PM" instead of "May 5 · 2:22 PM".
function formatStampHour(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const sameYear = d.getFullYear() === now.getFullYear();
  let h = d.getHours();
  const ampm = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  const time = `${h} ${ampm}`;
  if (sameDay) return time;
  if (sameYear) {
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return `${months[d.getMonth()]} ${d.getDate()} · ${time}`;
  }
  return `${d.toISOString().slice(0, 10)} · ${time}`;
}

// Compact stamp in viewer's local timezone, 12-hour AM/PM:
//   today        → "2:22 PM"
//   this year    → "May 5 · 2:22 PM"
//   older        → "2025-12-04 · 2:22 PM"
function formatStamp(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const sameYear = d.getFullYear() === now.getFullYear();
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, "0");
  const ampm = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  const time = `${h}:${m} ${ampm}`;
  if (sameDay) return time;
  if (sameYear) {
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return `${months[d.getMonth()]} ${d.getDate()} · ${time}`;
  }
  return `${d.toISOString().slice(0, 10)} · ${time}`;
}

function renderTagList() {
  const search = state.tagFilterText.toLowerCase();
  const tags = Object.entries(state.tagCounts)
    .filter(([t]) => !search || t.includes(search))
    .sort((a, b) => b[1] - a[1]);

  els.tagList.innerHTML = tags.map(([tag, count]) => {
    const active = state.selectedTags.has(tag) ? "active" : "";
    return `
      <li class="${active}" data-tag="${escapeHtml(tag)}" tabindex="0" role="button">
        <span>${escapeHtml(tag)}</span>
        <span class="count">${count}</span>
      </li>`;
  }).join("");
}

function renderActiveFilters() {
  const parts = [];
  if (state.query) {
    parts.push(`<span class="active-chip">search: "${escapeHtml(state.query)}" <button data-clear="search">×</button></span>`);
  }
  for (const tag of state.selectedTags) {
    parts.push(`<span class="active-chip">#${escapeHtml(tag)} <button data-clear="tag" data-tag="${escapeHtml(tag)}">×</button></span>`);
  }
  if (state.mediaFilter) {
    const label = { media: "has media", no_media: "no media", ocr: "has OCR" }[state.mediaFilter] || state.mediaFilter;
    parts.push(`<span class="active-chip">${label} <button data-clear="media">×</button></span>`);
  }
  els.activeFilters.innerHTML = parts.join("");
}

// True for any media we can render as a still image. Includes videos —
// their `url` is the poster .jpg, which displays just like a regular image
// (X.com's video previews are baked at the poster URL).
function isDisplayableMedia(m) {
  return !!(m && m.url && (m.type === "image" || m.type === "video" || !m.type));
}

function renderImageWithOverlay(m) {
  const url = escapeHtml(m.url);
  const alt = escapeHtml(m.alt || "");
  const blocks = m.ocr_blocks || [];
  if (!blocks.length) {
    return `<div class="media-frame"><img loading="lazy" src="${url}" alt="${alt}"></div>`;
  }
  const overlays = blocks.map(b => {
    const left = (b.x * 100).toFixed(3) + "%";
    const top = (b.y * 100).toFixed(3) + "%";
    const w = (b.w * 100).toFixed(3) + "%";
    const h = (b.h * 100).toFixed(3) + "%";
    return `<span class="ocr-block" data-h="${b.h.toFixed(4)}" style="left:${left};top:${top};width:${w};height:${h}">${escapeHtml(b.text)}</span>`;
  }).join("");
  return `
    <div class="media-frame">
      <img loading="lazy" src="${url}" alt="${alt}">
      <div class="ocr-overlay" aria-hidden="false">${overlays}</div>
    </div>`;
}

// Set font-size on every .ocr-block according to its parent frame's height.
// Called once on initial render and again on window resize. Accepts either a
// container (we look for .media-frame descendants) or a single .media-frame
// element (we size just that one) — the latter is what `sizeAfterImageLoad`
// passes once each image's `load` event fires.
function sizeOcrOverlays(root = document) {
  if (!root) return;
  const isFrame = root.classList && root.classList.contains("media-frame");
  const frames = isFrame ? [root] : root.querySelectorAll(".media-frame");
  for (const frame of frames) {
    const h = frame.clientHeight;
    if (!h) continue;
    const overlays = frame.querySelectorAll(".ocr-block");
    for (const el of overlays) {
      const blockH = parseFloat(el.dataset.h || "0");
      if (!blockH) continue;
      // Use 92% of the bbox height as font-size so the rendered text fits
      // comfortably in the row without clipping descenders.
      el.style.fontSize = Math.max(6, blockH * h * 0.92).toFixed(2) + "px";
    }
  }
}

// Also fire when individual images finish loading (since clientHeight depends
// on natural aspect ratio for the "single" full-bleed case).
function sizeAfterImageLoad(root = document) {
  const imgs = root.querySelectorAll(".media-frame img");
  for (const img of imgs) {
    if (img.complete) {
      sizeOcrOverlays(img.closest(".media-frame"));
    } else {
      img.addEventListener("load", () => sizeOcrOverlays(img.closest(".media-frame")), { once: true });
    }
  }
}

function renderCard(row) {
  const author = escapeHtml(row.username || row.author || "unknown");
  const handle = row.username ? `@${escapeHtml(row.username)}` : "";
  const time = formatTimeAgo(row.created_at);
  const text = (row.text || "").trim();

  const images = (row.media || []).filter(isDisplayableMedia);
  const mediaSingleClass = images.length === 1 ? "single" : "";
  const mediaHtml = images.length ? `
    <div class="card-media ${mediaSingleClass}">
      ${images.map(renderImageWithOverlay).join("")}
    </div>` : "";

  const tagsHtml = (row.tags || []).length ? `
    <div class="card-tags">
      ${row.tags.map(t => `<span class="tag-chip ${state.selectedTags.has(t) ? 'active' : ''}" data-tag="${escapeHtml(t)}">#${escapeHtml(t)}</span>`).join("")}
    </div>` : "";

  const m = row.metrics || {};
  const metaParts = [];
  if (m.like_count) metaParts.push(`♥ ${formatNum(m.like_count)}`);
  if (m.retweet_count) metaParts.push(`↻ ${formatNum(m.retweet_count)}`);
  if (m.reply_count) metaParts.push(`◐ ${formatNum(m.reply_count)}`);
  if (m.impression_count) metaParts.push(`☼ ${formatNum(m.impression_count)}`);
  if (row.media_count) metaParts.push(`${row.media_count} media`);
  const metaHtml = metaParts.length
    ? `<div class="card-meta">${metaParts.map(p => `<span class="card-meta-item">${p}</span>`).join("")}</div>`
    : "";

  const originalLink = row.url
    ? `<a class="card-original" href="${escapeHtml(row.url)}" target="_blank" rel="noopener" title="Open on x.com" aria-label="Open on x.com"><iconify-icon icon="fluent-mdl2:arrow-up-right-8" aria-hidden="true"></iconify-icon></a>`
    : "";
  const writingArticle = articleIsWriting(row.id);
  const queuedArticle = articleIsQueued(row.id);
  const hasArticle = state.articleIds.has(String(row.id));
  const articleBusy = writingArticle || (queuedArticle && !hasArticle);
  const articleActionTitle = writingArticle
    ? "Writing Article"
    : queuedArticle && !hasArticle
    ? "Article Queued"
    : hasArticle ? "Open editorial brief" : "Generate editorial brief";
  const isStale = state.staleArticleIds.has(String(row.id));
  // Show a ↻ regenerate button when the cached article is stale — either it
  // predates a discovered quoted-tweet parent, or the bookmark's tweet text
  // was expanded by the backfill after the article was written.
  const refreshAction = (hasArticle && isStale) ? `
    <button type="button"
      class="card-article-refresh"
      data-bookmark-id="${escapeHtml(row.id)}"
      title="Regenerate brief (shift-click for focus hint)"
      aria-label="Regenerate brief">↻</button>` : "";
  const articleAction = `
    <button type="button"
      class="card-article-action${hasArticle ? ' has-article' : ''}${queuedArticle && !hasArticle ? ' article-queued' : ''}${writingArticle ? ' article-writing-pulse' : ''}"
      data-bookmark-id="${escapeHtml(row.id)}"
      title="${articleActionTitle}"
      aria-label="${articleActionTitle}"
      aria-disabled="${articleBusy ? 'true' : 'false'}"
      ${articleBusy ? 'disabled' : ''}><iconify-icon class="article-icon${writingArticle ? ' article-writing-pulse' : ''}" icon="material-symbols-light:article-outline-sharp" aria-hidden="true"></iconify-icon></button>`;
  const q = row.quoted_tweet;
  const qImg = q && (q.media || []).find(isDisplayableMedia);
  const quotedHtml = q && (q.text || q.raw || qImg) ? `
    <blockquote class="card-quoted">
      ${q.author_username ? `<header class="card-quoted-head"><span class="card-author">${escapeHtml(q.author_name || q.author_username)}</span>${q.date_text ? ` <span class="card-dot">·</span> <span class="card-time">${escapeHtml(q.date_text)}</span>` : ""}</header>` : ""}
      ${(q.text || q.raw) ? `<div class="card-quoted-text">${escapeHtml(q.text || q.raw || "")}</div>` : ""}
      ${qImg ? `<div class="card-quoted-media">${renderImageWithOverlay(qImg)}</div>` : ""}
    </blockquote>` : "";
  // Reply / thread context: surface the immediate parent ABOVE the card text
  // so it's clear what the bookmark is responding to. Uses thread.before[-1]
  // when we captured the parent, falls back to a bare "↳ Reply to @user" chip
  // when we know it's a reply but couldn't fetch the parent.
  const parents = (row.thread || []).filter(t => t.position === "before");
  const head = parents[parents.length - 1];
  const replyTo = (row.reply_to && row.reply_to.length) ? row.reply_to[0] : null;
  let parentHtml = "";
  if (head) {
    // Render the chain in posting order: deepest-original (grandparent's
    // quoted_tweet, with media) → middle (parent post, retweet/quote) →
    // bookmark's own reply (rendered later by the card body). Track image
    // URLs already shown so the same picture (e.g. a parent + its quoted
    // tweet both reporting the SNDK chart) only renders once in the chain.
    const grand = head.quoted_tweet;
    const seenImg = new Set();
    const renderNode = (n) => {
      if (!n) return "";
      const img = (n.media || []).find(m => m && m.url && !seenImg.has(m.url));
      if (img) seenImg.add(img.url);
      const text = (n.text || "").slice(0, 280);
      if (!text && !img) return "";
      return `
        <blockquote class="card-quoted">
          ${n.author_username ? `<header class="card-quoted-head"><span class="card-author">${escapeHtml(n.author_name || n.author_username)}</span></header>` : ""}
          ${text ? `<div class="card-quoted-text">${escapeHtml(text)}</div>` : ""}
          ${img ? `<div class="card-quoted-media">${renderImageWithOverlay(img)}</div>` : ""}
        </blockquote>`;
    };
    const parentLabel = parents.length > 1 ? '↵ Earlier in thread by' : '↳ In reply to';
    const grandLabel = grand ? `<span class="card-parent-sublabel">↻ Quoted by <span class="card-handle">@${escapeHtml(head.author_username || '')}</span></span>` : "";
    parentHtml = `
      <div class="card-parent">
        ${grand ? renderNode(grand) + grandLabel : ""}
        ${renderNode(head)}
        <span class="card-parent-label">${parentLabel} <span class="card-handle">@${escapeHtml(head.author_username || '')}</span></span>
      </div>`;
  } else if (replyTo) {
    parentHtml = `
      <div class="card-parent card-parent-bare">
        <span class="card-parent-label">↳ In reply to <span class="card-handle">@${escapeHtml(replyTo)}</span></span>
      </div>`;
  }
  return `
    <article class="card${hasArticle ? ' has-article' : ''}${writingArticle ? ' article-writing' : ''}${queuedArticle && !hasArticle ? ' article-queued' : ''}" data-id="${escapeHtml(row.id)}">
      <header class="card-head">
        <span class="card-author">${author}</span>
        <span class="card-dot">·</span>
        <span class="card-time" title="${escapeHtml(row.created_at || '')}">${time}</span>
        ${originalLink}
      </header>
      ${parentHtml}
      <div class="card-text">${escapeHtml(text)}</div>
      ${quotedHtml}
      ${mediaHtml}
      ${tagsHtml}
      <footer class="card-foot">
        <div class="card-foot-meta">${metaHtml}</div>
        ${refreshAction}
        ${articleAction}
      </footer>
    </article>`;
}

function renderCards(rows, matched, total) {
  els.matchCount.textContent = `${matched} of ${total} bookmarks`;
  if (!rows.length) {
    els.cards.innerHTML = `<div class="empty">No bookmarks match the current filters.</div>`;
    els.feedFooter.textContent = "";
    return;
  }
  els.cards.innerHTML = rows.map(renderCard).join("");
  sizeAfterImageLoad(els.cards);
  if (rows.length < matched) {
    els.feedFooter.textContent = `Showing ${rows.length} of ${matched}. Refine filters to narrow further.`;
  } else {
    els.feedFooter.textContent = "";
  }
}

// ---------- card open: routes to pane (wide) or modal (narrow) ----------

function buildCardContent(row) {
  const text = (row.text || "").trim();
  const author = escapeHtml(row.username || row.author || "unknown");
  const handle = row.username ? `@${escapeHtml(row.username)}` : "";
  const time = row.created_at ? new Date(row.created_at).toLocaleString() : "";

  const images = (row.media || []).filter(isDisplayableMedia);
  const mediaHtml = images.length ? `
    <div class="card-media ${images.length === 1 ? 'single' : ''}">
      ${images.map(renderImageWithOverlay).join("")}
    </div>` : "";

  const tagsHtml = (row.tags || []).length ? `
    <div class="card-tags">
      ${row.tags.map(t => `<span class="tag-chip" data-tag="${escapeHtml(t)}">#${escapeHtml(t)}</span>`).join("")}
    </div>` : "";

  return `
    <header class="card-head">
      <span class="card-author">${author}</span>
      <span class="card-dot">·</span>
      <span class="card-time">${escapeHtml(time)}</span>
    </header>
    <div class="card-text">${escapeHtml(text)}</div>
    ${mediaHtml}
    <div class="article-slot" data-bookmark-id="${escapeHtml(row.id)}">
      ${articleEmptyHtml(row.id)}
    </div>
    ${tagsHtml}
    ${row.url ? `<a class="open-original" href="${escapeHtml(row.url)}" target="_blank" rel="noopener">Open on x.com →</a>` : ""}
  `;
}

function openCard(rowId) {
  const row = state.rows.find(r => String(r.id) === String(rowId));
  if (!row) return;
  if (paneAvailable()) {
    openInPane(row);
  } else {
    openInModal(row);
  }
}

function openInModal(row) {
  els.modalBody.innerHTML = buildCardContent(row);
  els.modal.classList.remove("hidden");
  els.modal.setAttribute("aria-hidden", "false");
  sizeAfterImageLoad(els.modalBody);
  setSelectedCard(row.id);
  loadCachedArticle(row.id);
}

function openInPane(row) {
  // The pane renders the article only — the tweet header, body, and original
  // media live inside the article (image under the lede, "Open on x.com" at
  // the foot). Keeps the editorial column clean. Quoted-parent context lives
  // in the feed card (and feeds the prompt at generation time), not here.
  els.articlePaneBody.innerHTML = `
    <div class="article-slot" data-bookmark-id="${escapeHtml(row.id)}">
      ${articleEmptyHtml(row.id)}
    </div>`;
  els.articlePane.dataset.bookmarkId = row.id;
  els.articlePane.setAttribute("aria-hidden", "false");
  els.articlePaneBody.scrollTop = 0;
  els.articlePane.scrollTop = 0;
  setSelectedCard(row.id);
  sizeAfterImageLoad(els.articlePaneBody);
  loadCachedArticle(row.id);
}

function clearPane() {
  els.articlePaneBody.innerHTML = `<div class="article-pane-empty">Click a bookmark to read its brief here.</div>`;
  delete els.articlePane.dataset.bookmarkId;
  els.articlePane.setAttribute("aria-hidden", "true");
  setSelectedCard(null);
}

// Articles-mode full-page article view. Replaces the grid with a single
// editorial article (using the same slot/renderer the pane uses). Pushes a
// history entry so the browser back button returns to the grid instead of
// leaving the site.
function openInDetail(row, { pushHistory = true } = {}) {
  if (!els.articleDetail) return;
  els.articlesView.classList.add("hidden");
  els.articleDetail.classList.remove("hidden");
  els.articleDetail.innerHTML = `
    <button type="button" class="article-detail-back" id="articleDetailBack">
      <iconify-icon icon="fluent-mdl2:arrow-up-right-8" style="transform: rotate(225deg)"></iconify-icon>
      <span>Back</span>
    </button>
    <div class="article-detail-body">
      <div class="article-slot" data-bookmark-id="${escapeHtml(row.id)}">
        ${articleIsWriting(row.id) ? articleEmptyHtml(row.id) : '<div class="article-empty"><div class="article-loading"><span class="spinner"></span> Loading…</div></div>'}
      </div>
    </div>`;
  window.scrollTo(0, 0);
  setSelectedCard(row.id);
  loadCachedArticle(row.id);
  if (pushHistory) {
    history.pushState({ view: "article-detail", id: row.id }, "", `#article=${row.id}`);
  }
}

function closeArticleDetail({ pushHistory = true } = {}) {
  if (!els.articleDetail) return;
  els.articleDetail.classList.add("hidden");
  els.articleDetail.innerHTML = "";
  els.articlesView.classList.remove("hidden");
  setSelectedCard(null);
  if (pushHistory) {
    history.pushState({ view: "articles" }, "", "#articles");
  }
}

function setSelectedCard(rowId) {
  els.cards.querySelectorAll(".card.is-selected").forEach((c) => c.classList.remove("is-selected"));
  if (rowId == null) return;
  const sel = els.cards.querySelector(`.card[data-id="${CSS.escape(String(rowId))}"]`);
  if (sel) sel.classList.add("is-selected");
}

// Backwards-compat: any old call sites still use openModal.
function openModal(rowId) { openCard(rowId); }

// ---------- article generator ----------

async function loadCachedArticle(bookmarkId) {
  if (articleIsWriting(bookmarkId)) updateArticleWritingIndicators(bookmarkId);
  try {
    // cache: "no-store" so the browser doesn't return a stale cached response
    // after a regenerate. Articles are written by background pipelines that
    // don't bump any cache headers; without this, the user can click ↻, see
    // the regen succeed server-side, but get the old article on next visit.
    const res = await apiFetch(`/api/article/${encodeURIComponent(bookmarkId)}`,
                            { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    renderArticleSlot(bookmarkId, data);
  } catch {}
}

async function loadArticleHistory(bookmarkId) {
  const slot = document.querySelector(
    `.article-versions[data-bookmark-id="${CSS.escape(String(bookmarkId))}"]`);
  if (!slot) return;
  try {
    const res = await apiFetch(`/api/article/${encodeURIComponent(bookmarkId)}/history`);
    if (!res.ok) { slot.innerHTML = ""; return; }
    const data = await res.json();
    const versions = (data.versions || []);
    if (versions.length <= 1) { slot.innerHTML = ""; return; }
    // versions[0] is latest. Render as "v1, v2, …" newest-first; current marked.
    const fmt = (ts) => {
      try {
        const d = new Date(ts);
        return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      } catch { return ts; }
    };
    slot.innerHTML = versions.map((v, i) => `
      <button type="button" class="article-version${v.current ? ' current' : ''}"
        data-bookmark-id="${escapeHtml(bookmarkId)}"
        data-version="${escapeHtml(v.id)}"
        title="${escapeHtml(v.headline || '')}">v${versions.length - i}${v.current ? ' (now)' : ''} · ${escapeHtml(fmt(v.generated_at))}</button>
    `).join("");
  } catch {
    slot.innerHTML = "";
  }
}

function safeArticleVersionId(value, fallback = "v0") {
  const cleaned = String(value || "").replace(/[:./]/g, "-").trim();
  return cleaned || fallback;
}

function articleVersionIdFromArticle(article) {
  return safeArticleVersionId(article && article.generated_at, "current");
}

async function loadArticleVersion(bookmarkId, versionId) {
  const slot = document.querySelector(
    `.article-slot[data-bookmark-id="${CSS.escape(String(bookmarkId))}"]`);
  if (!slot) return;
  slot.innerHTML = `<div class="article-loading"><span class="spinner"></span> Loading version…</div>`;
  try {
    const res = await apiFetch(`/api/article/${encodeURIComponent(bookmarkId)}/v/${encodeURIComponent(versionId)}`);
    if (!res.ok) throw new Error((await res.json()).error || "failed");
    const data = await res.json();
    renderArticleSlot(bookmarkId, data, versionId);
  } catch (err) {
    slot.innerHTML = `<div class="article-error">Failed: ${escapeHtml(String(err))}</div>`;
  }
}

// ---------- focus modal ----------

function openFocusModal({ bookmarkId, onSubmit }) {
  const modal = document.getElementById("focusModal");
  const form = document.getElementById("focusForm");
  const input = document.getElementById("focusInput");
  const cancel = document.getElementById("focusCancel");
  input.value = "";
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  setTimeout(() => input.focus(), 30);
  const close = () => {
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    form.removeEventListener("submit", submit);
    cancel.removeEventListener("click", close);
    modal.removeEventListener("click", backdropClose);
    document.removeEventListener("keydown", keyClose);
  };
  const submit = (e) => {
    e.preventDefault();
    const value = input.value.trim();
    close();
    if (value) onSubmit(value);
  };
  const backdropClose = (e) => { if (e.target === modal) close(); };
  const keyClose = (e) => { if (e.key === "Escape") close(); };
  form.addEventListener("submit", submit);
  cancel.addEventListener("click", close);
  modal.addEventListener("click", backdropClose);
  document.addEventListener("keydown", keyClose);
}

async function forceRegenerateArticle(bookmarkId, focus = null) {
  if (articleIsWriting(bookmarkId)) return;
  const slot = document.querySelector(
    `.article-slot[data-bookmark-id="${CSS.escape(String(bookmarkId))}"]`);
  if (!slot) return;
  markArticleWriting(bookmarkId);
  const hadArticle = !!slot.querySelector(".article-block");
  if (!hadArticle) {
    slot.innerHTML = articleEmptyHtml(bookmarkId);
  }
  try {
    const res = await apiFetch("/api/article", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bookmark_id: bookmarkId, force: true, focus }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "failed");
    renderArticleSlot(bookmarkId, data);
  } catch (err) {
    if (!hadArticle) {
      slot.innerHTML = `<div class="article-error">Failed: ${escapeHtml(String(err))}</div>` +
        `<button class="article-generate" type="button">Try again</button>`;
    }
  } finally {
    unmarkArticleWriting(bookmarkId);
  }
}

async function generateArticle(bookmarkId, slot) {
  if (articleIsWriting(bookmarkId)) return;
  markArticleWriting(bookmarkId);
  if (!slot.querySelector(".article-block")) {
    slot.innerHTML = articleEmptyHtml(bookmarkId);
  }
  try {
    const res = await apiFetch("/api/article", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bookmark_id: bookmarkId }),
    });
    const data = await res.json();
    if (!res.ok) {
      slot.innerHTML = `<div class="article-error">Failed: ${escapeHtml(data.error || "unknown")}</div>` +
        `<button class="article-generate" type="button">Try again</button>`;
      return;
    }
    renderArticleSlot(bookmarkId, data);
  } catch (e) {
    slot.innerHTML = `<div class="article-error">Failed: ${escapeHtml(String(e))}</div>` +
      `<button class="article-generate" type="button">Try again</button>`;
  } finally {
    unmarkArticleWriting(bookmarkId);
  }
}

function renderArticleSlot(bookmarkId, data, displayedVersionId = null) {
  const slot = document.querySelector(
    `.article-slot[data-bookmark-id="${CSS.escape(String(bookmarkId))}"]`
  );
  if (!slot) return;
  // Mark this bookmark as having an article so the feed marker reflects it
  // without waiting for a /api/stats refresh.
  if (!state.articleIds.has(String(bookmarkId))) {
    state.articleIds.add(String(bookmarkId));
    const card = els.cards.querySelector(`.card[data-id="${CSS.escape(String(bookmarkId))}"]`);
    if (card) {
      card.classList.add("has-article");
      const action = card.querySelector(".card-article-action");
      if (action) {
        action.classList.add("has-article");
        action.title = "Open editorial brief";
        action.setAttribute("aria-label", "Open editorial brief");
      }
    }
  }
  // History list is loaded after slot.innerHTML is written below.
  // Editorial layouts (right-column pane AND full-page detail in articles
   // mode) embed media + a source line inside the article block. The modal
   // popup uses the older layout where those live above the article-slot.
  const inEditorialLayout = !!(slot.closest(".article-pane-body") || slot.closest(".article-detail-body"));
  const inPane = inEditorialLayout; // alias kept for downstream code
  // Try the loaded feed rows first; if we're in articles mode the bookmark
  // may not be there, so fall back to the cached articles list which the
  // /api/articles endpoint includes a media URL on.
  let row = state.rows.find(r => String(r.id) === String(bookmarkId));
  if (!row) {
    const a2 = state.articles.find(x => String(x.bookmark_id) === String(bookmarkId));
    if (a2) {
      row = {
        id: bookmarkId,
        username: a2.poster,
        author: a2.poster_name || a2.poster,
        author_name: a2.poster_name,
        created_at: a2.created_at,
        media: a2.image_url ? [{ type: "image", url: a2.image_url }] : [],
        text: a2.tweet_text,
        quoted_tweet: a2.quoted_tweet,
        reply_to: a2.reply_to,
        thread: a2.thread,
      };
    }
  }
  const a = data.article || data;
  const modules = data.modules || {};
  const escape = escapeHtml;
  slot.dataset.displayedVersion = String(displayedVersionId || articleVersionIdFromArticle(a));

  const upper = (s) => (s || "").toString().toUpperCase();

  const posterModule = a.poster ? modules[`poster_${a.poster.toLowerCase()}`] : null;
  const posterHtml = posterModule ? `
    <section class="ed-section">
      <h4 class="ed-rubric">The poster</h4>
      <p class="ed-entry">
        <strong>@${escape(posterModule.key || a.poster)}</strong>${posterModule.display_name && posterModule.display_name !== posterModule.key ? ` — ${escape(posterModule.display_name)}` : ""}.
        ${escape(posterModule.blurb || "")}
        ${posterModule.credibility_note ? `<span class="ed-aside">${escape(posterModule.credibility_note)}</span>` : ""}
      </p>
    </section>` : "";

  const tickerEntries = (a.tickers || []).map((tk) => {
    const m = modules[`ticker_${tk.toLowerCase()}`];
    if (!m) return `<p class="ed-entry"><strong>$${escape(upper(tk))}</strong> — <span class="muted">(no profile yet)</span></p>`;
    return `<p class="ed-entry">
      <strong>$${escape(upper(m.key || tk))}</strong>${m.name ? ` — ${escape(m.name)}` : ""}${m.exchange ? ` <span class="muted">(${escape(m.exchange)})</span>` : ""}.
      ${escape(m.blurb || "")}
      ${m.recent ? `<span class="ed-aside"><em>Recent:</em> ${escape(m.recent)}</span>` : ""}
    </p>`;
  }).join("");
  const tickersHtml = tickerEntries ? `
    <section class="ed-section">
      <h4 class="ed-rubric">Companies</h4>
      ${tickerEntries}
    </section>` : "";

  // Glossary entries are placed inline, immediately after the body paragraph
  // that first mentions each term — like a magazine's marginal definition.
  // Matching tolerates: hyphens vs spaces vs underscores, plurals, acronym vs
  // expanded form, and the parenthetical-tail form ("MLCC (Multi-Layer ...)")
  // Some agents emit concepts as bare strings rather than {term,definition}
  // objects; coerce those to {term: <string>} so they're at least labelable
  // (and skip them later if they have no definition).
  const concepts = (a.concepts || [])
    .map((c) => (typeof c === "string" ? { term: c } : c))
    .filter((c) => c && (c.term || c.key) && c.definition);
  const bodyParagraphs = (a.body || "").split(/\n\n+/);
  const norm = (s) => (s || "").toLowerCase()
    .replace(/[\s\-_/]+/g, " ")
    .replace(/[^\w\s]/g, "")
    .trim();
  const placed = new Set();
  const conceptByParagraph = bodyParagraphs.map((p) => {
    const matches = [];
    const pn = " " + norm(p) + " ";
    for (let i = 0; i < concepts.length; i++) {
      if (placed.has(i)) continue;
      const c = concepts[i];
      const term = (c.term || c.key || "").trim();
      if (!term) continue;
      const tShort = term.split("(")[0].trim();
      const tParen = (term.match(/\(([^)]+)\)/) || [])[1] || "";
      const candidates = [term, tShort, tParen, c.key]
        .map(norm)
        .filter(s => s && s.length >= 2);
      const probes = new Set(candidates);
      // also try plural/singular variants
      for (const x of [...probes]) {
        if (x.endsWith("s")) probes.add(x.slice(0, -1));
        else probes.add(x + "s");
      }
      let hit = false;
      for (const probe of probes) {
        if (pn.includes(" " + probe + " ")) { hit = true; break; }
      }
      if (hit) {
        matches.push(i);
        placed.add(i);
      }
    }
    return matches;
  });
  const remainingConcepts = concepts.filter((_, i) => !placed.has(i));

  const renderInlineDef = (c) => `
    <aside class="ed-inline-def">
      <span class="ed-margin-label">${escape(c.term || c.key || "")} —</span> ${escape(c.definition || "")}
    </aside>`;

  // Tiny safe markdown: only **bold** and *italic*; everything else escaped.
  // Pattern: escape first, then re-substitute the escaped markers.
  const mdInline = (s) => {
    let out = escape(s).replace(/\n/g, "<br>");
    out = out.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[\s(])\*([^*\n]+?)\*(?=$|[\s.,;:!?)])/g,
      '$1<em>$2</em>');
    out = out.replace(/(^|[\s(])_([^_\n]+?)_(?=$|[\s.,;:!?)])/g,
      '$1<em>$2</em>');
    return out;
  };
  const renderBodyWithDefs = () => {
    if (!bodyParagraphs.length) return "";
    // Distribute interleaved images evenly between paragraphs (skipping the
    // very first slot — the lede already has the lead photo). Images get
    // their own full-width figure between paragraphs, magazine-style.
    const total = bodyParagraphs.length;
    const imgAfter = new Map();
    if (interleavedImages.length && total > 1) {
      const slots = total - 1; // gaps between paragraphs (after p[0]..p[total-2])
      for (let i = 0; i < interleavedImages.length; i++) {
        const after = Math.floor(((i + 1) * slots) / (interleavedImages.length + 1));
        const arr = imgAfter.get(after) || [];
        arr.push(interleavedImages[i]);
        imgAfter.set(after, arr);
      }
    }
    return bodyParagraphs.map((p, idx) => {
      const para = `<p>${mdInline(p)}</p>`;
      const defs = (conceptByParagraph[idx] || []).map(i => renderInlineDef(concepts[i])).join("");
      const imgs = (imgAfter.get(idx) || []).map(m => `
        <figure class="article-media single article-media-inline">
          ${renderImageWithOverlay(m)}
        </figure>`).join("");
      return para + defs + imgs;
    }).join("");
  };

  const conceptsHtml = remainingConcepts.length ? `
    <section class="ed-section">
      <h4 class="ed-rubric">Glossary</h4>
      ${remainingConcepts.map((c) => `
        <p class="ed-entry"><strong>${escape(c.term || c.key)}.</strong> ${escape(c.definition || "")}</p>
      `).join("")}
    </section>` : "";

  const booksNormalized = (a.books || [])
    .map((b) => (typeof b === "string" ? { title: b } : b))
    .filter((b) => b && (b.title || b.author));
  const booksHtml = booksNormalized.length ? `
    <section class="ed-section">
      <h4 class="ed-rubric">Books</h4>
      ${booksNormalized.map((b) => `
        <p class="ed-entry"><strong>${escape(b.title || "")}</strong>${b.author ? `, by ${escape(b.author)}` : ""}. ${escape(b.summary || b.note || "")}</p>
      `).join("")}
    </section>` : "";

  const reposNormalized = (a.repos || [])
    .map((r) => (typeof r === "string" ? { url: r } : r))
    .filter((r) => r && (r.url || r.name || r.key));
  const reposHtml = reposNormalized.length ? `
    <section class="ed-section">
      <h4 class="ed-rubric">Repositories</h4>
      ${reposNormalized.map((r) => `
        <p class="ed-entry"><strong><a href="${escape(r.url || "")}" target="_blank" rel="noopener">${escape(r.name || r.url || r.key || "")}</a></strong>. ${escape(r.summary || r.note || "")}</p>
      `).join("")}
    </section>` : "";

  const factCheckHtml = a.fact_check ? `
    <aside class="ed-margin-note"><span class="ed-margin-label">Fact-check —</span> ${escape(a.fact_check)}</aside>` : "";
  const threadHtml = a.thread_summary ? `
    <aside class="ed-margin-note"><span class="ed-margin-label">Thread —</span> ${escape(a.thread_summary)}</aside>` : "";

  // Some agents emit sources as bare URL strings rather than {url, title} objects.
  // Normalize so a string source still renders as a clickable link.
  const sourcesNormalized = (a.sources || [])
    .map((s) => (typeof s === "string" ? { url: s, title: s } : s))
    .filter((s) => s && (s.url || s.title));
  const sourcesHtml = sourcesNormalized.length ? `
    <section class="ed-section ed-sources">
      <h4 class="ed-rubric">Sources</h4>
      <ol>${sourcesNormalized.map((s) => `<li><a href="${escape(s.url || "")}" target="_blank" rel="noopener">${escape(s.title || s.url || "")}</a>${s.note ? ` — <span class="muted">${escape(s.note)}</span>` : ""}</li>`).join("")}</ol>
    </section>` : "";

  const backendBadge = a.backend === "ollama"
    ? `<span class="article-badge">via local model — research limited; rerun for live web data</span>`
    : a.backend === "deepseek"
    ? `<span class="article-badge">via deepseek</span>` : "";

  // In pane mode, images become part of the article itself. Editorial style:
  // the first image is the lead photo (under the deck), the rest are spaced
  // through the body — one between paragraphs at evenly-spaced positions —
  // so the page reads like a magazine spread instead of a contact sheet.
  let articleLeadMediaHtml = "";
  let articleSourceHtml = "";
  let interleavedImages = [];
  // Relationship context block: small editorial chip above the headline,
   // with parent-tweet text inset where we have it. Three flavours:
   //   thread      → "Part of a thread · 2 surrounding posts"
   //   reply       → "↳ Reply to @user · <parent text excerpt>"
   //   quote       → renders the existing quoted_tweet
  let relationshipHtml = "";
  if (inPane && row) {
    const parents = (row.thread || []).filter(t => t.position === "before");
    const isThread = parents.length > 0;
    const isReply = !isThread && (row.reply_to && row.reply_to.length);
    if (isThread) {
      const head = parents[parents.length - 1]; // immediate parent
      relationshipHtml = `
        <aside class="article-relationship article-relationship-thread">
          <span class="article-relationship-label">↵ Thread</span>
          <span class="article-relationship-meta">${escape(parents.length)} earlier post${parents.length === 1 ? '' : 's'} from this conversation</span>
          ${head ? `<blockquote class="article-relationship-parent">
            ${head.author_username ? `<span class="article-relationship-author">@${escape(head.author_username)}</span>` : ""}
            <span class="article-relationship-text">${escape((head.text || "").slice(0, 280))}</span>
          </blockquote>` : ""}
        </aside>`;
    } else if (isReply) {
      relationshipHtml = `
        <aside class="article-relationship article-relationship-reply">
          <span class="article-relationship-label">↳ Reply</span>
          <span class="article-relationship-meta">to ${row.reply_to.map(h => `@${escape(h)}`).join(" ")}</span>
        </aside>`;
    }
  }

  let articleKeyartHtml = "";
  if (inPane && row) {
    let images = (row.media || []).filter(isDisplayableMedia);
    // No own media? Walk the parent / quoted-tweet chain for the next-best
    // visual (the SNDK chart in BobertBedford's case lives on the parent's
    // quoted-tweet, two hops away).
    if (!images.length) {
      const candidates = [];
      const q = row.quoted_tweet;
      if (q && q.media) candidates.push(...q.media);
      for (const t of (row.thread || [])) {
        if (t.media) candidates.push(...t.media);
        if (t.quoted_tweet && t.quoted_tweet.media) candidates.push(...t.quoted_tweet.media);
      }
      // Dedupe by URL — parent + parent.quoted_tweet often point at the same
      // image (BobertBedford's SNDK chart was on both).
      const seen = new Set();
      images = candidates.filter(m => {
        if (!isDisplayableMedia(m)) return false;
        if (seen.has(m.url)) return false;
        seen.add(m.url);
        return true;
      });
    }
    if (images.length) {
      articleLeadMediaHtml = `
        <figure class="article-media single article-media-lead">
          ${renderImageWithOverlay(images[0])}
        </figure>`;
      interleavedImages = images.slice(1);
    }
    // Always present the original tweet (or quoted-parent) as a Baldessari-
    // style typographic block. When there's no lead image it stands alone
    // as the lead; when there IS a lead image it sits underneath as the
    // article's "voice" anchor. Falls back to the article's lede for orphan
    // articles whose underlying bookmark was lost in an earlier merge.
    const q = row.quoted_tweet;
    const keyText = (q && (q.text || q.raw))
      ? (q.text || q.raw)
      : (row.text || a.lede || "");
    if (keyText && keyText.trim()) {
      const palette = ["k","r","b","y","g"];
      let hh = 0;
      for (let i = 0; i < String(bookmarkId).length; i++) hh = (hh * 31 + String(bookmarkId).charCodeAt(i)) >>> 0;
      const tone = palette[hh % palette.length];
      const postStamp = row.created_at ? formatStampHour(row.created_at) : "";
      const authorName = row.author_name || row.author || row.username || "";
      articleKeyartHtml = `
        <figure class="article-keyart-lead" data-tone="${tone}">
          ${postStamp ? `<span class="article-keyart-stamp">${escape(postStamp)}</span>` : ""}
          <div class="article-keyart-text">${escape(keyText)}</div>
          ${authorName ? `<span class="article-keyart-byline">${escape(authorName)}</span>` : ""}
        </figure>`;
    }
    const author = row.username || row.author || "";
    const time = row.created_at ? new Date(row.created_at).toLocaleString() : "";
    const url = row.url || "";
    // An article is "stale" if it wasn't generated by the current top-tier
    // pipeline (Sonnet subagent path with full OCR + recursive research).
    // We flag the refresh button so the reader can see at a glance that this
    // brief was written before the upgrade and could be regenerated for more
    // depth + sources + OCR-aware context.
    const isStale = a.backend && a.backend !== "claude-sonnet-subagent";
    articleSourceHtml = `
      <p class="article-source">
        ${author ? `<span>@${escape(author)}</span>` : ""}
        ${time ? `<span class="muted">· ${escape(time)}</span>` : ""}
        <span class="article-source-actions">
          <button type="button" class="article-source-focus" data-bookmark-id="${escape(row.id)}" title="Regenerate with focus hint" aria-label="Regenerate with focus hint">Focus…</button>
          <button type="button" class="article-source-refresh${isStale ? ' needs-upgrade' : ''}" data-bookmark-id="${escape(row.id)}" title="${isStale ? 'This brief predates the current pipeline (no Sonnet sub-agent / OCR-aware research). Click to regenerate.' : 'Regenerate brief (shift-click for focus hint)'}" aria-label="Regenerate brief">↻</button>
          ${url ? `<a class="open-original" href="${escape(url)}" target="_blank" rel="noopener">Open on x.com →</a>` : ""}
        </span>
      </p>`;
  }

  slot.innerHTML = `
    <article class="article-block">
      <div class="article-toolbar">
        <div class="article-versions" data-bookmark-id="${escape(bookmarkId)}"></div>
        <div class="article-font-controls">
          <button type="button" class="toolbar-btn" data-action="font-toggle" aria-pressed="false">Serif</button>
          <button type="button" class="toolbar-btn" data-action="size-down" aria-label="Decrease text size">A−</button>
          <button type="button" class="toolbar-btn" data-action="size-up" aria-label="Increase text size">A+</button>
        </div>
      </div>
      <h3 class="article-headline">${escape(a.headline || "")}</h3>
      <p class="article-lede">${escape(a.lede || "")}</p>
      ${articleLeadMediaHtml}
      ${articleKeyartHtml}
      <div class="article-body">${renderBodyWithDefs()}</div>
      ${factCheckHtml}
      ${threadHtml}
      ${posterHtml}
      ${tickersHtml}
      ${conceptsHtml}
      ${booksHtml}
      ${reposHtml}
      ${sourcesHtml}
      ${articleSourceHtml}
      ${backendBadge ? `<div class="article-controls">${backendBadge}</div>` : ""}
    </article>`;
  applyArticleFontPrefs(slot.querySelector(".article-block"));
  // Load history pills now that the .article-versions element exists.
  loadArticleHistory(bookmarkId);
  if (articleIsWriting(bookmarkId)) updateArticleWritingIndicators(bookmarkId);
  if (inPane && (articleLeadMediaHtml || interleavedImages.length)) sizeAfterImageLoad(slot);
}

// ---------- article font / pane size prefs ----------

function applyArticleFontPrefs(block) {
  if (!block) return;
  const scale = parseFloat(localStorage.getItem("article-scale") || "1");
  const serif = localStorage.getItem("article-serif") === "1";
  block.style.setProperty("--s", String(Math.max(0.8, Math.min(1.6, scale))));
  block.classList.toggle("serif", serif);
  const toggle = block.querySelector('.toolbar-btn[data-action="font-toggle"]');
  if (toggle) {
    toggle.classList.toggle("active", serif);
    toggle.setAttribute("aria-pressed", String(serif));
  }
}

window.addEventListener("resize", () => sizeOcrOverlays(document));

function closeModal() {
  els.modal.classList.add("hidden");
  els.modal.setAttribute("aria-hidden", "true");
  setSelectedCard(null);
}

// ---------- core load ----------

async function refreshStats() {
  const stats = await fetchStats();
  if (!stats) return;
  const oldQueueIds = new Set(state.articleQueueIds);
  state.tagCounts = stats.tag_counts || {};
  state.totals = stats;
  state.articleIds = new Set((stats.article_ids || []).map(String));
  state.staleArticleIds = new Set((stats.stale_article_ids || []).map(String));
  state.articleQueue = stats.article_queue || { remaining_count: 0, total_count: 0, status: "idle" };
  state.articleQueueIds = new Set(((state.articleQueue || {}).remaining_ids || []).map(String));
  renderStats(stats);
  renderTagList();
  const touched = new Set([...oldQueueIds, ...state.articleQueueIds, ...state.inFlightArticleGen]);
  touched.forEach(updateArticleWritingIndicators);
}

async function refreshFeed() {
  const myToken = ++state.loadToken;
  els.cards.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
  const data = await fetchBookmarks();
  if (myToken !== state.loadToken) return;
  if (!data) {
    els.cards.innerHTML = `<div class="empty">Failed to load.</div>`;
    return;
  }
  state.rows = data.rows || [];
  renderActiveFilters();
  renderCards(state.rows, data.matched || 0, data.total || 0);
}

async function waitForArticleQueue() {
  while (Number((state.articleQueue || {}).remaining_count || 0) > 0) {
    await sleep(5000);
    await refreshStats();
    const queue = state.articleQueue || {};
    if ((queue.status === "stalled" || queue.status === "unknown") && Number(queue.remaining_count || 0) > 0) {
      break;
    }
  }
  if (Number((state.articleQueue || {}).remaining_count || 0) === 0) {
    await refreshStats();
    await refreshFeed();
  }
}

let statsPollBusy = false;
async function pollStatsQuietly() {
  if (statsPollBusy || document.hidden) return;
  statsPollBusy = true;
  const hadQueue = Number((state.articleQueue || {}).remaining_count || 0) > 0;
  try {
    await refreshStats();
    const hasQueue = Number((state.articleQueue || {}).remaining_count || 0) > 0;
    if (hadQueue && !hasQueue) {
      await refreshFeed();
    }
  } catch (err) {
    console.debug("stats poll failed", err);
  } finally {
    statsPollBusy = false;
  }
}

function toggleTag(tag) {
  if (state.multiSelect) {
    if (state.selectedTags.has(tag)) {
      state.selectedTags.delete(tag);
    } else {
      state.selectedTags.add(tag);
    }
  } else {
    // Single-select: clicking the active tag deselects, otherwise replace.
    if (state.selectedTags.has(tag) && state.selectedTags.size === 1) {
      state.selectedTags.clear();
    } else {
      state.selectedTags.clear();
      state.selectedTags.add(tag);
    }
  }
  renderTagList();
  refreshFeed();
}

// ---------- events ----------

const debouncedSearch = debounce(() => {
  state.query = els.search.value.trim();
  if (state.articlesMode) renderArticlesView();
  else refreshFeed();
}, 220);

els.search.addEventListener("input", debouncedSearch);

// Articles toggle: switch the feed body between the bookmark cards list and a
// newspaper-style grid of editorial briefs. Sort dropdown applies to either
// view; "By tag" only matters in articles mode.
async function loadArticles() {
  const res = await apiFetch("/api/articles");
  if (!res.ok) return [];
  const data = await res.json();
  return data.articles || [];
}

function escForId(s) { return CSS.escape(String(s)); }

function renderArticlesView() {
  if (!els.articlesView) return;
  let articles = state.articles.slice();
  // Search filter — match the same fields server-side haystack does.
  const q = state.query.toLowerCase().trim();
  if (q) {
    articles = articles.filter(a => {
      const hay = [
        a.headline, a.lede, a.poster,
        (a.tags || []).join(" "),
        (a.tickers || []).join(" "),
      ].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }

  if (state.sort === "tags") {
    // Group by each article's PRIMARY tag (the first tag in its tag list)
    // so every brief appears exactly once in exactly one section.
    const byTag = new Map();
    const untagged = [];
    for (const a of articles) {
      const primary = (a.tags || [])[0];
      if (!primary) { untagged.push(a); continue; }
      if (!byTag.has(primary)) byTag.set(primary, []);
      byTag.get(primary).push(a);
    }
    const sortedTags = [...byTag.entries()]
      .map(([t, arr]) => {
        arr.sort((x, y) => (y.generated_at || "").localeCompare(x.generated_at || ""));
        return [t, arr];
      })
      .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
    if (untagged.length) sortedTags.push(["untagged", untagged]);
    els.articlesView.innerHTML = sortedTags.map(([tag, arr]) => `
      <section class="news-tag-section">
        <h2 class="news-tag-rubric">#${escapeHtml(tag)}</h2>
        <div class="news-grid">
          ${arr.map(renderNewsCard).join("")}
        </div>
      </section>
    `).join("");
    return;
  }
  // Single chronological grid.
  const isPostSort = state.sort === "post-recent" || state.sort === "post-oldest";
  const isBookmarkSort = state.sort === "bookmarked-recent" || state.sort === "bookmarked-oldest";
  const recentDir = state.sort === "recent" || state.sort === "post-recent" || state.sort === "bookmarked-recent";
  const key = isPostSort
    ? (a) => a.created_at || a.generated_at || ""
    : isBookmarkSort
    ? (a) => a.bookmarked_at || a.created_at || a.generated_at || ""
    : (a) => a.generated_at || a.created_at || "";
  articles.sort((x, y) => {
    const ax = key(x), ay = key(y);
    return recentDir ? ay.localeCompare(ax) : ax.localeCompare(ay);
  });
  els.articlesView.innerHTML = `<div class="news-grid">${articles.map(renderNewsCard).join("")}</div>`;
}

function relationshipBadge(a) {
  // Show a small, sharp chip indicating how this bookmark relates to other
  // posts: thread (multi-part), reply (to another tweet), quote (of another
  // tweet), or nothing for plain originals.
  const isThread = !!(a.thread && a.thread.length);
  const isReply = !!(a.reply_to && a.reply_to.length);
  const isQuote = !!(a.quoted_tweet && (a.quoted_tweet.text || a.quoted_tweet.raw));
  if (isThread) return `<span class="rel-chip rel-thread" title="Part of a thread">↵ thread</span>`;
  if (isReply) return `<span class="rel-chip rel-reply" title="Reply to @${escapeHtml(a.reply_to[0])}">↳ reply to @${escapeHtml(a.reply_to[0])}</span>`;
  if (isQuote) return `<span class="rel-chip rel-quote" title="Quote tweet of @${escapeHtml(a.quoted_tweet.author_username || '')}">❝ quote @${escapeHtml(a.quoted_tweet.author_username || '')}</span>`;
  return "";
}

// When a bookmark has no media of its own, fall through the relationship
// chain — quoted-tweet media, then thread parent media, then thread parent's
// quoted-tweet media — for the next-best key visual. (Reply → quote-tweet →
// original image, etc.) Returns the first URL it finds.
function inheritedMediaUrl(a) {
  if (a.image_url) return a.image_url;
  const q = a.quoted_tweet;
  if (q && q.media && q.media[0] && q.media[0].url) return q.media[0].url;
  for (const t of (a.thread || [])) {
    if (t.media && t.media[0] && t.media[0].url) return t.media[0].url;
    if (t.quoted_tweet && t.quoted_tweet.media && t.quoted_tweet.media[0] && t.quoted_tweet.media[0].url) {
      return t.quoted_tweet.media[0].url;
    }
  }
  return null;
}

function renderNewsCard(a) {
  // Surface the date matching the selected chronological sort.
  const usePostDate = state.sort === "post-recent" || state.sort === "post-oldest";
  const useBookmarkedDate = state.sort === "bookmarked-recent" || state.sort === "bookmarked-oldest";
  const cardDate = usePostDate
    ? (a.created_at || a.generated_at)
    : useBookmarkedDate
    ? (a.bookmarked_at || a.created_at || a.generated_at)
    : (a.generated_at || a.created_at);
  let key;
  const visualUrl = inheritedMediaUrl(a);
  if (visualUrl) {
    key = `<div class="news-card-image"><img loading="lazy" src="${escapeHtml(visualUrl)}" alt=""></div>`;
  } else {
    // Typographic key art: a serif treatment of the tweet itself (or, for
    // quote-tweets, the parent tweet, which is usually the substantive bit).
    const q = a.quoted_tweet;
    const text = (q && (q.text || q.raw)) ? (q.text || q.raw) : (a.tweet_text || a.lede);
    if (text) {
      // Stable per-card palette pick (Baldessari primaries rotation): hash
      // the bookmark id so a card always gets the same colourway.
      const palette = ["k", "r", "b", "y", "g"];
      let h = 0;
      for (let i = 0; i < a.bookmark_id.length; i++) h = (h * 31 + a.bookmark_id.charCodeAt(i)) >>> 0;
      const tone = palette[h % palette.length];
      const postStamp = a.created_at ? formatStampHour(a.created_at) : "";
      const authorName = a.poster_name || a.poster || "";
      key = `<div class="news-card-keyart" data-tone="${tone}">${postStamp ? `<span class="news-card-keyart-stamp">${escapeHtml(postStamp)}</span>` : ""}<span class="news-card-keyart-text">${escapeHtml(text.slice(0, 280))}</span>${authorName ? `<span class="news-card-keyart-byline">${escapeHtml(authorName)}</span>` : ""}</div>`;
    } else {
      key = "";
    }
  }
  const date = cardDate ? formatStamp(cardDate) : "";
  return `
    <article class="news-card" data-bookmark-id="${escapeHtml(a.bookmark_id)}">
      ${key}
      <div class="news-card-meta">
        ${a.poster ? `<span>@${escapeHtml(a.poster)}</span>` : ""}
        ${date ? `<span class="muted">· ${escapeHtml(date)}</span>` : ""}
      </div>
      <h3 class="news-card-headline">${escapeHtml(a.headline)}</h3>
      <p class="news-card-lede">${escapeHtml(a.lede)}</p>
    </article>`;
}

// Index view: aggregated entity catalog (tickers, companies, concepts,
// books, repos, posters) across all written articles. Drawn from /api/index.
let _indexCache = null;
let _indexActiveTab = "tickers";

async function loadIndex() {
  if (_indexCache) return _indexCache;
  const r = await apiFetch("/api/index");
  _indexCache = await r.json();
  return _indexCache;
}

function setIndexMode(on, { pushHistory = true } = {}) {
  state.indexMode = on;
  if (els.indexBtn) els.indexBtn.setAttribute("aria-pressed", String(on));
  els.cards.classList.toggle("hidden", on);
  document.querySelector(".feed-header").classList.toggle("hidden", on);
  els.feedFooter.classList.toggle("hidden", on);
  if (els.indexView) els.indexView.classList.toggle("hidden", !on);
  document.body.classList.toggle("index-mode", on);
  if (on) {
    if (els.indexView) els.indexView.innerHTML = `<div class="empty"><span class="spinner"></span> Loading index…</div>`;
    loadIndex().then(() => renderIndexView(_indexActiveTab));
  }
  if (pushHistory) {
    history.pushState({ view: on ? "index" : "feed" }, "", on ? "#index" : "#");
  }
}

function renderIndexView(tab = _indexActiveTab) {
  if (!els.indexView || !_indexCache) return;
  _indexActiveTab = tab;
  const data = _indexCache;
  const escape = escapeHtml;

  const tabs = [
    { id: "tickers",   label: "Tickers",   n: (data.tickers || []).length },
    { id: "companies", label: "Companies", n: (data.companies || []).length },
    { id: "concepts",  label: "Concepts",  n: (data.concepts || []).length },
    { id: "books",     label: "Books",     n: (data.books || []).length },
    { id: "repos",     label: "Repos",     n: (data.repos || []).length },
    { id: "posters",   label: "Posters",   n: (data.posters || []).length },
  ];
  const tabsHtml = tabs.map(t => `
    <button type="button" class="index-tab${t.id === tab ? " active" : ""}" data-tab="${t.id}">
      ${escape(t.label)} <span class="index-tab-count">${t.n}</span>
    </button>`).join("");

  const items = data[tab] || [];
  const renderItem = (e) => {
    const idsAttr = (e.articles || []).join(",");
    if (tab === "tickers") {
      const m = e.module || {};
      return `<div class="index-entry">
        <div class="index-entry-head">
          <strong class="index-entry-key">$${escape(e.ticker)}</strong>
          ${m.name ? `<span class="index-entry-meta">${escape(m.name)}${m.exchange ? ` (${escape(m.exchange)})` : ""}</span>` : `<span class="index-entry-meta muted">no profile yet</span>`}
          <span class="index-entry-articles" data-ids="${escape(idsAttr)}" title="Open first article">${e.count} article${e.count===1?"":"s"}</span>
        </div>
        ${m.blurb ? `<p class="index-entry-body">${escape(m.blurb)}</p>` : ""}
      </div>`;
    }
    if (tab === "companies") {
      const m = e.module || {};
      return `<div class="index-entry">
        <div class="index-entry-head">
          <strong class="index-entry-key">${escape(e.name)}</strong>
          ${e.ticker ? `<span class="index-entry-meta">$${escape(e.ticker)}</span>` : ""}
          <span class="index-entry-articles" data-ids="${escape(idsAttr)}">${e.count} article${e.count===1?"":"s"}</span>
        </div>
        ${m.blurb ? `<p class="index-entry-body">${escape(m.blurb)}</p>` : ""}
      </div>`;
    }
    if (tab === "concepts") {
      return `<div class="index-entry">
        <div class="index-entry-head">
          <strong class="index-entry-key">${escape(e.term)}</strong>
          <span class="index-entry-articles" data-ids="${escape(idsAttr)}">${e.count} article${e.count===1?"":"s"}</span>
        </div>
        ${e.definition ? `<p class="index-entry-body">${escape(e.definition)}</p>` : ""}
      </div>`;
    }
    if (tab === "books") {
      return `<div class="index-entry">
        <div class="index-entry-head">
          <strong class="index-entry-key">${escape(e.title)}</strong>
          ${e.author ? `<span class="index-entry-meta">${escape(e.author)}</span>` : ""}
          <span class="index-entry-articles" data-ids="${escape(idsAttr)}">${e.count} article${e.count===1?"":"s"}</span>
        </div>
        ${e.summary ? `<p class="index-entry-body">${escape(e.summary)}</p>` : ""}
      </div>`;
    }
    if (tab === "repos") {
      return `<div class="index-entry">
        <div class="index-entry-head">
          <strong class="index-entry-key">${escape(e.key)}</strong>
          ${e.url ? `<a class="index-entry-meta" href="${escape(e.url)}" target="_blank" rel="noopener">↗</a>` : ""}
          <span class="index-entry-articles" data-ids="${escape(idsAttr)}">${e.count} article${e.count===1?"":"s"}</span>
        </div>
        ${e.summary ? `<p class="index-entry-body">${escape(e.summary)}</p>` : ""}
      </div>`;
    }
    if (tab === "posters") {
      const m = e.module || {};
      return `<div class="index-entry">
        <div class="index-entry-head">
          <strong class="index-entry-key">@${escape(e.username)}</strong>
          ${m.display_name ? `<span class="index-entry-meta">${escape(m.display_name)}</span>` : ""}
          <span class="index-entry-articles" data-ids="${escape(idsAttr)}">${e.count} article${e.count===1?"":"s"}</span>
        </div>
        ${m.blurb ? `<p class="index-entry-body">${escape(m.blurb)}</p>` : ""}
      </div>`;
    }
    return "";
  };
  els.indexView.innerHTML = `
    <div class="index-tabs">${tabsHtml}</div>
    <div class="index-entries">${items.map(renderItem).join("")}</div>
  `;
}

function setArticlesMode(on, { pushHistory = true } = {}) {
  state.articlesMode = on;
  els.articlesToggle.setAttribute("aria-pressed", String(on));
  // Label is fixed — Posts/Articles/Longform are four permanent topbar
  // buttons now; the pressed state is signalled via aria-pressed only.
  els.cards.classList.toggle("hidden", on);
  document.querySelector(".feed-header").classList.toggle("hidden", on);
  els.feedFooter.classList.toggle("hidden", on);
  els.articlesView.classList.toggle("hidden", !on);
  document.body.classList.toggle("articles-mode", on);
  if (els.articleDetail) {
    els.articleDetail.classList.add("hidden");
    els.articleDetail.innerHTML = "";
  }
  if (on) {
    // Always refetch — articles are regenerated frequently in the background
    // by the upgrade pass, and a stale cache hides the new versions.
    if (!state.articles.length) {
      els.articlesView.innerHTML = `<div class="empty"><span class="spinner"></span> Loading briefs…</div>`;
    } else {
      renderArticlesView(); // show cached immediately while we refetch
    }
    loadArticles().then((arr) => { state.articles = arr; renderArticlesView(); });
  }
  if (pushHistory) {
    history.pushState({ view: on ? "articles" : "feed" }, "", on ? "#articles" : "#");
  }
}

// Browser back/forward navigates between feed → articles → article-detail
// without leaving the site. Restore from URL hash on each popstate.
window.addEventListener("popstate", () => {
  const hash = location.hash || "";
  const isDetail = hash.startsWith("#article=");
  const isArticles = hash === "#articles" || isDetail;
  if (isArticles && !state.articlesMode) {
    setArticlesMode(true, { pushHistory: false });
  } else if (!isArticles && state.articlesMode) {
    setArticlesMode(false, { pushHistory: false });
    return;
  }
  const detailOpen = els.articleDetail && !els.articleDetail.classList.contains("hidden");
  if (isDetail) {
    const id = hash.slice("#article=".length);
    if (!detailOpen || els.articleDetail.querySelector(`.article-slot[data-bookmark-id="${CSS.escape(id)}"]`) == null) {
      openInDetail({ id }, { pushHistory: false });
    }
  } else if (detailOpen) {
    closeArticleDetail({ pushHistory: false });
  }
});

// Restore state on page load (e.g. when reloading a #article=… URL). Wait
// for state.articles to load before opening the detail so the keyart and
// media have the row data they need.
async function restoreFromHash() {
  const hash = location.hash || "";
  if (hash === "#articles") {
    setArticlesMode(true, { pushHistory: false });
  } else if (hash.startsWith("#article=")) {
    const id = hash.slice("#article=".length);
    setArticlesMode(true, { pushHistory: false });
    if (!state.articles.length) {
      try { state.articles = await loadArticles(); } catch {}
    }
    openInDetail({ id }, { pushHistory: false });
  } else if (hash === "#longform" || hash.startsWith("#longform=")) {
    if (hash.startsWith("#longform=")) {
      // Decode the URI-escaped segment ("companies%2Fvrt" → "companies/vrt").
      // openLongformReport re-encodes via encodeURIComponent before fetching;
      // leaving the encoded form here would cause a double-encode, and the
      // server's "companies/<id>" routing would miss it.
      state.longformReportId = decodeURIComponent(hash.slice("#longform=".length));
    }
    setLongformMode(true, { pushHistory: false });
  }
}

// Posts/Articles/Longform/Index render as four permanent topbar buttons.
// Exactly one is active at a time; clicking a button switches into that mode
// (and clicking the already-active button is a no-op rather than a toggle-off).
// updateTopbarNavState() keeps the aria-pressed attributes in sync so the
// active button reads pressed in the UI.
function updateTopbarNavState() {
  const inDefault = !state.articlesMode && !state.indexMode && !state.longformMode;
  if (els.postsBtn) els.postsBtn.setAttribute("aria-pressed", String(inDefault));
  if (els.articlesToggle) els.articlesToggle.setAttribute("aria-pressed", String(!!state.articlesMode));
  if (els.indexBtn) els.indexBtn.setAttribute("aria-pressed", String(!!state.indexMode));
  if (els.longformToggle) els.longformToggle.setAttribute("aria-pressed", String(!!state.longformMode));
}

if (els.postsBtn) {
  els.postsBtn.addEventListener("click", () => {
    if (state.articlesMode) setArticlesMode(false, { pushHistory: false });
    if (state.indexMode) setIndexMode(false, { pushHistory: false });
    if (state.longformMode) setLongformMode(false, { pushHistory: false });
    history.pushState({ view: "feed" }, "", "#");
    updateTopbarNavState();
  });
}
if (els.articlesToggle) {
  els.articlesToggle.addEventListener("click", () => {
    if (state.articlesMode) return;  // already active
    if (state.indexMode) setIndexMode(false, { pushHistory: false });
    if (state.longformMode) setLongformMode(false, { pushHistory: false });
    setArticlesMode(true);
    updateTopbarNavState();
  });
}
if (els.indexBtn) {
  els.indexBtn.addEventListener("click", () => {
    if (state.indexMode) return;
    if (state.articlesMode) setArticlesMode(false, { pushHistory: false });
    if (state.longformMode) setLongformMode(false, { pushHistory: false });
    setIndexMode(true);
    updateTopbarNavState();
  });
}

// ---------- longform mode ----------
//
// Editorial reader for long pieces compiled by Bookmark/longform/. Each report
// is one assembled JSON in data/longform/<id>/reports/report.json with
// {meta, abstract, chapters[], entities, bibliography}. Chapters carry markdown
// bodies (a small subset: paragraphs, ## subhead, **bold**, *italic*, > quote,
// links, code spans).

state.longformMode = false;
state.longformReportId = null;
state.longformReport = null;
state.longformObserver = null;
state.longformOptionTracks = {};

// Keep the topbar nav buttons in sync with the active mode after popstate
// (browser back/forward triggers mode changes outside the click handlers).
window.addEventListener("popstate", () => updateTopbarNavState());

const LONGFORM_INLINE_RE = {
  link: /\[([^\]]+)\]\(([^)]+)\)/g,
  code: /`([^`]+)`/g,
  // Fact-check redline markup: ~~removed~~ renders as a red strikethrough
  // deletion; ==added== renders as a yellow-highlighted insertion. Applied
  // before bold/italic so the corrected spans can themselves carry emphasis.
  strike: /~~([^~]+)~~/g,
  highlight: /==([^=]+)==/g,
  bold: /\*\*([^*]+)\*\*/g,
  italic: /\*([^*]+)\*/g,
};

// === Entity highlighting (terminal-style) ============================
// Colour-code dates / money / metrics / tickers / glossary / names in the
// chapter body so the reader can scan for the right datapoint quickly.
// Applied to ESCAPED HTML text BEFORE markdown bold/italic so the wrapping
// spans don't collide with **strong** or *em* asterisks.
//
// Order in the alternation matters — longest/most-specific first. Each
// branch is a NAMED capture group; the function replacement picks the
// matching group and assigns a class.

const _MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec";
const _UNITS_FULL = "trillion|billion|million|thousand|hundred";
const _NUM_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety";

const LONGFORM_HIGHLIGHT_RE = new RegExp([
  // 1. DATES
  //    "December 31, 2025" / "December 2025" / "May 1982" / "July 17".
  //    Try 4-digit year FIRST so "May 2022" isn't truncated to "May 20".
  `(?<dateMonth>(?:${_MONTHS})\\.?\\s+(?:\\d{4}|\\d{1,2}(?:st|nd|rd|th)?(?:,\\s*\\d{4})?))`,
  //    "Q2 FY2025" / "Q2 2025" / "Q2'25" / "Q2–Q3 2026" (range)
  `(?<dateQtr>Q[1-4](?:[\\s]*[–\\-]\\s*Q[1-4])?(?:\\s*FY)?\\s*(?:'?\\d{2,4}|\\d{4}))`,
  //    "FY2025" / "FY25" / "H1 2024" / "1H25" / "CY2024".
  //    H1/H2 must be SEPARATED from the year (space or apostrophe) so that
  //    GPU/product codes like H100, H200, H300 don't false-positive as dates.
  `(?<dateFY>(?:FY|CY)\\s*\\d{2,4}|H[12](?:\\s+|')\\d{2,4}|[12]H\\s*'?\\d{2,4})`,
  //    "mid-2010s" / "early 1990s" / "late 2020s" / "2010-2020" / "2024"
  `(?<dateYear>(?:(?:mid|early|late)[\\s\\-])?(?:19|20)\\d{2}s?(?:[–\\-](?:19|20)?\\d{2}s?)?)`,

  // 2. MONEY
  //    "$12M" / "$270 million" / "$1.85B" / "$2.5 trillion" / "$25–30 million"
  `(?<moneyDollar>\\$\\d[\\d,]*(?:\\.\\d+)?(?:[\\s]*[–\\-—]\\s*\\$?\\d[\\d,]*(?:\\.\\d+)?)?(?:\\s*(?:${_UNITS_FULL}|[KMBT]|bn|mn|tn))?)`,
  //    "Nine billion" / "two hundred million" (word-based)
  `(?<moneyWord>\\b(?:${_NUM_WORDS})(?:[- ](?:hundred|thousand))?\\s+(?:${_UNITS_FULL})(?:\\s+dollars?)?\\b)`,

  // 4. METRICS
  //    "200% YoY" / "13% growth" / "+12.9%" / "−3.0%" / "38 percent" /
  //    "57-percent" / "57 percentage points" / "25–30 percent" (ranges) /
  //    "+168 per cent" (British two-word spelling).
  `(?<metricPct>[+\\-−]?\\d+(?:\\.\\d+)?(?:[\\s]*[–\\-—]\\s*\\d+(?:\\.\\d+)?)?[\\s\\-]*(?:%|per\\s*cent(?:age)?(?:\\s+points?)?|pct|bps)(?:\\s*(?:YoY|QoQ|MoM|YTD|CAGR|year[- ]over[- ]year))?)`,
  //    "5x Sales" / "45.2x" / "2.5×" / "0.7–0.9x" (range)
  `(?<metricMult>\\d+(?:\\.\\d+)?(?:[\\s]*[–\\-—]\\s*\\d+(?:\\.\\d+)?)?[x×])`,

  // 6. TICKERS
  //    "$TSM" / "$NVDA" / "$ASML.AS" / "$0992.HK" — alphanumeric ticker syms
  `(?<ticker>\\$[A-Z][A-Z0-9]{0,5}(?:\\.[A-Z]{1,3})?\\b)`,
].join("|"), "g");

// Classify a number-bearing match as positive / negative / neutral by
// inspecting ~30–50 chars before and after the match for sign words.
// "up 23 percent" → positive; "$35M loss" → negative; "$10B revenue" → neutral.
const _SIGN_NEG_BEFORE = /\b(down|fell|fall|fallen|lost|los[ts]|loss|losses|declin[a-z]*|decreas[a-z]*|lower|contract[a-z]*|drop[a-z]*|plunge[a-z]*|tumble[a-z]*|shr[au]nk|narrow[a-z]*|reduc[a-z]*|impair[a-z]*|writedown|writeoff|negat[a-z]*|misse?d?|short(fall)?|under|deficit|sank|slow[a-z]*)\b[^.?!]{0,40}$/i;
const _SIGN_POS_BEFORE = /\b(up|rose|risen|grew|grow[ns]?|gain[a-z]*|increas[a-z]*|higher|expand[a-z]*|jump[a-z]*|surg[a-z]*|rall[a-z]*|advanc[a-z]*|climb[a-z]*|added|swell[a-z]*|posit[a-z]*|beat|exceed[a-z]*|outperform[a-z]*|over|profit[a-z]*|boost[a-z]*|topp[a-z]*)\b[^.?!]{0,40}$/i;
// Allow a short connector (in / of / from / for / as / per) between the
// number and the financial-context noun, with up to two adjectives between
// connector and noun so "$8.9M in operating cash flow" / "$5.1B in net
// revenue" / "$48B of strategic impairment" all classify correctly.
const _SIGN_NEG_AFTER = /^[\s,;]*?(?:(?:in|of|from|for|as|to|per)\s+)?(?:[a-z]+(?:\s+[a-z]+)?\s+)?\b(loss|losses|deficit|impair[a-z]*|writedown|writeoff|short(fall)?|decline|drop|fell|declin[a-z]*|debt|leverage|liabilit[a-z]*|charge|penal[a-z]*|fine|interest\s+expense|net\s+loss)\b/i;
const _SIGN_POS_AFTER = /^[\s,;]*?(?:(?:in|of|from|for|as|to|per)\s+)?(?:[a-z]+(?:\s+[a-z]+)?\s+)?\b(cash\s+flow|free\s+cash\s+flow|FCF|operating\s+cash|operating\s+income|net\s+income|gross\s+profit|margin\s+expansion|gain|gains|profit[a-z]*|surplus|income|growth|revenue|earnings|cash|dividend|buyback|EBITDA|backlog|ARR|RPO|orders|increase)\b/i;

// "from X to Y" range continuation — if we're looking at Y (the destination
// number) and the prose said "expanded from N1 to N2", N2 should inherit the
// "expanded" verb's positive sign even though no direct verb sits next to N2.
// Use lazy quantifiers so a sentence like "climbed from $5B to $10B" actually
// matches "climbed ... from ... to" with N2 at the end (greedy would consume
// everything and fail).
const _RANGE_FROM_TO = /\b(expand|grew|grow|gain|increas|higher|jump|surg|rall|advanc|climb|rose|rise|risen|swell|posit|beat|exceed|outperform|boost|reach|raised|raise|widen|deepen|swing|recover|rebound|hit|topp|outpac|accelerat)[a-z]*\b(?:[^.?!]|\.\d){0,30}?\bfrom\b(?:[^.?!]|\.\d){0,140}?\bto\s*$/i;
const _RANGE_FROM_TO_NEG = /\b(declin|fell|fall|fallen|drop|plunge|tumble|shr[au]nk|narrow|reduc|decreas|lower|contract|loss|sank|slow|miss|underperform|deteriorate|erode|weaken)[a-z]*\b(?:[^.?!]|\.\d){0,30}?\bfrom\b(?:[^.?!]|\.\d){0,140}?\bto\s*$/i;

function classifyNumberSign(match, chunk, offset, kind) {
  // Explicit sign at the start of the match wins.
  if (/^\+/.test(match)) return `lf-hl-${kind}-pos`;
  if (/^[−\-]/.test(match)) return `lf-hl-${kind}-neg`;
  const before = chunk.slice(Math.max(0, offset - 200), offset);
  const after = chunk.slice(offset + match.length, offset + match.length + 40);
  // Trailing "loss" / "gain" within ~8 chars after the number is strongest.
  if (_SIGN_NEG_AFTER.test(after)) return `lf-hl-${kind}-neg`;
  if (_SIGN_POS_AFTER.test(after)) return `lf-hl-${kind}-pos`;
  // "from X to Y" pattern — Y inherits the sign of the verb before "from".
  if (_RANGE_FROM_TO.test(before)) return `lf-hl-${kind}-pos`;
  if (_RANGE_FROM_TO_NEG.test(before)) return `lf-hl-${kind}-neg`;
  // Otherwise look at the directional verb immediately preceding.
  if (_SIGN_NEG_BEFORE.test(before.slice(-60))) return `lf-hl-${kind}-neg`;
  if (_SIGN_POS_BEFORE.test(before.slice(-60))) return `lf-hl-${kind}-pos`;
  return `lf-hl-${kind}`;
}

// Wrap LLM-identified metric phrases as background-tinted spans. Runs BEFORE
// the regex/scope passes so the wrap is preserved across them; the inner
// money / metric / date highlights still apply within the phrase.
function wrapMetricPhrases(escapedHtml, phrases) {
  if (!phrases || !phrases.length) return escapedHtml;
  let out = escapedHtml;
  for (const p of phrases) {
    // The phrase text from JSON is unescaped; escape it to match the
    // already-escaped HTML, then replace ONE occurrence (avoid double-wrap).
    const needle = escapeHtml(p.text);
    const idx = out.indexOf(needle);
    if (idx < 0) continue;
    out = out.slice(0, idx) +
          `<span class="lf-hl-phrase lf-hl-phrase-${p.sign || "neutral"}">${needle}</span>` +
          out.slice(idx + needle.length);
  }
  return out;
}

// Wrap the LLM-selected top sentences ("understand the company at a glance")
// with a highlighter-pen background. Runs BEFORE other wrappers so the inner
// number / company / date highlights still apply within the sentence.
function wrapKeySentences(escapedHtml, keySentences) {
  if (!keySentences || !keySentences.length) return escapedHtml;
  let out = escapedHtml;
  for (const ks of keySentences) {
    const needle = escapeHtml(ks.text);
    const idx = out.indexOf(needle);
    if (idx < 0) continue;
    const tooltip = ks.why ? ` title="${escapeAttr(ks.why)}"` : "";
    out = out.slice(0, idx) +
          `<mark class="lf-hl-keysentence"${tooltip}>${needle}</mark>` +
          out.slice(idx + needle.length);
  }
  return out;
}

function highlightEntities(escapedHtml) {
  // Skip text already inside HTML tags (we ran link/code/strike before this).
  // Strategy: split on tag boundaries, only transform text segments.
  return escapedHtml.replace(/<[^>]*>|[^<]+/g, (chunk) => {
    if (chunk.startsWith("<")) return chunk;
    // Pass 1 — pure regex categories (no entity-list dependency):
    //          dates, money, metrics, $TICKER patterns.
    let out = chunk.replace(LONGFORM_HIGHLIGHT_RE, (m, ...rest) => {
      // Last 3 of `rest` are [offset, fullString, namedGroups].
      const groups = rest[rest.length - 1] || {};
      const fullStr = rest[rest.length - 2];
      const offset = rest[rest.length - 3];
      if (groups.dateMonth || groups.dateQtr || groups.dateFY || groups.dateYear)
        return `<span class="lf-hl-date">${m}</span>`;
      if (groups.moneyDollar || groups.moneyWord)
        return `<span class="${classifyNumberSign(m, fullStr, offset, "money")}">${m}</span>`;
      if (groups.metricPct || groups.metricMult)
        return `<span class="${classifyNumberSign(m, fullStr, offset, "metric")}">${m}</span>`;
      if (groups.ticker) {
        const tk = m.replace(/^\$/, "").toUpperCase();
        const slug = (state.lfTickerToSlug || {})[tk];
        if (slug) return `<a class="lf-hl-ticker" href="#longform=companies%2F${slug}" data-lf-ticker="${tk}">${m}</a>`;
        return `<a class="lf-hl-ticker" href="#index" data-lf-ticker="${tk}">${m}</a>`;
      }
      return m;
    });
    // Pass 2 — scope-based highlights (LLM-confirmed entities only). The
    // dossier-level scope is the source of truth for companies / people /
    // places / corporate-concepts. If no scope is loaded yet (sweep hasn't
    // produced this dossier's data) we fall back to the global company-name
    // regex from /api/index.
    const scope = state.lfDossierScope;
    if (scope && scope.entityRe) {
      out = highlightFromScope(out, scope);
    } else {
      out = highlightCompanyNames(out);
    }
    // Pass 3 — sitewide glossary terms (acronyms) — case-sensitive.
    if (scope && scope.glossaryRe) {
      out = highlightGlossary(out, scope);
    }
    return out;
  });
}

// Scope-based highlighter. Walks the chunk against the per-dossier entity
// regex; for each match, looks up which category it belongs to and emits
// the appropriate tag + colour. Uses an inner tag-skip splitter so already-
// wrapped content (links from earlier passes) isn't re-wrapped.
function highlightFromScope(text, scope) {
  return text.replace(/<[^>]*>|[^<]+/g, (chunk) => {
    if (chunk.startsWith("<")) return chunk;
    return chunk.replace(scope.entityRe, (match) => {
      const k = match.toLowerCase();
      const co = scope.companies.get(k);
      if (co) {
        const slug = (state.lfTickerToSlug || {})[(co.ticker || "").toUpperCase()] ||
                     (state.lfCompanyMap && state.lfCompanyMap[k] && state.lfCompanyMap[k].slug) || "";
        const title = co.ticker ? `${co.name} ($${co.ticker})` : co.name;
        if (slug) return `<a class="lf-hl-company" href="#longform=companies%2F${slug}" title="${escapeAttr(title)}">${match}</a>`;
        return `<a class="lf-hl-company" href="#index" data-entity="${escapeAttr(co.name)}" title="${escapeAttr(title)}">${match}</a>`;
      }
      const p = scope.people.get(k);
      if (p) {
        const title = p.role ? `${p.name} — ${p.role}` : p.name;
        return `<span class="lf-hl-name" title="${escapeAttr(title)}">${match}</span>`;
      }
      const pl = scope.places.get(k);
      if (pl) {
        const title = pl.type ? `${pl.name} (${pl.type})` : pl.name;
        return `<span class="lf-hl-place" title="${escapeAttr(title)}">${match}</span>`;
      }
      const cc = scope.concepts.get(k);
      if (cc) return `<span class="lf-hl-concept" title="${escapeAttr(cc.brief)}">${match}</span>`;
      return match;
    });
  });
}

// Highlight glossary acronyms (CPO, MBE, HBM, EUV) with their expansion as a
// hover tooltip. Case-sensitive because acronyms are typically uppercase
// (lowercase "mbe" inside a longer word shouldn't match).
function highlightGlossary(text, scope) {
  return text.replace(/<[^>]*>|[^<]+/g, (chunk) => {
    if (chunk.startsWith("<")) return chunk;
    return chunk.replace(scope.glossaryRe, (match) => {
      // glossary keys are lowercased in the Map, but we want the match's case
      const entry = scope.glossary.get(match.toLowerCase());
      if (!entry) return match;
      return `<span class="lf-hl-glossary" title="${escapeAttr(entry.expansion)}">${match}</span>`;
    });
  });
}

// Fallback when no per-dossier entity scope is loaded yet: highlight company
// names from the global /api/index. Kept so non-swept dossiers still get
// some highlighting; will be obsolete once every dossier has entities.
function highlightCompanyNames(text) {
  const re = state.lfCompanyRe;
  if (!re) return text;
  return text.replace(re, (match) => {
    const key = match.toLowerCase();
    const entry = state.lfCompanyMap[key];
    if (!entry) return match;
    if (entry.slug) {
      return `<a class="lf-hl-company" href="#longform=companies%2F${entry.slug}" title="${escapeAttr(entry.title || entry.name)}">${match}</a>`;
    }
    return `<a class="lf-hl-company" href="#index" data-entity="${escapeAttr(entry.name)}" title="${escapeAttr(entry.title || entry.name)}">${match}</a>`;
  });
}

function escapeAttr(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// Per-dossier entity scope (LLM-confirmed): the renderer treats this as the
// SOURCE OF TRUTH for company / people / place / glossary / concept mentions.
// Names not confirmed by the per-chapter Sonnet extraction are NOT highlighted
// even if a regex or the global index would otherwise match them — that's
// what eliminates the false positives ("Apple" the fruit, "Mark" the verb).
async function ensureDossierEntityScope(slug) {
  if (!slug) return null;
  if (state.lfDossierScope && state.lfDossierScope._slug === slug) {
    return state.lfDossierScope;
  }
  try {
    const idxR = await apiFetch(`/api/longform-entities/${encodeURIComponent(slug)}`);
    if (!idxR.ok) { state.lfDossierScope = null; return null; }
    const data = await idxR.json();
    const scope = {
      _slug: slug,
      companies: new Map(),  // lowercased name -> {name, ticker?, slug?}
      people: new Map(),     // lowercased name -> {name, role?}
      places: new Map(),     // lowercased name -> {name, type?}
      glossary: new Map(),   // lowercased term -> {term, expansion}
      concepts: new Map(),   // lowercased name -> {name, brief}
    };
    for (const c of (data.companies || [])) {
      if (!c.name) continue;
      scope.companies.set(c.name.toLowerCase(), c);
    }
    for (const p of (data.people || [])) {
      if (!p.name) continue;
      scope.people.set(p.name.toLowerCase(), p);
    }
    for (const pl of (data.places || [])) {
      if (!pl.name) continue;
      scope.places.set(pl.name.toLowerCase(), pl);
    }
    for (const g of (data.glossary || [])) {
      if (!g.term) continue;
      scope.glossary.set(g.term.toLowerCase(), g);
    }
    for (const cc of (data.corporate_concepts || [])) {
      if (!cc.name) continue;
      scope.concepts.set(cc.name.toLowerCase(), cc);
    }
    // Official social-media handles (codex-discovered). Surfaced as icon links
    // next to the rail's "N sources" line.
    scope.socials = data.socials || null;
    // LLM-identified metric phrases — whole sub-sentences carrying a number
    // whose sign is set by the full sentence's context. The renderer wraps
    // these phrases verbatim BEFORE the regex pass so the wrapping survives.
    scope.phrases = (data.metric_phrases || [])
      .filter((mp) => mp && mp.text && mp.text.length >= 8)
      .map((mp) => ({ text: mp.text, sign: (mp.sign || "neutral").toLowerCase() }))
      // Longest first so a phrase containing another phrase wins.
      .sort((a, b) => b.text.length - a.text.length);
    // LLM-selected key sentences — the top ~10-20% of sentences in a chapter
    // that matter most for understanding the company. Rendered with a
    // highlighter-pen background so they stand out at a glance.
    scope.keySentences = (data.key_sentences || [])
      .filter((ks) => ks && ks.text && ks.text.length >= 20)
      .map((ks) => ({ text: ks.text, why: ks.why || "" }))
      .sort((a, b) => b.text.length - a.text.length);
    // Compile a single regex matching any confirmed entity name. Sort by
    // length descending so longer names win the alternation.
    const allKeys = [
      ...scope.companies.keys(),
      ...scope.people.keys(),
      ...scope.places.keys(),
      ...scope.concepts.keys(),
    ].filter((k) => k.length >= 3);
    const glossaryKeys = [...scope.glossary.keys()].filter((k) => k.length >= 2);
    const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (allKeys.length) {
      const sorted = [...new Set(allKeys)].sort((a, b) => b.length - a.length);
      scope.entityRe = new RegExp("\\b(" + sorted.map(escapeRe).join("|") + ")\\b", "gi");
    }
    if (glossaryKeys.length) {
      const sorted = [...new Set(glossaryKeys)].sort((a, b) => b.length - a.length);
      // Glossary terms are usually acronyms — match case-sensitively so "MBE"
      // matches "MBE" but not "mbe" inside another word.
      scope.glossaryRe = new RegExp("\\b(" + sorted.map(escapeRe).join("|") + ")\\b", "g");
    }
    state.lfDossierScope = scope;
    // Once the scope is loaded, paint the social-link icons into the rail.
    renderSocialIcons(scope.socials);
    return scope;
  } catch (e) {
    console.warn("Failed to load dossier entity scope:", e);
    state.lfDossierScope = null;
    return null;
  }
}

// Render the rail's social-link icons (X / Instagram / LinkedIn / YouTube)
// once codex has discovered them for this dossier.
function renderSocialIcons(socials) {
  const slot = document.getElementById("lfTocSocials");
  if (!slot) return;
  if (!socials) { slot.innerHTML = ""; return; }
  const make = (key, url, label, glyph) => url
    ? `<a class="lf-soc lf-soc-${key}" href="${escapeAttr(url)}"
          target="_blank" rel="noopener" title="${label}"
          aria-label="${label}">${glyph}</a>`
    : "";
  // Plain-text monogram labels — render the same in every browser and font
  // stack. Tooltip names each on hover.
  const html = [
    make("x",  socials.twitter,   "Twitter / X", "X"),
    make("ig", socials.instagram, "Instagram",   "IG"),
    make("li", socials.linkedin,  "LinkedIn",    "in"),
    make("yt", socials.youtube,   "YouTube",     "YT"),
  ].filter(Boolean).join("");
  slot.innerHTML = html;
}

// Fetch the sitewide entity index + dossier overview, build a single regex
// over company names + tickers so chapter rendering can colour-link mentions.
async function ensureLongformEntityIndex() {
  if (state.lfCompanyRe) return;
  try {
    const [idxR, ovR] = await Promise.all([
      apiFetch("/api/index").then(r => r.ok ? r.json() : null).catch(() => null),
      apiFetch("/api/longform-overview").then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    const map = {};       // lowercased name -> entry
    const tickerToSlug = {};
    const dossiers = (ovR && ovR.reports) || [];
    for (const r of dossiers) {
      if (r.ticker) tickerToSlug[r.ticker.toUpperCase()] = r.slug;
      if (r.name) map[r.name.toLowerCase()] = { name: r.name, slug: r.slug, title: `${r.name} dossier` };
      if (r.display_name && r.display_name !== r.name) {
        map[r.display_name.toLowerCase()] = { name: r.display_name, slug: r.slug, title: `${r.name} dossier` };
      }
    }
    // Companies from the bookmark index — keep ones with count ≥ 2 to filter
    // noise. Allow 3-char names like "AMD" / "TSM" / "IBM" but only if they
    // are all-uppercase (so common 3-letter words don't over-match). The
    // regex still requires word boundaries.
    const companies = (idxR && idxR.companies) || [];
    for (const c of companies) {
      if (!c.name) continue;
      if (c.name.length < 3) continue;
      if (c.name.length === 3 && c.name !== c.name.toUpperCase()) continue;
      if ((c.count || 0) < 2) continue;
      const k = c.name.toLowerCase();
      // If a longform dossier exists for this company's ticker, link
      // straight to it; otherwise this is an article-index reference.
      const dossierSlug = c.ticker ? tickerToSlug[c.ticker.toUpperCase()] : null;
      if (!map[k]) {
        map[k] = { name: c.name, ticker: c.ticker || null,
                   slug: dossierSlug || undefined,
                   title: dossierSlug ? `${c.name} dossier` : `${c.name} — ${c.count} bookmarks` };
      } else if (!map[k].slug && dossierSlug) {
        map[k].slug = dossierSlug;
      }
    }
    // Also seed bare tickers ("AMD", "TSM", "NVDA") as company aliases so
    // their bare-word usage in chapter prose links back to the dossier.
    for (const r of dossiers) {
      if (!r.ticker || r.ticker.length < 3) continue;
      const k = r.ticker.toLowerCase();
      if (!map[k]) {
        map[k] = { name: r.ticker, slug: r.slug, title: `${r.name} dossier` };
      }
    }
    // Tickers from the bookmark index (for the $TICKER pattern dossier-linking).
    for (const t of (idxR && idxR.tickers) || []) {
      if (t.ticker) tickerToSlug[t.ticker.toUpperCase()] = tickerToSlug[t.ticker.toUpperCase()] || null;
    }
    // Build the alternation. Sort by length desc so longer names win.
    const names = Object.keys(map).sort((a, b) => b.length - a.length);
    if (!names.length) return;
    const escaped = names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    state.lfCompanyMap = map;
    state.lfCompanyRe = new RegExp("\\b(" + escaped.join("|") + ")\\b", "gi");
    state.lfTickerToSlug = tickerToSlug;
  } catch (e) {
    console.warn("Failed to build longform entity index:", e);
  }
}

// Split a chapter title at the first ": " into a kicker + the title proper,
// rendered on two lines. "Financial Deep Dive: Revenue Trajectory, ..." →
//   kicker = "Financial Deep Dive"
//   title  = "Revenue Trajectory, ..."
// Falls back to the unchanged title if there's no colon.
function renderChapterTitle(title) {
  const t = String(title || "").trim();
  if (!t) return "";
  const i = t.indexOf(": ");
  if (i <= 0 || i > 60) return escapeHtml(t);
  const kicker = t.slice(0, i).trim();
  const main = t.slice(i + 2).trim();
  return `<span class="lf-chapter-kicker">${escapeHtml(kicker)}</span>` +
         `<span class="lf-chapter-mainline">${escapeHtml(main)}</span>`;
}

// Split a long FT-style dossier title into a punchy lead + a smaller deck.
// "TSMC: How Chairman C.C. Wei Is Defending The World's Largest Foundry..."
// becomes  H1="TSMC"  +  deck="How Chairman C.C. Wei Is Defending...".
// Splits at the first ":" or " — " (em-dash). If no natural break exists
// (or the lead would be ridiculously short), renders the title whole as H1.
function renderLongformTitle(title, fallbackSubtitle) {
  const t = String(title || "").trim();
  if (!t) return "";
  // Split at the first ":" (not part of a time / URL fragment) or " — ".
  const colonIdx = t.indexOf(": ");
  const dashIdx = t.indexOf(" — ");
  let cut = -1, cutLen = 0;
  if (colonIdx > 0 && colonIdx < 80) { cut = colonIdx; cutLen = 2; }
  if (dashIdx > 0 && dashIdx < 80 && (cut < 0 || dashIdx < cut)) { cut = dashIdx; cutLen = 3; }
  if (cut < 0) {
    return `<h1 class="lf-title">${escapeHtml(t)}</h1>` +
      (fallbackSubtitle ? `<p class="lf-subtitle">${escapeHtml(fallbackSubtitle)}</p>` : "");
  }
  const lead = t.slice(0, cut).trim();
  const deck = t.slice(cut + cutLen).trim();
  return `<h1 class="lf-title">${escapeHtml(lead)}</h1>
          <p class="lf-deck">${escapeHtml(deck)}</p>`;
}

// Break a long single-paragraph abstract into readable chunks. The dossier
// abstracts come back as one giant prose block (no \n\n in source). Split on
// sentence boundaries, then group ~3 sentences per paragraph so the lede
// reads like an FT/Bloomberg lead instead of a wall of text. If the source
// already contains paragraph breaks, honour those instead.
function splitAbstractParagraphs(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return [];
  if (/\n\s*\n/.test(trimmed)) {
    return trimmed.split(/\n\s*\n+/).map(s => s.trim()).filter(Boolean);
  }
  // Sentence-split that ignores Inc./Corp./Ltd./Dr./U.S./Mr. style fake-ends.
  const ABBR = /(?:Inc|Corp|Ltd|Co|U\.S|U\.K|Dr|Mr|Mrs|Ms|St|No|vs|approx|e\.g|i\.e|Jr|Sr)$/i;
  const sentences = [];
  let buf = "";
  for (let i = 0; i < trimmed.length; i++) {
    buf += trimmed[i];
    if (/[.?!]/.test(trimmed[i]) && /\s/.test(trimmed[i + 1] || " ")
        && /[A-Z(]/.test(trimmed[i + 2] || "")) {
      // Check the token immediately before the punctuation for abbreviations.
      const m = buf.match(/(\S+)[.?!]$/);
      if (!m || !ABBR.test(m[1].replace(/[.?!]$/, ""))) {
        sentences.push(buf.trim());
        buf = "";
      }
    }
  }
  if (buf.trim()) sentences.push(buf.trim());
  // Group into paragraphs of 2–3 sentences. Aim for ~350–500 chars per para.
  const paras = [];
  let cur = "";
  for (const s of sentences) {
    if (cur && (cur.length + s.length > 420 || cur.split(/[.?!]\s/).length >= 3)) {
      paras.push(cur.trim());
      cur = s;
    } else {
      cur = cur ? cur + " " + s : s;
    }
  }
  if (cur.trim()) paras.push(cur.trim());
  return paras;
}

function longformInline(s) {
  if (!s) return "";
  let out = escapeHtml(s);
  out = out.replace(LONGFORM_INLINE_RE.link, (_, txt, url) =>
    `<a href="${url.replace(/"/g, "&quot;")}" target="_blank" rel="noopener">${txt}</a>`);
  out = out.replace(LONGFORM_INLINE_RE.code, "<code>$1</code>");
  out = out.replace(LONGFORM_INLINE_RE.strike, '<del class="fc-del">$1</del>');
  out = out.replace(LONGFORM_INLINE_RE.highlight, '<mark class="fc-ins">$1</mark>');
  // Wrap LLM-selected key sentences FIRST (outer highlighter-pen background),
  // then metric phrases on top of those, then regex/scope inner highlights.
  const scope = state.lfDossierScope;
  if (scope && scope.keySentences) out = wrapKeySentences(out, scope.keySentences);
  if (scope && scope.phrases) out = wrapMetricPhrases(out, scope.phrases);
  // Apply colour-coded entity highlights to remaining plain text. Skips
  // text already wrapped by the steps above so we don't double-wrap.
  out = highlightEntities(out);
  // Post-pass: when a number-bearing redline insertion sits next to its
  // unit ("<mark>55–58</mark> percent"), the original entity highlighter
  // couldn't reach across the tag boundary. Re-wrap the unit so the whole
  // metric reads as a coloured run.
  out = stitchRedlineMetrics(out);
  out = out.replace(LONGFORM_INLINE_RE.bold, "<strong>$1</strong>");
  out = out.replace(LONGFORM_INLINE_RE.italic, "<em>$1</em>");
  return out;
}

// Glue a metric / money / date unit that sits OUTSIDE a fact-check insertion
// to the number INSIDE it, so "<mark class=fc-ins>55–58</mark> percent" reads
// as one cohesive metric.
function stitchRedlineMetrics(html) {
  const _UNIT_PCT = /^(\s*)(per\s*cent(?:age)?(?:\s+points?)?|percent|pct|bps|%)/i;
  const _UNIT_MONEY = /^(\s*)(million|billion|trillion|thousand|bn|mn|tn|M|B|T|K)\b/i;
  return html.replace(
    /(<mark class="fc-ins">)([+\-−]?\d[\d.,\s\-–—xX×]*?)(<\/mark>)([^<]{0,40})/gi,
    (full, openTag, number, closeTag, tail) => {
      const pctM = _UNIT_PCT.exec(tail);
      if (pctM) {
        return `${openTag}<span class="lf-hl-metric">${number}${pctM[1]}${pctM[2]}</span>${closeTag}${tail.slice(pctM[0].length)}`;
      }
      const monM = _UNIT_MONEY.exec(tail);
      if (monM && /^[$£€¥]?\d/.test(number.trim())) {
        return `${openTag}<span class="lf-hl-money">${number}${monM[1]}${monM[2]}</span>${closeTag}${tail.slice(monM[0].length)}`;
      }
      return full;
    }
  );
}

// Split a markdown table row "| a | b | c |" into trimmed cells.
function splitTableRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}
// Is `line` a markdown table separator like "|---|---|" / "---|---" /
// "| :--- | ---: |"? Tolerates missing leading/trailing pipes.
function isTableSeparator(line) {
  if (!line || !line.includes("|")) return false;
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((c) => /^\s*:?-{2,}:?\s*$/.test(c));
}

function longformMarkdown(md) {
  if (!md) return "";
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let buf = [];
  let inQuote = false;
  const flush = () => {
    if (!buf.length) return;
    const text = buf.join(" ").trim();
    if (text) {
      out.push(inQuote
        ? `<blockquote>${longformInline(text)}</blockquote>`
        : `<p>${longformInline(text)}</p>`);
    }
    buf = []; inQuote = false;
  };
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { flush(); continue; }
    const h2 = line.match(/^##\s+(.*)$/);
    if (h2) { flush(); out.push(`<h3>${longformInline(h2[1])}</h3>`); continue; }
    // Markdown table: any sequence of ≥2 consecutive lines starting with "|"
    // OR a separator line followed by "|" body rows. Tolerates missing
    // leading pipes, mismatched column counts, and absent header rows.
    const startsTable = line.trim().startsWith("|") || isTableSeparator(line);
    const nextLooksTabular = (lines[i + 1] || "").trim().startsWith("|")
      || isTableSeparator(lines[i + 1] || "");
    if (startsTable && nextLooksTabular) {
      flush();
      // Collect all consecutive table-shaped lines as table content.
      const rawRows = [];
      let j = i;
      while (j < lines.length && (lines[j].trim().startsWith("|") || isTableSeparator(lines[j]))) {
        rawRows.push(lines[j]);
        j++;
      }
      // Find a separator row (every cell matches /^:?-{2,}:?$/) if present.
      let sepIdx = rawRows.findIndex(isTableSeparator);
      const headerRows = sepIdx > 0 ? rawRows.slice(0, sepIdx) : (sepIdx === 0 ? [] : [rawRows[0]]);
      const bodyRows = sepIdx >= 0 ? rawRows.slice(sepIdx + 1) : rawRows.slice(1);
      const parsedHeader = headerRows.map(splitTableRow);
      const parsedBody = bodyRows.map(splitTableRow);
      // Per-column numeric detection: a column is "numeric" when at least
      // 60% of its non-empty body cells match a money / metric / number /
      // date-year pattern. Numeric columns right-align via .lf-num.
      const colCount = Math.max(
        ...parsedHeader.map(r => r.length),
        ...parsedBody.map(r => r.length),
        0
      );
      const numericRe = /^[\s+\-−]*[$£€¥]?\d[\d,.\s]*(?:[KMBT%×x]|million|billion|trillion|bn|mn|bps|pct|percent|points?)?[\s\-—]*$/i;
      const isNumCol = [];
      for (let c = 0; c < colCount; c++) {
        let total = 0, hit = 0;
        for (const row of parsedBody) {
          const cell = (row[c] || "").trim();
          if (!cell || cell === "—" || cell === "-") continue;
          total++;
          if (numericRe.test(cell)) hit++;
        }
        isNumCol.push(total > 0 && hit / total >= 0.6);
      }
      const cellClass = (c) => isNumCol[c] ? ' class="lf-num"' : "";
      const thead = parsedHeader.length
        ? `<thead>${parsedHeader.map(r => `<tr>${r.map((c, i) => `<th${cellClass(i)}>${longformInline(c)}</th>`).join("")}</tr>`).join("")}</thead>`
        : "";
      const tbody = parsedBody.length
        ? `<tbody>${parsedBody.map(r => `<tr>${r.map((c, i) => `<td${cellClass(i)}>${longformInline(c)}</td>`).join("")}</tr>`).join("")}</tbody>`
        : "";
      // Skip if nothing parsed (e.g. a stray "|" line); otherwise emit.
      if (thead || tbody) {
        out.push(`<div class="lf-table-wrap"><table class="lf-table">${thead}${tbody}</table></div>`);
        i = j - 1;
        continue;
      }
    }
    const bq = line.match(/^>\s?(.*)$/);
    if (bq) {
      if (!inQuote && buf.length) flush();
      inQuote = true;
      buf.push(bq[1]);
      continue;
    }
    if (inQuote) flush();
    buf.push(line.trim());
  }
  flush();
  return out.join("\n");
}

function setLongformMode(on, { pushHistory = true } = {}) {
  state.longformMode = on;
  if (els.longformToggle) {
    els.longformToggle.setAttribute("aria-pressed", String(on));
    // Longform is an on/off toggle — label stays "Longform" both ways.
    // Pressed/black styling reflects the active state via aria-pressed.
    els.longformToggle.textContent = "Longform";
  }
  els.cards.classList.toggle("hidden", on);
  document.querySelector(".feed-header").classList.toggle("hidden", on);
  els.feedFooter.classList.toggle("hidden", on);
  // When leaving longform, hide both report and overview panes; when entering,
  // loadLongformReports() decides which to show.
  if (!on) {
    if (els.longformView) els.longformView.classList.add("hidden");
    if (els.longformOverview) els.longformOverview.classList.add("hidden");
  }
  document.body.classList.toggle("longform-mode", on);
  if (els.articleDetail) {
    els.articleDetail.classList.add("hidden");
    els.articleDetail.innerHTML = "";
  }
  if (on) {
    loadLongformReports();
  } else if (state.longformObserver) {
    state.longformObserver.disconnect();
    state.longformObserver = null;
  }
  if (pushHistory) {
    history.pushState({ view: on ? "longform" : "feed" }, "",
      on ? "#longform" : "#");
  }
}

async function loadLongformReports() {
  if (!els.longformMain || !els.longformToc) return;
  // Fetch the report index in the background so the picker is ready when a
  // user opens a specific report, but the FIRST screen is the overview.
  try {
    const res = await apiFetch("/api/longform");
    if (res.ok) state.longformReportsList = (await res.json()).reports || [];
  } catch {}
  if (state.longformReportId) {
    await openLongformReport(state.longformReportId);
  } else {
    await showLongformOverview();
  }
}

async function showLongformOverview() {
  state.longformReportId = null;
  if (els.longformView) {
    els.longformView.classList.remove("show-corrections");
    els.longformView.classList.add("hidden");
  }
  if (els.longformOverview) els.longformOverview.classList.remove("hidden");
  if (els.lfOverviewBody && !state.longformOverviewData) {
    els.lfOverviewBody.innerHTML = `<div class="empty"><span class="spinner"></span> Loading…</div>`;
  }
  if (!state.longformOverviewData) {
    try {
      const res = await apiFetch("/api/longform-overview");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      state.longformOverviewData = d.reports || [];
    } catch (e) {
      if (els.lfOverviewBody) {
        els.lfOverviewBody.innerHTML = `<pre class="report-error">Failed to load overview: ${escapeHtml(String(e.message || e))}</pre>`;
      }
      return;
    }
  }
  renderLongformOverview();
}

// Tier order for the cap dimension — Micro → Giga (small to large).
const LF_CAP_TIER_ORDER = ["Micro", "Small", "Mid", "Large", "Mega", "Giga"];
// Single-word sectors, alphabetical.
const LF_SECTOR_ORDER = ["AI", "Cloud", "Energy", "Health",
                          "Materials", "Power", "Semis", "Space"];

function renderLongformOverview() {
  if (!els.lfOverviewBody) return;
  const all = state.longformOverviewData || [];
  if (els.lfOverviewCount) {
    els.lfOverviewCount.textContent = `${all.length} dossier${all.length === 1 ? "" : "s"}`;
  }
  const mode = (els.lfOverviewSort && els.lfOverviewSort.value) || "sector";
  // Build groups: an array of [groupLabel, items[]] preserving display order.
  let groups = [];
  if (mode === "az") {
    const sorted = all.slice().sort((a, b) => (a.name || a.ticker || "").localeCompare(b.name || b.ticker || ""));
    groups = [["All dossiers", sorted]];
  } else if (mode === "cap") {
    const byTier = new Map();
    for (const r of all) {
      const t = r.cap_tier || "Unknown";
      if (!byTier.has(t)) byTier.set(t, []);
      byTier.get(t).push(r);
    }
    const order = LF_CAP_TIER_ORDER.concat(["Unknown"]);
    groups = order.filter(t => byTier.has(t)).map(t => {
      const arr = byTier.get(t).slice().sort((a, b) => (b.market_cap_usd || 0) - (a.market_cap_usd || 0));
      return [t === "Unknown" ? "Cap unknown" : `${t} cap`, arr];
    });
  } else if (mode === "etf") {
    const byEtf = new Map();
    const offEtf = [];
    for (const r of all) {
      const etfs = r.etfs || [];
      if (!etfs.length) { offEtf.push(r); continue; }
      // Multi-membership: include the dossier under every ETF it sits in.
      for (const e of etfs) {
        if (!byEtf.has(e)) byEtf.set(e, []);
        byEtf.get(e).push(r);
      }
    }
    groups = [...byEtf.entries()]
      .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
      .map(([etf, arr]) => [etf, arr.slice().sort((a, b) => (b.market_cap_usd || 0) - (a.market_cap_usd || 0))]);
    if (offEtf.length) groups.push(["Off-ETF", offEtf.slice().sort((a, b) => (a.name || "").localeCompare(b.name || ""))]);
  } else if (mode === "tag") {
    // group by industry tag; rubrics sorted alphabetically.
    const byTag = new Map();
    for (const r of all) {
      const t = r.tag || "Untagged";
      if (!byTag.has(t)) byTag.set(t, []);
      byTag.get(t).push(r);
    }
    groups = [...byTag.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([t, arr]) => [t, arr.slice().sort((a, b) => (b.market_cap_usd || 0) - (a.market_cap_usd || 0))]);
  } else {
    // sector — group by single sector; dossier shows once.
    const bySec = new Map();
    for (const r of all) {
      const s = r.sector || "Untagged";
      if (!bySec.has(s)) bySec.set(s, []);
      bySec.get(s).push(r);
    }
    const order = LF_SECTOR_ORDER.concat(["Untagged"]);
    groups = order.filter(s => bySec.has(s)).map(s => {
      const arr = bySec.get(s).slice().sort((a, b) => (b.market_cap_usd || 0) - (a.market_cap_usd || 0));
      return [s, arr];
    });
  }
  // Each `.lf-ov-rubric` is CSS-sticky just below the controls bar; the next
  // section's rubric naturally pushes the previous one out as you scroll. No
  // JS observer needed.
  els.lfOverviewBody.innerHTML = groups.map(([label, arr]) => `
    <section class="lf-ov-group">
      <h2 class="lf-ov-rubric">${escapeHtml(label)} <span class="lf-ov-rubric-count">${arr.length}</span></h2>
      <div class="lf-ov-grid">
        ${arr.map(renderLongformCard).join("")}
      </div>
    </section>
  `).join("");
}

function formatCapUSD(n) {
  if (n == null) return "";
  if (n >= 1e12) return `$${(n/1e12).toFixed(2)}T`;
  if (n >= 1e9)  return `$${(n/1e9).toFixed(1)}B`;
  if (n >= 1e6)  return `$${(n/1e6).toFixed(0)}M`;
  return `$${Math.round(n)}`;
}

// Stable Baldessari-style colour pick per slug — same dossier always gets the
// same tone across reloads. Matches the news-card palette tokens.
function lfCardTone(slug) {
  const palette = ["k", "r", "b", "y", "g"];
  let h = 0;
  for (let i = 0; i < slug.length; i++) h = (h * 31 + slug.charCodeAt(i)) >>> 0;
  return palette[h % palette.length];
}

function renderLongformCard(r) {
  // Macro line packs identity: legal name, $TICKER, market cap, primary sector,
  // cap tier, country. Lives inside the coloured key-art block.
  const ticker = r.ticker || r.slug.toUpperCase();
  const cap = formatCapUSD(r.market_cap_usd);
  const sector = r.sector || "";
  const tag = r.tag || "";
  const capTier = r.cap_tier ? `${r.cap_tier} Cap` : "";
  const country = (r.country || "").toUpperCase();
  // Top of the macro: full legal name on line 1, $TICKER on line 2 — both
  // big and emphatic. Small meta strip at the bottom.
  const nameLine = escapeHtml(r.name || r.slug);
  const tickerLine = `$${escapeHtml(ticker)}`;
  const metaParts = [
    cap ? escapeHtml(cap) : "",
    escapeHtml(sector),
    escapeHtml(tag),
    escapeHtml(capTier),
    escapeHtml(country),
  ].filter(Boolean);
  const metaLine = metaParts.join(" · ");
  // ETF strip — sits ABOVE the cap/country line in the macro. Only shown
  // when the company is a constituent of at least one tracked ETF.
  const etfList = (r.etfs || []).map(escapeHtml).join(" · ");
  const etfLine = etfList
    ? `<span class="lf-ov-card-macro-etfs">${etfList}</span>`
    : "";
  // Below the key-art: headline = short company name; lede = abstract-derived.
  const headline = escapeHtml(r.display_name || r.name || r.slug);
  const lede = escapeHtml(r.subtitle || "");
  const tone = lfCardTone(r.slug);
  return `
    <article class="lf-ov-card" data-slug="${escapeHtml(r.slug)}">
      <div class="lf-ov-card-macro" data-tone="${tone}">
        <div class="lf-ov-card-macro-top">
          <div class="lf-ov-card-macro-name">${nameLine}</div>
          <div class="lf-ov-card-macro-ticker">${tickerLine}</div>
        </div>
        <div class="lf-ov-card-macro-bottom">
          ${etfLine}
          <span class="lf-ov-card-macro-meta">${metaLine}</span>
        </div>
      </div>
      <h3 class="lf-ov-card-headline">${headline}</h3>
      <p class="lf-ov-card-lede">${lede}</p>
    </article>`;
}

async function openLongformReport(id) {
  if (!els.longformMain || !els.longformToc) return;
  state.longformReportId = id;
  // Hide overview, show single-report layout.
  if (els.longformOverview) els.longformOverview.classList.add("hidden");
  if (els.longformView) els.longformView.classList.remove("hidden");
  els.longformMain.innerHTML = `<div class="empty"><span class="spinner"></span> Loading ${escapeHtml(id)}…</div>`;
  // Kick off the entity index (companies + tickers) so chapter rendering
  // can colour-link mentions to dossiers / the bookmark index.
  ensureLongformEntityIndex();
  // Also kick off the per-dossier scope (people/places/glossary/concepts/
  // phrases/key-sentences/socials) — once it lands, the renderer wraps
  // confirmed entities and the rail picks up social icons.
  const _slug = id.replace(/^companies\//, "");
  ensureDossierEntityScope(_slug).then(() => {
    // Re-render the report so the new scope is applied to all chapters
    // (entity wraps + phrase tints + key-sentence highlighter).
    if (state.longformReport === report) renderLongformReport(report);
  });
  let report;
  try {
    const res = await apiFetch(`/api/longform/${encodeURIComponent(id)}`);
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`HTTP ${res.status}: ${body.slice(0, 200)}`);
    }
    report = await res.json();
  } catch (e) {
    els.longformMain.innerHTML = `<pre class="report-error">Failed to load ${escapeHtml(id)}: ${escapeHtml(String(e.message || e))}</pre>`;
    return;
  }
  state.longformReport = report;
  renderLongformReport(report);
  // Kick off a live refresh once the report is rendered so the Timeline
  // tab reflects today's prices instead of frozen assemble-time numbers.
  refreshLongformLive(id);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!state.longformMode || !state.longformReportId) return;
  refreshLongformLive(state.longformReportId);
});

// Builds the coloured ticker macro that pins to the top of the report's
// TOC pane. Mirrors the overview card's macro block (Name / $TICKER / meta
// strip) but with a "← Back" hint indicating the macro itself is the
// back gesture.
function renderLongformReportMacro(ovRow, meta, slug) {
  // Pull display fields. Prefer the overview row (carries sector/tag/etc.);
  // fall back to the report meta when the overview hasn't loaded yet.
  const name = (ovRow && ovRow.name) || meta.name || slug;
  const ticker = (ovRow && ovRow.ticker) || meta.ticker || slug.toUpperCase();
  const cap = ovRow ? formatCapUSD(ovRow.market_cap_usd) : "";
  const sector = ovRow ? ovRow.sector : "";
  const tag = ovRow ? ovRow.tag : "";
  const capTier = ovRow && ovRow.cap_tier ? `${ovRow.cap_tier} Cap` : "";
  const country = ovRow && ovRow.country ? ovRow.country.toUpperCase() : "";
  const metaParts = [
    cap ? escapeHtml(cap) : "",
    escapeHtml(sector),
    escapeHtml(tag),
    escapeHtml(capTier),
    escapeHtml(country),
  ].filter(Boolean);
  const tone = lfCardTone(slug);
  return `
    <button type="button" class="lf-toc-macro" data-tone="${tone}"
            aria-label="Back to all dossiers">
      <span class="lf-toc-macro-back">← All dossiers</span>
      <span class="lf-toc-macro-name">${escapeHtml(name)}</span>
      <span class="lf-toc-macro-ticker">$${escapeHtml(ticker)}</span>
      ${metaParts.length ? `<span class="lf-toc-macro-meta">${metaParts.join(" · ")}</span>` : ""}
    </button>`;
}

function renderLongformReport(report) {
  const meta = report.meta || {};
  const chapters = report.chapters || [];

  // Count fact-check redline spans across chapters (~~deletion~~ ==insertion==).
  // Each correction is one ~~..~~ pair; this drives the "Show corrections" toggle,
  // which is only offered when the dossier actually carries audited corrections.
  const correctionCount = chapters.reduce(
    (n, c) => n + ((String(c.body || "").match(/~~[^~]+~~/g) || []).length), 0);
  const toggleBarHtml = `
      <button class="lf-rail-toggle" id="lfColorToggle" aria-pressed="true"
              title="Colour-code dates, money, metrics, companies, names, etc.">
        Color
      </button>
      ${correctionCount > 0 ? `
        <button class="lf-rail-toggle" id="lfCorrectionsToggle" aria-pressed="false"
                title="Show the fact-check changes — removed text struck through, added text highlighted">
          Changes <span class="lf-corrections-count">${correctionCount}</span>
        </button>` : ""}`;

  // Replace the in-report TOC chapter list with the dossier's coloured
  // ticker macro, pinned to the top. Clicking the macro returns to the
  // overview (the same gesture as the "← All dossiers" back button).
  const slug = state.longformReportId.replace(/^companies\//, "");
  const ovRow = (state.longformOverviewData || []).find((r) => r.slug === slug);
  // If the user landed straight on this report URL we may not have the
  // overview data yet — fetch it in the background and re-render once it
  // arrives so the macro can populate.
  if (!ovRow) {
    apiFetch("/api/longform-overview").then((r) => r.json()).then((d) => {
      state.longformOverviewData = d.reports || [];
      if (state.longformReport === report) renderLongformReport(report);
    }).catch(() => {});
  }
  const macroHtml = renderLongformReportMacro(ovRow, meta, slug);

  els.longformToc.innerHTML = `
    ${macroHtml}
    <h2 class="lf-toc-heading">Contents</h2>
    <ol class="lf-toc-list">
      ${chapters.map((c) => `
        <li data-slug="${escapeHtml(c.slug)}">
          <a href="#lf-${escapeHtml(c.slug)}">${escapeHtml(c.title || c.slug)}</a>
        </li>
      `).join("")}
    </ol>
    <div class="lf-toc-meta">
      ${meta.snapshot_date ? `Snapshot ${escapeHtml(meta.snapshot_date)}<br>` : ""}
      ${meta.word_count ? `${meta.word_count.toLocaleString()} words<br>` : ""}
      ${meta.n_sources ? `<a href="#lf-sources" class="lf-toc-meta-link">${meta.n_sources} sources</a>` : ""}
      <span class="lf-toc-socials" id="lfTocSocials"></span>
    </div>
    <div class="lf-toc-toolbar">
      ${toggleBarHtml}
    </div>`;

  // The macro is the back gesture. Click → return to longform overview.
  const macroEl = els.longformToc.querySelector(".lf-toc-macro");
  if (macroEl) {
    macroEl.addEventListener("click", () => {
      showLongformOverview();
      history.pushState({ view: "longform" }, "", "#longform");
    });
  }

  // If the dossier scope (with socials) is already loaded, paint icons now.
  // Otherwise renderSocialIcons fires later when ensureDossierEntityScope
  // resolves. Either way the rail picks them up.
  if (state.lfDossierScope && state.lfDossierScope.socials) {
    renderSocialIcons(state.lfDossierScope.socials);
  }

  // Defaults per dossier load: Color ON, Changes OFF.
  if (els.longformView) {
    els.longformView.classList.remove("show-corrections");
    els.longformView.classList.remove("colors-off");
  }
  const colorBtn = document.getElementById("lfColorToggle");
  if (colorBtn && els.longformView) {
    colorBtn.addEventListener("click", () => {
      const off = els.longformView.classList.toggle("colors-off");
      colorBtn.setAttribute("aria-pressed", String(!off));
    });
  }
  const ctBtn = document.getElementById("lfCorrectionsToggle");
  if (ctBtn && els.longformView) {
    ctBtn.addEventListener("click", () => {
      const on = els.longformView.classList.toggle("show-corrections");
      ctBtn.setAttribute("aria-pressed", String(on));
    });
  }

  // Clear the legacy mobile picker — the macro is the navigation now.
  if (els.longformMobileBarPicker) els.longformMobileBarPicker.innerHTML = "";
  if (els.longformView) els.longformView.classList.remove("lf-has-mobilepicker");

  const abstract = report.abstract
    ? `<section class="lf-abstract">${splitAbstractParagraphs(report.abstract).map(p => `<p>${longformInline(p)}</p>`).join("")}</section>`
    : "";

  let chaptersHtml = "";
  if (!chapters.length) {
    chaptersHtml = `<div class="empty">
      No chapters generated yet. Missing:
      ${(meta.missing_chapters || []).map((s) => `<code>${escapeHtml(s)}</code>`).join(", ") || "all"}.
      <br>Run <code>python3 longform/research/runner.py --all</code> then
      <code>python3 longform/compose/assemble.py</code>.
    </div>`;
  } else {
    chaptersHtml = chapters.map((c, i) => `
      <section class="lf-chapter" id="lf-${escapeHtml(c.slug)}">
        <p class="lf-chapter-num">${String(i + 1).padStart(2, "0")}</p>
        <h2 class="lf-chapter-title">${renderChapterTitle(c.title || c.slug)}</h2>
        ${c.lede ? `<p class="lf-chapter-lede">${longformInline(c.lede)}</p>` : ""}
        <div class="lf-chapter-body article-body">${longformMarkdown(c.body || "")}</div>
        ${renderLongformChapterMeta(c)}
      </section>`).join("");
  }

  const bibHtml = (report.bibliography || []).length
    ? `<section class="lf-bibliography" id="lf-sources">
        <h2>Sources</h2>
        <ol class="lf-bib-list">
          ${(report.bibliography || []).map((s) => `
            <li>
              <a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.title || s.url)}</a>
              ${s.note ? `<span class="lf-bib-note">${escapeHtml(s.note)}</span>` : ""}
              ${s.first_used_in ? `<span class="lf-bib-first-in">→ ${escapeHtml(s.first_used_in)}</span>` : ""}
            </li>
          `).join("")}
        </ol>
      </section>`
    : "";

  const companiesHtml = renderLongformCompanies(report);
  const glossaryHtml = renderLongformGlossary(report);
  const timelineHtml = renderLongformTimeline(report);

  // Synthetic ToC entries for each appendix section that actually rendered.
  const appendixToc = [
    timelineHtml ? { slug: "lf-timeline", title: "Timeline" } : null,
    companiesHtml ? { slug: "lf-companies", title: "Companies" } : null,
    glossaryHtml ? { slug: "lf-glossary", title: "Glossary" } : null,
    bibHtml ? { slug: "lf-sources", title: "Sources" } : null,
  ].filter(Boolean);

  if (appendixToc.length) {
    const list = els.longformToc.querySelector(".lf-toc-list");
    if (list) {
      list.insertAdjacentHTML("beforeend", appendixToc.map((c) => `
        <li class="lf-toc-appendix" data-slug="${escapeHtml(c.slug)}">
          <a href="#${escapeHtml(c.slug)}">${escapeHtml(c.title)}</a>
        </li>
      `).join(""));
    }
  }

  els.longformMain.innerHTML = `
    <article class="article-block lf-article">
      <header class="lf-header">
        ${renderLongformTitle(meta.title || state.longformReportId, meta.subtitle)}
        ${meta.byline ? `<p class="lf-byline">${escapeHtml(meta.byline)}</p>` : ""}
      </header>
      ${abstract}
      ${chaptersHtml}
      ${timelineHtml}
      ${companiesHtml}
      ${glossaryHtml}
      ${bibHtml}
    </article>`;

  applyArticleFontPrefs(els.longformMain.querySelector(".article-block"));
  // Scroll-spy on chapters + appendix sections (plain h2 anchors, separate ids).
  const scrollSpyTargets = chapters.map((c) => `lf-${c.slug}`)
    .concat(appendixToc.map((c) => c.slug));
  setupLongformScrollSpy(scrollSpyTargets, /*idPrefixed=*/true);
  hydrateLongformOptionTracks(report);
}

// ---- Companies appendix ----

function renderLongformCompanyCard(entry) {
  // entry is from report.entities.companies[i] OR .tickers[i] —
  // both shapes carry `module` (cached Bookmark ticker module) and `quotes`
  // (block-quotes from the chapter bodies adjacent to a ticker mention) and
  // `dossier` (xalpha dossier tier/score/mentions).
  const ticker = entry.ticker || (entry.module && entry.module.ticker) || "";
  const name = (entry.module && entry.module.name) || entry.name || ticker;
  const exchange = (entry.module && entry.module.exchange) || "";
  const blurb = (entry.module && entry.module.blurb) || "";
  const recent = (entry.module && entry.module.recent) || "";
  const quotes = entry.quotes || [];
  const sources = (entry.module && entry.module.sources) || [];
  const firstIn = entry.first_in;
  const dossier = entry.dossier;

  const tierBadge = (dossier && dossier.tier)
    ? `<span class="lf-tl-tier lf-tl-tier-${escapeHtml(dossier.tier)}" title="xalpha tier ${escapeHtml(dossier.tier)} — score ${escapeHtml(String(dossier.score || ''))}, ${escapeHtml(String(dossier.total_mentions || 0))} mentions">${escapeHtml(dossier.tier)}</span>`
    : "";
  const tickerLine = ticker
    ? `<span class="lf-co-ticker">${escapeHtml(ticker)}</span>${tierBadge}${exchange ? ` <span class="lf-co-exchange">· ${escapeHtml(exchange)}</span>` : ""}`
    : (exchange ? `<span class="lf-co-exchange">${escapeHtml(exchange)}</span>` : "");

  const quotesHtml = quotes.map((q) => `
    <blockquote class="lf-co-quote">${escapeHtml(q.quote)}
      <cite>— in <a href="#lf-${escapeHtml(q.chapter)}">${escapeHtml(q.chapter)}</a></cite>
    </blockquote>
  `).join("");

  const sourcesHtml = sources.length
    ? `<ul class="lf-co-sources">
        ${sources.slice(0, 4).map((s) => `<li><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.title || s.url)}</a></li>`).join("")}
      </ul>`
    : "";

  return `<article class="lf-co-card" id="lf-co-${escapeHtml((ticker || name).toLowerCase())}">
    <header class="lf-co-head">
      <h3 class="lf-co-name">${escapeHtml(name)}</h3>
      ${tickerLine ? `<div class="lf-co-meta">${tickerLine}</div>` : ""}
    </header>
    ${blurb ? `<p class="lf-co-blurb">${escapeHtml(blurb)}</p>` : ""}
    ${recent ? `<p class="lf-co-recent"><strong>Recent:</strong> ${escapeHtml(recent)}</p>` : ""}
    ${quotesHtml}
    ${firstIn ? `<p class="lf-co-firstin">First mentioned in <a href="#lf-${escapeHtml(firstIn)}">${escapeHtml(firstIn)}</a></p>` : ""}
    ${sourcesHtml}
  </article>`;
}

function renderLongformCompanies(report) {
  const ents = (report.entities || {});
  // Prefer companies[] (richer — name + ticker + module), fall back to tickers[]
  // for any ticker without a matching company entry.
  const companyKeys = new Set();
  const cards = [];
  for (const c of (ents.companies || [])) {
    if (!c.module && !(c.quotes && c.quotes.length)) continue;
    cards.push(renderLongformCompanyCard(c));
    if (c.ticker) companyKeys.add(c.ticker.toUpperCase());
  }
  for (const t of (ents.tickers || [])) {
    if (!t.module) continue;
    if (companyKeys.has(t.ticker.toUpperCase())) continue;
    cards.push(renderLongformCompanyCard(t));
    companyKeys.add(t.ticker.toUpperCase());
  }
  if (!cards.length) return "";
  return `<section class="lf-companies" id="lf-companies">
    <h2>Companies</h2>
    <p class="lf-section-note">Encyclopedic entries on every public company referenced in the report, with adjacent quotes from ${escapeHtml(report.meta?.display_name || "the subject")} and links to primary sources.</p>
    <div class="lf-co-grid">${cards.join("")}</div>
  </section>`;
}

// ---- Glossary appendix ----

function renderLongformGlossary(report) {
  const concepts = ((report.entities || {}).concepts || []);
  if (!concepts.length) return "";
  return `<section class="lf-glossary" id="lf-glossary">
    <h2>Glossary</h2>
    <dl class="lf-glossary-list">
      ${concepts.map((c) => `
        <dt id="lf-glossary-${escapeHtml(c.key || "")}">${escapeHtml(c.term || c.key || "")}</dt>
        <dd>${escapeHtml(c.definition || "")}
          ${c.first_in ? `<span class="lf-glossary-firstin">— in <a href="#lf-${escapeHtml(c.first_in)}">${escapeHtml(c.first_in)}</a></span>` : ""}
        </dd>
      `).join("")}
    </dl>
  </section>`;
}

// ---- Timeline appendix ----

// ---- Per-company timeline ----
//
// Replaces the old flat chronological list. Each ticker gets a card with:
// - Header (ticker + name from entities lookup, prediction count, status pills)
// - Optional sparkline (if entry/peak/trough/current prices populated)
// - Timeline rows (one per prediction): target line, progress bar, status,
//   current price, link to source tweet
//
// The data shape this consumes is `report.timeline` produced by
// discover/timeline_progress.py (schema_version: 2). Falls back gracefully
// to v1 (flat predictions, no enrichment) so the UI works before the
// progress pipeline runs.

function _statusPill(status) {
  if (!status) return "";
  // Codex's status enum: hit | in_progress | missing_cache | qualitative
  // Plus legacy: met | missed | on_track | behind | pending | expired_near
  const labels = {
    hit: "met",
    in_progress: "in progress",
    miss: "missed",
    missing_cache: "no price data",
    qualitative: "catalyst",
    currency_mismatch: "currency mismatch",
    // legacy fallthrough
    met: "met", missed: "missed", on_track: "on track",
    behind: "behind", pending: "pending", expired_near: "expiring",
    ahead: "ahead", peaked_met: "met (peaked)",
  };
  return `<span class="lf-tl-status lf-tl-status-${escapeHtml(status)}">${escapeHtml(labels[status] || status)}</span>`;
}

function _formatPrice(value, currency) {
  if (value == null || !isFinite(value)) return "—";
  const c = currency || "USD";
  const symbols = { USD: "$", EUR: "€", GBP: "£", SEK: "kr", JPY: "¥", TWD: "NT$" };
  const prefix = symbols[c] || "";
  const suffix = (c === "SEK") ? " kr" : "";
  if (prefix === "kr") {
    return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
  }
  return `${prefix}${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

function _progressBar(p) {
  // Direction-aware progress bar. Codex emits `progress_pct_current` — the
  // % of distance from entry to threshold the price has covered. >=100 means
  // hit. Negative means moved against the trade.
  const cur = (p.progress_pct_current != null) ? p.progress_pct_current : null;
  if (cur == null) return "";
  const w = Math.max(0, Math.min(100, cur));
  const overshoot = cur > 100 ? Math.min(50, cur - 100) : 0;  // visual cap on overshoot bar
  const status = p.status || "missing_cache";
  return `<div class="lf-tl-progress lf-tl-progress-${escapeHtml(status)}">
    <div class="lf-tl-progress-track">
      <div class="lf-tl-progress-fill" style="width:${w}%"></div>
      ${overshoot ? `<div class="lf-tl-progress-overshoot" style="width:${overshoot}%; left:${w}%"></div>` : ""}
    </div>
    <div class="lf-tl-progress-num">${cur >= 0 ? "+" : ""}${cur.toFixed(0)}%</div>
  </div>`;
}

function _renderSparkline(prices) {
  // prices: [{date, close}] — render an 80×24 SVG with a single polyline.
  if (!prices || prices.length < 2) return "";
  const lo = Math.min(...prices.map(p => p.close));
  const hi = Math.max(...prices.map(p => p.close));
  if (hi === lo) return "";
  const pts = prices.map((p, i) => {
    const x = (i / (prices.length - 1)) * 78 + 1;
    const y = 23 - ((p.close - lo) / (hi - lo)) * 22;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<svg class="lf-tl-spark" width="80" height="24" viewBox="0 0 80 24" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.2" />
  </svg>`;
}

function _isOptionTrackCandidate(p) {
  const basis = p && p.target && p.target.basis;
  const text = `${p?.prediction || ""} ${p?.target?.raw || ""}`.toLowerCase();
  return basis === "option"
    || (basis === "derivative" && /\b(option|options|leap|leaps|iv|implied volatility)\b/.test(text));
}

function _renderOptionTrackShell(p) {
  if (!_isOptionTrackCandidate(p) || !p.prediction_id) return "";
  return `<div class="lf-option-track" data-option-pid="${escapeHtml(p.prediction_id)}">
    <div class="lf-option-track-loading">Loading option track...</div>
  </div>`;
}

function _num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function _fmtPct(v) {
  const n = _num(v);
  return n == null ? "n/a" : `${(n * 100).toFixed(1)}%`;
}

function _fmtMoney(v) {
  const n = _num(v);
  return n == null ? "n/a" : `$${n.toFixed(2)}`;
}

function _optionSeries(candles, key, minDate) {
  return (candles || [])
    .map((c) => {
      const value = _num(c[key]);
      const date = c.date || "";
      const t = Date.parse(`${date}T00:00:00Z`);
      if (value == null || !Number.isFinite(t) || (minDate && date < minDate)) return null;
      return { date, t, value };
    })
    .filter(Boolean)
    .sort((a, b) => a.t - b.t);
}

function _pathForSeries(series, xScale, yScale) {
  return series.map((p, i) => {
    const x = xScale(p.t).toFixed(1);
    const y = yScale(p.value).toFixed(1);
    return `${i ? "L" : "M"}${x},${y}`;
  }).join(" ");
}

function _renderOptionTrackChart(track) {
  const entryDate = track?.spec?.entry_date || "";
  const spot = _optionSeries(track?.spot?.candles, "close", entryDate);
  const option = _optionSeries(track?.option?.candles, "close", entryDate);
  if (spot.length < 2 || option.length < 2) {
    return `<div class="lf-option-track-empty">Option track data is incomplete.</div>`;
  }

  const w = 520, h = 138;
  const left = 34, right = 42, top = 12, bottom = 24;
  const minT = Math.min(spot[0].t, option[0].t);
  const maxT = Math.max(spot[spot.length - 1].t, option[option.length - 1].t);
  const xScale = (t) => left + ((t - minT) / Math.max(1, maxT - minT)) * (w - left - right);

  const spotVals = spot.map((p) => p.value);
  const optVals = option.map((p) => p.value);
  const entryClose = _num(track?.option?.entry_close);
  const threshold = entryClose == null ? null : entryClose * 2;
  if (threshold != null) optVals.push(threshold);

  function yScaleFor(vals) {
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
      lo = 0; hi = 1;
    }
    if (hi === lo) hi = lo + 1;
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    return (v) => top + (1 - ((v - lo) / (hi - lo))) * (h - top - bottom);
  }

  const ySpot = yScaleFor(spotVals);
  const yOpt = yScaleFor(optVals);
  const spotPath = _pathForSeries(spot, xScale, ySpot);
  const optPath = _pathForSeries(option, xScale, yOpt);
  const thresholdY = threshold == null ? null : yOpt(threshold);
  const firstDate = spot[0].date;
  const lastDate = spot[spot.length - 1].date;
  const entry = track?.thesis?.iv_vs_rv_at_entry || {};
  const today = track?.thesis?.iv_vs_rv_today || {};
  const doubled = !!track?.thesis?.doubled;
  const caption = `IV @entry: ${_fmtPct(entry.iv)} | RV30 @entry: ${_fmtPct(entry.rv_30d)} | IV today: ${_fmtPct(today.iv)} | RV30 today: ${_fmtPct(today.rv_30d)}`;

  return `<div class="lf-option-track-card">
    <div class="lf-option-track-meta">
      <span>${escapeHtml(track?.option?.occ_symbol || track?.spec?.label || "Option")}</span>
      <span>${escapeHtml(_fmtMoney(entryClose))} -> ${escapeHtml(_fmtMoney(track?.option?.current_close))}</span>
      <span class="${doubled ? "is-met" : ""}">${doubled ? "doubled" : "not doubled"}</span>
    </div>
    <svg class="lf-option-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeHtml(track?.spec?.label || "Option track")}">
      <line x1="${left}" y1="${h - bottom}" x2="${w - right}" y2="${h - bottom}" />
      <line x1="${left}" y1="${top}" x2="${left}" y2="${h - bottom}" />
      <line x1="${w - right}" y1="${top}" x2="${w - right}" y2="${h - bottom}" />
      ${thresholdY == null ? "" : `<line class="threshold" x1="${left}" y1="${thresholdY.toFixed(1)}" x2="${w - right}" y2="${thresholdY.toFixed(1)}" />`}
      <path class="spot" d="${spotPath}" />
      <path class="option" d="${optPath}" />
      <text x="${left}" y="${h - 6}">${escapeHtml(firstDate.slice(5))}</text>
      <text x="${w - right}" y="${h - 6}" text-anchor="end">${escapeHtml(lastDate.slice(5))}</text>
      ${thresholdY == null ? "" : `<text class="threshold-label" x="${w - right - 4}" y="${Math.max(11, thresholdY - 4).toFixed(1)}" text-anchor="end">2x option</text>`}
    </svg>
    <div class="lf-option-legend">
      <span><i class="spot"></i>EWY spot</span>
      <span><i class="option"></i>option close</span>
      <span><i class="threshold"></i>2x entry</span>
    </div>
    <div class="lf-option-caption">${escapeHtml(caption)}</div>
  </div>`;
}

async function hydrateLongformOptionTracks(report) {
  const reportId = state.longformReportId || report?.meta?.report_id || report?.timeline?.report_id;
  const predictions = report?.timeline?.predictions || [];
  if (!reportId || !predictions.length) return;
  const candidates = predictions.filter(_isOptionTrackCandidate);
  if (!candidates.length) return;
  state.longformOptionTracks[reportId] = state.longformOptionTracks[reportId] || {};

  for (const p of candidates) {
    const pid = p.prediction_id;
    if (!pid) continue;
    const mount = els.longformMain?.querySelector(`.lf-option-track[data-option-pid="${CSS.escape(pid)}"]`);
    if (!mount) continue;
    const cached = state.longformOptionTracks[reportId][pid];
    if (cached) {
      mount.innerHTML = _renderOptionTrackChart(cached);
      continue;
    }
    try {
      const res = await apiFetch(`/api/longform/${encodeURIComponent(reportId)}/options/${encodeURIComponent(pid)}`);
      if (!res.ok) {
        mount.remove();
        continue;
      }
      const payload = await res.json();
      if (state.longformReportId !== reportId) return;
      state.longformOptionTracks[reportId][pid] = payload;
      mount.innerHTML = _renderOptionTrackChart(payload);
    } catch (e) {
      mount.remove();
    }
  }
}

function _renderTargetLine(p) {
  // Render the parsed target. Codex's schema:
  //   target.basis ∈ {price, percent_return, multiple, market_cap, comparative,
  //                    benchmark_relative, event, option, fundamental, revenue, qualitative}
  //   target.target_price (single)  OR  target.target_price_low/high (range)
  //   target.threshold_price (the less-aggressive bound for ranges)
  //   target.target_date (resolved YYYY-MM-DD or YYYY-Qn)
  //   target.raw (original text)
  const t = p.target;
  if (!t) return `<div class="lf-tl-target-line">${escapeHtml(p.target_value || "")}${p.target_date ? ` <span class="lf-tl-by">by ${escapeHtml(p.target_date)}</span>` : ""}</div>`;
  const raw = t.raw || p.target_value || "";
  const td = t.target_date || p.target_date;
  const basis = t.basis;

  // Non-price bases — show the basis badge instead of a price target line.
  if (["event", "comparative", "qualitative", "fundamental", "revenue"].includes(basis)) {
    return `<div class="lf-tl-target-line">
      ${escapeHtml(raw)}
      <span class="lf-tl-basis">${escapeHtml(basis)}</span>
      ${td ? `<span class="lf-tl-by">by ${escapeHtml(td)}</span>` : ""}
    </div>`;
  }
  if (basis === "market_cap") {
    const mc = t.market_cap;
    const tgtNative = t.market_cap_currency || "USD";
    const fmtNative = mc != null ? `$${(mc / 1e9).toFixed(1)}B mcap` : raw;
    // FX-converted into quote currency (e.g. SEK)? Show both.
    const converted = p.target_market_cap;
    const convertedCcy = p.quote_currency;
    const showFx = p.fx_converted && converted != null && convertedCcy && convertedCcy !== tgtNative;
    const fmtConverted = showFx ? ` ≈ ${(converted / 1e9).toFixed(1)}B ${convertedCcy}` : "";
    const fxNote = showFx && p.fx_rate_note ? `<span class="lf-tl-fx" title="FX rate ${escapeHtml(p.fx_rate_note)}">≈ converted</span>` : "";
    return `<div class="lf-tl-target-line">
      target: <strong>${escapeHtml(fmtNative)}${escapeHtml(fmtConverted)}</strong>
      <span class="lf-tl-basis">market cap</span>
      ${fxNote}
      ${td ? `<span class="lf-tl-by">by ${escapeHtml(td)}</span>` : ""}
      <span class="lf-tl-rawtarget">${escapeHtml(raw)}</span>
    </div>`;
  }
  if (basis === "benchmark_relative") {
    const bm = t.benchmark || "benchmark";
    const pct = t.benchmark_relative_pct != null ? `+${t.benchmark_relative_pct}% vs ${bm}` : raw;
    return `<div class="lf-tl-target-line">
      target: <strong>${escapeHtml(pct)}</strong>
      <span class="lf-tl-basis">vs benchmark</span>
      ${td ? `<span class="lf-tl-by">by ${escapeHtml(td)}</span>` : ""}
    </div>`;
  }
  if (basis === "option") {
    const mult = t.option_multiple || raw;
    return `<div class="lf-tl-target-line">
      target: <strong>${escapeHtml(String(mult))}</strong>
      <span class="lf-tl-basis">option return</span>
      ${td ? `<span class="lf-tl-by">by ${escapeHtml(td)}</span>` : ""}
      <span class="lf-tl-rawtarget">${escapeHtml(raw)}</span>
    </div>`;
  }

  // Price-equivalent bases: price, percent_return, multiple
  let priceLine = "";
  if (t.target_price_low != null && t.target_price_high != null) {
    const lo = _formatPrice(t.target_price_low, p.price_currency);
    const hi = _formatPrice(t.target_price_high, p.price_currency);
    priceLine = (t.target_price_low === t.target_price_high) ? lo : `${lo} – ${hi}`;
  } else if (t.target_price != null) {
    priceLine = _formatPrice(t.target_price, p.price_currency);
  } else if (t.return_pct_low != null && t.return_pct_high != null) {
    priceLine = `${t.return_pct_low}–${t.return_pct_high}%`;
  } else if (t.return_pct != null) {
    priceLine = `${t.return_pct >= 0 ? "+" : ""}${t.return_pct}%`;
  }

  const arrow = (t.direction === "down") ? "↓" : "↑";
  return `<div class="lf-tl-target-line">
    target: <strong>${escapeHtml(arrow + " " + priceLine)}</strong>
    ${td ? `<span class="lf-tl-by">by ${escapeHtml(td)}</span>` : ""}
    <span class="lf-tl-rawtarget">${escapeHtml(raw)}</span>
  </div>`;
}

function _renderPredictionRow(p) {
  const conv = p.conviction ? `<span class="lf-tl-conv lf-tl-conv-${escapeHtml(p.conviction)}">${escapeHtml(p.conviction)}</span>` : "";
  const stance = p.stance ? `<span class="lf-tl-stance">${escapeHtml(p.stance)}</span>` : "";

  const entryLabel = (p.entry_price != null)
    ? `entry ${_formatPrice(p.entry_price, p.price_currency)} on ${escapeHtml(p.date || "")}`
    : `posted ${escapeHtml(p.date || "")}`;
  const currentLabel = (p.current_price != null)
    ? `current ${_formatPrice(p.current_price, p.price_currency)}`
    : "";
  const peakLabel = (p.hit_source === "peak" && p.hit_price != null)
    ? `peaked ${_formatPrice(p.hit_price, p.price_currency)} on ${escapeHtml(p.hit_date || "")}`
    : "";

  return `<li class="lf-tl-row" data-status="${escapeHtml(p.status || "pending")}" data-pid="${escapeHtml(p.prediction_id || "")}" data-currency="${escapeHtml(p.price_currency || "")}">
    <div class="lf-tl-row-head">
      <span class="lf-tl-postdate">${escapeHtml(p.date || "—")}</span>
      ${stance} ${conv}
      ${p.url ? `<a class="lf-tl-postlink" href="${escapeHtml(p.url)}" target="_blank" rel="noopener">post →</a>` : ""}
    </div>
    <div class="lf-tl-prediction">${escapeHtml(p.prediction || "")}</div>
    ${_renderTargetLine(p)}
    <div class="lf-tl-row-foot">
      ${_progressBar(p)}
      ${_statusPill(p.status)}
      <span class="lf-tl-prices">
        <span class="lf-tl-prices-entry">${escapeHtml(entryLabel)}</span>
        <span class="lf-tl-prices-current">${currentLabel ? escapeHtml(currentLabel) : ""}</span>
        ${peakLabel ? `<span>${escapeHtml(peakLabel)}</span>` : ""}
      </span>
    </div>
    ${_renderOptionTrackShell(p)}
  </li>`;
}

function _patchPredictionRow(li, p) {
  if (!li || !p) return;
  const status = p.status || li.dataset.status || "pending";
  li.dataset.status = status;
  // Replace the progress bar + status pill (rebuilt from helpers).
  const foot = li.querySelector(".lf-tl-row-foot");
  if (foot) {
    const oldBar = foot.querySelector(".lf-tl-progress");
    const newBarHtml = _progressBar({
      progress_pct_current: p.progress_pct_current,
      status,
    });
    if (oldBar) {
      const tmp = document.createElement("div");
      tmp.innerHTML = newBarHtml;
      const newBar = tmp.firstElementChild;
      if (newBar) foot.replaceChild(newBar, oldBar);
      else oldBar.remove();
    } else if (newBarHtml) {
      foot.insertAdjacentHTML("afterbegin", newBarHtml);
    }
    const oldPill = foot.querySelector(".lf-tl-status");
    if (oldPill) {
      const tmp = document.createElement("div");
      tmp.innerHTML = _statusPill(status);
      const newPill = tmp.firstElementChild;
      if (newPill) oldPill.replaceWith(newPill);
    }
    const curSpan = foot.querySelector(".lf-tl-prices-current");
    if (curSpan) {
      const ccy = li.dataset.currency || "";
      curSpan.textContent = (p.current_price != null)
        ? `current ${_formatPrice(p.current_price, ccy)}`
        : "";
    }
  }
}

async function refreshLongformLive(reportId) {
  if (!reportId) return;
  let payload;
  try {
    const res = await apiFetch(`/api/longform/${encodeURIComponent(reportId)}/live`);
    if (!res.ok) return;
    payload = await res.json();
  } catch (e) {
    return;
  }
  const preds = (payload && payload.predictions) || {};
  const root = document.querySelector(".lf-timeline-by-ticker");
  if (!root) return;
  for (const [pid, fields] of Object.entries(preds)) {
    if (fields.stale) continue;
    const li = root.querySelector(`.lf-tl-row[data-pid="${CSS.escape(pid)}"]`);
    if (li) _patchPredictionRow(li, fields);
  }
  // Update per-company rollups + filter chip counts.
  const filters = root.querySelector(".lf-tl-filters");
  if (filters && payload.status_counts) {
    const total = Object.values(payload.status_counts).reduce((a, b) => a + b, 0);
    const allBtn = filters.querySelector('[data-status="all"]');
    if (allBtn) allBtn.textContent = `All (${total})`;
    filters.querySelectorAll(".lf-tl-filter[data-status]").forEach((btn) => {
      const s = btn.dataset.status;
      if (s === "all") return;
      const n = payload.status_counts[s] || 0;
      const label = btn.textContent.replace(/\s*\(\d+\)\s*$/, "");
      btn.textContent = `${label} (${n})`;
      btn.classList.toggle("is-empty", n === 0);
    });
  }
  root.querySelectorAll(".lf-tl-co-card").forEach((card) => {
    const counts = {};
    card.querySelectorAll(".lf-tl-row").forEach((row) => {
      const s = row.dataset.status || "missing_cache";
      counts[s] = (counts[s] || 0) + 1;
    });
    const rollup = card.querySelector(".lf-tl-co-rollup");
    if (rollup) rollup.innerHTML = _formatRollup(counts);
  });
}

function _rollupForCompany(predictions) {
  // Codex statuses: hit, in_progress, missing_cache, qualitative.
  // Plus legacy: met, missed, on_track, behind, pending, expired_near.
  const counts = {};
  for (const p of predictions) {
    const s = p.status || "missing_cache";
    counts[s] = (counts[s] || 0) + 1;
  }
  return counts;
}

function _formatRollup(counts) {
  // Display order: hits first, then in_progress, then catalysts, then no-data, then misses.
  const order = ["hit", "met", "in_progress", "on_track", "ahead", "behind", "qualitative", "pending", "missing_cache", "currency_mismatch", "expired_near", "miss", "missed"];
  const labels = {
    hit: "met", met: "met", peaked_met: "peaked",
    in_progress: "in progress", on_track: "on track", ahead: "ahead",
    behind: "behind", pending: "pending",
    qualitative: "catalyst", missing_cache: "no data",
    currency_mismatch: "currency",
    expired_near: "expiring", missed: "missed", miss: "missed",
  };
  return order
    .filter((k) => counts[k])
    .map((k) => `<span class="lf-tl-rollup-pill lf-tl-status-${k}">${counts[k]} ${labels[k] || k}</span>`)
    .join(" ");
}

function _lookupTickerEntity(report, ticker) {
  // Find the matching entities.companies or entities.tickers row for this
  // ticker so we can pull name + exchange + dossier without re-fetching.
  const ents = report.entities || {};
  const upper = (ticker || "").toUpperCase();
  for (const c of (ents.companies || [])) {
    if ((c.ticker || "").toUpperCase() === upper) return c;
  }
  for (const t of (ents.tickers || [])) {
    if ((t.ticker || "").toUpperCase() === upper) return t;
  }
  return null;
}

function renderLongformTimeline(report) {
  const tl = report.timeline;
  if (!tl || !(tl.predictions || []).length) return "";
  const window = tl.window || {};

  // Decide between v1 (flat list, no enrichment) and v2 (per-prediction
  // ticker fan-out + price/progress fields).
  const isV2 = tl.schema_version === 2 || (tl.predictions || []).some((p) => p.ticker || p.target);

  if (!isV2) {
    // Legacy flat-list fallback (kept for the v1 timeline.json shape).
    return _renderLegacyFlatTimeline(report);
  }

  // Group by ticker. Schema v2: each prediction has a single .ticker.
  const groups = new Map();
  for (const p of tl.predictions) {
    const tk = (p.ticker || (p.tickers || [])[0] || "").toUpperCase();
    if (!tk) continue;
    if (!groups.has(tk)) groups.set(tk, []);
    groups.get(tk).push(p);
  }

  // Sort tickers: most predictions first, then most-recent prediction date.
  const sortedTickers = [...groups.keys()].sort((a, b) => {
    const la = groups.get(a).length, lb = groups.get(b).length;
    if (lb !== la) return lb - la;
    const ma = groups.get(a).reduce((acc, p) => Math.max(acc, +new Date(p.date || 0)), 0);
    const mb = groups.get(b).reduce((acc, p) => Math.max(acc, +new Date(p.date || 0)), 0);
    return mb - ma;
  });

  const cards = sortedTickers.map((tk) => {
    const preds = groups.get(tk).sort((a, b) => (a.date || "").localeCompare(b.date || ""));
    const ent = _lookupTickerEntity(report, tk);
    const name = (ent && ent.name) || (ent && ent.module && ent.module.name) || tk;
    const exch = (ent && ent.module && ent.module.exchange) || "";
    const dossier = ent && ent.dossier;
    const tier = dossier && dossier.tier;
    const counts = _rollupForCompany(preds);
    const rows = preds.map(_renderPredictionRow).join("");

    return `<article class="lf-tl-co-card" data-ticker="${escapeHtml(tk)}">
      <header class="lf-tl-co-head">
        <h3 class="lf-tl-co-name">
          <span class="lf-tl-co-ticker">${escapeHtml(tk)}</span>
          ${tier ? `<span class="lf-tl-tier lf-tl-tier-${escapeHtml(tier)}">${escapeHtml(tier)}</span>` : ""}
          <span class="lf-tl-co-fullname">${escapeHtml(name)}</span>
          ${exch ? `<span class="lf-tl-co-exchange">${escapeHtml(exch)}</span>` : ""}
        </h3>
        <div class="lf-tl-co-rollup">${_formatRollup(counts)}</div>
      </header>
      <ol class="lf-tl-rows">${rows}</ol>
    </article>`;
  }).join("");

  // Status filter chip row at the top of the section.
  const allCounts = (tl.status_counts) || _rollupForCompany(tl.predictions);
  const filterStatuses = [
    ["hit", "Met"],
    ["in_progress", "In progress"],
    ["miss", "Missed"],
    ["qualitative", "Catalysts"],
    ["currency_mismatch", "Currency mismatch"],
    ["missing_cache", "No price data"],
    // legacy fallthrough
    ["met", "Met"], ["on_track", "On track"], ["behind", "Behind"],
    ["pending", "Pending"], ["missed", "Missed"],
  ];
  const filterChips = `
    <div class="lf-tl-filters">
      <button type="button" class="lf-tl-filter is-active" data-status="all">All (${tl.predictions.length})</button>
      ${filterStatuses
        .filter(([s, _]) => allCounts[s])
        .map(([s, label]) => `<button type="button" class="lf-tl-filter" data-status="${s}">${label} (${allCounts[s]})</button>`)
        .join("")}
    </div>`;

  return `<section class="lf-timeline lf-timeline-by-ticker" id="lf-timeline">
    <h2>Timeline of dated predictions</h2>
    <p class="lf-section-note">Every dated, falsifiable claim ${window.start ? `from ${escapeHtml(window.start)} → ${escapeHtml(window.end || "today")}` : ""}, grouped by company. Targets are extracted from the post text and resolved to absolute prices where possible. Progress is measured against ${tl.price_as_of ? `prices as of ${escapeHtml(tl.price_as_of)}` : "the current price"} from a frozen stax cache. ${tl.predictions.length} predictions across ${groups.size} tickers.</p>
    ${filterChips}
    <div class="lf-tl-co-grid">${cards}</div>
  </section>`;
}

function _renderLegacyFlatTimeline(report) {
  // Fallback for the v1 timeline.json shape. Used when discover/timeline.py
  // has run but timeline_progress.py has not yet. Same shape as before.
  const tl = report.timeline;
  const window = tl.window || {};
  const sorted = [...tl.predictions].sort((a, b) =>
    (a.target_date || a.date || "").localeCompare(b.target_date || b.date || ""));
  const items = sorted.map((p) => {
    const tickers = (p.tickers || []).join(", ");
    const target = p.target_date || "—";
    const conv = p.conviction ? `<span class="lf-tl-conv lf-tl-conv-${escapeHtml(p.conviction)}">${escapeHtml(p.conviction)}</span>` : "";
    const stance = p.stance ? `<span class="lf-tl-stance">${escapeHtml(p.stance)}</span>` : "";
    return `<li class="lf-tl-item">
      <div class="lf-tl-target">${escapeHtml(target)}</div>
      <div class="lf-tl-body">
        <div class="lf-tl-head">
          <span class="lf-tl-tickers">${escapeHtml(tickers)}</span>
          ${stance} ${conv}
          <span class="lf-tl-posted">posted ${escapeHtml(p.date || "")}${p.url ? ` · <a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">post</a>` : ""}</span>
        </div>
        <div class="lf-tl-prediction">${escapeHtml(p.prediction || "")}</div>
        ${p.target_value ? `<div class="lf-tl-targetval">target: ${escapeHtml(p.target_value)}${p.target_metric ? ` (${escapeHtml(p.target_metric)})` : ""}</div>` : ""}
      </div>
    </li>`;
  }).join("");
  return `<section class="lf-timeline" id="lf-timeline">
    <h2>Timeline of dated predictions</h2>
    <p class="lf-section-note">Pre-progress view (run <code>longform/discover/timeline_progress.py --report-id ${escapeHtml(report.meta?.report_id || "")}</code> to enrich with prices). ${tl.predictions.length} predictions across ${Object.keys(tl.by_ticker || {}).length} tickers.</p>
    <ol class="lf-tl-list">${items}</ol>
  </section>`;
}

// Status-filter chip wiring — toggle visibility of timeline rows by status.
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".lf-tl-filter");
  if (!btn) return;
  const status = btn.dataset.status;
  const root = btn.closest(".lf-timeline-by-ticker");
  if (!root) return;
  root.querySelectorAll(".lf-tl-filter").forEach((b) => b.classList.toggle("is-active", b === btn));
  root.querySelectorAll(".lf-tl-row").forEach((row) => {
    if (status === "all") {
      row.classList.remove("is-hidden");
    } else {
      row.classList.toggle("is-hidden", row.dataset.status !== status);
    }
  });
  // Hide cards whose every row got hidden.
  root.querySelectorAll(".lf-tl-co-card").forEach((card) => {
    const visible = card.querySelectorAll(".lf-tl-row:not(.is-hidden)").length;
    card.classList.toggle("is-hidden", visible === 0);
  });
});

function renderLongformChapterMeta(c) {
  const tickers = (c.tickers || []).map((t) =>
    `<span class="ticker-chip">${escapeHtml(t)}</span>`).join("");
  const concepts = (c.concepts || []).map((k) =>
    `<span class="concept-chip" title="${escapeHtml(k.definition || "")}">${escapeHtml(k.term || k.key || "")}</span>`).join("");
  if (!tickers && !concepts) return "";
  return `<div class="lf-chapter-meta">
    ${tickers ? `<div class="chip-row">${tickers}</div>` : ""}
    ${concepts ? `<div class="chip-row">${concepts}</div>` : ""}
  </div>`;
}

function setupLongformScrollSpy(targets, idPrefixed = false) {
  // `targets`: when idPrefixed is false (legacy), each entry is a chapter
  // slug like "photonics" and the section id is "lf-<slug>". When true,
  // each entry is the full element id ("lf-photonics", "lf-companies", ...)
  // and the toc data-slug is the suffix after "lf-".
  if (state.longformObserver) state.longformObserver.disconnect();
  if (!targets.length) return;

  const sectionIds = idPrefixed ? targets : targets.map((s) => `lf-${s}`);
  const slugs = idPrefixed
    ? targets.map((id) => id.replace(/^lf-/, ""))
    : targets;

  const sections = sectionIds.map((id) => document.getElementById(id)).filter(Boolean);
  const tocItems = new Map(slugs.map((s) =>
    [s, els.longformToc.querySelector(`li[data-slug="${CSS.escape(s)}"]`)
        || els.longformToc.querySelector(`li[data-slug="${CSS.escape("lf-" + s)}"]`)]));
  const visible = new Set();
  state.longformObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const id = e.target.id.replace(/^lf-/, "");
      if (e.isIntersecting) visible.add(id); else visible.delete(id);
    }
    let chosen = null;
    for (const s of slugs) { if (visible.has(s)) { chosen = s; break; } }
    if (!chosen) return;
    for (const [s, li] of tocItems) {
      if (li) li.classList.toggle("is-active", s === chosen);
    }
    updateLongformTocBar(chosen, tocItems);
  }, { rootMargin: "-20% 0px -60% 0px", threshold: 0 });
  sections.forEach((s) => state.longformObserver.observe(s));

  const firstSlug = slugs[0];
  if (firstSlug) updateLongformTocBar(firstSlug, tocItems);
}

function updateLongformTocBar(slug, tocItems) {
  if (!els.longformTocBarTitle) return;
  const li = tocItems.get(slug);
  if (!li) return;
  const a = li.querySelector("a");
  const title = a ? a.textContent.trim() : slug;
  els.longformTocBarTitle.textContent = title;
  if (els.longformTocBarNum) {
    if (li.classList.contains("lf-toc-appendix")) {
      els.longformTocBarNum.textContent = "";
    } else {
      const idx = Array.from(li.parentElement.children)
        .filter((n) => !n.classList.contains("lf-toc-appendix"))
        .indexOf(li);
      els.longformTocBarNum.textContent = idx >= 0
        ? String(idx + 1).padStart(2, "0")
        : "";
    }
  }
}

function setLongformTocOpen(open) {
  if (!els.longformView || !els.longformTocBar) return;
  els.longformView.classList.toggle("lf-toc-open", open);
  els.longformTocBar.setAttribute("aria-expanded", open ? "true" : "false");
  document.body.classList.toggle("lf-toc-open", open);
}

if (els.longformToggle) {
  els.longformToggle.addEventListener("click", () => {
    if (state.longformMode) return;
    if (state.articlesMode) setArticlesMode(false, { pushHistory: false });
    if (state.indexMode) setIndexMode(false, { pushHistory: false });
    setLongformMode(true);
    updateTopbarNavState();
  });
}

// Overview sort selector + card click.
if (els.lfOverviewSort) {
  els.lfOverviewSort.addEventListener("change", () => renderLongformOverview());
}
if (els.lfOverviewBody) {
  els.lfOverviewBody.addEventListener("click", (e) => {
    const card = e.target.closest(".lf-ov-card");
    if (!card) return;
    const slug = card.getAttribute("data-slug");
    if (!slug) return;
    const id = `companies/${slug}`;
    openLongformReport(id);
    history.pushState({ view: "longform", id }, "", `#longform=${encodeURIComponent(id)}`);
  });
}

if (els.longformTocBar) {
  els.longformTocBar.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = els.longformTocBar.getAttribute("aria-expanded") === "true";
    setLongformTocOpen(!open);
  });
}
if (els.longformToc) {
  els.longformToc.addEventListener("click", (e) => {
    if (e.target.closest("a")) setLongformTocOpen(false);
  });
}
document.addEventListener("click", (e) => {
  if (!els.longformView || !els.longformView.classList.contains("lf-toc-open")) return;
  if (e.target.closest(".longform-toc") || e.target.closest(".lf-toc-mobilebar")) return;
  setLongformTocOpen(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && els.longformView && els.longformView.classList.contains("lf-toc-open")) {
    setLongformTocOpen(false);
  }
});
// Index click delegate: tag chips and entity rows. Tag chip toggles a tag
// filter and exits index mode; entity row toggles a tag filter for tweets
// that mention that ticker/company/etc.
if (els.indexView) {
  els.indexView.addEventListener("click", (e) => {
    const tab = e.target.closest(".index-tab[data-tab]");
    if (tab) { renderIndexView(tab.dataset.tab); return; }
    const link = e.target.closest(".index-entry-articles[data-ids]");
    if (link) {
      const firstId = (link.dataset.ids.split(",")[0] || "").trim();
      if (firstId) {
        setIndexMode(false, { pushHistory: false });
        if (!state.articlesMode) setArticlesMode(true);
        // open the article detail for that bookmark
        setTimeout(() => {
          window.location.hash = `#article=${firstId}`;
          window.dispatchEvent(new HashChangeEvent("hashchange"));
        }, 100);
      }
    }
  });
}
// Clicking the wordmark jumps to Articles mode sorted by recent bookmarks.
if (els.mastheadWord) {
  els.mastheadWord.addEventListener("click", () => {
    state.sort = "bookmarked-recent";
    if (els.sort) els.sort.value = "bookmarked-recent";
    if (!state.articlesMode) {
      setArticlesMode(true);
    } else {
      // Already in articles mode — close any open detail and re-render.
      if (els.articleDetail && !els.articleDetail.classList.contains("hidden")) {
        closeArticleDetail();
      }
      renderArticlesView();
    }
    window.scrollTo(0, 0);
  });
}
if (els.articlesView) {
  els.articlesView.addEventListener("click", (e) => {
    const card = e.target.closest(".news-card[data-bookmark-id]");
    if (!card) return;
    const row = state.articles.find(a => String(a.bookmark_id) === card.dataset.bookmarkId)
      || { id: card.dataset.bookmarkId };
    // openInDetail expects a row-shaped object; minimum is .id
    openInDetail({ id: card.dataset.bookmarkId });
  });
}
if (els.articleDetail) {
  els.articleDetail.addEventListener("click", (e) => {
    if (e.target.closest("#articleDetailBack")) {
      e.preventDefault();
      closeArticleDetail();
    }
  });
  // Reuse the existing toolbar / regen / focus / version-pick handlers.
  els.articleDetail.addEventListener("click", handleArticleButtonClick);
  els.articleDetail.addEventListener("click", handleArticleToolbarClick);
}

// Leaving articles mode mid-detail should clean up.
function leaveArticlesModeIfDetailClosed() {
  if (!state.articlesMode && els.articleDetail) {
    els.articleDetail.classList.add("hidden");
    els.articleDetail.innerHTML = "";
  }
}

// Masthead bookmark icon → trigger Playwright pull. Server runs the scrape
// asynchronously; we just spin until it's done, then refresh stats + feed.
if (els.mastheadRefresh) {
  els.mastheadRefresh.addEventListener("click", async () => {
    if (els.mastheadRefresh.classList.contains("pulsing")) return;
    els.mastheadRefresh.classList.add("pulsing");
    els.mastheadRefresh.title = "Pulling latest bookmarks…";
    if (els.mastheadStamp) {
      els.mastheadStamp.textContent = "Refreshing…";
      els.mastheadStamp.classList.add("refreshing");
    }
    try {
      const res = await apiFetch("/api/refresh", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "refresh failed");
      await refreshStats();
      await refreshFeed();
      if (Number((state.articleQueue || {}).remaining_count || 0) > 0) {
        await waitForArticleQueue();
      }
      els.mastheadRefresh.title = `Pulled ${data.added || 0} new bookmarks`;
      setTimeout(() => { els.mastheadRefresh.title = "Pull latest bookmarks"; }, 4000);
    } catch (err) {
      console.error("refresh failed", err);
      els.mastheadRefresh.title = `Refresh failed: ${err.message || err}`;
      if (els.mastheadStamp) els.mastheadStamp.classList.remove("refreshing");
      setTimeout(() => { els.mastheadRefresh.title = "Pull latest bookmarks"; }, 6000);
    } finally {
      els.mastheadRefresh.classList.remove("pulsing");
    }
  });
}

els.sort.addEventListener("change", () => {
  state.sort = els.sort.value;
  if (state.articlesMode) renderArticlesView();
  else refreshFeed();
});

els.mediaFilter.addEventListener("change", () => {
  state.mediaFilter = els.mediaFilter.value;
  refreshFeed();
});

els.tagSearch.addEventListener("input", () => {
  state.tagFilterText = els.tagSearch.value.trim();
  renderTagList();
});

els.tagModeBtn.addEventListener("click", () => {
  state.tagMode = state.tagMode === "any" ? "all" : "any";
  els.tagModeBtn.textContent = state.tagMode;
  if (state.selectedTags.size) refreshFeed();
});

els.multiToggleBtn.addEventListener("click", () => {
  state.multiSelect = !state.multiSelect;
  els.multiToggleBtn.classList.toggle("active", state.multiSelect);
  els.tagModeBtn.classList.toggle("hidden", !state.multiSelect);
  // Leaving multi-select with multiple tags chosen: keep only the first one.
  if (!state.multiSelect && state.selectedTags.size > 1) {
    const first = state.selectedTags.values().next().value;
    state.selectedTags.clear();
    state.selectedTags.add(first);
    renderTagList();
    refreshFeed();
  }
});

els.tagList.addEventListener("click", (e) => {
  const li = e.target.closest("li[data-tag]");
  if (!li) return;
  toggleTag(li.dataset.tag);
});

els.activeFilters.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-clear]");
  if (!btn) return;
  const kind = btn.dataset.clear;
  if (kind === "search") {
    state.query = "";
    els.search.value = "";
  } else if (kind === "tag") {
    state.selectedTags.delete(btn.dataset.tag);
    renderTagList();
  } else if (kind === "media") {
    state.mediaFilter = "";
    els.mediaFilter.value = "";
  }
  refreshFeed();
});

els.cards.addEventListener("click", (e) => {
  if (e.target.closest(".card-original")) return; // let the link navigate
  const chip = e.target.closest(".tag-chip[data-tag]");
  if (chip) {
    e.stopPropagation();
    toggleTag(chip.dataset.tag);
    return;
  }
  // The pilcrow button on each card opens the brief, generating one if needed.
  const action = e.target.closest(".card-article-action[data-bookmark-id]");
  if (action) {
    e.stopPropagation();
    const id = action.dataset.bookmarkId;
    openCard(id);
    if (!state.articleIds.has(String(id))) {
      // No brief yet — kick off generation in whichever slot got rendered.
      const slot = document.querySelector(
        `.article-slot[data-bookmark-id="${CSS.escape(String(id))}"]`);
      if (slot) generateArticle(id, slot);
    }
    return;
  }
  // The ↻ refresh button: open the card and force-regenerate the article.
  // Shift/Alt/Cmd-click opens the focus modal to redirect the brief.
  const refresh = e.target.closest(".card-article-refresh[data-bookmark-id]");
  if (refresh) {
    e.stopPropagation();
    const id = refresh.dataset.bookmarkId;
    openCard(id);
    const run = (focus) => {
      refresh.classList.add("spinning");
      forceRegenerateArticle(id, focus).finally(() => refresh.classList.remove("spinning"));
    };
    if (e.shiftKey || e.altKey || e.metaKey) {
      openFocusModal({ bookmarkId: id, onSubmit: run });
    } else {
      run(null);
    }
    return;
  }
  // If the user has an active text selection (e.g. they just selected OCR
  // text on an image), don't open the modal — the click was the end of a drag.
  const sel = window.getSelection();
  if (sel && sel.toString().length > 0) return;
  const card = e.target.closest(".card[data-id]");
  if (!card) return;
  openCard(card.dataset.id);
});

els.modalClose.addEventListener("click", closeModal);
els.modal.addEventListener("click", (e) => {
  if (e.target === els.modal) closeModal();
});

// Article generate / regenerate buttons (works in modal or pane)
async function handleArticleButtonClick(e) {
  // Source-line "Focus…" — open the focus modal.
  const sourceFocus = e.target.closest("button.article-source-focus[data-bookmark-id]");
  if (sourceFocus) {
    e.preventDefault();
    const id = sourceFocus.dataset.bookmarkId;
    openFocusModal({
      bookmarkId: id,
      onSubmit: async (hint) => {
        sourceFocus.classList.add("spinning");
        try { await forceRegenerateArticle(id, hint); }
        finally { sourceFocus.classList.remove("spinning"); }
      },
    });
    return;
  }
  // Source-line ↻ — plain regenerate (shift-click for focus modal).
  const sourceRefresh = e.target.closest("button.article-source-refresh[data-bookmark-id]");
  if (sourceRefresh) {
    e.preventDefault();
    const id = sourceRefresh.dataset.bookmarkId;
    const run = async (focus) => {
      sourceRefresh.classList.add("spinning");
      try { await forceRegenerateArticle(id, focus); }
      finally { sourceRefresh.classList.remove("spinning"); }
    };
    if (e.shiftKey || e.altKey || e.metaKey) {
      openFocusModal({ bookmarkId: id, onSubmit: run });
    } else {
      run(null);
    }
    return;
  }
  // Article-version pill — load a previous version of the brief.
  const versionBtn = e.target.closest("button.article-version[data-version]");
  if (versionBtn) {
    e.preventDefault();
    const slot = versionBtn.closest(".article-slot");
    if (slot && slot.dataset.displayedVersion === versionBtn.dataset.version) return;
    loadArticleVersion(versionBtn.dataset.bookmarkId, versionBtn.dataset.version);
    return;
  }
  const btn = e.target.closest("button.article-generate, button.article-regenerate");
  if (!btn) return;
  const slot = btn.closest(".article-slot");
  if (!slot) return;
  const bookmarkId = slot.dataset.bookmarkId;
  if (!bookmarkId) return;
  if (btn.classList.contains("article-regenerate")) {
    await forceRegenerateArticle(bookmarkId);
    return;
  }
  await generateArticle(bookmarkId, slot);
}
els.modalBody.addEventListener("click", handleArticleButtonClick);
els.articlePaneBody.addEventListener("click", handleArticleButtonClick);

// Article font toolbar (serif toggle + size up/down). Persists to localStorage.
function handleArticleToolbarClick(e) {
  const btn = e.target.closest('.toolbar-btn[data-action]');
  if (!btn) return;
  e.preventDefault();
  let scale = parseFloat(localStorage.getItem("article-scale") || "1");
  let serif = localStorage.getItem("article-serif") === "1";
  if (btn.dataset.action === "size-up") scale = Math.min(1.6, +(scale + 0.1).toFixed(2));
  else if (btn.dataset.action === "size-down") scale = Math.max(0.8, +(scale - 0.1).toFixed(2));
  else if (btn.dataset.action === "font-toggle") serif = !serif;
  localStorage.setItem("article-scale", String(scale));
  localStorage.setItem("article-serif", serif ? "1" : "0");
  // Apply to every visible article-block (modal + pane + longform).
  document.querySelectorAll(".article-block").forEach(applyArticleFontPrefs);
  // Keep the Serif button's pressed state in sync.
  document.querySelectorAll('.toolbar-btn[data-action="font-toggle"]')
    .forEach((b) => b.setAttribute("aria-pressed", String(serif)));
}
els.modalBody.addEventListener("click", handleArticleToolbarClick);
els.articlePaneBody.addEventListener("click", handleArticleToolbarClick);
// Longform toolbar lives inside the TOC rail (outside .article-block).
if (els.longformToc) els.longformToc.addEventListener("click", handleArticleToolbarClick);

// Draggable sidebar resizer.
(() => {
  const layout = document.querySelector(".layout");
  const resizer = document.getElementById("sidebarResizer");
  if (!layout || !resizer) return;
  const saved = parseFloat(localStorage.getItem("sidebar-w") || "");
  if (saved && saved >= 180) layout.style.setProperty("--sidebar-w", saved + "px");

  let dragging = false;
  resizer.addEventListener("pointerdown", (e) => {
    dragging = true;
    resizer.setPointerCapture(e.pointerId);
    resizer.classList.add("dragging");
    document.body.style.userSelect = "none";
  });
  resizer.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = layout.getBoundingClientRect();
    let w = e.clientX - rect.left;
    const min = 180;
    const max = Math.max(min, Math.floor(rect.width / 2));
    w = Math.max(min, Math.min(max, w));
    layout.style.setProperty("--sidebar-w", w + "px");
  });
  const stop = (e) => {
    if (!dragging) return;
    dragging = false;
    try { resizer.releasePointerCapture(e.pointerId); } catch {}
    resizer.classList.remove("dragging");
    document.body.style.userSelect = "";
    const cur = layout.style.getPropertyValue("--sidebar-w");
    if (cur && cur.endsWith("px")) localStorage.setItem("sidebar-w", parseFloat(cur));
  };
  resizer.addEventListener("pointerup", stop);
  resizer.addEventListener("pointercancel", stop);
  resizer.addEventListener("dblclick", () => {
    layout.style.removeProperty("--sidebar-w");
    localStorage.removeItem("sidebar-w");
  });
})();

// Draggable pane resizer (wide-mode only).
(() => {
  const layout = document.querySelector(".layout");
  const resizer = document.getElementById("paneResizer");
  if (!layout || !resizer) return;
  // Restore saved width
  const saved = parseFloat(localStorage.getItem("article-pane-w") || "");
  if (saved && saved > 200) layout.style.setProperty("--pane-w", saved + "px");

  let dragging = false;
  resizer.addEventListener("pointerdown", (e) => {
    if (!paneAvailable()) return;
    dragging = true;
    resizer.setPointerCapture(e.pointerId);
    resizer.classList.add("dragging");
    document.body.style.userSelect = "none";
  });
  resizer.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = layout.getBoundingClientRect();
    // distance from right edge of layout to cursor = desired pane width
    let w = rect.right - e.clientX;
    const minPane = 360;
    const maxPane = Math.max(minPane, rect.width - 280 - 440); // leave sidebar + min feed
    w = Math.max(minPane, Math.min(maxPane, w));
    layout.style.setProperty("--pane-w", w + "px");
  });
  const stop = (e) => {
    if (!dragging) return;
    dragging = false;
    try { resizer.releasePointerCapture(e.pointerId); } catch {}
    resizer.classList.remove("dragging");
    document.body.style.userSelect = "";
    const cur = layout.style.getPropertyValue("--pane-w");
    if (cur && cur.endsWith("px")) localStorage.setItem("article-pane-w", parseFloat(cur));
  };
  resizer.addEventListener("pointerup", stop);
  resizer.addEventListener("pointercancel", stop);
  resizer.addEventListener("dblclick", () => {
    layout.style.removeProperty("--pane-w");
    localStorage.removeItem("article-pane-w");
  });
})();

// Tag-chip clicks inside the pane should also toggle filters.
els.articlePaneBody.addEventListener("click", (e) => {
  const chip = e.target.closest(".tag-chip[data-tag]");
  if (chip) { e.stopPropagation(); toggleTag(chip.dataset.tag); }
});

// On viewport resize, move an open card between modal and pane so the user
// doesn't lose their place.
window.addEventListener("resize", () => {
  if (paneAvailable()) {
    if (!els.modal.classList.contains("hidden")) {
      const id = els.articlePane.dataset.bookmarkId;
      // Modal is open; move to pane.
      const openId = (state.rows.find(r => r._modalOpen) || {}).id;
      // simpler: read from any article-slot inside modal
      const slot = els.modalBody.querySelector(".article-slot[data-bookmark-id]");
      const rowId = slot ? slot.dataset.bookmarkId : null;
      if (rowId) {
        closeModal();
        openCard(rowId);
      }
    }
  } else {
    // Narrow now: if pane has content, hide it; user can re-click to open in modal.
    if (els.articlePane.dataset.bookmarkId) {
      // Move to modal
      const rowId = els.articlePane.dataset.bookmarkId;
      clearPane();
      openCard(rowId);
    }
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!els.modal.classList.contains("hidden")) closeModal();
    else if (els.articleDetail && !els.articleDetail.classList.contains("hidden")) closeArticleDetail();
    else if (els.articlePane.dataset.bookmarkId) clearPane();
  }
});

// Track the site .topbar height as a CSS custom property so sticky elements
// below it (like the longform chapter-title bar) can position themselves
// flush against its bottom edge regardless of how many rows it wraps to.
(() => {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  const updateTopbarHeight = () => {
    document.documentElement.style.setProperty(
      "--topbar-h",
      `${Math.round(topbar.getBoundingClientRect().height)}px`,
    );
  };
  updateTopbarHeight();
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(updateTopbarHeight).observe(topbar);
  } else {
    window.addEventListener("resize", updateTopbarHeight);
  }
})();

// ---------- bootstrap ----------

(async () => {
  await refreshStats();
  await refreshFeed();
  // If the URL pins us to articles mode or a specific article, restore it.
  restoreFromHash();
  // Sync topbar nav buttons (Posts/Articles/Index/Longform) to whichever mode
  // restoreFromHash() landed on.
  if (typeof updateTopbarNavState === "function") updateTopbarNavState();
  // Wide-mode first run (only when not coming in via a hash): prefill the
  // article pane with the first row that already has a brief on disk so the
  // user lands on something reading immediately.
  if (!state.articlesMode && paneAvailable() && !els.articlePane.dataset.bookmarkId) {
    const first =
      state.rows.find((r) => state.articleIds.has(String(r.id))) ||
      state.rows[0];
    if (first) openCard(first.id);
  }
  setInterval(pollStatsQuietly, 5000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollStatsQuietly();
  });
})();
