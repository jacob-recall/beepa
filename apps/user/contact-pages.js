// Read complete imported-contact pages. A failed later page must never look
// like a complete smaller address book when planning contact overrides.
export async function loadContactPages(fetchPage, validate) {
  const rows = new Map();
  const seen = new Set();
  let cursor = null;
  do {
    let response;
    try { response = await fetchPage(cursor); }
    catch { throw new Error('the local contacts helper is not running'); }
    if (!response.ok) throw new Error('the local contacts helper returned ' + response.status);
    const body = await response.json().catch(() => null);
    if (!body || !Array.isArray(body.contacts)) {
      throw new Error('the local contacts helper returned an invalid page');
    }
    for (const row of validate(body)) rows.set(row.key, row);
    const next = body.next_cursor;
    if (next === undefined || next === null) return [...rows.values()];
    if (typeof next !== 'string' || !next || next.length > 4096) {
      throw new Error('the local contacts helper returned an invalid cursor');
    }
    if (seen.has(next)) throw new Error('the local contacts helper repeated a pagination cursor');
    seen.add(next);
    cursor = next;
  } while (cursor !== null);
  return [...rows.values()];
}
