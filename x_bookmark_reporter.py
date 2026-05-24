#!/usr/bin/env python3
"""Fetch X bookmarks with OAuth 2.0 PKCE and write a Markdown report.

The script intentionally uses only Python's standard library so it can run on a
fresh machine without a package install.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import email.utils
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from archive_io import write_archive_locked


API_BASE = "https://api.x.com/2"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = f"{API_BASE}/oauth2/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_SCOPES = ("tweet.read", "users.read", "bookmark.read", "offline.access")

TWEET_FIELDS = (
    "article",
    "attachments",
    "author_id",
    "card_uri",
    "community_id",
    "context_annotations",
    "conversation_id",
    "created_at",
    "entities",
    "lang",
    "note_tweet",
    "possibly_sensitive",
    "public_metrics",
    "referenced_tweets",
    "reply_settings",
    "source",
)
EXPANSIONS = (
    "author_id",
    "attachments.media_keys",
    "attachments.poll_ids",
    "geo.place_id",
    "referenced_tweets.id",
    "referenced_tweets.id.author_id",
)
USER_FIELDS = (
    "created_at",
    "description",
    "id",
    "name",
    "public_metrics",
    "username",
    "verified",
    "verified_type",
)
MEDIA_FIELDS = (
    "alt_text",
    "duration_ms",
    "height",
    "media_key",
    "preview_image_url",
    "public_metrics",
    "type",
    "url",
    "width",
)
POLL_FIELDS = ("duration_minutes", "end_datetime", "id", "options", "voting_status")
PLACE_FIELDS = ("contained_within", "country", "country_code", "full_name", "geo", "id", "name", "place_type")

TOPIC_KEYWORDS = {
    "AI and machine learning": (
        "ai",
        "artificial intelligence",
        "llm",
        "gpt",
        "openai",
        "anthropic",
        "claude",
        "model",
        "models",
        "agent",
        "agents",
        "rag",
        "prompt",
        "inference",
        "eval",
        "transformer",
        "embedding",
        "fine tune",
        "fine-tune",
        "neural",
    ),
    "Software engineering": (
        "api",
        "architecture",
        "backend",
        "bug",
        "code",
        "compiler",
        "database",
        "debug",
        "developer",
        "devops",
        "docker",
        "frontend",
        "github",
        "javascript",
        "kubernetes",
        "next.js",
        "programming",
        "python",
        "react",
        "rust",
        "sql",
        "testing",
        "typescript",
    ),
    "Design and product": (
        "brand",
        "design",
        "figma",
        "interface",
        "product",
        "prototype",
        "ui",
        "ux",
        "visual",
        "workflow",
        "wireframe",
    ),
    "Business and startups": (
        "b2b",
        "business",
        "customer",
        "founder",
        "funding",
        "growth",
        "gtm",
        "market",
        "marketing",
        "pricing",
        "revenue",
        "sales",
        "startup",
        "strategy",
    ),
    "Writing and knowledge": (
        "book",
        "essay",
        "learn",
        "learning",
        "newsletter",
        "notes",
        "read",
        "research",
        "thread",
        "writing",
    ),
    "Data and research": (
        "analysis",
        "benchmark",
        "chart",
        "data",
        "dataset",
        "experiment",
        "paper",
        "research",
        "science",
        "study",
        "survey",
        "visualization",
    ),
    "Finance and crypto": (
        "bitcoin",
        "crypto",
        "defi",
        "economy",
        "finance",
        "fund",
        "investing",
        "market",
        "money",
        "stock",
        "trading",
        "web3",
    ),
    "Productivity and tools": (
        "automation",
        "habit",
        "notion",
        "obsidian",
        "productivity",
        "shortcut",
        "template",
        "tool",
        "tools",
        "workflow",
    ),
    "Culture and media": (
        "art",
        "culture",
        "film",
        "game",
        "music",
        "photo",
        "podcast",
        "story",
        "video",
        "youtube",
    ),
    "Politics and news": (
        "bill",
        "campaign",
        "congress",
        "court",
        "election",
        "government",
        "law",
        "news",
        "policy",
        "politics",
        "regulation",
        "senate",
    ),
    "Career and work": (
        "career",
        "hiring",
        "interview",
        "job",
        "leadership",
        "management",
        "remote",
        "team",
        "work",
    ),
}

SAFARI_BOOKMARK_EXTRACT_JS = r"""
(function(){
function clean(value){return String(value||'').replace(/\s+/g,' ').trim();}
function absolute(href){try{return new URL(href, location.href).href;}catch(error){return href||'';}}
function getCookie(name){
  const match = document.cookie.match(new RegExp('(?:^|; )' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)'));
  return match ? decodeURIComponent(match[1]) : '';
}
function graphqlFeatures(){
  return {
    rweb_video_screen_enabled:false,
    profile_label_improvements_pcf_label_in_post_enabled:true,
    rweb_tipjar_consumption_enabled:true,
    responsive_web_graphql_exclude_directive_enabled:true,
    verified_phone_label_enabled:false,
    creator_subscriptions_tweet_preview_api_enabled:true,
    responsive_web_graphql_timeline_navigation_enabled:true,
    responsive_web_graphql_skip_user_profile_image_extensions_enabled:false,
    premium_content_api_read_enabled:false,
    communities_web_enable_tweet_community_results_fetch:true,
    c9s_tweet_anatomy_moderator_badge_enabled:true,
    responsive_web_grok_analyze_button_fetch_trends_enabled:false,
    responsive_web_grok_analyze_post_followups_enabled:true,
    responsive_web_jetfuel_frame:false,
    responsive_web_grok_share_attachment_enabled:true,
    articles_preview_enabled:true,
    responsive_web_edit_tweet_api_enabled:true,
    graphql_is_translatable_rweb_tweet_is_translatable_enabled:true,
    view_counts_everywhere_api_enabled:true,
    longform_notetweets_consumption_enabled:true,
    responsive_web_twitter_article_tweet_consumption_enabled:true,
    tweet_awards_web_tipping_enabled:false,
    responsive_web_grok_show_grok_translated_post:false,
    responsive_web_grok_analysis_button_from_backend:false,
    creator_subscriptions_quote_tweet_preview_enabled:false,
    freedom_of_speech_not_reach_fetch_enabled:true,
    standardized_nudges_misinfo:true,
    tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled:true,
    longform_notetweets_rich_text_read_enabled:true,
    longform_notetweets_inline_media_enabled:true,
    responsive_web_grok_image_annotation_enabled:true,
    responsive_web_enhance_cards_enabled:false
  };
}
function findGraphqlTweet(obj, id, depth){
  if(!obj || typeof obj !== 'object' || depth > 40) return null;
  const candidate = obj.tweet || obj;
  if(candidate && typeof candidate === 'object' && (String(candidate.rest_id || '') === id || String((candidate.legacy || {}).id_str || '') === id)){
    return candidate;
  }
  if(Array.isArray(obj)){
    for(const value of obj){
      const found = findGraphqlTweet(value, id, depth + 1);
      if(found) return found;
    }
    return null;
  }
  for(const key of Object.keys(obj)){
    const found = findGraphqlTweet(obj[key], id, depth + 1);
    if(found) return found;
  }
  return null;
}
function noteTweetText(tweet){
  const note = (((tweet || {}).note_tweet || {}).note_tweet_results || {}).result || {};
  return clean(note.text || '');
}
function fetchLongTweetText(tweetId){
  tweetId = String(tweetId || '');
  if(!tweetId || !getCookie('ct0')) return '';
  const variables = {
    focalTweetId: tweetId,
    referrer: 'tweet',
    with_rux_injections: false,
    rankingMode: 'Relevance',
    includePromotedContent: true,
    withCommunity: true,
    withQuickPromoteEligibilityTweetFields: true,
    withBirdwatchNotes: true,
    withVoice: true
  };
  const fieldToggles = {
    withArticleRichContentState: true,
    withArticlePlainText: false,
    withGrokAnalyze: false,
    withDisallowedReplyControls: false
  };
  const requests = [
    {path:'/i/api/graphql/b9Yw90FMr_zUb8DvA8r2ug/TweetDetail', variables:variables},
    {path:'/i/api/graphql/qxWQxcMLiTPcavz9Qy5hwQ/TweetResultByRestId', variables:{
      tweetId: tweetId,
      withCommunity: false,
      includePromotedContent: false,
      withVoice: false
    }}
  ];
  for(const request of requests){
    const url = location.origin + request.path
      + '?variables=' + encodeURIComponent(JSON.stringify(request.variables))
      + '&features=' + encodeURIComponent(JSON.stringify(graphqlFeatures()))
      + '&fieldToggles=' + encodeURIComponent(JSON.stringify(fieldToggles));
    try{
      const xhr = new XMLHttpRequest();
      xhr.open('GET', url, false);
      xhr.withCredentials = true;
      xhr.setRequestHeader('accept', '*/*');
      xhr.setRequestHeader('authorization', 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA');
      xhr.setRequestHeader('x-csrf-token', getCookie('ct0'));
      xhr.setRequestHeader('x-twitter-active-user', 'yes');
      xhr.setRequestHeader('x-twitter-auth-type', 'OAuth2Session');
      xhr.setRequestHeader('x-twitter-client-language', document.documentElement.lang || 'en');
      xhr.send(null);
      if(xhr.status < 200 || xhr.status >= 300) continue;
      const payload = JSON.parse(xhr.responseText || '{}');
      const tweet = findGraphqlTweet(payload, tweetId, 0);
      const text = noteTweetText(tweet);
      if(text) return text;
    }catch(error){}
  }
  return '';
}
function showMoreElements(article){
  const out = new Set(Array.from(article.querySelectorAll('[data-testid="tweet-text-show-more-link"]')));
  for(const el of Array.from(article.querySelectorAll('a,button,div[role="button"],span'))){
    if(clean(el.innerText || el.textContent || '') !== 'Show more') continue;
    const clickable = el.closest('a,button,div[role="button"]') || el;
    if(article.contains(clickable)) out.add(clickable);
  }
  return Array.from(out);
}
function unhideTweetText(article){
  for(const node of Array.from(article.querySelectorAll('[data-testid="tweetText"]'))){
    for(const el of [node, node.parentElement].filter(Boolean)){
      try{
        el.style.webkitLineClamp = 'unset';
        el.style.maxHeight = 'none';
        el.style.overflow = 'visible';
      }catch(error){}
    }
  }
}
function parseCount(raw){
  raw = String(raw||'').replace(/,/g,'').trim().toLowerCase();
  const match = raw.match(/([0-9]+(?:\.[0-9]+)?)([kmb])?/);
  if(!match) return 0;
  let value = parseFloat(match[1]);
  if(match[2] === 'k') value *= 1000;
  if(match[2] === 'm') value *= 1000000;
  if(match[2] === 'b') value *= 1000000000;
  return Math.round(value);
}
function metrics(article){
  const result = {reply_count:0, retweet_count:0, like_count:0, quote_count:0, bookmark_count:0, impression_count:0};
  const labels = Array.from(article.querySelectorAll('[aria-label]')).map((el) => el.getAttribute('aria-label') || '');
  for(const label of labels){
    const lower = label.toLowerCase();
    if(lower.includes('reply')) result.reply_count = Math.max(result.reply_count, parseCount(label));
    else if(lower.includes('repost')) result.retweet_count = Math.max(result.retweet_count, parseCount(label));
    else if(lower.includes('like')) result.like_count = Math.max(result.like_count, parseCount(label));
    else if(lower.includes('bookmark')) result.bookmark_count = Math.max(result.bookmark_count, parseCount(label));
    else if(lower.includes('view')) result.impression_count = Math.max(result.impression_count, parseCount(label));
  }
  const metricLine = (article.innerText || '').match(/([0-9.,KMBkmb]+) replies?.*?([0-9.,KMBkmb]+) reposts?.*?([0-9.,KMBkmb]+) likes?.*?([0-9.,KMBkmb]+) bookmarks?.*?([0-9.,KMBkmb]+) views?/s);
  if(metricLine){
    result.reply_count = parseCount(metricLine[1]);
    result.retweet_count = parseCount(metricLine[2]);
    result.like_count = parseCount(metricLine[3]);
    result.bookmark_count = parseCount(metricLine[4]);
    result.impression_count = parseCount(metricLine[5]);
  }
  return result;
}
function mediaItems(article){
  const items = [];
  const seen = new Set();
  function canonicalMediaUrl(src){
    try{
      const url = new URL(src || '', location.href);
      if(url.hostname === 'pbs.twimg.com' && /\/(media|card_img|amplify_video_thumb|ext_tw_video_thumb)\//.test(url.pathname)){
        url.searchParams.set('name', 'large');
      }
      return url.href;
    }catch(error){
      return src || '';
    }
  }
  function sourceFromMediaUrl(url){
    if(/\/media\//.test(url)) return 'tweet_media';
    if(/\/card_img\//.test(url)) return 'link_card';
    if(/\/(amplify_video_thumb|ext_tw_video_thumb)\//.test(url)) return 'video_thumb';
    return '';
  }
  function add(kind, url, alt, meta){
    url = canonicalMediaUrl(url || '');
    if(!url || seen.has(url)) return;
    seen.add(url);
    const item = {type:kind, url:url, alt:clean(alt || '')};
    Object.assign(item, meta || {});
    items.push(item);
  }
  // Skip elements nested inside an embedded quoted-tweet card. Those images
  // belong to the quote, not the outer post.
  function isInsideNestedQuote(el){
    let p = el.parentElement;
    while (p && p !== article) {
      if (p.matches && p.matches('div[role="link"]')) {
        if (p.querySelector('a[href*="/status/"]')) return true;
      }
      p = p.parentElement;
    }
    return false;
  }
  for(const img of Array.from(article.querySelectorAll('img[src]'))){
    if (isInsideNestedQuote(img)) continue;
    let src = canonicalMediaUrl(img.currentSrc || img.src || img.getAttribute('src') || '');
    const isTweetMedia = /pbs\.twimg\.com\/(media|card_img|amplify_video_thumb|ext_tw_video_thumb)/.test(src);
    const isAvatar = /profile_images|emoji|hashflags/.test(src);
    if(!isTweetMedia || isAvatar) continue;
    add('image', src, img.getAttribute('alt') || img.getAttribute('aria-label') || '', {
      width: img.naturalWidth || img.width || 0,
      height: img.naturalHeight || img.height || 0,
      source: sourceFromMediaUrl(src)
    });
  }
  for(const video of Array.from(article.querySelectorAll('video'))){
    if (isInsideNestedQuote(video)) continue;
    const src = video.getAttribute('poster') || video.poster || video.currentSrc || video.getAttribute('src') || '';
    add('video', src, video.getAttribute('aria-label') || 'Video', {
      width: video.videoWidth || video.clientWidth || 0,
      height: video.videoHeight || video.clientHeight || 0,
      source: sourceFromMediaUrl(src) || 'video'
    });
  }
  return items;
}
function articleItem(article){
  const links = Array.from(article.querySelectorAll('a[href]')).map((anchor) => ({
    href:absolute(anchor.getAttribute('href')),
    text:clean(anchor.innerText || anchor.textContent || ''),
    aria:anchor.getAttribute('aria-label') || ''
  }));
  const status = links.find((link) => /\/status\/\d+($|[/?#])/.test(link.href) && !/\/analytics/.test(link.href));
  if(!status) return null;
  const idMatch = status.href.match(/\/status\/(\d+)/);
  if(!idMatch) return null;
  const time = article.querySelector('time');
  unhideTweetText(article);
  const tweetTexts = Array.from(article.querySelectorAll('[data-testid="tweetText"]'))
    .map((node) => clean(node.innerText || node.textContent || ''))
    .filter(Boolean);
  const showMore = showMoreElements(article);
  const longText = showMore.length ? fetchLongTweetText(idMatch[1]) : '';
  const profileLinks = links.filter((link) => /^https:\/\/x\.com\/[^/?#]+$/.test(link.href) && !/\/i\//.test(link.href));
  const usernameCandidate = profileLinks.find((link) => /^@/.test(link.text)) || profileLinks[0];
  const username = usernameCandidate ? (new URL(usernameCandidate.href).pathname.split('/').filter(Boolean)[0] || '') : '';
  const nameCandidate = profileLinks.find((link) => link.text && !/^@/.test(link.text));
  const name = nameCandidate ? nameCandidate.text.replace(/Verified account/g, '').trim() : username;
  const urls = links
    .filter((link) => !/\/status\/\d+/.test(link.href))
    .filter((link) => !/^https:\/\/x\.com\/[^/?#]+$/.test(link.href))
    .filter((link) => !/\/analytics($|[/?#])/.test(link.href))
    .map((link) => ({expanded_url:link.href, display_url:link.text, title:link.aria || link.text}));

  // "Replying to @x [and @y]" banner — present on reply tweets in the
  // bookmarks timeline. Extract the parent author handles so the enrich pass
  // (or the renderer) can show "↳ in reply to @x" without a status-page visit.
  let replyTo = [];
  const replyBanner = Array.from(article.querySelectorAll('div, span'))
    .find((el) => /^\s*Replying to\b/i.test(el.innerText || ''));
  if (replyBanner) {
    const handles = new Set();
    for (const a of replyBanner.querySelectorAll('a[href]')) {
      const m = absolute(a.getAttribute('href')).match(/^https:\/\/x\.com\/([^/?#]+)$/);
      if (m && m[1] !== 'i') handles.add(m[1]);
    }
    replyTo = Array.from(handles);
  }

  // Quoted-tweet card embedded inside the bookmarked tweet. X renders it as
  // a div[role="link"] with a status link to a different tweet id.
  let quoted = null;
  for (const inner of Array.from(article.querySelectorAll('div[role="link"]'))) {
    const nestedStatus = inner.querySelector('a[href*="/status/"]');
    if (!nestedStatus) continue;
    const nestedHref = absolute(nestedStatus.getAttribute('href'));
    const m = nestedHref.match(/\/([^/]+)\/status\/(\d+)/);
    if (!m) continue;
    if (m[2] === idMatch[1]) continue; // self-link, not a quote
    const innerTextEl = inner.querySelector('[data-testid="tweetText"]');
    const innerText = clean(innerTextEl ? innerTextEl.innerText : inner.innerText || '');
    if (!innerText) continue;
    const innerHandle = m[1];
    const innerNameEl = inner.querySelector('[data-testid="User-Name"]');
    const innerName = clean(innerNameEl ? innerNameEl.innerText.split('\n')[0] : innerHandle);
    const innerTime = inner.querySelector('time');
    quoted = {
      id: m[2],
      url: nestedHref,
      author_username: innerHandle,
      author_name: innerName,
      text: innerText,
      created_at: innerTime ? innerTime.getAttribute('datetime') : '',
      media: mediaItems(inner)
    };
    break;
  }

  return {
    id:idMatch[1],
    url:status.href,
    text:longText || tweetTexts[0] || clean(article.innerText || ''),
    created_at:time ? time.getAttribute('datetime') : '',
    author:{username:username, name:name},
    public_metrics:metrics(article),
    urls:urls,
    media:mediaItems(article),
    raw_text:clean(article.innerText || ''),
    truncated: showMore.length > 0 && !longText,
    longform_fetched: !!longText,
    reply_to: replyTo,
    quoted_tweet: quoted
  };
}
const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]')).map(articleItem).filter(Boolean);
return JSON.stringify({
  href:location.href,
  title:document.title,
  scroll_y:window.scrollY,
  inner_height:window.innerHeight,
  scroll_height:document.documentElement.scrollHeight,
  article_count:articles.length,
  articles:articles
});
})();
"""

SAFARI_BOOKMARK_EXPAND_JS = r"""
(function(){
function clean(value){return String(value||'').replace(/\s+/g,' ').trim();}
function showMoreElements(article){
  const out = new Set(Array.from(article.querySelectorAll('[data-testid="tweet-text-show-more-link"]')));
  for(const el of Array.from(article.querySelectorAll('a,button,div[role="button"],span'))){
    if(clean(el.innerText || el.textContent || '') !== 'Show more') continue;
    const clickable = el.closest('a,button,div[role="button"]') || el;
    if(article.contains(clickable)) out.add(clickable);
  }
  return Array.from(out);
}
function unhideTweetText(article){
  for(const node of Array.from(article.querySelectorAll('[data-testid="tweetText"]'))){
    for(const el of [node, node.parentElement].filter(Boolean)){
      try{
        el.style.webkitLineClamp = 'unset';
        el.style.maxHeight = 'none';
        el.style.overflow = 'visible';
      }catch(error){}
    }
  }
}
let clicked = 0;
for (const a of Array.from(document.querySelectorAll('article[data-testid="tweet"]'))) {
  unhideTweetText(a);
  for (const sm of showMoreElements(a)) {
    if (sm.matches && sm.matches('a[href*="/status/"]')) continue;
    try { sm.click(); clicked += 1; } catch (e) {}
  }
}
return JSON.stringify({clicked: clicked});
})();
"""

SAFARI_BOOKMARK_SCROLL_JS = r"""
(function(step){
  const before = window.scrollY;
  window.scrollBy(0, Math.max(600, Math.floor(window.innerHeight * step)));
  return JSON.stringify({
    before:before,
    after:window.scrollY,
    inner_height:window.innerHeight,
    scroll_height:document.documentElement.scrollHeight
  });
})(__STEP__);
"""

SAFARI_STATUS_CONTEXT_JS = r"""
(function(targetId){
function clean(value){return String(value||'').replace(/\s+/g,' ').trim();}
function absolute(href){try{return new URL(href, location.href).href;}catch(error){return href||'';}}
function getCookie(name){
  const match = document.cookie.match(new RegExp('(?:^|; )' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)'));
  return match ? decodeURIComponent(match[1]) : '';
}
function graphqlFeatures(){
  return {
    rweb_video_screen_enabled:false,
    profile_label_improvements_pcf_label_in_post_enabled:true,
    rweb_tipjar_consumption_enabled:true,
    responsive_web_graphql_exclude_directive_enabled:true,
    verified_phone_label_enabled:false,
    creator_subscriptions_tweet_preview_api_enabled:true,
    responsive_web_graphql_timeline_navigation_enabled:true,
    responsive_web_graphql_skip_user_profile_image_extensions_enabled:false,
    premium_content_api_read_enabled:false,
    communities_web_enable_tweet_community_results_fetch:true,
    c9s_tweet_anatomy_moderator_badge_enabled:true,
    responsive_web_grok_analyze_button_fetch_trends_enabled:false,
    responsive_web_grok_analyze_post_followups_enabled:true,
    responsive_web_jetfuel_frame:false,
    responsive_web_grok_share_attachment_enabled:true,
    articles_preview_enabled:true,
    responsive_web_edit_tweet_api_enabled:true,
    graphql_is_translatable_rweb_tweet_is_translatable_enabled:true,
    view_counts_everywhere_api_enabled:true,
    longform_notetweets_consumption_enabled:true,
    responsive_web_twitter_article_tweet_consumption_enabled:true,
    tweet_awards_web_tipping_enabled:false,
    responsive_web_grok_show_grok_translated_post:false,
    responsive_web_grok_analysis_button_from_backend:false,
    creator_subscriptions_quote_tweet_preview_enabled:false,
    freedom_of_speech_not_reach_fetch_enabled:true,
    standardized_nudges_misinfo:true,
    tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled:true,
    longform_notetweets_rich_text_read_enabled:true,
    longform_notetweets_inline_media_enabled:true,
    responsive_web_grok_image_annotation_enabled:true,
    responsive_web_enhance_cards_enabled:false
  };
}
function findGraphqlTweet(obj, id, depth){
  if(!obj || typeof obj !== 'object' || depth > 40) return null;
  const candidate = obj.tweet || obj;
  if(candidate && typeof candidate === 'object' && (String(candidate.rest_id || '') === id || String((candidate.legacy || {}).id_str || '') === id)){
    return candidate;
  }
  if(Array.isArray(obj)){
    for(const value of obj){
      const found = findGraphqlTweet(value, id, depth + 1);
      if(found) return found;
    }
    return null;
  }
  for(const key of Object.keys(obj)){
    const found = findGraphqlTweet(obj[key], id, depth + 1);
    if(found) return found;
  }
  return null;
}
function noteTweetText(tweet){
  const note = (((tweet || {}).note_tweet || {}).note_tweet_results || {}).result || {};
  return clean(note.text || '');
}
function fetchLongTweetText(tweetId){
  tweetId = String(tweetId || '');
  if(!tweetId || !getCookie('ct0')) return '';
  const variables = {
    focalTweetId: tweetId,
    referrer: 'tweet',
    with_rux_injections: false,
    rankingMode: 'Relevance',
    includePromotedContent: true,
    withCommunity: true,
    withQuickPromoteEligibilityTweetFields: true,
    withBirdwatchNotes: true,
    withVoice: true
  };
  const fieldToggles = {
    withArticleRichContentState: true,
    withArticlePlainText: false,
    withGrokAnalyze: false,
    withDisallowedReplyControls: false
  };
  const requests = [
    {path:'/i/api/graphql/b9Yw90FMr_zUb8DvA8r2ug/TweetDetail', variables:variables},
    {path:'/i/api/graphql/qxWQxcMLiTPcavz9Qy5hwQ/TweetResultByRestId', variables:{
      tweetId: tweetId,
      withCommunity: false,
      includePromotedContent: false,
      withVoice: false
    }}
  ];
  for(const request of requests){
    const url = location.origin + request.path
      + '?variables=' + encodeURIComponent(JSON.stringify(request.variables))
      + '&features=' + encodeURIComponent(JSON.stringify(graphqlFeatures()))
      + '&fieldToggles=' + encodeURIComponent(JSON.stringify(fieldToggles));
    try{
      const xhr = new XMLHttpRequest();
      xhr.open('GET', url, false);
      xhr.withCredentials = true;
      xhr.setRequestHeader('accept', '*/*');
      xhr.setRequestHeader('authorization', 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA');
      xhr.setRequestHeader('x-csrf-token', getCookie('ct0'));
      xhr.setRequestHeader('x-twitter-active-user', 'yes');
      xhr.setRequestHeader('x-twitter-auth-type', 'OAuth2Session');
      xhr.setRequestHeader('x-twitter-client-language', document.documentElement.lang || 'en');
      xhr.send(null);
      if(xhr.status < 200 || xhr.status >= 300) continue;
      const payload = JSON.parse(xhr.responseText || '{}');
      const tweet = findGraphqlTweet(payload, tweetId, 0);
      const text = noteTweetText(tweet);
      if(text) return text;
    }catch(error){}
  }
  return '';
}
function showMoreElements(article){
  const out = new Set(Array.from(article.querySelectorAll('[data-testid="tweet-text-show-more-link"]')));
  for(const el of Array.from(article.querySelectorAll('a,button,div[role="button"],span'))){
    if(clean(el.innerText || el.textContent || '') !== 'Show more') continue;
    const clickable = el.closest('a,button,div[role="button"]') || el;
    if(article.contains(clickable)) out.add(clickable);
  }
  return Array.from(out);
}
function unhideTweetText(article){
  for(const node of Array.from(article.querySelectorAll('[data-testid="tweetText"]'))){
    for(const el of [node, node.parentElement].filter(Boolean)){
      try{
        el.style.webkitLineClamp = 'unset';
        el.style.maxHeight = 'none';
        el.style.overflow = 'visible';
      }catch(error){}
    }
  }
}
function parseCount(raw){
  raw = String(raw||'').replace(/,/g,'').trim().toLowerCase();
  const match = raw.match(/([0-9]+(?:\.[0-9]+)?)([kmb])?/);
  if(!match) return 0;
  let value = parseFloat(match[1]);
  if(match[2] === 'k') value *= 1000;
  if(match[2] === 'm') value *= 1000000;
  if(match[2] === 'b') value *= 1000000000;
  return Math.round(value);
}
function metrics(article){
  const result = {reply_count:0, retweet_count:0, like_count:0, quote_count:0, bookmark_count:0, impression_count:0};
  const labels = Array.from(article.querySelectorAll('[aria-label]')).map((el) => el.getAttribute('aria-label') || '');
  for(const label of labels){
    const lower = label.toLowerCase();
    if(lower.includes('reply')) result.reply_count = Math.max(result.reply_count, parseCount(label));
    else if(lower.includes('repost')) result.retweet_count = Math.max(result.retweet_count, parseCount(label));
    else if(lower.includes('like')) result.like_count = Math.max(result.like_count, parseCount(label));
    else if(lower.includes('bookmark')) result.bookmark_count = Math.max(result.bookmark_count, parseCount(label));
    else if(lower.includes('view')) result.impression_count = Math.max(result.impression_count, parseCount(label));
  }
  return result;
}
function mediaItems(article){
  const items = [];
  const seen = new Set();
  function canonicalMediaUrl(src){
    try{
      const url = new URL(src || '', location.href);
      if(url.hostname === 'pbs.twimg.com' && /\/(media|card_img|amplify_video_thumb|ext_tw_video_thumb)\//.test(url.pathname)){
        url.searchParams.set('name', 'large');
      }
      return url.href;
    }catch(error){
      return src || '';
    }
  }
  function sourceFromMediaUrl(url){
    if(/\/media\//.test(url)) return 'tweet_media';
    if(/\/card_img\//.test(url)) return 'link_card';
    if(/\/(amplify_video_thumb|ext_tw_video_thumb)\//.test(url)) return 'video_thumb';
    return '';
  }
  function add(kind, url, alt, meta){
    url = canonicalMediaUrl(url || '');
    if(!url || seen.has(url)) return;
    seen.add(url);
    const item = {type:kind, url:url, alt:clean(alt || '')};
    Object.assign(item, meta || {});
    items.push(item);
  }
  // Skip elements nested inside an embedded quoted-tweet card. Those images
  // belong to the quote, not the outer post.
  function isInsideNestedQuote(el){
    let p = el.parentElement;
    while (p && p !== article) {
      if (p.matches && p.matches('div[role="link"]')) {
        if (p.querySelector('a[href*="/status/"]')) return true;
      }
      p = p.parentElement;
    }
    return false;
  }
  for(const img of Array.from(article.querySelectorAll('img[src]'))){
    if (isInsideNestedQuote(img)) continue;
    let src = canonicalMediaUrl(img.currentSrc || img.src || img.getAttribute('src') || '');
    const isTweetMedia = /pbs\.twimg\.com\/(media|card_img|amplify_video_thumb|ext_tw_video_thumb)/.test(src);
    const isAvatar = /profile_images|emoji|hashflags/.test(src);
    if(!isTweetMedia || isAvatar) continue;
    add('image', src, img.getAttribute('alt') || img.getAttribute('aria-label') || '', {
      width: img.naturalWidth || img.width || 0,
      height: img.naturalHeight || img.height || 0,
      source: sourceFromMediaUrl(src)
    });
  }
  for(const video of Array.from(article.querySelectorAll('video'))){
    if (isInsideNestedQuote(video)) continue;
    const src = video.getAttribute('poster') || video.poster || video.currentSrc || video.getAttribute('src') || '';
    add('video', src, video.getAttribute('aria-label') || 'Video', {
      width: video.videoWidth || video.clientWidth || 0,
      height: video.videoHeight || video.clientHeight || 0,
      source: sourceFromMediaUrl(src) || 'video'
    });
  }
  return items;
}
function articleItem(article, position){
  const links = Array.from(article.querySelectorAll('a[href]')).map((anchor) => ({
    href:absolute(anchor.getAttribute('href')),
    text:clean(anchor.innerText || anchor.textContent || ''),
    aria:anchor.getAttribute('aria-label') || ''
  }));
  const status = links.find((link) => /\/status\/\d+($|[/?#])/.test(link.href) && !/\/analytics/.test(link.href));
  if(!status) return null;
  const idMatch = status.href.match(/\/status\/(\d+)/);
  if(!idMatch) return null;
  const time = article.querySelector('time');
  unhideTweetText(article);
  const tweetTexts = Array.from(article.querySelectorAll('[data-testid="tweetText"]'))
    .map((node) => clean(node.innerText || node.textContent || ''))
    .filter(Boolean);
  const showMore = showMoreElements(article);
  const longText = (showMore.length || idMatch[1] === targetId) ? fetchLongTweetText(idMatch[1]) : '';
  const profileLinks = links.filter((link) => /^https:\/\/x\.com\/[^/?#]+$/.test(link.href) && !/\/i\//.test(link.href));
  const usernameCandidate = profileLinks.find((link) => /^@/.test(link.text)) || profileLinks[0];
  const username = usernameCandidate ? (new URL(usernameCandidate.href).pathname.split('/').filter(Boolean)[0] || '') : '';
  const nameCandidate = profileLinks.find((link) => link.text && !/^@/.test(link.text));
  const name = nameCandidate ? nameCandidate.text.replace(/Verified account/g, '').trim() : username;
  return {
    id:idMatch[1],
    url:status.href,
    text:longText || tweetTexts[0] || clean(article.innerText || ''),
    created_at:time ? time.getAttribute('datetime') : '',
    author:{username:username, name:name},
    public_metrics:metrics(article),
    media:mediaItems(article),
    raw_text:clean(article.innerText || ''),
    truncated: showMore.length > 0 && !longText,
    longform_fetched: !!longText,
    position:position,
    is_target:idMatch[1] === targetId
  };
}
const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]')).map(articleItem).filter(Boolean);
return JSON.stringify({
  href:location.href,
  title:document.title,
  scroll_y:window.scrollY,
  inner_height:window.innerHeight,
  scroll_height:document.documentElement.scrollHeight,
  article_count:articles.length,
  target_id:targetId,
  articles:articles
});
})(__TARGET_ID__);
"""

SAFARI_STATUS_EXPAND_JS = r"""
(function(){
function clean(value){return String(value||'').replace(/\s+/g,' ').trim();}
function showMoreElements(article){
  const out = new Set(Array.from(article.querySelectorAll('[data-testid="tweet-text-show-more-link"]')));
  for(const el of Array.from(article.querySelectorAll('a,button,div[role="button"],span'))){
    if(clean(el.innerText || el.textContent || '') !== 'Show more') continue;
    const clickable = el.closest('a,button,div[role="button"]') || el;
    if(article.contains(clickable)) out.add(clickable);
  }
  return Array.from(out);
}
function unhideTweetText(article){
  for(const node of Array.from(article.querySelectorAll('[data-testid="tweetText"]'))){
    for(const el of [node, node.parentElement].filter(Boolean)){
      try{
        el.style.webkitLineClamp = 'unset';
        el.style.maxHeight = 'none';
        el.style.overflow = 'visible';
      }catch(error){}
    }
  }
}
let clicked = 0;
for (const a of Array.from(document.querySelectorAll('article[data-testid="tweet"]'))) {
  unhideTweetText(a);
  for (const sm of showMoreElements(a)) {
    if (sm.matches && sm.matches('a[href*="/status/"]')) continue;
    try { sm.click(); clicked += 1; } catch (e) {}
  }
}
return JSON.stringify({clicked: clicked});
})();
"""

SAFARI_STATUS_SCROLL_JS = r"""
(function(){
  const before = window.scrollY;
  window.scrollBy(0, Math.max(700, Math.floor(window.innerHeight * 0.95)));
  return JSON.stringify({
    before:before,
    after:window.scrollY,
    inner_height:window.innerHeight,
    scroll_height:document.documentElement.scrollHeight
  });
})();
"""


class XApiHTTPError(RuntimeError):
    def __init__(self, status: int, url: str, body: str, headers: Any):
        self.status = status
        self.url = url
        self.body = body
        self.headers = headers
        super().__init__(self._message())

    def _message(self) -> str:
        detail = self.body.strip()
        try:
            payload = json.loads(self.body)
            if isinstance(payload, dict):
                parts = []
                if payload.get("title"):
                    parts.append(str(payload["title"]))
                if payload.get("detail"):
                    parts.append(str(payload["detail"]))
                for err in payload.get("errors", []) or []:
                    if isinstance(err, dict):
                        bits = [str(err.get(key)) for key in ("title", "detail", "message") if err.get(key)]
                        if bits:
                            parts.append(": ".join(bits))
                if parts:
                    detail = "; ".join(parts)
        except json.JSONDecodeError:
            pass
        return f"X API request failed with HTTP {self.status}: {detail or self.url}"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def parse_scopes(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(raw, (list, tuple)):
        pieces = raw
    else:
        pieces = re.split(r"[\s,]+", raw.strip())
    return [piece for piece in pieces if piece]


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any, mode: int | None = None) -> None:
    ensure_parent(path)
    if mode is None:
        write_archive_locked(path, payload, sort_keys=True)
        return
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)
    if mode is not None:
        os.chmod(path, mode)


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    timeout: int = 60,
) -> tuple[Any, Any]:
    if params:
        clean_params = {key: value for key, value in params.items() if value is not None}
        url = f"{url}?{urllib.parse.urlencode(clean_params)}"
    body = None
    request_headers = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return {}, response.headers
            return json.loads(raw), response.headers
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        raise XApiHTTPError(exc.code, url, raw_body, exc.headers) from exc


def token_headers(client_id: str, client_secret: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        credentials = f"{client_id}:{client_secret}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
    return headers


def save_token(token_file: Path, token: dict[str, Any]) -> None:
    token = dict(token)
    token["saved_at"] = iso_z()
    write_json(token_file, token, mode=0o600)
    eprint(f"Saved OAuth token to {token_file}")


def load_token(token_file: Path) -> dict[str, Any] | None:
    if not token_file.exists():
        return None
    return read_json(token_file)


def normalize_token_response(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    token = dict(previous or {})
    token.update(payload)
    now = int(time.time())
    token["obtained_at"] = now
    if payload.get("expires_in") is not None:
        token["expires_at"] = now + int(payload["expires_in"]) - 60
    return token


def token_is_expired(token: dict[str, Any] | None) -> bool:
    if not token or not token.get("access_token"):
        return True
    expires_at = token.get("expires_at")
    if not expires_at:
        return False
    return int(time.time()) >= int(expires_at)


def pkce_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(client_id: str, redirect_uri: str, scopes: list[str], state: str, challenge: str) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"


def can_listen_for_redirect(redirect_uri: str) -> bool:
    parsed = urllib.parse.urlparse(redirect_uri)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port


def wait_for_local_callback(redirect_uri: str, expected_state: str) -> str:
    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    result: dict[str, str] = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            status = 200
            heading = "X authorization complete"
            body = "You can close this tab and return to the terminal."

            if parsed.path != parsed_redirect.path:
                status = 404
                heading = "Unexpected callback path"
                body = "The path did not match the redirect URI configured for this utility."
            elif query.get("error"):
                result["error"] = query.get("error_description", query["error"])[0]
                heading = "X authorization was not completed"
                body = html.escape(result["error"])
            elif query.get("state", [""])[0] != expected_state:
                result["error"] = "OAuth state did not match. Refusing this callback."
                heading = "OAuth state mismatch"
                body = result["error"]
            elif query.get("code"):
                result["code"] = query["code"][0]
            else:
                result["error"] = "Callback did not include an authorization code."
                heading = "Missing authorization code"
                body = result["error"]

            page = f"""<!doctype html>
<meta charset="utf-8">
<title>{html.escape(heading)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 4rem; line-height: 1.5; }}
main {{ max-width: 42rem; }}
</style>
<main>
  <h1>{html.escape(heading)}</h1>
  <p>{body}</p>
</main>"""
            encoded = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    host = parsed_redirect.hostname or "127.0.0.1"
    port = parsed_redirect.port
    assert port is not None
    with ReusableTCPServer((host, port), CallbackHandler) as server:
        eprint(f"Waiting for X OAuth callback on {redirect_uri}")
        server.handle_request()

    if result.get("error"):
        raise RuntimeError(result["error"])
    if not result.get("code"):
        raise RuntimeError("No authorization code was received.")
    return result["code"]


def prompt_for_manual_callback(expected_state: str) -> str:
    redirected_url = input("Paste the full redirected callback URL here: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(redirected_url).query)
    if query.get("state", [""])[0] != expected_state:
        raise RuntimeError("OAuth state did not match. Refusing this callback.")
    if query.get("error"):
        raise RuntimeError(query.get("error_description", query["error"])[0])
    if not query.get("code"):
        raise RuntimeError("The callback URL did not contain a code parameter.")
    return query["code"][0]


def exchange_code_for_token(
    *,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    code: str,
    verifier: str,
) -> dict[str, Any]:
    form = {
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if not client_secret:
        form["client_id"] = client_id
    payload, _ = http_json(TOKEN_URL, method="POST", headers=token_headers(client_id, client_secret), form=form)
    return normalize_token_response(payload)


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str | None,
    token_file: Path,
    token: dict[str, Any],
) -> dict[str, Any]:
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("The saved token is expired and does not include a refresh token. Run `auth` again.")
    form = {"refresh_token": refresh_token, "grant_type": "refresh_token"}
    if not client_secret:
        form["client_id"] = client_id
    payload, _ = http_json(TOKEN_URL, method="POST", headers=token_headers(client_id, client_secret), form=form)
    new_token = normalize_token_response(payload, previous=token)
    save_token(token_file, new_token)
    return new_token


def authorize(
    *,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    scopes: list[str],
    token_file: Path,
    no_browser: bool = False,
    manual_callback: bool = False,
) -> dict[str, Any]:
    verifier = pkce_verifier()
    challenge = pkce_challenge(verifier)
    state = secrets.token_urlsafe(32)
    auth_url = build_authorize_url(client_id, redirect_uri, scopes, state, challenge)

    print("Open this X authorization URL:")
    print(auth_url)
    if not no_browser:
        webbrowser.open(auth_url)

    if not manual_callback and can_listen_for_redirect(redirect_uri):
        code = wait_for_local_callback(redirect_uri, state)
    else:
        code = prompt_for_manual_callback(state)

    token = exchange_code_for_token(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code=code,
        verifier=verifier,
    )
    save_token(token_file, token)
    return token


class XClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
        scopes: list[str],
        token_file: Path,
        interactive_auth: bool,
        wait_on_rate_limit: bool,
        max_rate_limit_sleep: int,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.token_file = token_file
        self.interactive_auth = interactive_auth
        self.wait_on_rate_limit = wait_on_rate_limit
        self.max_rate_limit_sleep = max_rate_limit_sleep
        self._token: dict[str, Any] | None = None

    def token(self) -> dict[str, Any]:
        token = self._token or load_token(self.token_file)
        if token_is_expired(token):
            if token and token.get("refresh_token"):
                eprint("Refreshing X OAuth token...")
                token = refresh_access_token(
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    token_file=self.token_file,
                    token=token,
                )
            elif self.interactive_auth:
                token = authorize(
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    redirect_uri=self.redirect_uri,
                    scopes=self.scopes,
                    token_file=self.token_file,
                )
            else:
                raise RuntimeError(f"No usable OAuth token found at {self.token_file}. Run `auth` first.")
        assert token is not None
        self._token = token
        return token

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        attempted_refresh = False
        while True:
            access_token = self.token()["access_token"]
            try:
                payload, _ = http_json(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                return payload
            except XApiHTTPError as exc:
                if exc.status == 401 and not attempted_refresh and self._token and self._token.get("refresh_token"):
                    attempted_refresh = True
                    eprint("Access token was rejected; refreshing once and retrying...")
                    self._token = refresh_access_token(
                        client_id=self.client_id,
                        client_secret=self.client_secret,
                        token_file=self.token_file,
                        token=self._token,
                    )
                    continue
                if exc.status == 429 and self.wait_on_rate_limit:
                    reset_header = exc.headers.get("x-rate-limit-reset")
                    if reset_header:
                        sleep_for = max(1, int(reset_header) - int(time.time()) + 5)
                        if sleep_for <= self.max_rate_limit_sleep:
                            eprint(f"Rate limit reached. Sleeping {sleep_for}s before retrying...")
                            time.sleep(sleep_for)
                            continue
                raise


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(normalized).astimezone(dt.timezone.utc)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except (TypeError, ValueError):
            return None


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def truncate(value: str, limit: int = 160) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "..."


def md_cell(value: Any, limit: int = 140) -> str:
    text = truncate(str(value or ""), limit)
    return text.replace("|", "\\|").replace("\n", "<br>")


def percent(count: int, total: int) -> str:
    if not total:
        return "0%"
    return f"{count / total:.0%}"


def tweet_full_text(tweet: dict[str, Any]) -> str:
    note_tweet = tweet.get("note_tweet") or {}
    return clean_text(note_tweet.get("text") or tweet.get("text") or "")


def add_entity_urls(container: dict[str, Any] | None, urls: list[dict[str, str]]) -> None:
    if not container:
        return
    entities = container.get("entities") or container
    for entry in entities.get("urls", []) or []:
        if not isinstance(entry, dict):
            continue
        expanded = entry.get("unwound_url") or entry.get("expanded_url") or entry.get("url")
        if not expanded:
            continue
        urls.append(
            {
                "url": entry.get("url") or expanded,
                "expanded_url": expanded,
                "display_url": entry.get("display_url") or "",
                "title": entry.get("title") or "",
                "description": entry.get("description") or "",
            }
        )


def extract_urls(tweet: dict[str, Any]) -> list[dict[str, str]]:
    urls: list[dict[str, str]] = []
    add_entity_urls(tweet.get("entities"), urls)
    add_entity_urls(tweet.get("note_tweet"), urls)
    seen = set()
    unique = []
    for url in urls:
        key = url.get("expanded_url") or url.get("url")
        if key and key not in seen:
            seen.add(key)
            unique.append(url)
    return unique


def domain_from_url(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or host in {"t.co", "x.com", "twitter.com"}:
        return None
    return host


def engagement_score(bookmark: dict[str, Any]) -> float:
    metrics = bookmark.get("public_metrics") or {}
    return (
        float(metrics.get("like_count") or 0)
        + (float(metrics.get("retweet_count") or 0) * 2.0)
        + (float(metrics.get("quote_count") or 0) * 1.5)
        + float(metrics.get("reply_count") or 0)
    )


def tweet_url(tweet_id: str, author: dict[str, Any] | None) -> str:
    username = (author or {}).get("username")
    if username:
        return f"https://x.com/{username}/status/{tweet_id}"
    return f"https://x.com/i/web/status/{tweet_id}"


def normalize_bookmark(
    tweet: dict[str, Any],
    users_by_id: dict[str, dict[str, Any]],
    media_by_key: dict[str, dict[str, Any]],
    included_tweets_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    author = users_by_id.get(str(tweet.get("author_id", "")), {})
    media_keys = (tweet.get("attachments") or {}).get("media_keys") or []
    referenced = []
    for ref in tweet.get("referenced_tweets", []) or []:
        ref_id = str(ref.get("id", ""))
        ref_tweet = included_tweets_by_id.get(ref_id, {})
        ref_author = users_by_id.get(str(ref_tweet.get("author_id", "")), {})
        referenced.append(
            {
                "type": ref.get("type"),
                "id": ref_id,
                "url": tweet_url(ref_id, ref_author) if ref_id else "",
                "text": tweet_full_text(ref_tweet),
                "author": ref_author,
            }
        )
    text = tweet_full_text(tweet)
    return {
        "id": str(tweet.get("id")),
        "url": tweet_url(str(tweet.get("id")), author),
        "text": text,
        "created_at": tweet.get("created_at"),
        "lang": tweet.get("lang"),
        "author": author,
        "public_metrics": tweet.get("public_metrics") or {},
        "possibly_sensitive": tweet.get("possibly_sensitive"),
        "conversation_id": tweet.get("conversation_id"),
        "reply_settings": tweet.get("reply_settings"),
        "source": tweet.get("source"),
        "context_annotations": tweet.get("context_annotations") or [],
        "urls": extract_urls(tweet),
        "media": [media_by_key[key] for key in media_keys if key in media_by_key],
        "referenced_tweets": referenced,
        "raw": tweet,
    }


def merge_includes(all_includes: dict[str, dict[str, dict[str, Any]]], includes: dict[str, Any]) -> None:
    mapping = {
        "users": ("users", "id"),
        "media": ("media", "media_key"),
        "tweets": ("tweets", "id"),
        "polls": ("polls", "id"),
        "places": ("places", "id"),
    }
    for include_key, (target_key, id_key) in mapping.items():
        for item in includes.get(include_key, []) or []:
            identifier = item.get(id_key)
            if identifier:
                all_includes[target_key][str(identifier)] = item


def fetch_bookmarks(args: argparse.Namespace) -> dict[str, Any]:
    client = XClient(
        client_id=require_client_id(args),
        client_secret=args.client_secret,
        redirect_uri=args.redirect_uri,
        scopes=parse_scopes(args.scopes),
        token_file=Path(args.token_file),
        interactive_auth=not args.no_interactive_auth,
        wait_on_rate_limit=not args.no_wait_rate_limit,
        max_rate_limit_sleep=args.max_rate_limit_sleep,
    )
    me_payload = client.get("/users/me", {"user.fields": ",".join(USER_FIELDS)})
    me = me_payload.get("data") or {}
    user_id = args.user_id or me.get("id")
    if not user_id:
        raise RuntimeError("Could not determine your X user ID. Pass --user-id explicitly.")

    raw_bookmarks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    all_includes: dict[str, dict[str, dict[str, Any]]] = {
        "users": {},
        "media": {},
        "tweets": {},
        "polls": {},
        "places": {},
    }
    pages: list[dict[str, Any]] = []
    next_token = args.pagination_token
    max_results = max(1, min(args.max_results, 100))
    limit = args.limit if args.limit and args.limit > 0 else None

    while True:
        params = {
            "max_results": max_results,
            "tweet.fields": ",".join(TWEET_FIELDS),
            "expansions": ",".join(EXPANSIONS),
            "user.fields": ",".join(USER_FIELDS),
            "media.fields": ",".join(MEDIA_FIELDS),
            "poll.fields": ",".join(POLL_FIELDS),
            "place.fields": ",".join(PLACE_FIELDS),
            "pagination_token": next_token,
        }
        page = client.get(f"/users/{user_id}/bookmarks", params)
        merge_includes(all_includes, page.get("includes") or {})
        page_data = page.get("data") or []
        for tweet in page_data:
            tweet_id = str(tweet.get("id"))
            if tweet_id in seen_ids:
                continue
            seen_ids.add(tweet_id)
            raw_bookmarks.append(tweet)
            if limit and len(raw_bookmarks) >= limit:
                break
        meta = page.get("meta") or {}
        pages.append({"meta": meta, "errors": page.get("errors") or []})
        eprint(f"Fetched {len(raw_bookmarks)} bookmarks...")
        if limit and len(raw_bookmarks) >= limit:
            break
        next_token = meta.get("next_token")
        if not next_token:
            break

    bookmarks = [
        normalize_bookmark(tweet, all_includes["users"], all_includes["media"], all_includes["tweets"])
        for tweet in raw_bookmarks
    ]
    for bookmark in bookmarks:
        bookmark["category"] = categorize_bookmark(bookmark)
        bookmark["engagement_score"] = engagement_score(bookmark)

    payload = {
        "generated_at": iso_z(),
        "source": "x-api-v2-bookmarks",
        "authenticated_user": me,
        "requested_user_id": user_id,
        "bookmark_count": len(bookmarks),
        "bookmarks": bookmarks,
        "includes": all_includes,
        "pages": pages,
    }
    output = Path(args.output)
    write_json(output, payload)
    eprint(f"Wrote bookmark archive to {output}")
    if args.csv:
        write_bookmarks_csv(Path(args.csv), bookmarks)
        eprint(f"Wrote bookmark CSV to {args.csv}")
    return payload


def keyword_hits(text: str, keyword: str) -> int:
    keyword = keyword.lower()
    if len(keyword) <= 3 and keyword.isalpha():
        return len(re.findall(rf"\b{re.escape(keyword)}\b", text))
    return text.count(keyword)


def categorize_bookmark(bookmark: dict[str, Any]) -> str:
    author = bookmark.get("author") or {}
    context_names = " ".join(
        annotation.get("entity", {}).get("name", "")
        for annotation in bookmark.get("context_annotations", []) or []
        if isinstance(annotation, dict)
    )
    domains = " ".join(domain_from_url(url.get("expanded_url", "")) or "" for url in bookmark.get("urls", []) or [])
    haystack = " ".join(
        [
            bookmark.get("text") or "",
            author.get("username") or "",
            author.get("name") or "",
            author.get("description") or "",
            context_names,
            domains,
        ]
    ).lower()
    scores = {
        topic: sum(keyword_hits(haystack, keyword) for keyword in keywords)
        for topic, keywords in TOPIC_KEYWORDS.items()
    }
    best_topic, best_score = max(scores.items(), key=lambda item: item[1])
    return best_topic if best_score > 0 else "Uncategorized"


def write_bookmarks_csv(path: Path, bookmarks: list[dict[str, Any]]) -> None:
    ensure_parent(path)
    fields = [
        "id",
        "created_at",
        "author_username",
        "author_name",
        "category",
        "lang",
        "like_count",
        "retweet_count",
        "reply_count",
        "quote_count",
        "engagement_score",
        "url",
        "expanded_urls",
        "text",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for bookmark in bookmarks:
            author = bookmark.get("author") or {}
            metrics = bookmark.get("public_metrics") or {}
            writer.writerow(
                {
                    "id": bookmark.get("id"),
                    "created_at": bookmark.get("created_at"),
                    "author_username": author.get("username"),
                    "author_name": author.get("name"),
                    "category": bookmark.get("category") or categorize_bookmark(bookmark),
                    "lang": bookmark.get("lang"),
                    "like_count": metrics.get("like_count", 0),
                    "retweet_count": metrics.get("retweet_count", 0),
                    "reply_count": metrics.get("reply_count", 0),
                    "quote_count": metrics.get("quote_count", 0),
                    "engagement_score": f"{engagement_score(bookmark):.1f}",
                    "url": bookmark.get("url"),
                    "expanded_urls": " ".join(url.get("expanded_url", "") for url in bookmark.get("urls", []) or []),
                    "text": bookmark.get("text"),
                }
            )


def applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def safari_do_javascript(js: str, *, timeout: int = 30, target_url_contains: str | None = None) -> str:
    js_string = applescript_string(js)
    if target_url_contains:
        target_string = applescript_string(target_url_contains)
        script = f"""
tell application "Safari"
  set targetNeedle to "{target_string}"
  repeat with w in windows
    try
      repeat with t in tabs of w
        try
          if (URL of t as text) contains targetNeedle then
            return do JavaScript "{js_string}" in t
          end if
        end try
      end repeat
    end try
  end repeat
  error "No Safari tab found for " & targetNeedle
end tell
"""
    else:
        script = f'tell application "Safari" to do JavaScript "{js_string}" in current tab of front window'
    return subprocess.check_output(["osascript", "-e", script], text=True, timeout=timeout).strip()


def safari_set_url(url: str, *, timeout: int = 10, target_url_contains: str = "x.com") -> None:
    url_string = applescript_string(url)
    target_string = applescript_string(target_url_contains)
    script = f"""
tell application "Safari"
  set targetNeedle to "{target_string}"
  set destinationURL to "{url_string}"
  set chosenTab to missing value
  repeat with w in windows
    try
      repeat with t in tabs of w
        try
          if (URL of t as text) contains targetNeedle then
            set chosenTab to t
            exit repeat
          end if
        end try
      end repeat
      if chosenTab is not missing value then exit repeat
    end try
  end repeat
  if chosenTab is missing value then
    error "No Safari tab containing " & targetNeedle & " found. Open x.com in any Safari window and retry."
  end if
  set URL of chosenTab to destinationURL
end tell
"""
    subprocess.check_call(["osascript", "-e", script], timeout=timeout)


def safari_activate_tab(url_contains: str, *, timeout: int = 10) -> bool:
    target_string = applescript_string(url_contains)
    script = f"""
tell application "Safari"
  set targetNeedle to "{target_string}"
  repeat with w in windows
    try
      repeat with t in tabs of w
        try
          if (URL of t as text) contains targetNeedle then
            set current tab of w to t
            return "true"
          end if
        end try
      end repeat
    end try
  end repeat
  return "false"
end tell
"""
    return subprocess.check_output(["osascript", "-e", script], text=True, timeout=timeout).strip() == "true"


def _has_truncated_articles(page: dict[str, Any]) -> bool:
    return any(article.get("truncated") for article in page.get("articles", []) or [])


def safari_expand_show_more(target_url_contains: str) -> int:
    js = SAFARI_STATUS_EXPAND_JS if "/status/" in target_url_contains else SAFARI_BOOKMARK_EXPAND_JS
    raw = safari_do_javascript(js, timeout=15, target_url_contains=target_url_contains)
    try:
        return int((json.loads(raw) or {}).get("clicked") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0


def safari_extract_bookmark_page() -> dict[str, Any]:
    target = "x.com/i/bookmarks"
    if safari_expand_show_more(target):
        time.sleep(0.35)
    page: dict[str, Any] = {}
    for attempt in range(3):
        raw = safari_do_javascript(SAFARI_BOOKMARK_EXTRACT_JS, timeout=45, target_url_contains=target)
        page = json.loads(raw)
        if not _has_truncated_articles(page):
            return page
        if safari_expand_show_more(target):
            time.sleep(0.35 + attempt * 0.25)
        else:
            time.sleep(0.25)
    return page


def safari_reload_bookmarks_timeline() -> None:
    safari_do_javascript(
        "window.location.reload(); JSON.stringify({ok:true, href:window.location.href})",
        timeout=10,
        target_url_contains="x.com/i/bookmarks",
    )


def safari_wait_for_bookmarks_timeline(*, timeout: int = 45) -> dict[str, Any]:
    deadline = time.time() + timeout
    page: dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(1)
        try:
            page = safari_extract_bookmark_page()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
        if "x.com/i/bookmarks" in page.get("href", "") and page.get("article_count", 0) > 0:
            return page
    raise RuntimeError("Safari did not load the X bookmarks timeline. Make sure you are signed in to x.com in Safari.")


def safari_scroll(step: float) -> dict[str, Any]:
    js = SAFARI_BOOKMARK_SCROLL_JS.replace("__STEP__", str(max(0.2, min(step, 3.0))))
    raw = safari_do_javascript(js, timeout=15, target_url_contains="x.com/i/bookmarks")
    return json.loads(raw)


def safari_extract_status_page(tweet_id: str) -> dict[str, Any]:
    js = SAFARI_STATUS_CONTEXT_JS.replace("__TARGET_ID__", json.dumps(str(tweet_id)))
    target = f"/status/{tweet_id}"
    if safari_expand_show_more(target):
        time.sleep(0.35)
    page: dict[str, Any] = {}
    for attempt in range(3):
        raw = safari_do_javascript(js, timeout=45, target_url_contains=target)
        page = json.loads(raw)
        if not _has_truncated_articles(page):
            return page
        if safari_expand_show_more(target):
            time.sleep(0.35 + attempt * 0.25)
        else:
            time.sleep(0.25)
    return page


def safari_scroll_status(tweet_id: str) -> dict[str, Any]:
    raw = safari_do_javascript(SAFARI_STATUS_SCROLL_JS, timeout=15, target_url_contains=f"/status/{tweet_id}")
    return json.loads(raw)


def compact_context_article(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author") or {}
    return {
        "id": str(item.get("id") or ""),
        "url": item.get("url") or "",
        "text": clean_text(item.get("text") or ""),
        "created_at": item.get("created_at") or "",
        "author": {
            "username": author.get("username") or "",
            "name": author.get("name") or author.get("username") or "",
        },
        "public_metrics": item.get("public_metrics") or {},
        "media": item.get("media") or [],
        "position": item.get("position"),
    }


def safari_extract_status_context(
    bookmark: dict[str, Any],
    *,
    reply_limit: int = 8,
    scrolls: int = 2,
    pause: float = 1.0,
) -> dict[str, Any]:
    tweet_id = str(bookmark.get("id") or "")
    if not tweet_id:
        raise RuntimeError("Cannot enrich a bookmark without a tweet id.")
    url = bookmark.get("url") or f"https://x.com/i/web/status/{tweet_id}"
    safari_set_url(url)

    page: dict[str, Any] = {}
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(1)
        try:
            page = safari_extract_status_page(tweet_id)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        if f"/status/{tweet_id}" in page.get("href", "") and page.get("article_count", 0) > 0:
            break
    else:
        raise RuntimeError(f"Safari did not load the X status page for {tweet_id}.")

    articles_by_id: dict[str, dict[str, Any]] = {}
    for index in range(max(1, int(scrolls or 1))):
        page = safari_extract_status_page(tweet_id)
        for article in page.get("articles", []) or []:
            article_id = str(article.get("id") or "")
            if article_id and article_id not in articles_by_id:
                articles_by_id[article_id] = compact_context_article(article)
        if index < scrolls - 1:
            safari_scroll_status(tweet_id)
            time.sleep(max(0.2, pause))

    articles = list(articles_by_id.values())
    target = next((article for article in articles if article.get("id") == tweet_id), None)
    if not target and articles:
        target = articles[0]
    replies = [article for article in articles if article.get("id") != tweet_id][: max(0, reply_limit)]
    media = (target or {}).get("media") or bookmark.get("media") or []
    return {
        "loaded_at": iso_z(),
        "source": "safari-x-status-page",
        "url": page.get("href") or url,
        "post": target or {},
        "media": media,
        "replies": replies,
        "articles_seen": len(articles),
    }


def normalize_safari_bookmark(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author") or {}
    bookmark = {
        "id": str(item.get("id") or ""),
        "url": item.get("url") or f"https://x.com/i/web/status/{item.get('id')}",
        "text": clean_text(item.get("text") or item.get("raw_text") or ""),
        "created_at": item.get("created_at") or None,
        "lang": None,
        "author": {
            "id": author.get("username") or "",
            "username": author.get("username") or "",
            "name": author.get("name") or author.get("username") or "",
        },
        "public_metrics": item.get("public_metrics") or {},
        "possibly_sensitive": None,
        "conversation_id": None,
        "reply_settings": None,
        "source": "Safari X bookmark timeline",
        "context_annotations": [],
        "urls": item.get("urls") or [],
        "media": item.get("media") or [],
        "referenced_tweets": [],
        "raw": item,
    }
    if item.get("truncated"):
        bookmark["text_truncated"] = True
    if item.get("reply_to"):
        bookmark["reply_to"] = list(item["reply_to"])
    if item.get("quoted_tweet"):
        bookmark["quoted_tweet"] = item["quoted_tweet"]
    bookmark["category"] = categorize_bookmark(bookmark)
    bookmark["engagement_score"] = engagement_score(bookmark)
    return bookmark


def import_bookmarks_from_safari(args: argparse.Namespace) -> dict[str, Any]:
    target_url = "https://x.com/i/bookmarks"
    try:
        page = safari_extract_bookmark_page()
    except subprocess.CalledProcessError:
        page = {"href": ""}
    if "x.com/i/bookmarks" not in page.get("href", ""):
        # Make this fully autonomous: launch Safari if it isn't running, and
        # open a new x.com/i/bookmarks tab if no x.com tab exists. Only after
        # that fall back to safari_set_url (which retargets an existing tab).
        eprint(f"Ensuring Safari is open with {target_url} ...")
        try:
            subprocess.run(["open", "-a", "Safari", target_url], check=False, timeout=10)
        except Exception:
            pass
        # Give Safari a moment to launch and load.
        time.sleep(2)
        try:
            page = safari_extract_bookmark_page()
        except subprocess.CalledProcessError:
            page = {"href": ""}
        if "x.com/i/bookmarks" not in page.get("href", ""):
            try:
                safari_set_url(target_url)
            except Exception as exc:
                eprint(f"safari_set_url fallback failed: {exc}")
        page = safari_wait_for_bookmarks_timeline(timeout=45)

    eprint("Reloading Safari bookmarks timeline before scraping...")
    safari_reload_bookmarks_timeline()
    page = safari_wait_for_bookmarks_timeline(timeout=45)
    safari_do_javascript("window.scrollTo(0, 0); JSON.stringify({ok:true})", timeout=10, target_url_contains="x.com/i/bookmarks")
    time.sleep(1)

    seen: dict[str, dict[str, Any]] = {}
    max_scrolls = max(1, int(getattr(args, "max_scrolls", 400) or 400))
    idle_rounds = max(1, int(getattr(args, "idle_rounds", 8) or 8))
    scroll_pause = max(0.1, float(getattr(args, "scroll_pause", 0.85) or 0.85))
    scroll_step = max(0.2, float(getattr(args, "scroll_step", 0.85) or 0.85))
    limit = int(getattr(args, "limit", 0) or 0)
    # Stop early once we've passed the boundary into already-archived bookmarks.
    # X serves the bookmarks timeline newest-first by add-time, so a stretch of
    # all-known IDs means we've caught up. 0 disables the heuristic.
    stop_after_known = int(getattr(args, "stop_after_known", 0) or 0)
    known_ids: set[str] = set()
    if stop_after_known:
        # Always check against the canonical archive, not the fresh-output target.
        archive_target = Path("data/x-bookmarks.json")
        if archive_target.exists():
            try:
                existing = json.loads(archive_target.read_text())
                known_ids = {str(b.get("id")) for b in existing.get("bookmarks", []) if b.get("id")}
                eprint(f"Loaded {len(known_ids)} known IDs from {archive_target} for early-stop check.")
            except Exception as exc:
                eprint(f"Could not load existing archive for early-stop ({exc}); continuing without it.")
    consecutive_known = 0
    idle = 0
    last_progress = 0

    media_updates = 0
    early_exit_reason: str | None = None
    for index in range(max_scrolls):
        try:
            page = safari_extract_bookmark_page()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            early_exit_reason = f"safari extraction failed at scroll {index}: {type(e).__name__}"
            eprint(early_exit_reason)
            break
        href = page.get("href", "")
        if "x.com/i/bookmarks" not in href:
            early_exit_reason = f"Safari left the bookmarks page at scroll {index}: {href}"
            eprint(early_exit_reason)
            break

        new_count = 0
        for item in page.get("articles", []) or []:
            tweet_id = str(item.get("id") or "")
            if not tweet_id:
                continue
            if tweet_id in seen:
                # Update media if the previously-captured entry was missing it
                # (images lazy-load on x.com, so re-encountering an article in a
                # later scroll often surfaces media that wasn't in the DOM before)
                existing = seen[tweet_id]
                fresh_media = item.get("media") or []
                if fresh_media and not existing.get("media"):
                    existing["media"] = fresh_media
                    media_updates += 1
                # Update text if the new pass got an expanded ("Show more") version
                fresh_text = clean_text(item.get("text") or item.get("raw_text") or "")
                old_text = (existing.get("text") or "")
                if fresh_text and len(fresh_text) > len(old_text) + 10 \
                        and not item.get("truncated"):
                    existing["text"] = fresh_text
                    existing.pop("text_truncated", None)
                # Late-fill reply_to / quoted_tweet — these can also lazy-render
                # (e.g. quoted-tweet cards take a beat to hydrate).
                if item.get("reply_to") and not existing.get("reply_to"):
                    existing["reply_to"] = list(item["reply_to"])
                if item.get("quoted_tweet") and not existing.get("quoted_tweet"):
                    existing["quoted_tweet"] = item["quoted_tweet"]
                continue
            seen[tweet_id] = normalize_safari_bookmark(item)
            new_count += 1
            # Track how many CONSECUTIVE freshly-discovered IDs are already in
            # the prior archive. A long run-on means we've crossed the boundary
            # from new bookmarks back into already-known ones.
            if known_ids and tweet_id in known_ids:
                consecutive_known += 1
            else:
                consecutive_known = 0
            if limit and len(seen) >= limit:
                break

        if len(seen) >= last_progress + 25 or new_count:
            with_media = sum(1 for b in seen.values() if b.get("media"))
            eprint(f"Captured {len(seen)} Safari bookmarks ({with_media} with media, {media_updates} media late-fills)...")
            last_progress = len(seen)

        if limit and len(seen) >= limit:
            break

        if stop_after_known and consecutive_known >= stop_after_known:
            eprint(f"Hit {consecutive_known} consecutive known bookmarks — stopping early (boundary reached).")
            break

        idle = idle + 1 if new_count == 0 else 0
        at_bottom = (float(page.get("scroll_y") or 0) + float(page.get("inner_height") or 0)) >= (float(page.get("scroll_height") or 0) - 80)
        if idle >= idle_rounds and at_bottom:
            break

        try:
            scroll_state = safari_scroll(scroll_step)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            early_exit_reason = f"safari scroll failed at scroll {index}: {type(e).__name__}"
            eprint(early_exit_reason)
            break
        if scroll_state.get("after") == scroll_state.get("before") and new_count == 0:
            idle += 1
            if idle >= idle_rounds:
                break
        time.sleep(scroll_pause)

    if early_exit_reason:
        eprint(f"Persisting partial result ({len(seen)} bookmarks) despite early exit.")
    bookmarks = list(seen.values())
    bookmarks.sort(key=lambda bookmark: parse_datetime(bookmark.get("created_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    payload = {
        "generated_at": iso_z(),
        "source": "safari-x-bookmarks-timeline",
        "authenticated_user": {},
        "requested_user_id": None,
        "bookmark_count": len(bookmarks),
        "bookmarks": bookmarks,
        "includes": {},
        "pages": [{"source": "Safari", "scrolls": min(max_scrolls, index + 1 if "index" in locals() else 0)}],
    }

    output = Path(args.output)
    write_json(output, payload)
    eprint(f"Wrote Safari bookmark archive to {output}")
    if getattr(args, "csv", None):
        write_bookmarks_csv(Path(args.csv), bookmarks)
        eprint(f"Wrote bookmark CSV to {args.csv}")
    return payload


def bookmark_domains(bookmark: dict[str, Any]) -> list[str]:
    domains = []
    for url in bookmark.get("urls", []) or []:
        domain = domain_from_url(url.get("expanded_url", ""))
        if domain:
            domains.append(domain)
    return domains


def bookmark_month(bookmark: dict[str, Any]) -> str | None:
    parsed = parse_datetime(bookmark.get("created_at"))
    if not parsed:
        return None
    return parsed.strftime("%Y-%m")


def metric_total(bookmarks: list[dict[str, Any]], key: str) -> int:
    return int(sum((bookmark.get("public_metrics") or {}).get(key) or 0 for bookmark in bookmarks))


def author_label(author: dict[str, Any]) -> str:
    username = author.get("username")
    name = author.get("name")
    if username and name:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    return name or "Unknown author"


def format_bookmark_link(bookmark: dict[str, Any], limit: int = 110) -> str:
    label = md_cell(bookmark.get("text") or bookmark.get("id"), limit)
    return f"[{label}]({bookmark.get('url')})"


def top_bookmarks(bookmarks: list[dict[str, Any]], count: int, *, newest: bool = False, oldest: bool = False) -> list[dict[str, Any]]:
    if newest or oldest:
        dated = [(parse_datetime(item.get("created_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc), item) for item in bookmarks]
        dated.sort(key=lambda item: item[0], reverse=newest)
        return [item for _, item in dated[:count]]
    return sorted(bookmarks, key=engagement_score, reverse=True)[:count]


def build_report(payload: dict[str, Any], index_limit: int) -> str:
    bookmarks = payload.get("bookmarks") or []
    total = len(bookmarks)
    generated_at = payload.get("generated_at") or iso_z()
    user = payload.get("authenticated_user") or {}

    category_counts = Counter((bookmark.get("category") or categorize_bookmark(bookmark)) for bookmark in bookmarks)
    author_counts = Counter(author_label(bookmark.get("author") or {}) for bookmark in bookmarks)
    domain_counts = Counter(domain for bookmark in bookmarks for domain in bookmark_domains(bookmark))
    lang_counts = Counter(bookmark.get("lang") or "unknown" for bookmark in bookmarks)
    month_counts = Counter(month for month in (bookmark_month(bookmark) for bookmark in bookmarks) if month)
    media_counts = Counter(media.get("type") or "media" for bookmark in bookmarks for media in bookmark.get("media", []) or [])

    datetimes = [parse_datetime(bookmark.get("created_at")) for bookmark in bookmarks]
    datetimes = [value for value in datetimes if value]
    newest = max(datetimes).strftime("%Y-%m-%d") if datetimes else "unknown"
    oldest = min(datetimes).strftime("%Y-%m-%d") if datetimes else "unknown"

    linked_count = sum(1 for bookmark in bookmarks if bookmark.get("urls"))
    media_count = sum(1 for bookmark in bookmarks if bookmark.get("media"))
    quote_or_reply_count = sum(1 for bookmark in bookmarks if bookmark.get("referenced_tweets"))

    top_categories = category_counts.most_common(5)
    category_sentence = ", ".join(f"{name} ({count})" for name, count in top_categories) or "No obvious categories"
    lines = [
        "# X Bookmark Report",
        "",
        f"Generated: {generated_at}",
        "",
        "## Snapshot",
        "",
        f"- Account: {author_label(user) if user else 'Authenticated X user'}",
        f"- Bookmarks analyzed: {total:,}",
        f"- Post date range: {oldest} to {newest}",
        f"- Bookmarks with external links: {linked_count:,} ({percent(linked_count, total)})",
        f"- Bookmarks with media: {media_count:,} ({percent(media_count, total)})",
        f"- Quotes/replies/reposts represented: {quote_or_reply_count:,} ({percent(quote_or_reply_count, total)})",
        f"- Aggregate public engagement: {metric_total(bookmarks, 'like_count'):,} likes, "
        f"{metric_total(bookmarks, 'retweet_count'):,} reposts, "
        f"{metric_total(bookmarks, 'reply_count'):,} replies, "
        f"{metric_total(bookmarks, 'quote_count'):,} quotes",
        "",
        "## Executive Read",
        "",
        f"The strongest bookmark themes are {category_sentence}. "
        f"Your saved material comes from {len(author_counts):,} distinct authors"
        + (f" and links out to {len(domain_counts):,} distinct external domains." if domain_counts else "."),
        "",
    ]

    if category_counts:
        lines += [
            "## Theme Breakdown",
            "",
            "| Theme | Count | Share | Representative saves |",
            "| --- | ---: | ---: | --- |",
        ]
        examples_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for bookmark in sorted(bookmarks, key=engagement_score, reverse=True):
            category = bookmark.get("category") or categorize_bookmark(bookmark)
            if len(examples_by_category[category]) < 2:
                examples_by_category[category].append(bookmark)
        for category, count in category_counts.most_common():
            examples = "<br>".join(format_bookmark_link(item, 82) for item in examples_by_category[category])
            lines.append(f"| {md_cell(category)} | {count:,} | {percent(count, total)} | {examples} |")
        lines.append("")

    if author_counts:
        lines += [
            "## Top Authors",
            "",
            "| Author | Saved posts | Share |",
            "| --- | ---: | ---: |",
        ]
        for author, count in author_counts.most_common(15):
            lines.append(f"| {md_cell(author, 90)} | {count:,} | {percent(count, total)} |")
        lines.append("")

    if domain_counts:
        lines += [
            "## Link Map",
            "",
            "| Domain | Links |",
            "| --- | ---: |",
        ]
        for domain, count in domain_counts.most_common(20):
            lines.append(f"| {md_cell(domain, 90)} | {count:,} |")
        lines.append("")

    if month_counts:
        lines += [
            "## Time Pattern",
            "",
            "| Month | Saved posts published in month |",
            "| --- | ---: |",
        ]
        for month, count in month_counts.most_common(18):
            lines.append(f"| {month} | {count:,} |")
        lines.append("")

    if media_counts or lang_counts:
        lines += [
            "## Content Mix",
            "",
            "| Signal | Top values |",
            "| --- | --- |",
            f"| Languages | {md_cell(', '.join(f'{key}: {value}' for key, value in lang_counts.most_common(8)), 180)} |",
            f"| Media types | {md_cell(', '.join(f'{key}: {value}' for key, value in media_counts.most_common(8)) or 'none detected', 180)} |",
            "",
        ]

    engaged = top_bookmarks(bookmarks, 15)
    if engaged:
        lines += [
            "## High-Signal Saves",
            "",
            "| Score | Author | Post |",
            "| ---: | --- | --- |",
        ]
        for bookmark in engaged:
            lines.append(
                f"| {engagement_score(bookmark):,.0f} | {md_cell(author_label(bookmark.get('author') or {}), 70)} | "
                f"{format_bookmark_link(bookmark)} |"
            )
        lines.append("")

    recent = top_bookmarks(bookmarks, 10, newest=True)
    if recent:
        lines += [
            "## Newest Saved Posts",
            "",
            "| Date | Author | Post |",
            "| --- | --- | --- |",
        ]
        for bookmark in recent:
            parsed = parse_datetime(bookmark.get("created_at"))
            date_label = parsed.strftime("%Y-%m-%d") if parsed else "unknown"
            lines.append(
                f"| {date_label} | {md_cell(author_label(bookmark.get('author') or {}), 70)} | "
                f"{format_bookmark_link(bookmark)} |"
            )
        lines.append("")

    oldest_items = top_bookmarks(bookmarks, 10, oldest=True)
    if oldest_items:
        lines += [
            "## Oldest Saves",
            "",
            "| Date | Author | Post |",
            "| --- | --- | --- |",
        ]
        for bookmark in oldest_items:
            parsed = parse_datetime(bookmark.get("created_at"))
            date_label = parsed.strftime("%Y-%m-%d") if parsed else "unknown"
            lines.append(
                f"| {date_label} | {md_cell(author_label(bookmark.get('author') or {}), 70)} | "
                f"{format_bookmark_link(bookmark)} |"
            )
        lines.append("")

    if index_limit != 0 and bookmarks:
        limit = min(index_limit, len(bookmarks))
        lines += [
            f"## Bookmark Index ({limit:,} of {len(bookmarks):,})",
            "",
            "| Date | Category | Author | Post |",
            "| --- | --- | --- | --- |",
        ]
        sorted_by_date = top_bookmarks(bookmarks, limit, newest=True)
        for bookmark in sorted_by_date:
            parsed = parse_datetime(bookmark.get("created_at"))
            date_label = parsed.strftime("%Y-%m-%d") if parsed else "unknown"
            category = bookmark.get("category") or categorize_bookmark(bookmark)
            lines.append(
                f"| {date_label} | {md_cell(category, 42)} | {md_cell(author_label(bookmark.get('author') or {}), 56)} | "
                f"{format_bookmark_link(bookmark, 100)} |"
            )
        if limit < len(bookmarks):
            lines += [
                "",
                f"_Index truncated to {limit:,} rows. Re-run with `--index-limit {len(bookmarks)}` or `--index-limit 0` to omit it._",
            ]
        lines.append("")

    lines += [
        "## Files",
        "",
        "- The JSON archive contains every fetched bookmark and raw API payload fields kept by the utility.",
        "- The CSV export is a spreadsheet-friendly index for sorting, filtering, and tagging.",
        "",
    ]
    return "\n".join(lines)


def write_report_from_payload(payload: dict[str, Any], output: Path, index_limit: int) -> None:
    # Recompute categories so reports remain compatible with older archive files.
    for bookmark in payload.get("bookmarks", []) or []:
        bookmark["category"] = bookmark.get("category") or categorize_bookmark(bookmark)
        bookmark["engagement_score"] = bookmark.get("engagement_score") or engagement_score(bookmark)
    ensure_parent(output)
    output.write_text(build_report(payload, index_limit), encoding="utf-8")
    eprint(f"Wrote report to {output}")


def require_client_id(args: argparse.Namespace) -> str:
    client_id = args.client_id or os.getenv("X_CLIENT_ID")
    if not client_id:
        raise RuntimeError("Missing X OAuth client ID. Pass --client-id or set X_CLIENT_ID.")
    return client_id


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--client-id", default=os.getenv("X_CLIENT_ID"), help="X OAuth 2.0 Client ID.")
    parser.add_argument("--client-secret", default=os.getenv("X_CLIENT_SECRET"), help="X OAuth Client Secret, if using a confidential app.")
    parser.add_argument("--redirect-uri", default=os.getenv("X_REDIRECT_URI", DEFAULT_REDIRECT_URI), help=f"OAuth callback URI. Default: {DEFAULT_REDIRECT_URI}")
    parser.add_argument("--token-file", default=os.getenv("X_TOKEN_FILE", ".x_tokens.json"), help="Where to save the OAuth access/refresh token.")
    parser.add_argument("--scopes", default=os.getenv("X_SCOPES", " ".join(DEFAULT_SCOPES)), help="Space- or comma-separated OAuth scopes.")
    parser.add_argument("--no-interactive-auth", action="store_true", help="Fail instead of opening OAuth if no saved token exists.")
    parser.add_argument("--no-wait-rate-limit", action="store_true", help="Fail immediately on HTTP 429 instead of waiting for reset.")
    parser.add_argument("--max-rate-limit-sleep", type=int, default=20 * 60, help="Maximum seconds to sleep for a rate-limit reset.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch all X bookmarks via the official API and write a local analysis report.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Authorize with X and save an OAuth token.")
    add_auth_args(auth_parser)
    auth_parser.add_argument("--no-browser", action="store_true", help="Print the authorize URL without opening a browser.")
    auth_parser.add_argument("--manual-callback", action="store_true", help="Paste the callback URL manually instead of listening locally.")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch bookmarks and write JSON/CSV.")
    add_auth_args(fetch_parser)
    fetch_parser.add_argument("--user-id", help="Authenticated X user ID. Defaults to /2/users/me.")
    fetch_parser.add_argument("--output", default="data/x-bookmarks.json", help="JSON archive output path.")
    fetch_parser.add_argument("--csv", default="data/x-bookmarks.csv", help="CSV output path. Pass an empty string to skip.")
    fetch_parser.add_argument("--limit", type=int, default=0, help="Fetch only the first N bookmarks for testing. Default: all.")
    fetch_parser.add_argument("--max-results", type=int, default=100, help="Bookmarks per API page, 1-100.")
    fetch_parser.add_argument("--pagination-token", help="Resume from a specific X pagination token.")

    report_parser = subparsers.add_parser("report", help="Build a Markdown report from an existing JSON archive.")
    report_parser.add_argument("--input", default="data/x-bookmarks.json", help="Input JSON archive.")
    report_parser.add_argument("--output", default="reports/x-bookmark-report.md", help="Markdown report path.")
    report_parser.add_argument("--index-limit", type=int, default=200, help="Rows in the report index. Use 0 to omit.")

    run_parser = subparsers.add_parser("run", help="Fetch bookmarks and immediately write the report.")
    add_auth_args(run_parser)
    run_parser.add_argument("--user-id", help="Authenticated X user ID. Defaults to /2/users/me.")
    run_parser.add_argument("--json", default="data/x-bookmarks.json", help="JSON archive output path.")
    run_parser.add_argument("--csv", default="data/x-bookmarks.csv", help="CSV output path. Pass an empty string to skip.")
    run_parser.add_argument("--report", default="reports/x-bookmark-report.md", help="Markdown report path.")
    run_parser.add_argument("--index-limit", type=int, default=200, help="Rows in the report index. Use 0 to omit.")
    run_parser.add_argument("--limit", type=int, default=0, help="Fetch only the first N bookmarks for testing. Default: all.")
    run_parser.add_argument("--max-results", type=int, default=100, help="Bookmarks per API page, 1-100.")
    run_parser.add_argument("--pagination-token", help="Resume from a specific X pagination token.")

    safari_parser = subparsers.add_parser("safari-import", help="Import bookmarks from the signed-in Safari X bookmarks timeline.")
    safari_parser.add_argument("--json", default="data/x-bookmarks.json", help="JSON archive output path.")
    safari_parser.add_argument("--csv", default="data/x-bookmarks.csv", help="CSV output path. Pass an empty string to skip.")
    safari_parser.add_argument("--report", default="reports/x-bookmark-report.md", help="Markdown report path.")
    safari_parser.add_argument("--index-limit", type=int, default=200, help="Rows in the report index. Use 0 to omit.")
    safari_parser.add_argument("--limit", type=int, default=0, help="Import only the first N bookmarks. Default: all found before the scroll limit.")
    safari_parser.add_argument("--max-scrolls", type=int, default=400, help="Maximum Safari timeline scrolls.")
    safari_parser.add_argument("--idle-rounds", type=int, default=8, help="Stop after this many no-progress bottom checks.")
    safari_parser.add_argument("--scroll-pause", type=float, default=0.85, help="Seconds to wait after each Safari scroll.")
    safari_parser.add_argument("--scroll-step", type=float, default=0.85, help="Viewport multiples to scroll each step.")
    safari_parser.add_argument("--stop-after-known", type=int, default=0, help="Stop after this many consecutive bookmarks already in the archive (newest-first means a known run-on = boundary reached). Set 0 to disable.")

    ui_parser = subparsers.add_parser("ui", help="Start the local web UI.")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host for the local UI server.")
    ui_parser.add_argument("--port", type=int, default=8787, help="Port for the local UI server.")
    ui_parser.add_argument("--no-open", action="store_true", help="Do not open the UI in a browser.")

    return parser


def run_command(args: argparse.Namespace) -> None:
    if args.command == "auth":
        authorize(
            client_id=require_client_id(args),
            client_secret=args.client_secret,
            redirect_uri=args.redirect_uri,
            scopes=parse_scopes(args.scopes),
            token_file=Path(args.token_file),
            no_browser=args.no_browser,
            manual_callback=args.manual_callback,
        )
        return
    if args.command == "fetch":
        if args.csv == "":
            args.csv = None
        fetch_bookmarks(args)
        return
    if args.command == "report":
        payload = read_json(Path(args.input))
        write_report_from_payload(payload, Path(args.output), args.index_limit)
        return
    if args.command == "run":
        args.output = args.json
        if args.csv == "":
            args.csv = None
        payload = fetch_bookmarks(args)
        write_report_from_payload(payload, Path(args.report), args.index_limit)
        return
    if args.command == "safari-import":
        args.output = args.json
        if args.csv == "":
            args.csv = None
        payload = import_bookmarks_from_safari(args)
        if args.report:
            write_report_from_payload(payload, Path(args.report), args.index_limit)
        return
    if args.command == "ui":
        import x_bookmark_ui

        x_bookmark_ui.serve(args.host, args.port, open_browser=not args.no_open)
        return
    raise RuntimeError(f"Unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run_command(args)
    except KeyboardInterrupt:
        eprint("Cancelled.")
        return 130
    except Exception as exc:
        eprint(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
