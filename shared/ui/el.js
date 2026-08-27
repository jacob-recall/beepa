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

export { $, el, sanitize, sanitizeLine, txn };
