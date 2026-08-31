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
    reconcile_decisions, select_new_events, next_watermark, plan_contact_mirror,
    select_contacts_to_tombstone,
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


# ---- contact mirror planner: plan_contact_mirror (pm_mng-q5u.2) -----------
# Per-pass diff of desired-shared-and-live vs mirrored. Replaces the forward-
# only contact_cursor so that enabling a source BACKFILLS its already-imported
# contacts. `sources` filters rows only; `mirrored` is the complete
# contact_mirror table and is never filtered (unknown-source mirrors are
# tombstoned, not stranded). A not-shared row appears in neither list.
def _crows(*versions, source="imessage", deleted=0):
    return [{"source": source, "network_id": "h%d" % v, "kind": "phone",
             "display_name": None, "person_id": None, "deleted": deleted, "version": v}
            for v in versions]


SOURCES = ("imessage", "whatsapp", "linkedin")
share_imsg = normalize_contact_policy({"sources": {"imessage": "share-all"}})
share_all = normalize_contact_policy({"global": "share-all"})
not_shared = normalize_contact_policy({"global": "private"})
K = lambda v, s="imessage": (s, "h%d" % v)  # noqa: E731


def _plan(rows, mirrored, policy, sources=SOURCES, **kw):
    return plan_contact_mirror(rows, mirrored, policy, sources, **kw)


# BACKFILL: rows at 1..3 imported before the flip, nothing mirrored, source
# now shared -> all three pushed, ascending version order.
p = _plan(_crows(3, 1, 2), {}, share_imsg)
eq([r["version"] for r in p["push"]], [1, 2, 3], "plan: backfill pushes all, sorted")
eq(p["tombstone"], [], "plan: backfill tombstones none")
eq((p["not_shared"], p["pending"]), (0, 0), "plan: backfill counts")
# NOT shared -> nothing leaves the machine, in either direction; counted.
p = _plan(_crows(1, 2, 3), {}, not_shared)
eq((p["push"], p["tombstone"], p["not_shared"]), ([], [], 3), "plan: private -> none, counted")
# already mirrored at the SAME version -> not re-pushed (no duplicate work).
p = _plan(_crows(1, 2), {K(1): 1, K(2): 2}, share_imsg)
eq(p["push"], [], "plan: same version -> no re-push")
eq(p["tombstone"], [], "plan: same version -> no tombstone")
# mirrored at a DIFFERENT version -> re-pushed: older (an update) AND newer
# (a rebuilt contacts.db restarts versions at 1; `<` would strand stale PII).
p = _plan(_crows(5) + _crows(2), {K(5): 3, K(2): 9}, share_imsg)
eq([r["version"] for r in p["push"]], [2, 5], "plan: != version -> re-push, both directions")
# deleted row never mirrored -> appears nowhere.
p = _plan(_crows(4, deleted=1), {}, share_imsg)
eq((p["push"], p["tombstone"]), ([], []), "plan: deleted+unmirrored -> nowhere")
# deleted row that IS mirrored -> tombstone (and not pushed).
p = _plan(_crows(4, deleted=1), {K(4): 4}, share_imsg)
eq((p["push"], p["tombstone"]), ([], [K(4)]), "plan: deleted+mirrored -> tombstone")
# source flipped to private with mirrors -> tombstone ALL of them, push none.
p = _plan(_crows(1, 2, 3), {K(1): 1, K(2): 2, K(3): 3}, not_shared)
eq((p["push"], p["tombstone"]), ([], [K(1), K(2), K(3)]), "plan: revoke -> tombstone all")
# mixed sources: only the shared source pushes; the other is counted not_shared.
p = _plan(_crows(3, source="imessage") + _crows(4, source="whatsapp"), {}, share_imsg)
eq([(r["source"], r["version"]) for r in p["push"]], [("imessage", 3)], "plan: only shared source pushes")
eq(p["not_shared"], 1, "plan: other source counted not_shared")
# per-source private-all beats global share-all (most-specific-wins).
priv_imsg = normalize_contact_policy({"global": "share-all", "sources": {"imessage": "private-all"}})
p = _plan(_crows(3, 4), {}, priv_imsg)
eq(p["push"], [], "plan: per-source private-all beats global share-all")
# a row whose source is NOT in `sources` is ignored even under global
# share-all (the planner is the self-contained gate; SR-4).
p = _plan(_crows(7, source="mystery"), {}, share_all)
eq((p["push"], p["not_shared"]), ([], 0), "plan: unknown source never pushed")
# a MIRRORED handle whose source is not in `sources` (and has no row) is
# tombstoned, not stranded: `mirrored` is never filtered by `sources`.
p = _plan(_crows(1), {("oldsource", "h1"): 1, K(1): 1}, share_imsg)
eq(p["tombstone"], [("oldsource", "h1")], "plan: stranded unknown-source mirror tombstoned")
eq(p["push"], [], "plan: known mirrored row untouched alongside")
# re-share after tombstone: the mirror row was dropped on the tombstone 2xx,
# so the handle is simply "not mirrored" and is pushed again.
p = _plan(_crows(1), {}, share_imsg)
eq([r["version"] for r in p["push"]], [1], "plan: re-share re-pushes")
# push cap: 5 shared rows, cap 2 -> exactly the 2 lowest versions, pending=3,
# tombstones unaffected (never capped).
p = _plan(_crows(5, 4, 3, 2, 1), {("linkedin", "gone"): 1}, share_imsg, push_cap=2)
eq([r["version"] for r in p["push"]], [1, 2], "plan: cap -> lowest versions")
eq(p["pending"], 3, "plan: cap -> pending counted")
eq(p["tombstone"], [("linkedin", "gone")], "plan: cap never applies to tombstones")
# empty / None inputs are safe.
eq(_plan([], {}, share_imsg), {"tombstone": [], "push": [], "not_shared": 0, "pending": 0},
   "plan: empty")
eq(_plan(None, None, share_imsg, sources=None),
   {"tombstone": [], "push": [], "not_shared": 0, "pending": 0}, "plan: None inputs")


# ---- contact revocation: select_contacts_to_tombstone (pm_mng-q5u.1) ------
# The pure "which mirrored handles are no longer shared-and-live" diff. Handles
# are (source, network_id) tuples: mirrored = every contact_mirror row;
# currently_shared = handles whose source resolves shared AND are not deleted.
A = ("imessage", "hA")
B = ("imessage", "hB")
C = ("imessage", "hC")

# mirrored {A,B,C}, currently shared {A,C} -> tombstone exactly {B}.
eq(select_contacts_to_tombstone({A, B, C}, {A, C}), [B],
   "contact-revoke: unshared handle tombstoned")
# nothing to tombstone when every mirrored handle is still shared.
eq(select_contacts_to_tombstone({A, B, C}, {A, B, C}), [],
   "contact-revoke: all still shared -> none")
# idempotent: after B's row is dropped on the master 2xx, B leaves `mirrored`
# and is never re-selected (already-tombstoned is not re-sent).
eq(select_contacts_to_tombstone({A, C}, {A, C}), [],
   "contact-revoke: already-tombstoned not re-selected")
# a re-shared handle (back in currently_shared) is left alone, not tombstoned.
eq(select_contacts_to_tombstone({A, B, C}, {A, B, C}), [],
   "contact-revoke: re-shared handle not tombstoned")
# a whole source going private tombstones ALL its mirrored handles (deterministic
# sorted order), while another still-shared source's handle stays.
W = ("whatsapp", "hW")
eq(select_contacts_to_tombstone({A, B, C, W}, {W}), [A, B, C],
   "contact-revoke: source-wide revoke tombstones all, sorted, keeps other source")
# a deleted (not-live) handle on a still-shared source is absent from
# currently_shared, so it is tombstoned even though its source is shared.
eq(select_contacts_to_tombstone({A, B}, {A}), [B],
   "contact-revoke: deleted handle tombstoned though source shared")
# empty / None inputs are safe.
eq(select_contacts_to_tombstone(set(), {A}), [], "contact-revoke: empty mirrored")
eq(select_contacts_to_tombstone({A, B}, set()), [A, B],
   "contact-revoke: nothing shared -> all tombstoned")
eq(select_contacts_to_tombstone(None, None), [], "contact-revoke: None inputs")


print("\n%d passed, %d failed" % (_pass, _fail))
if _fail:
    sys.stderr.write("\nFailures:\n")
    for f in _failures:
        sys.stderr.write("  - " + f + "\n")
    sys.exit(1)
