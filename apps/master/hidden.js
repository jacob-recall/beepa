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

// ---- per-browser display names for teammates (convenience only) ----------
// The VERIFIED label (space creator's localpart) stays the key everywhere; a
// name is purely how this browser shows it. Never an identity decision.
export function parseNames(raw) {
  if (typeof raw !== 'string' || !raw) return new Map();
  let parsed;
  try { parsed = JSON.parse(raw); } catch (e) { return new Map(); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return new Map();
  const out = new Map();
  for (const [label, name] of Object.entries(parsed)) {
    if (typeof label === 'string' && label && typeof name === 'string' && name.trim()) out.set(label, name.trim().slice(0, 64));
  }
  return out;
}

export function dumpNames(map) {
  return JSON.stringify(Object.fromEntries(map));
}

export function rename(map, label, name) {
  const next = new Map(map);
  const clean = typeof name === 'string' ? name.trim().slice(0, 64) : '';
  if (typeof label !== 'string' || !label) return next;
  if (clean && clean !== label) next.set(label, clean); else next.delete(label);
  return next;
}

export function displayName(map, label) {
  if (typeof label !== 'string' || !label) return '';
  const n = map && typeof map.get === 'function' ? map.get(label) : undefined;
  return (typeof n === 'string' && n) ? n : label;
}

// Teammates who currently have at least one shared conversation — what the
// icon rail shows. Hidden labels are excluded; zero-shared ones stay reachable
// from the Teammates list.
export function sharingUsers(byUser, hidden) {
  return visibleUsers(byUser, hidden).filter(([, convos]) => Array.isArray(convos) && convos.length > 0);
}
