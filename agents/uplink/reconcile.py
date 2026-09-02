"""Pure reconcile + idempotency logic for the uplink daemon.

PLAN-MASTER-SYNC.md §5.4 (reconcile) and §8.2 (watermark & idempotency).
No I/O, no side effects at import — unit-tested in tests/unit/uplink_reconcile.test.py.
"""

# consent is a sibling pure module (agents/uplink/consent.py): the contact-share
# resolver, no I/O, no import-time side effects. It is the SINGLE authority on
# whether a source's contacts may leave the machine, so the contact-selection
# planner below calls it rather than re-deriving the rule.
import consent


# D2b (direct-share-level plan): a desired-map value is the per-room LEVEL, not
# a bool — 'share' and 'direct' both mirror, 'private' does not. The mirroring
# decision is still exactly "level != private"; what the level ADDS is the
# com.jkali.share_level stamp the master app reads (D4), which must be re-PUT
# when a kept mirror's level flips in either direction.
SHARE_LEVELS = ("share", "direct")


def level_is_shared(level):
    """Whether a desired-map value means "mirror this room" (D2b).

    Accepts the per-room level string ('share'|'direct'|'private') and the
    pre-D2b boolean, so both call shapes resolve identically. A string is
    matched against the two sharing levels EXACTLY: an unrecognized level is
    NOT shared, the same fail-closed rule consent.effective_level() applies
    (never `bool(level)`, under which 'private' — a non-empty string — would
    read as shared).
    """
    if isinstance(level, str):
        return level in SHARE_LEVELS
    return bool(level)


def reconcile_decisions(desired_shared, existing_mirrors):
    """Decide per local room what the reconciler must do.

    desired_shared   : { local_room_id: level|bool }  effective share level
                       (§4 / D2b) — see level_is_shared().
    existing_mirrors : iterable of local_room_ids that already have a mirror.

    Returns { 'create': [...], 'delete': [...], 'keep': [...] } with room ids in
    deterministic (sorted) order:
      - create : effectively shared, no mirror yet    (share -> create)
      - delete : has a mirror, no longer shared        (unshare -> delete)
      - keep   : shared and already mirrored           (tail continues)

    A room that is neither shared nor mirrored is a no-op and appears nowhere.
    """
    mirrors = set(existing_mirrors or ())
    desired = desired_shared or {}
    create, delete, keep = [], [], []

    for rid, level in desired.items():
        shared = level_is_shared(level)
        if shared and rid not in mirrors:
            create.append(rid)
        elif shared and rid in mirrors:
            keep.append(rid)

    for rid in mirrors:
        if not level_is_shared(desired.get(rid, False)):
            delete.append(rid)

    return {
        "create": sorted(create),
        "delete": sorted(delete),
        "keep": sorted(keep),
    }


def plan_level_restamp(desired, stamped, keep):
    """Which KEPT mirrors need their com.jkali.share_level state re-PUT (D2b).

    desired : { local_room_id: level }   this pass's resolved levels.
    stamped : { local_room_id: level }   the level last stamped on each mirror
                                         (state.db mirror_rooms.stamped_level;
                                         None/absent for a mirror created
                                         before D2b shipped).
    keep    : the kept-mirror ids from reconcile_decisions().

    Returns a sorted list of (local_room_id, level) for rooms whose CURRENT
    sharing level differs from the last stamped one — promotion (share ->
    direct) and demotion (direct -> share) alike, plus the one-time backfill
    of a pre-D2b mirror that carries no stamp yet. Pure, and a diff rather
    than a per-pass write: once stamped, a room drops out until it changes.
    """
    stamped = stamped or {}
    out = []
    for rid in (keep or []):
        level = (desired or {}).get(rid)
        if level in SHARE_LEVELS and stamped.get(rid) != level:
            out.append((rid, level))
    return sorted(out)


def select_new_events(event_ids, mapped_ids):
    """Filter an ordered list of local event ids down to the un-mirrored ones.

    event_ids : local event ids in stream order (backfill or /sync timeline).
    mapped_ids: set of local_event_ids already present in event_map.

    Preserves order and drops any event already mapped. This is the idempotency
    guard: on restart or a replayed /sync batch, already-forwarded events are
    skipped, so no duplicate is ever posted to the master. Also de-duplicates
    repeats within the same batch.
    """
    mapped = set(mapped_ids or ())
    out = []
    seen = set()
    for eid in (event_ids or []):
        if eid in mapped or eid in seen:
            continue
        seen.add(eid)
        out.append(eid)
    return out


PUSH_CAP = 200  # per-pass push budget; tombstones are never capped


def plan_contact_mirror(rows, mirrored, policy, sources, overrides, push_cap=PUSH_CAP):
    """Plan one contact-mirror pass: a diff of desired-shared-and-live vs
    mirrored (§12 phase 5; pm_mng-q5u.2 backfill-on-enable).

    This is THE consent gate for address-book contacts. It replaces the old
    forward-only contact_cursor, which advanced over not-shared rows and so
    could never backfill a contact imported BEFORE its source was shared.

    rows     : store rows (dicts: source, network_id, version, deleted, ...),
               any order, deleted rows included.
    mirrored : {(source, network_id): mirrored_version} — the COMPLETE
               contact_mirror table. Deliberately NOT filtered by `sources`:
               a mirrored handle whose source is unknown/renamed/removed is no
               longer live-shared and is tombstoned, never stranded on the
               master.
    policy   : a normalized contact-share policy (consent.normalize_contact_policy).
    sources  : the daemon's known mirror sources (SOURCE_ID_TO_LABEL keys).
               Filters ONLY the rows: a row outside it can never be a push
               candidate and never counts as live-shared. Adding a store
               source means adding it there.
    overrides: the per-contact override map (consent.normalize_contact_overrides),
               '<source>|<network_id>' -> 'share'|'private'. A REQUIRED
               POSITIONAL parameter (F4): an unconverted call site must be a
               TypeError, never a silent widening back to source-only
               resolution. Pass None/{} explicitly for "no overrides".
               It is a BOOLEAN GATE ONLY — see the invariant below.
    push_cap : at most this many pushes per pass; the remainder is re-planned
               next pass, so a first backfill of a large address book resumes
               naturally without blocking the daemon's loop for minutes.

    Returns {"tombstone": [(source, network_id), sorted],
             "push":      [rows, ascending version, at most push_cap],
             "not_shared": live in-`sources` rows that resolved private,
             "pending":    shared pushes deferred by push_cap}.

    tombstone = mirrored handles minus live-shared handles (select_contacts_to_tombstone).
    push      = live (deleted=0) rows in `sources` that resolve shared under
                per-contact override > per-source > global
                AND (not mirrored OR mirrored_version != row version).
                `!=`, not `<`: version is a per-store change token, not a
                clock — a rebuilt contacts.db restarts at 1 and `<` would leave
                stale PII on the master forever.
    A not-shared row appears in neither list, so it never reaches a PUT.

    F4 INVARIANT — the override is a BOOLEAN GATE ONLY. It is consulted strictly
    AFTER the known-source allowlist and the `deleted` check (so an override can
    never resurrect an unknown source or a soft-deleted row), and NOTHING from
    the override key ever becomes pushed content: every field of a pushed
    contact comes from the store row (see uplink._put_contact). The key is only
    ever used to LOOK UP a boolean.
    """
    mirrored = dict(mirrored or {})
    known = set(sources or ())
    policy = policy or {}
    ovr = overrides if isinstance(overrides, dict) else {}
    live_shared = set()
    candidates = []
    not_shared = 0
    for row in rows or []:
        source = row.get("source")
        if source not in known:
            continue
        if row.get("deleted"):
            continue
        # Override lookup AFTER the two gates above (F4). A key the spec rejects
        # yields None -> inherit from source/global, never a widening.
        key = consent.contact_override_key(source, row.get("network_id"))
        override = ovr.get(key) if key is not None else None
        if not consent.resolve_contact_share(source, policy, override).get("shared"):
            not_shared += 1
            continue
        key = (source, row.get("network_id"))
        live_shared.add(key)
        if key not in mirrored or mirrored[key] != row.get("version"):
            candidates.append(row)
    candidates.sort(key=lambda r: (r.get("version") or 0))
    cap = max(0, int(push_cap)) if push_cap is not None else len(candidates)
    return {
        "tombstone": select_contacts_to_tombstone(mirrored.keys(), live_shared),
        "push": candidates[:cap],
        "not_shared": not_shared,
        "pending": max(0, len(candidates) - cap),
    }


def select_contacts_to_tombstone(mirrored, currently_shared):
    """Which already-mirrored contact handles must be tombstoned this pass.

    Revocation reconcile (pm_mng-q5u.1). The contact mirror is no longer purely
    forward-only: each pass diffs the DESIRED shared-and-live set against what is
    already on the master (contact_mirror) and removes the difference, the same
    reconcile shape conversations use in reconcile_decisions().

    mirrored         : iterable of (source, network_id) tuples that currently
                       have a LIVE master com.jkali.contact state event
                       (i.e. every row in contact_mirror).
    currently_shared : iterable of (source, network_id) tuples that resolve
                       shared under the current contact-share policy AND are
                       still live (not soft-deleted) in the store.

    Returns the sorted list of (source, network_id) handles to tombstone =
    mirrored MINUS currently_shared. A handle whose source has flipped to
    private, or whose contact was deleted, is mirrored-but-not-shared and so is
    selected; a still-shared (or re-shared) handle is in both sets and is left
    alone.

    Pure, and idempotent when the caller drops a handle from contact_mirror after
    the master confirms its tombstone: the handle then leaves `mirrored` and is
    never re-selected, so an already-tombstoned contact is not re-sent.
    """
    shared = set(currently_shared or ())
    return sorted(h for h in set(mirrored or ()) if h not in shared)


def next_watermark(current, candidate, confirmed):
    """Advance the per-room watermark ONLY after the master confirms receipt.

    current   : the last durably-synced position (may be None at first run).
    candidate : the position we would move to if delivery succeeded.
    confirmed : True iff the master returned 200 OK for every event up to here.

    Returns candidate when confirmed, else current unchanged. When the master is
    unreachable the caller passes confirmed=False, so the watermark never moves
    ahead of what the master actually holds (PLAN §7, §8.2, §11).
    """
    return candidate if confirmed else current
