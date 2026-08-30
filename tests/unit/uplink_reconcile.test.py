#!/usr/bin/env python3
"""Unit tests for the uplink's pure reconcile + idempotency logic.

Covers PLAN-MASTER-SYNC.md §5.4 / §8.2:
  - reconcile decisions: share -> create, unshare -> delete, kept, no-op
  - idempotency: no duplicate post on a replayed /sync batch or after restart
  - watermark: advances only after the master confirms; frozen while offline

Run: python3 tests/unit/uplink_reconcile.test.py  (exit 0 = all pass).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "agents", "uplink"))
import reconcile  # noqa: E402
from reconcile import (  # noqa: E402
    reconcile_decisions, select_new_events, next_watermark, select_contacts_to_mirror,
)
from consent import normalize_contact_policy  # noqa: E402

_pass = 0
_fail = 0
_failures = []


def eq(actual, expected, label):
    global _pass, _fail
    if actual == expected:
        _pass += 1
    else:
        _fail += 1
        _failures.append("%s: expected %r, got %r" % (label, expected, actual))


# ---- reconcile decisions --------------------------------------------------
# share -> create (no mirror yet); unshare -> delete (mirror exists, not shared);
# shared & mirrored -> keep; not shared & no mirror -> no-op (absent everywhere).
desired = {"!s:l": True, "!u:l": False, "!k:l": True, "!n:l": False}
existing = ["!u:l", "!k:l"]
plan = reconcile_decisions(desired, existing)
eq(plan["create"], ["!s:l"], "reconcile: share->create")
eq(plan["delete"], ["!u:l"], "reconcile: unshare->delete")
eq(plan["keep"], ["!k:l"], "reconcile: shared+mirrored->keep")
# no-op room appears nowhere
eq("!n:l" not in plan["create"] + plan["delete"] + plan["keep"], True, "reconcile: no-op absent")

# empty inputs
eq(reconcile_decisions({}, []), {"create": [], "delete": [], "keep": []}, "reconcile: empty")
eq(reconcile_decisions(None, None), {"create": [], "delete": [], "keep": []}, "reconcile: None inputs")

# a mirror for a room that dropped out of the desired set entirely -> delete
eq(reconcile_decisions({}, ["!gone:l"])["delete"], ["!gone:l"], "reconcile: vanished room deleted")

# deterministic ordering
eq(reconcile_decisions({"!b:l": True, "!a:l": True}, [])["create"], ["!a:l", "!b:l"], "reconcile: sorted create")


# ---- idempotency: select_new_events ---------------------------------------
# First sight of a batch: all events are new.
eq(select_new_events(["e1", "e2", "e3"], set()), ["e1", "e2", "e3"], "idempotency: fresh batch")
# Replay after e1,e2 already mapped (restart / re-sync): only e3 remains.
eq(select_new_events(["e1", "e2", "e3"], {"e1", "e2"}), ["e3"], "idempotency: replay skips mapped")
# Full replay: everything already mapped -> nothing re-posted (no dup).
eq(select_new_events(["e1", "e2", "e3"], {"e1", "e2", "e3"}), [], "idempotency: full replay -> no dup")
# Order preserved.
eq(select_new_events(["e3", "e1", "e2"], {"e1"}), ["e3", "e2"], "idempotency: order preserved")
# Duplicate within a single batch collapses to one.
eq(select_new_events(["e1", "e1", "e2"], set()), ["e1", "e2"], "idempotency: intra-batch dup collapsed")
eq(select_new_events([], {"e1"}), [], "idempotency: empty batch")
eq(select_new_events(None, None), [], "idempotency: None inputs")


# ---- restart-replay simulation --------------------------------------------
# Model event_map as a set; forwarding = post each new event then add to map.
# Run the same batch twice (simulating a crash+restart mid-way) and assert the
# master receives each event exactly once.
def forward(batch, event_map, master_posts):
    for eid in select_new_events(batch, event_map):
        master_posts.append(eid)   # "post to master"
        event_map.add(eid)         # persisted only after 200 OK
    return event_map, master_posts

emap = set()
posts = []
emap, posts = forward(["a", "b", "c"], emap, posts)   # first run
emap, posts = forward(["a", "b", "c"], emap, posts)   # restart: same batch re-delivered
eq(posts, ["a", "b", "c"], "restart: each event posted exactly once")

# Partial-progress crash: only 'a' got mapped before the crash; resume posts b,c.
emap2 = {"a"}
posts2 = []
emap2, posts2 = forward(["a", "b", "c"], emap2, posts2)
eq(posts2, ["b", "c"], "restart: resumes from partial progress, no dup of a")


# ---- watermark: advance only on master confirmation -----------------------
eq(next_watermark("p1", "p2", True), "p2", "watermark: advances on confirm")
eq(next_watermark("p1", "p2", False), "p1", "watermark: frozen when master unconfirmed")
eq(next_watermark(None, "p2", True), "p2", "watermark: first advance from None")
eq(next_watermark(None, "p2", False), None, "watermark: stays None while offline")
# Offline burst then reconnect: watermark holds across N failed cycles, then jumps.
wm = "p0"
for _ in range(5):
    wm = next_watermark(wm, "p9", False)   # master unreachable each cycle
eq(wm, "p0", "watermark: unchanged across an offline burst")
wm = next_watermark(wm, "p9", True)        # reconnect + confirm
eq(wm, "p9", "watermark: catches up after reconnect")


# ---- contact-share selection (Task 6) -------------------------------------
# The pure "which versions to push" planner: version > cursor AND the source
# resolves shared under the contact-share policy, in ascending version order.
# A not-shared source is omitted so it never reaches the daemon's PUT path.
def _crows(*versions, source="imessage"):
    return [{"source": source, "network_id": "h%d" % v, "kind": "phone",
             "display_name": None, "person_id": None, "deleted": 0, "version": v}
            for v in versions]


share_imsg = normalize_contact_policy({"sources": {"imessage": "share-all"}})
not_shared = normalize_contact_policy({"global": "private"})

# cursor=2, rows [1..5], policy shares imessage -> exactly 3,4,5 in order.
sel = select_contacts_to_mirror(_crows(1, 2, 3, 4, 5), 2, share_imsg)
eq([r["version"] for r in sel], [3, 4, 5], "contacts: cursor=2 shared -> 3,4,5")
# same rows, a not-shared policy -> none leave the machine.
eq(select_contacts_to_mirror(_crows(1, 2, 3, 4, 5), 2, not_shared), [],
   "contacts: not-shared policy -> none")
# cursor=0 picks up everything; unsorted input is returned in version order.
eq([r["version"] for r in select_contacts_to_mirror(_crows(5, 3, 1, 4, 2), 0, share_imsg)],
   [1, 2, 3, 4, 5], "contacts: unsorted input sorted by version")
# per-source private-all overrides global share-all (most-specific-wins).
priv_imsg = normalize_contact_policy({"global": "share-all", "sources": {"imessage": "private-all"}})
eq(select_contacts_to_mirror(_crows(3, 4, 5), 2, priv_imsg), [],
   "contacts: per-source private-all skips despite global share-all")
# mixed sources: only the shared source's rows are selected.
mixed = _crows(3, source="imessage") + _crows(4, source="whatsapp")
eq([r["version"] for r in select_contacts_to_mirror(mixed, 2, share_imsg)], [3],
   "contacts: only the shared source's rows selected")
# empty / None inputs are safe.
eq(select_contacts_to_mirror([], 0, share_imsg), [], "contacts: empty rows")
eq(select_contacts_to_mirror(None, 0, share_imsg), [], "contacts: None rows")


print("\n%d passed, %d failed" % (_pass, _fail))
if _fail:
    sys.stderr.write("\nFailures:\n")
    for f in _failures:
        sys.stderr.write("  - " + f + "\n")
    sys.exit(1)
