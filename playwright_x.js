#!/usr/bin/env node
/**
 * Playwright-driven X.com scraper that replaces the Safari-osascript path.
 *
 * Subcommands
 *   login                 Open a visible browser; user logs into x.com once;
 *                         storage state is saved for future headless runs.
 *   pull                  Equivalent of safari-import: scroll the bookmarks
 *                         timeline, click "Show more" links, capture full text
 *                         and media. Writes raw scrape to a fresh.json.
 *   backfill-text         Visit each truncated bookmark's status page and
 *                         update its text field if longer.
 *   backfill-media        Visit each missing-media bookmark's status page and
 *                         capture media.
 *
 * Examples
 *   node playwright_x.js login
 *   node playwright_x.js pull --out data/x-bookmarks.fresh.json
 *   node playwright_x.js backfill-text --limit 100
 *   node playwright_x.js backfill-media --limit 100
 */

const { chromium } = require("/Users/alphaone/.dev-browser/node_modules/playwright");
const fs = require("fs");
const path = require("path");

const ROOT = __dirname;
const STATE_PATH = path.join(ROOT, ".playwright_state.json");
const PROFILE_DIR = path.join(ROOT, ".playwright_profile");
const ARCHIVE = path.join(ROOT, "data/x-bookmarks.json");

const TIMELINE_URL = "https://x.com/i/bookmarks";
// Use a Chrome UA — Twitter is more permissive with Chrome than Safari/Chromium UA
const REAL_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";

const STEALTH_ARGS = [
  "--disable-blink-features=AutomationControlled",
  "--disable-features=IsolateOrigins,site-per-process",
  "--no-default-browser-check",
];

const STEALTH_INIT = `
// Hide that we're driven by Playwright. X.com blocks navigator.webdriver=true.
Object.defineProperty(navigator, "webdriver", { get: () => undefined });
// Fix language plurality and basic plugin presence
Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
Object.defineProperty(navigator, "plugins", {
  get: () => [1, 2, 3, 4, 5].map(() => ({}))
});
// Patch chrome runtime so feature detection sees a real Chrome
window.chrome = window.chrome || { runtime: {} };
`;

// ---------- helpers ----------

function loadJson(p) {
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

function saveJson(p, obj) {
  fs.writeFileSync(p, JSON.stringify(obj, null, 2));
}

function clean(s) {
  return String(s || "").replace(/\s+/g, " ").trim();
}

function nowIso() {
  return new Date().toISOString();
}

// Fold a freshly-extracted article into a bookmark, preserving the existing
// schema. Used by backfill-media and backfill-text so a single page visit
// captures everything (media, full text, fresh metrics, quoted parent,
// thread context) instead of just the field a particular subcommand cares
// about. Pass {skipText:true} to leave the text path alone.
function enrichBookmark(b, article, { skipText = false } = {}) {
  if (!article) return;
  // Full text — only overwrite if meaningfully longer (caller may have already
  // handled this in the text-backfill path).
  if (!skipText) {
    const newText = clean(article.text || "");
    const oldText = (b.text || "").trim();
    if (newText && newText.length > oldText.length + 10) {
      b.text = newText;
      b.text_full = true;
      delete b.text_truncated;
    } else if (article.truncated === false) {
      b.text_full = true;
    }
  }
  // Refresh public metrics (likes, RTs, views can move significantly).
  if (article.public_metrics) {
    b.public_metrics = article.public_metrics;
  }
  // Quoted-tweet parent — only set if we discovered one and the bookmark
  // doesn't already have a hand-parsed copy with more detail.
  if (article.quoted_tweet) {
    const have = b.quoted_tweet || {};
    const incoming = article.quoted_tweet;
    if (!(have.text && have.text.length >= (incoming.text || "").length)) {
      b.quoted_tweet = incoming;
    }
  }
  // Thread context — store the conversation around the bookmark for
  // generators that want to reason about replies / parent chains.
  if (article.thread && article.thread.length) {
    b.thread = article.thread;
  }
  // Track when we last refreshed the structured payload (separate from
  // text_checked_at / media_checked_at, both of which still track their
  // own subcommand's responsibility).
  b.enriched_at = nowIso();
}

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const k = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) {
        args[k] = true;
      } else {
        args[k] = next;
        i++;
      }
    } else {
      args._.push(a);
    }
  }
  return args;
}

async function launchContext({ headless = true, cdp = null } = {}) {
  // CDP attach mode: caller already started Chrome with --remote-debugging-port.
  // Playwright never launches anything — it just drives an existing browser.
  // This is the most stealth-resistant path for sites that detect Playwright.
  if (cdp) {
    const browser = await chromium.connectOverCDP(cdp);
    const contexts = browser.contexts();
    const ctx = contexts[0] || (await browser.newContext({ userAgent: REAL_UA }));
    return { browser, ctx, attached: true };
  }
  if (!fs.existsSync(PROFILE_DIR) && headless) {
    throw new Error(
      `No saved profile at ${PROFILE_DIR}. Run 'node playwright_x.js login' first ` +
        `(or use --cdp http://localhost:9222 to attach to your own Chrome).`,
    );
  }
  const ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless,
    channel: "chrome",
    args: STEALTH_ARGS,
    userAgent: REAL_UA,
    viewport: { width: 1280, height: 1800 },
    locale: "en-US",
    ignoreDefaultArgs: ["--enable-automation"],
  });
  await ctx.addInitScript(STEALTH_INIT);
  return { browser: ctx.browser(), ctx };
}

async function getOrCreatePage(ctx, attached) {
  if (!attached) return ctx.newPage();
  // When attaching over CDP, prefer reusing an existing tab so the user can
  // see what's happening; create one only if there are zero pages.
  const existing = ctx.pages();
  return existing[0] || (await ctx.newPage());
}

// ---------- DOM helpers (run inside page.evaluate) ----------

const PAGE_HELPERS = `
(() => {
  function clean(v) { return String(v || "").replace(/\\s+/g, " ").trim(); }
  function absolute(href) { try { return new URL(href, location.href).href; } catch { return href || ""; } }
  function parseCount(raw) {
    raw = String(raw || "").replace(/,/g, "").trim().toLowerCase();
    const m = raw.match(/([0-9]+(?:\\.[0-9]+)?)([kmb])?/);
    if (!m) return 0;
    let v = parseFloat(m[1]);
    if (m[2] === "k") v *= 1e3;
    if (m[2] === "m") v *= 1e6;
    if (m[2] === "b") v *= 1e9;
    return Math.round(v);
  }
  function metrics(article) {
    const r = { reply_count: 0, retweet_count: 0, like_count: 0, quote_count: 0, bookmark_count: 0, impression_count: 0 };
    const labels = Array.from(article.querySelectorAll("[aria-label]")).map(e => e.getAttribute("aria-label") || "");
    for (const lbl of labels) {
      const lo = lbl.toLowerCase();
      if (lo.includes("reply")) r.reply_count = Math.max(r.reply_count, parseCount(lbl));
      else if (lo.includes("repost")) r.retweet_count = Math.max(r.retweet_count, parseCount(lbl));
      else if (lo.includes("like")) r.like_count = Math.max(r.like_count, parseCount(lbl));
      else if (lo.includes("bookmark")) r.bookmark_count = Math.max(r.bookmark_count, parseCount(lbl));
      else if (lo.includes("view")) r.impression_count = Math.max(r.impression_count, parseCount(lbl));
    }
    return r;
  }
  function canonicalMediaUrl(src) {
    try {
      const u = new URL(src || "", location.href);
      if (u.hostname === "pbs.twimg.com" && /\\/(media|card_img|amplify_video_thumb|ext_tw_video_thumb)\\//.test(u.pathname)) {
        u.searchParams.set("name", "large");
      }
      return u.href;
    } catch { return src || ""; }
  }
  function mediaItems(article, { includeNestedQuotes = false } = {}) {
    const out = [];
    const seen = new Set();
    function add(kind, url, alt) {
      url = canonicalMediaUrl(url || "");
      if (!url || seen.has(url)) return;
      seen.add(url);
      out.push({ type: kind, url, alt: clean(alt || "") });
    }
    // Skip elements that live INSIDE an embedded quoted-tweet card. Those
    // images belong to the quote, not the outer post — leaving them in the
    // outer article's media double-counts and mis-attributes them.
    const isInsideNestedQuote = (el) => {
      if (includeNestedQuotes) return false;
      let p = el.parentElement;
      while (p && p !== article) {
        if (p.matches && p.matches('div[role="link"]')) {
          // Only treat as a quote card if the role=link contains a status
          // anchor pointing to a *different* tweet id from the article.
          const inner = p.querySelector('a[href*="/status/"]');
          if (inner) return true;
        }
        p = p.parentElement;
      }
      return false;
    };
    for (const img of Array.from(article.querySelectorAll("img[src]"))) {
      if (isInsideNestedQuote(img)) continue;
      const src = canonicalMediaUrl(img.currentSrc || img.src || "");
      const isMedia = /pbs\\.twimg\\.com\\/(media|card_img|amplify_video_thumb|ext_tw_video_thumb)/.test(src);
      const isAvatar = /profile_images|emoji|hashflags/.test(src);
      if (!isMedia || isAvatar) continue;
      add("image", src, img.getAttribute("alt") || "");
    }
    for (const v of Array.from(article.querySelectorAll("video"))) {
      if (isInsideNestedQuote(v)) continue;
      const src = v.getAttribute("poster") || v.poster || v.currentSrc || v.getAttribute("src") || "";
      add("video", src, v.getAttribute("aria-label") || "Video");
    }
    return out;
  }
  function articleItem(article) {
    const links = Array.from(article.querySelectorAll("a[href]")).map(a => ({
      href: absolute(a.getAttribute("href")),
      text: clean(a.innerText || a.textContent || ""),
    }));
    const status = links.find(l => /\\/status\\/\\d+($|[/?#])/.test(l.href) && !/\\/analytics/.test(l.href));
    if (!status) return null;
    const idMatch = status.href.match(/\\/status\\/(\\d+)/);
    if (!idMatch) return null;
    // "Replying to @x [and @y]" banner — when present, this article is itself
    // a reply, so we should walk further up the chain. Captured handles let
    // the outer loop decide whether to recursively fetch the ancestor.
    const replyTo = (() => {
      const banner = Array.from(article.querySelectorAll("div, span"))
        .find((el) => /^\\s*Replying to\\b/i.test(el.innerText || ""));
      if (!banner) return [];
      const handles = new Set();
      for (const a of banner.querySelectorAll("a[href]")) {
        const m = absolute(a.getAttribute("href")).match(/^https:\\/\\/x\\.com\\/([^/?#]+)$/);
        if (m && !/^i$/i.test(m[1])) handles.add(m[1]);
      }
      return Array.from(handles);
    })();
    const time = article.querySelector("time");
    const tweetText = clean((article.querySelector('[data-testid="tweetText"]') || {}).innerText || "");
    const profileLinks = links.filter(l => /^https:\\/\\/x\\.com\\/[^/?#]+$/.test(l.href) && !/\\/i\\//.test(l.href));
    const userCand = profileLinks.find(l => /^@/.test(l.text)) || profileLinks[0];
    const username = userCand ? new URL(userCand.href).pathname.split("/").filter(Boolean)[0] || "" : "";
    const nameCand = profileLinks.find(l => l.text && !/^@/.test(l.text));
    const name = nameCand ? nameCand.text.replace(/Verified account/g, "").trim() : username;
    const sm = article.querySelector('[data-testid="tweet-text-show-more-link"]');

    // Quoted tweet: X embeds the parent inside a div[role="link"] within the
    // outer article. We detect it by scanning role=link descendants for a
    // status-href with a different tweet id from the outer.
    let quoted = null;
    for (const inner of Array.from(article.querySelectorAll('div[role="link"]'))) {
      const nestedStatus = inner.querySelector('a[href*="/status/"]');
      if (!nestedStatus) continue;
      const nestedHref = absolute(nestedStatus.getAttribute("href"));
      const m = nestedHref.match(/\\/([^/]+)\\/status\\/(\\d+)/);
      if (!m) continue;
      if (m[2] === idMatch[1]) continue; // self-link, not a quote
      const innerTextEl = inner.querySelector('[data-testid="tweetText"]');
      const innerText = clean(innerTextEl ? innerTextEl.innerText : inner.innerText || "");
      if (!innerText) continue;
      // Author display name = the first text node in the inner card before the @handle
      const innerHandle = m[1];
      const innerNameEl = inner.querySelector('[data-testid="User-Name"]');
      const innerName = clean(innerNameEl ? innerNameEl.innerText.split("\\n")[0] : innerHandle);
      const innerTime = inner.querySelector("time");
      quoted = {
        id: m[2],
        url: nestedHref,
        author_username: innerHandle,
        author_name: innerName,
        text: innerText,
        created_at: innerTime ? innerTime.getAttribute("datetime") : "",
        media: mediaItems(inner),
      };
      break;
    }

    return {
      id: idMatch[1],
      url: status.href,
      text: tweetText || clean(article.innerText || ""),
      created_at: time ? time.getAttribute("datetime") : "",
      author: { username, name },
      public_metrics: metrics(article),
      media: mediaItems(article),
      truncated: !!sm,
      raw_text: clean(article.innerText || ""),
      quoted_tweet: quoted,
      reply_to: replyTo,
    };
  }
  function clickShowMore() {
    let clicked = 0;
    // Per-tweet "Show more" — expands truncated body text inline.
    for (const a of Array.from(document.querySelectorAll('article[data-testid="tweet"]'))) {
      const sm = a.querySelector('[data-testid="tweet-text-show-more-link"]');
      if (sm) { try { sm.click(); clicked++; } catch {} }
    }
    // Page-level affordances that reveal collapsed thread ancestors / hidden
    // replies. X uses different copy in different conditions, so match on
    // text rather than testid.
    const isExpander = (el) => {
      const t = clean(el.innerText || el.textContent || "").toLowerCase();
      return /^show (this thread|more replies|probable spam|additional replies|hidden replies)/.test(t)
        || /^show more$/.test(t);
    };
    for (const el of Array.from(document.querySelectorAll('a[role="link"], div[role="button"], button, span[role="button"]'))) {
      if (!isExpander(el)) continue;
      try { el.click(); clicked++; } catch {}
    }
    return clicked;
  }
  function readArticles() {
    // querySelectorAll matches every tweet article on the page including
    // those nested inside another (quoted parents). We want only top-level
    // ones — articles whose nearest article ancestor is themselves.
    return Array.from(document.querySelectorAll('article[data-testid="tweet"]'))
      .filter((a) => {
        let p = a.parentElement;
        while (p) {
          if (p.matches && p.matches('article[data-testid="tweet"]')) return false;
          p = p.parentElement;
        }
        return true;
      })
      .map(articleItem).filter(Boolean);
  }
  function pageState() {
    return {
      url: location.href,
      title: document.title,
      scroll_y: window.scrollY,
      inner_height: window.innerHeight,
      scroll_height: document.documentElement.scrollHeight,
    };
  }
  window.__bm = { clickShowMore, readArticles, pageState };
})();
`;

async function injectHelpers(page) {
  await page.evaluate(PAGE_HELPERS);
}

// ---------- subcommand: login ----------

async function cmdLogin() {
  console.log("Opening real Chrome with a persistent profile.");
  console.log(`Profile dir: ${PROFILE_DIR}`);
  const ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: false,
    channel: "chrome",
    args: STEALTH_ARGS,
    userAgent: REAL_UA,
    viewport: { width: 1280, height: 900 },
    locale: "en-US",
    ignoreDefaultArgs: ["--enable-automation"],
  });
  await ctx.addInitScript(STEALTH_INIT);

  // Reuse the first page Chrome opens (about:blank) instead of opening a new tab
  const pages = ctx.pages();
  const page = pages[0] || (await ctx.newPage());
  await page.goto("https://x.com/home", { waitUntil: "domcontentloaded" });

  console.log("");
  console.log("→ Log in to x.com in the Chrome window if you aren't already.");
  console.log("→ Once your home timeline loads, press Enter here to exit.");
  console.log("  (Cookies persist in the profile dir — no need to save state.)");
  await new Promise((resolve) => process.stdin.once("data", resolve));
  await ctx.close();
  console.log("Profile saved. Future commands will run headless.");
  process.exit(0);
}

// ---------- subcommand: pull ----------

async function scrollAndCollect(page, {
  maxScrolls = 400,
  idleRounds = 8,
  scrollPause = 1500,
  scrollStep = 0.5,
  log = () => {},
} = {}) {
  await page.goto(TIMELINE_URL, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 30000 });
  await injectHelpers(page);

  const seen = new Map();
  let mediaUpdates = 0;
  let textUpdates = 0;
  let idle = 0;
  let lastProgress = 0;

  for (let i = 0; i < maxScrolls; i++) {
    await injectHelpers(page); // re-inject in case of navigation
    await page.evaluate(() => window.__bm.clickShowMore());
    // Tiny settle so React can re-render expanded text before reading
    await page.waitForTimeout(250);
    const items = await page.evaluate(() => window.__bm.readArticles());
    let newCount = 0;
    for (const it of items) {
      const id = String(it.id || "");
      if (!id) continue;
      if (seen.has(id)) {
        const ex = seen.get(id);
        if ((it.media || []).length && !(ex.media || []).length) {
          ex.media = it.media;
          mediaUpdates++;
        }
        const fresh = clean(it.text || it.raw_text || "");
        if (fresh && fresh.length > (ex.text || "").length + 10 && !it.truncated) {
          ex.text = fresh;
          delete ex.text_truncated;
          textUpdates++;
        }
        continue;
      }
      seen.set(id, normalize(it));
      newCount++;
    }
    if (seen.size >= lastProgress + 25 || newCount) {
      log(
        `captured=${seen.size} new=${newCount} media_updates=${mediaUpdates} text_updates=${textUpdates}`,
      );
      lastProgress = seen.size;
    }

    // Are we at the bottom?
    const state = await page.evaluate(() => window.__bm.pageState());
    const atBottom =
      state.scroll_y + state.inner_height >= state.scroll_height - 80;
    idle = newCount === 0 ? idle + 1 : 0;
    if (idle >= idleRounds && atBottom) break;

    const target = Math.max(600, Math.floor(state.inner_height * scrollStep));
    await page.evaluate((y) => window.scrollBy(0, y), target);
    await page.waitForTimeout(scrollPause);
  }

  return { items: [...seen.values()], mediaUpdates, textUpdates };
}

function normalize(it) {
  const author = it.author || {};
  const out = {
    id: String(it.id || ""),
    url: it.url || `https://x.com/i/web/status/${it.id || ""}`,
    text: clean(it.text || it.raw_text || ""),
    created_at: it.created_at || null,
    lang: null,
    author: {
      id: author.username || "",
      username: author.username || "",
      name: author.name || author.username || "",
    },
    public_metrics: it.public_metrics || {},
    possibly_sensitive: null,
    conversation_id: null,
    reply_settings: null,
    source: "playwright-x-bookmarks",
    context_annotations: [],
    urls: [],
    media: it.media || [],
    referenced_tweets: [],
    raw: it,
  };
  if (it.truncated) out.text_truncated = true;
  return out;
}

async function cmdPull(args) {
  const out = args.out || path.join(ROOT, "data/x-bookmarks.fresh.json");
  const cdp = typeof args.cdp === "string" ? args.cdp : null;
  const { browser, ctx, attached } = await launchContext({
    headless: !args.headed && !cdp,
    cdp,
  });
  const page = await getOrCreatePage(ctx, attached);
  try {
    const result = await scrollAndCollect(page, {
      maxScrolls: parseInt(args["max-scrolls"] || "400", 10),
      idleRounds: parseInt(args["idle-rounds"] || "8", 10),
      scrollPause: parseInt(args["scroll-pause-ms"] || "1500", 10),
      scrollStep: parseFloat(args["scroll-step"] || "0.5"),
      log: (m) => console.log(`  ${m}`),
    });
    const payload = {
      generated_at: nowIso(),
      source: "playwright-x-bookmarks-timeline",
      bookmark_count: result.items.length,
      bookmarks: result.items.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")),
      pages: [{ source: "playwright" }],
    };
    saveJson(out, payload);
    console.log(`\nWrote ${out}`);
    console.log(
      `total=${result.items.length} media_updates=${result.mediaUpdates} text_updates=${result.textUpdates}`,
    );
  } finally {
    // When attached over CDP, we don't own the browser — leave it open.
    if (!attached && browser) await browser.close();
  }
}

// ---------- subcommand: backfill-text ----------

function likelyTruncated(b) {
  if (b.text_full) return false;
  if (b.text_truncated) return true;
  const text = (b.text || "").trim();
  if (text.length < 260) return false;
  return !'.!?"\')]…'.includes(text[text.length - 1]);
}

// Returns "rate-limited" / "missing" / "ok" so callers can distinguish a
// truly deleted/protected tweet (skip and move on) from X throttling us
// (cool down). When rate-limited the function throws RATE_LIMITED so the
// outer loop can apply backoff before continuing.
class RateLimitError extends Error {
  constructor(reason) { super(`rate-limited: ${reason}`); this.code = "RATE_LIMITED"; }
}

// Conservative rate-limit detector. Only fires on EXPLICIT signals:
// rate-limit text strings, HTTP 429/5xx (handled in fetchStatus), or the
// pre-hydration "big X logo only" splash where the body is essentially
// empty. Avoid heuristics that conflate a genuinely short text-only tweet
// with throttling.
async function detectRateLimit(page) {
  try {
    const txt = await page.evaluate(
      () => (document.body && document.body.innerText) || ""
    );
    const lower = txt.toLowerCase();
    // Explicit "we're throttling you" copy.
    if (lower.includes("rate limit")) return "explicit-rate-limit";
    if (lower.includes("rate limited")) return "explicit-rate-limit";
    if (lower.includes("try again later")) return "try-again-later";
    if (lower.includes("retry") && lower.includes("something went wrong")) return "something-went-wrong";
    // Pre-hydration splash: SPA hasn't rendered any chrome yet, just the
    // X-logo and maybe a loader. Even an X 404 page has hundreds of chars.
    const trimmed = txt.trim();
    if (trimmed.length < 40) return "x-logo-splash";
    return null;
  } catch {
    return null;
  }
}

async function fetchStatus(page, tweetId, { settleMs = 1500, loadTimeoutMs = 12000, _depth = 0 } = {}) {
  const MAX_PARENT_DEPTH = 3;
  const url = `https://x.com/i/web/status/${tweetId}`;
  let response;
  try {
    response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: loadTimeoutMs });
  } catch (e) {
    // Navigation timeout / network error — could be transient or rate-limit.
    throw new RateLimitError(`navigation-failed: ${(e && e.message) || e}`);
  }
  // HTTP-level rate-limit signal: any 429 or 5xx is a hard rate-limit signal.
  if (response) {
    const status = response.status();
    if (status === 429) throw new RateLimitError(`http-429`);
    if (status >= 500 && status < 600) throw new RateLimitError(`http-${status}`);
  }
  // Wait for at least one article — but cap quickly so deleted/private tweets don't hang
  try {
    await page.waitForSelector('article[data-testid="tweet"]', { timeout: 6000 });
  } catch {
    // No article surfaced. Distinguish "tweet is gone" from "we got blocked".
    const reason = await detectRateLimit(page);
    if (reason) throw new RateLimitError(reason);
    return null;
  }
  await injectHelpers(page);
  await page.evaluate(() => window.__bm.clickShowMore());
  await page.waitForTimeout(settleMs);
  // Scroll each article into view to trigger X's lazy image loader (parent
  // images and quoted-tweet images are otherwise empty placeholders until
  // the user scrolls them into the viewport).
  await page.evaluate(async () => {
    const arts = document.querySelectorAll('article[data-testid="tweet"]');
    for (const a of arts) {
      a.scrollIntoView({ block: "center" });
      await new Promise((r) => setTimeout(r, 250));
    }
    // Return cursor to top so subsequent navigations behave consistently.
    window.scrollTo(0, 0);
    await new Promise((r) => setTimeout(r, 200));
  });
  const items = await page.evaluate(() => window.__bm.readArticles());
  const idx = items.findIndex((a) => String(a.id) === String(tweetId));
  const main = idx >= 0 ? items[idx] : (items[0] || null);
  if (!main) {
    const reason = await detectRateLimit(page);
    if (reason) throw new RateLimitError(reason);
    return null;
  }
  // Thread context: every other top-level article on the page, ordered by
  // DOM position. Posts before the bookmark = parents/conversation; posts
  // after the bookmark by the same author = thread continuations.
  const others = items.filter((_, i) => i !== (idx >= 0 ? idx : 0));
  main.thread = others.map((a) => ({
    id: a.id,
    author_username: a.author && a.author.username,
    author_name: a.author && a.author.name,
    text: a.text,
    created_at: a.created_at,
    media: a.media || [],
    quoted_tweet: a.quoted_tweet || null,
    reply_to: a.reply_to || [],
    position: items.indexOf(a) < (idx >= 0 ? idx : 0) ? "before" : "after",
  }));

  // Recursive ancestor walk. X's status page only renders the immediate
  // parent — when the parent itself is a reply we navigate to ITS status
  // URL and pull whatever ancestors that page shows. Depth-capped to keep
  // runaway threads bounded.
  if (_depth < MAX_PARENT_DEPTH) {
    const parents = main.thread.filter((t) => t.position === "before");
    const head = parents[parents.length - 1];
    if (head && head.reply_to && head.reply_to.length) {
      try {
        const ancestor = await fetchStatus(page, head.id, {
          settleMs, loadTimeoutMs, _depth: _depth + 1,
        });
        if (ancestor) {
          // The ancestor fetch returns the *parent* as `main` and its own
          // ancestors as `thread[before]`. Prepend the new ancestors and
          // re-include the parent (with refreshed media/text) ahead of the
          // existing chain. Dedup by id.
          const seen = new Set(main.thread.map((t) => String(t.id)));
          const ancestorParents = (ancestor.thread || [])
            .filter((t) => t.position === "before");
          const ancestorAsItem = {
            id: ancestor.id,
            author_username: ancestor.author && ancestor.author.username,
            author_name: ancestor.author && ancestor.author.name,
            text: ancestor.text,
            created_at: ancestor.created_at,
            media: ancestor.media || [],
            quoted_tweet: ancestor.quoted_tweet || null,
            reply_to: ancestor.reply_to || [],
            position: "before",
          };
          const merged = [];
          for (const t of [...ancestorParents, ancestorAsItem, ...main.thread]) {
            if (seen.has(String(t.id)) && t !== ancestorAsItem) continue;
            seen.add(String(t.id));
            merged.push(t);
          }
          main.thread = merged;
        }
      } catch (e) {
        // Don't fail the whole capture if ancestor fetch hits rate-limit;
        // we already have the immediate parent.
      }
    }
  }
  return main;
}

// Tracks how many *distinct* tweets in a row have failed rate-limited even
// after a short cooldown. When that streak gets long enough, the loop
// applies a longer global cooldown before continuing. Reset on first success.
const rateLimitState = {
  consecutivePersistent: 0,
  globalCooldownStep: 0,  // 0 → 1 → 2 → 3 → max
};

// Per-tweet wrapper: one short cooldown + retry. If still rate-limited the
// second time, give up on THIS tweet (might just be a problematic page) and
// raise a persistent flag so the caller can decide whether to apply a
// longer global cooldown.
async function fetchWithCooldown(page, tweetId, opts = {}) {
  const PER_TWEET_COOLDOWN_MS = 5 * 60_000;
  try {
    const result = await fetchStatus(page, tweetId, opts);
    rateLimitState.consecutivePersistent = 0;
    rateLimitState.globalCooldownStep = 0;
    return { result, persistent: false };
  } catch (e) {
    if (e.code !== "RATE_LIMITED") throw e;
    const stamp = new Date().toISOString();
    console.log(
      `  ⚠  ${stamp}  rate-limited on ${tweetId} (${e.message}); cooling down 5 min then retrying once…`
    );
    await page.waitForTimeout(PER_TWEET_COOLDOWN_MS);
    try {
      const result = await fetchStatus(page, tweetId, opts);
      rateLimitState.consecutivePersistent = 0;
      rateLimitState.globalCooldownStep = 0;
      return { result, persistent: false };
    } catch (e2) {
      if (e2.code !== "RATE_LIMITED") throw e2;
      console.log(
        `  ✗ ${tweetId} still rate-limited after cooldown (${e2.message}); skipping`
      );
      rateLimitState.consecutivePersistent++;
      return { result: null, persistent: true };
    }
  }
}

// Global cooldown when many tweets in a row come back persistently rate-
// limited — that's a strong signal X is throttling us, not just bad pages.
async function maybeGlobalCooldown(page) {
  const STREAK_THRESHOLD = 3;
  if (rateLimitState.consecutivePersistent < STREAK_THRESHOLD) return;
  const STEPS_MS = [15 * 60_000, 30 * 60_000, 60 * 60_000, 120 * 60_000];
  const step = Math.min(rateLimitState.globalCooldownStep, STEPS_MS.length - 1);
  const ms = STEPS_MS[step];
  const mins = Math.round(ms / 60000);
  const stamp = new Date().toISOString();
  console.log(
    `  ⏸  ${stamp}  ${rateLimitState.consecutivePersistent} tweets in a row rate-limited — ` +
      `assuming X is throttling us; sleeping ${mins} min`
  );
  await page.waitForTimeout(ms);
  rateLimitState.globalCooldownStep++;
  rateLimitState.consecutivePersistent = 0; // reset and try again
}

async function cmdBackfillText(args) {
  const archive = args.archive || ARCHIVE;
  const limit = parseInt(args.limit || "0", 10);
  const throttleMs = parseInt(args["throttle-ms"] || "3000", 10);
  const breakEvery = parseInt(args["break-every"] || "30", 10);
  const breakMs = parseInt(args["break-ms"] || "30000", 10);

  const payload = loadJson(archive);
  const bms = payload.bookmarks || [];
  const targets = bms.filter((b) => b.id && likelyTruncated(b));
  console.log(`Truncated candidates: ${targets.length} of ${bms.length} total.`);
  const list = limit ? targets.slice(0, limit) : targets;

  const cdp = typeof args.cdp === "string" ? args.cdp : null;
  const { browser, ctx, attached } = await launchContext({
    headless: !args.headed && !cdp,
    cdp,
  });
  const page = await getOrCreatePage(ctx, attached);
  let updated = 0, noChange = 0, failed = 0;
  const started = Date.now();
  try {
    for (let i = 0; i < list.length; i++) {
      const tStart = Date.now();
      const b = list[i];
      try {
        const { result: article } = await fetchWithCooldown(page, b.id);
        if (!article) {
          failed++;
        } else {
          const newText = clean(article.text || "");
          const oldText = (b.text || "").trim();
          if (newText && newText.length > oldText.length + 10) {
            b.text = newText;
            b.text_full = true;
            delete b.text_truncated;
            updated++;
          } else {
            b.text_full = true;
            noChange++;
          }
          // Opportunistic enrichment: fold in media, metrics, quoted-tweet,
          // and thread context. Skip the text path here since we just handled
          // it above.
          enrichBookmark(b, article, { skipText: true });
        }
      } catch (e) {
        failed++;
      }
      b.text_checked_at = nowIso();

      if ((i + 1) % 25 === 0 || i + 1 === list.length) {
        const elapsed = (Date.now() - started) / 1000;
        const eta = ((list.length - (i + 1)) * elapsed) / Math.max(1, i + 1);
        console.log(
          `  [${i + 1}/${list.length}] updated=${updated} no_change=${noChange} ` +
            `failed=${failed}  elapsed=${elapsed.toFixed(0)}s eta=${eta.toFixed(0)}s`,
        );
      }
      if ((i + 1) % 20 === 0) saveJson(archive, payload);

      const spent = Date.now() - tStart;
      if (spent < throttleMs) await page.waitForTimeout(throttleMs - spent);
      if (breakEvery && (i + 1) % breakEvery === 0 && i + 1 < list.length) {
        console.log(`  -- break ${(breakMs / 1000).toFixed(0)}s --`);
        await page.waitForTimeout(breakMs);
      }
    }
    saveJson(archive, payload);
    console.log(`\nDone. updated=${updated} no_change=${noChange} failed=${failed}`);
  } finally {
    // When attached over CDP, we don't own the browser — leave it open.
    if (!attached && browser) await browser.close();
  }
}

// ---------- subcommand: backfill-media ----------

async function cmdBackfillMedia(args) {
  const archive = args.archive || ARCHIVE;
  const limit = parseInt(args.limit || "0", 10);
  const throttleMs = parseInt(args["throttle-ms"] || "3000", 10);
  const breakEvery = parseInt(args["break-every"] || "30", 10);
  const breakMs = parseInt(args["break-ms"] || "30000", 10);

  const payload = loadJson(archive);
  const bms = payload.bookmarks || [];
  const targets = bms.filter(
    (b) =>
      b.id &&
      !(b.media && b.media.length) &&
      !(b.media_unavailable && !args["retry-unavailable"]),
  );
  console.log(`Missing-media candidates: ${targets.length} of ${bms.length} total.`);
  const list = limit ? targets.slice(0, limit) : targets;

  const cdp = typeof args.cdp === "string" ? args.cdp : null;
  const { browser, ctx, attached } = await launchContext({
    headless: !args.headed && !cdp,
    cdp,
  });
  const page = await getOrCreatePage(ctx, attached);
  let filled = 0, unavail = 0, failed = 0;
  const started = Date.now();
  try {
    for (let i = 0; i < list.length; i++) {
      const tStart = Date.now();
      const b = list[i];
      try {
        const { result: article } = await fetchWithCooldown(page, b.id);
        if (!article) {
          b.media_unavailable = true;
          failed++;
        } else {
          // Media (the original purpose of this command).
          if ((article.media || []).length) {
            b.media = article.media;
            delete b.media_unavailable;
            filled++;
          } else {
            b.media = [];
            b.media_unavailable = true;
            unavail++;
          }
          // Opportunistic enrichment while we're here: fold in fresh full
          // text, metrics, quoted-tweet parent, and thread context.
          enrichBookmark(b, article);
        }
      } catch (e) {
        failed++;
      }
      b.media_checked_at = nowIso();

      if ((i + 1) % 25 === 0 || i + 1 === list.length) {
        const elapsed = (Date.now() - started) / 1000;
        const eta = ((list.length - (i + 1)) * elapsed) / Math.max(1, i + 1);
        console.log(
          `  [${i + 1}/${list.length}] filled=${filled} unavail=${unavail} ` +
            `failed=${failed}  elapsed=${elapsed.toFixed(0)}s eta=${eta.toFixed(0)}s`,
        );
      }
      if ((i + 1) % 20 === 0) saveJson(archive, payload);

      const spent = Date.now() - tStart;
      if (spent < throttleMs) await page.waitForTimeout(throttleMs - spent);
      if (breakEvery && (i + 1) % breakEvery === 0 && i + 1 < list.length) {
        console.log(`  -- break ${(breakMs / 1000).toFixed(0)}s --`);
        await page.waitForTimeout(breakMs);
      }
    }
    saveJson(archive, payload);
    console.log(`\nDone. filled=${filled} unavail=${unavail} failed=${failed}`);
  } finally {
    // When attached over CDP, we don't own the browser — leave it open.
    if (!attached && browser) await browser.close();
  }
}

// ---------- main ----------

// Visit each bookmark's status page and capture the full structured payload
// (text, media, metrics, quoted-tweet parent, thread context). Default scope:
// any bookmark missing the `enriched_at` field, so it's safe to re-run.
async function cmdEnrich(args) {
  const archive = args.archive || ARCHIVE;
  const limit = parseInt(args.limit || "0", 10);
  const throttleMs = parseInt(args["throttle-ms"] || "3000", 10);
  const breakEvery = parseInt(args["break-every"] || "30", 10);
  const breakMs = parseInt(args["break-ms"] || "30000", 10);
  const force = !!args.force;

  const payload = loadJson(archive);
  const bms = payload.bookmarks || [];
  const explicit = String(args.ids || "").split(",").map(s => s.trim()).filter(Boolean);
  let targets;
  if (explicit.length) {
    const set = new Set(explicit);
    targets = bms.filter((b) => set.has(String(b.id)));
  } else {
    targets = bms.filter((b) => b.id && (force || !b.enriched_at));
  }
  console.log(`Enrich candidates: ${targets.length} of ${bms.length} total.`);
  const list = limit ? targets.slice(0, limit) : targets;

  const cdp = typeof args.cdp === "string" ? args.cdp : null;
  const { browser, ctx, attached } = await launchContext({
    headless: !args.headed && !cdp,
    cdp,
  });
  const page = await getOrCreatePage(ctx, attached);
  let ok = 0, gone = 0, withQuoted = 0, withThread = 0, mediaFilled = 0;
  const started = Date.now();
  try {
    for (let i = 0; i < list.length; i++) {
      const tStart = Date.now();
      const b = list[i];
      try {
        const { result: article } = await fetchWithCooldown(page, b.id);
        if (!article) {
          b.media_unavailable = true;
          gone++;
        } else {
          const hadMedia = !!(b.media && b.media.length);
          enrichBookmark(b, article);
          if (article.media && article.media.length) {
            b.media = article.media;
            delete b.media_unavailable;
            if (!hadMedia) mediaFilled++;
          }
          if (b.quoted_tweet) withQuoted++;
          if (b.thread && b.thread.length) withThread++;
          ok++;
        }
      } catch (e) {
        gone++;
      }

      if ((i + 1) % 25 === 0 || i + 1 === list.length) {
        const elapsed = (Date.now() - started) / 1000;
        const eta = ((list.length - (i + 1)) * elapsed) / Math.max(1, i + 1);
        console.log(
          `  [${i + 1}/${list.length}] ok=${ok} gone=${gone} ` +
            `quoted=${withQuoted} thread=${withThread} +media=${mediaFilled}  ` +
            `elapsed=${elapsed.toFixed(0)}s eta=${eta.toFixed(0)}s`,
        );
      }
      if ((i + 1) % 20 === 0) saveJson(archive, payload);

      // If a streak of tweets came back persistently rate-limited, X is
      // throttling us — apply an escalating global cooldown before continuing.
      await maybeGlobalCooldown(page);

      const spent = Date.now() - tStart;
      if (spent < throttleMs) await page.waitForTimeout(throttleMs - spent);
      if (breakEvery && (i + 1) % breakEvery === 0 && i + 1 < list.length) {
        console.log(`  -- break ${(breakMs / 1000).toFixed(0)}s --`);
        await page.waitForTimeout(breakMs);
      }
    }
    saveJson(archive, payload);
    console.log(
      `\nDone. ok=${ok} gone=${gone} quoted=${withQuoted} thread=${withThread} +media=${mediaFilled}`
    );
  } finally {
    if (!attached && browser) await browser.close();
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cmd = args._[0];
  if (cmd === "login") return cmdLogin();
  if (cmd === "pull") return cmdPull(args);
  if (cmd === "backfill-text") return cmdBackfillText(args);
  if (cmd === "backfill-media") return cmdBackfillMedia(args);
  if (cmd === "enrich") return cmdEnrich(args);
  console.error(
    "Usage: node playwright_x.js <login|pull|backfill-text|backfill-media|enrich> [options]",
  );
  process.exit(2);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
