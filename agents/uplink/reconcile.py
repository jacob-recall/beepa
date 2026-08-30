"""Pure reconcile + idempotency logic for the uplink daemon.

PLAN-MASTER-SYNC.md §5.4 (reconcile) and §8.2 (watermark & idempotency).
No I/O, no side effects at import — unit-tested in tests/unit/uplink_reconcile.test.py.
"""

# consent is a sibling pure module (agents/uplink/consent.py): the contact-share
# resolver, no I/O, no import-time side effects. It is the SINGLE authority on
# whether a source's contacts may leave the machine, so the contact-selection
# planner below calls it rather than re-deriving the rule.
import consent


def reconcile_decisions(desired_shared, existing_mirrors):
    """Decide per local room what the reconciler must do.

    desired_shared   : { local_room_id: bool }  effective shared-state (§4).
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

    for rid, shared in desired.items():
        if shared and rid not in mirrors:
            create.append(rid)
        elif shared and rid in mirrors:
            keep.append(rid)

    for rid in mirrors:
        if not desired.get(rid, False):
            delete.append(rid)

    return {
        "create": sorted(create),
        "delete": sorted(delete),
        "keep": sorted(keep),
    }


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


def select_contacts_to_mirror(rows, cursor, policy):
    """Which contact rows must be pushed to the master this pass (§12 phase 5).

    rows   : store rows (dicts with at least 'source' and 'version'), any order.
    cursor : the last mirrored global version (contact_cursor); only rows with
             version > cursor are candidates.
    policy : a normalized contact-share policy (consent.normalize_contact_policy).

    Returns the candidate rows whose SOURCE resolves to shared under the policy,
    in ascending version order. A row that resolves NOT shared is OMITTED, so it
    is never handed to the caller's PUT path and never leaves the machine. This
    is the consent gate for contacts, kept pure so it is unit-testable without a
    live homeserver; the daemon still advances its cursor over the skipped rows
    it does not receive here.
    """
    out = []
    for row in sorted(rows or [], key=lambda r: (r.get("version") or 0)):
        v = row.get("version")
        if v is None or v <= cursor:
            continue
        if consent.resolve_contact_share(row.get("source"), policy).get("shared"):
            out.append(row)
    return out


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
