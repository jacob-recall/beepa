// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

// ---- tiny DOM helpers (no HTML-string sinks anywhere) ----
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
// Strip C0 controls (keep \n), bidi overrides, zero-width chars; clamp length.
function sanitize(s) {
  if (typeof s !== 'string') return '';
  return s.replace(/[\x00-	-‪-‮⁦-⁩​-‏﻿]/g, '')
          .slice(0, 4000);
}
// D-7: single-line variant for rows / titles / subs / badges / previews.
function sanitizeLine(s) {
  if (typeof s !== 'string') return '';
  return sanitize(s).replace(/[\r\n\t]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 64);
}
const txn = () => 'hub-' + crypto.randomUUID();

// ---- linkify (message bodies) ----
// Conservative by design: ONLY explicit http(s):// URLs become links — no bare
// domains, no other schemes (javascript:/data: can never match the regex, and
// new URL() + the protocol allowlist below is the authoritative gate anyway).
// The link's VISIBLE TEXT is the URL string itself, so a deceptive text/href
// mismatch is impossible. Input is expected to be sanitize()'d already
// (controls/bidi/zero-width stripped), keeping RTL-spoofed display out.
const LINK_RE = /https?:\/\/[^\s<>"']+/g;
const TRAIL_RE = /[.,!?;:)\]}]+$/;

// Pure tokenizer (node-testable, no DOM): text -> [{kind:'text'|'link', text, href?}]
function linkifyTokens(s) {
  const out = [];
  if (typeof s !== 'string' || !s) return out;
  let last = 0;
  for (const m of s.matchAll(LINK_RE)) {
    if (m.index > last) out.push({ kind: 'text', text: s.slice(last, m.index) });
    const raw = m[0];
    // Trailing sentence punctuation is almost never part of the URL; keep a
    // trailing ')' only when the URL contains a matching '(' (wiki-style).
    let url = raw.replace(TRAIL_RE, (t) => {
      let keep = '';
      for (const ch of t) {
        if (ch === ')' && (url_par(raw, t, keep))) { keep += ch; continue; }
        break;
      }
      return keep;
    });
    let ok = false, href = '';
    try {
      const u = new URL(url);
      ok = (u.protocol === 'http:' || u.protocol === 'https:');
      href = u.href;
    } catch (e) { ok = false; }
    if (ok) {
      out.push({ kind: 'link', text: url, href });
      if (url.length < raw.length) out.push({ kind: 'text', text: raw.slice(url.length) });
    } else {
      out.push({ kind: 'text', text: raw });
    }
    last = m.index + raw.length;
  }
  if (last < s.length) out.push({ kind: 'text', text: s.slice(last) });
  return out;
}
function url_par(raw, trail, kept) {
  // keep ')' if the candidate URL (minus the whole trail, plus what we kept)
  // has more '(' than ')'.
  const core = raw.slice(0, raw.length - trail.length) + kept;
  return (core.split('(').length - 1) > (core.split(')').length - 1);
}

// DOM applier: append the tokens into `parent` as text nodes + <a> elements.
// createElement + property assignment only — no HTML-string sinks; links open
// in a new tab with the opener severed.
function appendLinkified(parent, s) {
  for (const t of linkifyTokens(s)) {
    if (t.kind === 'link') {
      const a = document.createElement('a');
      a.href = t.href;
      a.textContent = t.text;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      parent.appendChild(a);
    } else {
      parent.appendChild(document.createTextNode(t.text));
    }
  }
}

export { $, el, sanitize, sanitizeLine, txn, linkifyTokens, appendLinkified };
