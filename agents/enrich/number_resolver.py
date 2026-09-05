"""Conversation number enrichment — read-only resolver (A1).

Maps each 1:1 conversation's Matrix room_id to the counterpart's real
E.164 phone number (or, for iMessage, an email address), by reading the
three bridge databases directly. This is a pure READ path: every query is
a SELECT, nothing here ever writes to a bridge database. A later task
exposes this over a loopback endpoint; the Hub uses the result to
auto-merge contacts that share a number.

Sources:
  - WhatsApp   — postgres `mautrix_whatsapp` in container matrix-wa-postgres-1.
                 1:1 portals carry a `lid-<LID>` privacy id in
                 other_user_id; the real phone is looked up via
                 whatsmeow_lid_map(lid, pn). ~96% resolve — the rest are
                 true WhatsApp privacy numbers with no phone mapping, and
                 are skipped, not guessed.
  - Google Messages — postgres `mautrix_gmessages`, same container.
                 portal -> ghost gives a `metadata->>'phone'` value that's
                 sometimes a real E.164 number, sometimes an SMS short
                 code or an RCS business id shaped `<slug>@rbm.goog`.
                 Only strict E.164 values are kept.
  - iMessage   — sqlite `imessage/state.db` (bridge-local map, NOT macOS
                 chat.db). `map.chat_id` is shaped `any;-;<handle>` for a
                 1:1 chat; the handle is a phone or an email. Group chats
                 (any other chat_id shape) are skipped.

Normalization (see `_norm_phone`): strict E.164 is
`^\\+[1-9]\\d{6,14}$`. A bare digit string with no leading '+' is only
ever treated as ALREADY carrying a country code (WhatsApp's `pn` and a
"00"-prefixed international dial string both look like this) — never as
a national number missing its country code. A country code is never
invented; if the digits can't be resolved to valid E.164 as given, the
value is dropped. This is the same "never fabricate" philosophy as
`agents/contacts/import_macos.py`'s `_normalize_phone`, minus the
system-region guess (there is no region signal available here).

Privacy: no phone number or email is ever logged or printed anywhere in
this module. The `__main__` block prints only per-source resolved/total
counts.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess

log = logging.getLogger("number_resolver")

from pathlib import Path
import sys
CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_ROOT))
from phone_numbers import normalize_phone
from install_config import compose_prefix
REPO = os.environ.get('BEEPA_INSTALL_ROOT', str(CODE_ROOT))
WHATSAPP_CONTAINER = 'postgres'  # Compose service, not a generated container name
WHATSAPP_DB = 'mautrix_whatsapp'
GMESSAGES_CONTAINER = 'postgres'
GMESSAGES_DB = 'mautrix_gmessages'
PG_USER = 'matrix'
IMESSAGE_STATE_DB = os.path.join(REPO, 'imessage', 'state.db')
_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


def _norm_phone(raw):
    """Freeform values require an explicit international prefix here."""
    return normalize_phone(raw)


def _norm_wa_pn(raw):
    """WhatsApp pn is a provider-defined country-code-bearing digit ID."""
    return normalize_phone(raw, provider=True)


def _parse_imessage_handle(chat_id):
    """Parse a `map.chat_id` value for a 1:1 iMessage chat.

    Returns ('+E.164', 'phone') or ('lower@email', 'email') for the
    `any;-;<handle>` shape, else None (including any group-chat shape).
    """
    if not isinstance(chat_id, str):
        return None
    parts = chat_id.split(";-;")
    if len(parts) != 2 or parts[0] != "any":
        return None
    handle = parts[1].strip()
    if not handle:
        return None
    if handle.startswith("+"):
        e164 = _norm_phone(handle)
        return (e164, "phone") if e164 else None
    if _EMAIL_RE.match(handle):
        return (handle.lower(), "email")
    return None


def _psql(container, db, query, timeout=15):
    """Run a read-only query via `docker exec psql -tAc`. Returns the raw
    stdout lines (tab-separated fields), or None on any failure. Never
    writes; the query string is caller-controlled (module constants
    only, no external input is interpolated into SQL here)."""
    argv = compose_prefix(REPO, CODE_ROOT) + ["exec", "-T", container, "psql", "-U", PG_USER, "-d", db, "-tAc", query]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("psql call failed for %s/%s: %s", container, db, type(e).__name__)
        return None
    if proc.returncode != 0:
        log.warning("psql exited %d for %s/%s", proc.returncode, container, db)
        return None
    out = proc.stdout.decode("utf-8", "replace")
    return out.split("\n") if out else []


def resolve_whatsapp():
    """{room_id: {"value": e164, "kind": "phone", "source": "whatsapp"}}"""
    query = (
        "SELECT p.mxid, m.pn FROM portal p "
        "JOIN whatsmeow_lid_map m ON m.lid = split_part(p.other_user_id,'-',2) "
        "WHERE p.room_type='dm' AND p.mxid IS NOT NULL"
    )
    lines = _psql(WHATSAPP_CONTAINER, WHATSAPP_DB, query)
    if lines is None:
        log.warning("whatsapp resolver unavailable, returning empty")
        return {}
    out = {}
    for line in lines:
        if not line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        room_id, pn = parts
        room_id = room_id.strip()
        e164 = _norm_wa_pn(pn.strip())
        if room_id and e164:
            out[room_id] = {"value": e164, "kind": "phone", "source": "whatsapp"}
    return out


def resolve_gmessages():
    """{room_id: {"value": e164, "kind": "phone", "source": "gmessages"}}"""
    query = (
        "SELECT p.mxid, g.metadata->>'phone' AS phone FROM portal p "
        "JOIN ghost g ON g.id = p.other_user_id WHERE p.mxid IS NOT NULL"
    )
    lines = _psql(GMESSAGES_CONTAINER, GMESSAGES_DB, query)
    if lines is None:
        log.warning("gmessages resolver unavailable, returning empty")
        return {}
    out = {}
    for line in lines:
        if not line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        room_id, phone = parts
        room_id, phone = room_id.strip(), phone.strip()
        # Strict E.164 match only — deliberately NOT run through
        # _norm_phone's bare-digit path here, since that path assumes a
        # bare digit string is a phone; an SMS short code or an
        # `<slug>@rbm.goog` RCS id could otherwise survive non-digit
        # stripping and be misread as one.
        normalized = _norm_phone(phone) if phone.startswith("+") else None
        if room_id and normalized:
            out[room_id] = {"value": normalized, "kind": "phone", "source": "gmessages"}
    return out


def resolve_imessage():
    """{room_id: {"value": e164-or-email, "kind": "phone"|"email", "source": "imessage"}}"""
    try:
        con = sqlite3.connect(
            "file:%s?mode=ro" % IMESSAGE_STATE_DB, uri=True, timeout=15
        )
    except sqlite3.Error as e:
        log.warning("imessage state.db unavailable: %s", type(e).__name__)
        return {}
    out = {}
    try:
        cur = con.execute("SELECT chat_id, room_id FROM map")
        for chat_id, room_id in cur:
            parsed = _parse_imessage_handle(chat_id)
            if parsed is None or not room_id:
                continue
            value, kind = parsed
            out[room_id] = {"value": value, "kind": kind, "source": "imessage"}
    except sqlite3.Error as e:
        log.warning("imessage query failed: %s", type(e).__name__)
        return {}
    finally:
        con.close()
    return out


def resolve_all():
    """Merge all three sources. A room_id is expected to be owned by
    exactly one bridge; if a conflict occurs anyway, the first value
    wins and the conflict is logged (room_id only, never the value)."""
    merged = {}
    for name, fn in (
        ("whatsapp", resolve_whatsapp),
        ("gmessages", resolve_gmessages),
        ("imessage", resolve_imessage),
    ):
        for room_id, entry in fn().items():
            if room_id in merged:
                log.warning("room_id %s already resolved by %s, %s kept first",
                            room_id, merged[room_id]["source"], name)
                continue
            merged[room_id] = entry
    return merged


def _total_portals():
    """Best-effort denominators for the printed counts (total 1:1
    portals/chats per source), for a resolved/total display. Failures
    fall back to just the resolved count (no "/total")."""
    totals = {}
    lines = _psql(WHATSAPP_CONTAINER, WHATSAPP_DB,
                  "SELECT count(*) FROM portal WHERE room_type='dm' AND mxid IS NOT NULL")
    if lines and lines[0].strip().isdigit():
        totals["whatsapp"] = int(lines[0].strip())
    lines = _psql(GMESSAGES_CONTAINER, GMESSAGES_DB,
                  "SELECT count(*) FROM portal p JOIN ghost g ON g.id = p.other_user_id "
                  "WHERE p.mxid IS NOT NULL")
    if lines and lines[0].strip().isdigit():
        totals["gmessages"] = int(lines[0].strip())
    try:
        con = sqlite3.connect("file:%s?mode=ro" % IMESSAGE_STATE_DB, uri=True, timeout=15)
        try:
            totals["imessage"] = con.execute("SELECT count(*) FROM map").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return totals


if __name__ == "__main__":
    totals = _total_portals()
    results = {
        "whatsapp": resolve_whatsapp(),
        "gmessages": resolve_gmessages(),
        "imessage": resolve_imessage(),
    }
    parts = []
    grand_resolved = 0
    grand_total = 0
    for name in ("whatsapp", "gmessages", "imessage"):
        resolved = len(results[name])
        grand_resolved += resolved
        if name in totals:
            parts.append("%s: %d/%d" % (name, resolved, totals[name]))
            grand_total += totals[name]
        else:
            parts.append("%s: %d" % (name, resolved))
    if grand_total:
        parts.append("total: %d/%d" % (grand_resolved, grand_total))
    else:
        parts.append("total: %d" % grand_resolved)
    print(", ".join(parts))
