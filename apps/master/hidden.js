// apps/master/hidden.js — per-browser "hide this teammate" filter.
//
// Convenience UI state only: which teammate labels this manager's browser
// should omit from lists. NEVER an authorization decision — the uplink still
// mirrors, the account still exists, another browser still sees them.
//
// PURE LEAF — zero imports, no DOM, no localStorage, no network. main.js
// owns persistence; this file only parses, dumps, and filters. Importable
// by plain node so tests/unit/master_hidden.test.js can hold the rules still.

export function parseHidden(raw) {
  if (typeof raw !== 'string' || !raw) return new Set();
  let parsed;
  try { parsed = JSON.parse(raw); } catch (e) { return new Set(); }
  if (!Array.isArray(parsed)) return new Set();
  const out = new Set();
  for (const item of parsed) {
    if (typeof item === 'string' && item) out.add(item);
  }
  return out;
}

export function dumpHidden(set) {
  return JSON.stringify([...set]);
}

export function hide(set, label) {
  const next = new Set(set);
  if (typeof label === 'string' && label) next.add(label);
  return next;
}

export function unhide(set, label) {
  const next = new Set(set);
  next.delete(label);
  return next;
}

export function visibleFeed(feed, hidden) {
  return (feed || []).filter(row => !hidden.has(row.userLabel));
}

export function visibleContacts(contacts, hidden) {
  return (contacts || []).filter(ct => !hidden.has(ct.label));
}

export function visibleUsers(byUser, hidden) {
  return [...byUser].filter(([label]) => !hidden.has(label));
}
