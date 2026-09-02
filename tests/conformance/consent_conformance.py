#!/usr/bin/env python3
"""Consent-resolver conformance harness: JS (shared/model/consent.js) vs
Python (agents/uplink/consent.py) on EVERY input, not curated cases.

Why this exists: the consent resolver is an authorization boundary implemented
twice on purpose (the UI shows the JS decision; the uplink ENFORCES the Python
one). Two hand-maintained copies drift by construction, and the failure mode
is the worst one this system has — Python says "share" where the JS UI showed
"private" and a private conversation silently leaks to the manager under a
green build. The curated parity tests (consent.test.js / consent_py.test.py)
only cover the cases someone thought of. This harness instead:

  1. EXHAUSTIVELY enumerates the structured input space of resolve():
     every override token x every profile shape x every global token x every
     per-source token x every `sources` container shape x every convo shape
     (incl. hostile ones: missing/empty/non-string labels, prototype-named
     source ids, non-object convos). Since D1 the conversation path is
     EXPLICIT-ONLY (only the override decides), so most of that cross-product
     now proves the opposite of what it used to: that the profile / per-source
     / global inputs are ignored IDENTICALLY on both sides.
  1b. Carries a dedicated UNKNOWN-VALUE vector class (F8): every unrecognized
     override shape crossed with the loudest possible standing policy and a
     shared profile. Those vectors are additionally self-checked — not just
     compared — because "absent or unrecognized resolves private" is a stated
     invariant, and two implementations agreeing on a leak would still be a
     leak.
  2. FUZZES the other entry points (normalize_policy, normalize_override,
     effective_level, overrides_from_sync, normalize_contact_policy,
     resolve_contact_share, resolve_all, effective_shared) with a seeded random
     JSON generator that mixes valid tokens with junk of every JSON type at
     every level,

then evaluates the SAME vectors through both implementations (JS in the
pinned node:20-alpine container, like tests/run.sh) and fails on:
  - any vector where the two outputs differ (a drift — the leak class), or
  - any vector where either side raises (a crash is not a decision; in the
    uplink it aborts the whole reconcile pass; in the UI it hides the panel).

Deterministic: a fixed seed, so a failure reproduces byte-for-byte. Prints the
first mismatches with their inputs so the fix is obvious.

Run:  python3 tests/conformance/consent_conformance.py   (exit 0 = conformant)
Env:  CONSENT_FUZZ_N  number of random vectors per entry point (default 3000)
      CONSENT_SEED    generator seed (default 20260830)
      CONSENT_NODE    how to run node: "docker" (default) or a node binary path
"""
import itertools
import json
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "agents", "uplink"))
import consent  # noqa: E402

SEED = int(os.environ.get("CONSENT_SEED", "20260830"))
FUZZ_N = int(os.environ.get("CONSENT_FUZZ_N", "3000"))

# ---------------------------------------------------------------- value pools
TOKENS = ["share-all", "private-all", "private", "share", "inherit"]
JUNK_STR = ["", "junk", "SHARE-ALL", "Share-All", "share-all ", " private", "0", "null",
            "__proto__", "constructor", "toString", "hasOwnProperty", "prototype", "valueOf",
            # unicode / NFKC lookalikes + NUL suffix: canaries against a future
            # .trim()/.lower()/normalize() on ONE side only (exact equality today)
            "\uff53hare-all", "share\u2011all", "share-all\u0000", "private\u00a0"]
# 2**53-1 is the largest integer both JSON parsers represent exactly; a bigger
# one is rounded by JS and would be a permanent false difference in any output
# that echoes its input (resolve_all echoes the convo).
SCALARS = [None, True, False, 0, 1, -1, 5, 9007199254740991, "5"]
# "imessage\n" / "!a:local\n": Python's `$` matches before a trailing newline,
# JS's does not — these canaries prove both sides use end-of-STRING anchoring.
SOURCE_IDS = ["imessage", "whatsapp", "gmessages", "linkedin", "twitter",
              "instagram", "5", "0", "1", "", "constructor", "__proto__", "toString",
              "imessage\n"]
ROOM_IDS = ["!a:local", "!b:local", "!c:local", "", "constructor", "__proto__", "5",
            "!a:local\n"]
# Per-contact override keys (per-contact-share plan, F5/F6). Valid ones, plus
# every malformation the key spec must drop: no pipe, empty source segment,
# empty network_id, a prototype-named or non-matching source, an embedded '|'
# in the network_id (legal — the importer's email charset admits it), and the
# trailing-newline canary for end-of-STRING anchoring. Deliberately NO
# integer-like key: JS hoists those in an object's key order, Python does not,
# which would be a false ordering difference in any echoed map.
OVERRIDE_KEYS = [
    "imessage|+15551234567", "whatsapp|+15550000000", "imessage|a@b.example",
    "imessage|a|b@example.com", "imessage||x", "imessage|", "|+1555", "nopipe",
    "__proto__|x", "constructor|x", "toString|x", "5|x", "iMessage|+1",
    "imessage|+1\n", "imessage\n|+1", "",
]
OVERRIDE_VALUES = ["share", "private", "inherit", "junk", "Share", "share-all",
                   "", None, 5, True, False, {}, [], {"state": "share"}]


def _key_pool():
    return SOURCE_IDS + ["x", "y"]


# ------------------------------------------------------------ exhaustive resolve
# Every structured combination for resolve(convo, policy, override, profile).
OVERRIDES = [None, "share", "direct", "private", "inherit", "junk", "", 5, True, {}, [],
             {"state": "share"}, {"state": "direct"}, {"state": "private"},
             {"state": "share", "migrated": True}]
PROFILES = [
    None,
    {"share": "share", "displayName": "Ann"},
    {"share": "private", "displayName": "Bob"},
    {"share": "private"},                      # no displayName -> 'profile'
    {"share": "inherit", "displayName": "Cy"},
    {"share": "junk", "displayName": "Di"},
    {"displayName": "Ed"},                     # no share
    {},                                        # empty object (truthy in JS)
    {"share": "share", "displayName": ""},     # empty name -> 'profile'
    {"share": "share", "displayName": 5},      # non-string name
    {"share": "share", "displayName": None},
    "share",                                   # non-object profile
    ["share"],
    [],
    7,
    True,
]
GLOBALS = [None, "share-all", "private", "inherit", "junk", 5, True, ""]
SRC_STATES = [None, "share-all", "private-all", "inherit", "share", "junk", 5, ""]
CONVOS = [
    {"id": "!r:l", "sourceId": "imessage", "sourceLabel": "iMessage"},
    {"id": "!r:l", "sourceId": "imessage"},                     # no label
    {"id": "!r:l", "sourceId": "imessage", "sourceLabel": ""},  # empty label
    {"id": "!r:l", "sourceId": "imessage", "sourceLabel": 5},   # non-string label
    {"id": "!r:l", "sourceId": "imessage", "sourceLabel": []},
    {"id": "!r:l", "sourceId": "imessage", "sourceLabel": {}},
    {"id": "!r:l", "sourceId": "", "sourceLabel": "Empty"},     # empty source id
    {"id": "!r:l"},                                             # no source id
    {"id": "!r:l", "sourceId": 5},                              # numeric source id
    {"id": "!r:l", "sourceId": "constructor"},                  # prototype-named
    {"id": "!r:l", "sourceId": "__proto__"},
    {"id": "!r:l", "sourceId": ["imessage"]},                   # array source id
    None,
    "imessage",
    [],
    5,
]


def _source_key_of(convo):
    """The key a per-source rule would be filed under for this convo, or None."""
    if not isinstance(convo, dict):
        return None
    sid = convo.get("sourceId")
    if isinstance(sid, str) and sid:
        return sid
    if isinstance(sid, int) and not isinstance(sid, bool):
        return str(sid)  # JS objects key numbers as strings; is Python equal?
    return None


def exhaustive_resolve_vectors():
    """Full cross-product on the canonical convo (every override x profile x
    global x source-state x sources-shape), plus every hostile convo shape
    crossed with the policy dimensions and a representative override/profile
    pair — ~60k vectors, deterministic order."""
    combos = list(itertools.product(CONVOS[:1], OVERRIDES, PROFILES, GLOBALS, SRC_STATES))
    combos += list(itertools.product(CONVOS[1:], [None, "share"], [None, PROFILES[1]],
                                     GLOBALS, SRC_STATES))
    out = []
    for convo, override, profile, g, s in combos:
        key = _source_key_of(convo)
        for shape in ("dict", "list", "str", "absent"):
            policy = {}
            if g is not None:
                policy["global"] = g
            if shape == "dict":
                sources = {}
                if key is not None and s is not None:
                    sources[key] = s
                policy["sources"] = sources
            elif shape == "list":
                policy["sources"] = [s] if s is not None else []
            elif shape == "str":
                policy["sources"] = "share-all"
            out.append({"kind": "resolve", "convo": convo, "policy": policy,
                        "override": override, "profile": profile})
    return out


# ------------------------------------------------ the unknown-value class (F8)
# "Absent or ANY unrecognized override resolves private" is a stated invariant
# of the explicit model, so it gets its own vector class AND its own assertion
# (see check_invariants): every one of these, next to the loudest standing
# policy and a shared contact profile, must be private on both sides.
UNKNOWN_OVERRIDES = [
    None, "", "inherit", "junk", "shared", "Share", "SHARE", "share ", " share",
    "Direct", "DIRECT", "direct ", "directs", "share-all", "private-all", "auto",
    "__proto__", "constructor", "toString", 0, 1, 5, -1, True, False, [], {},
    ["share"], ["direct"], {"state": "inherit"}, {"state": "junk"},
    {"state": None}, {"state": 5}, {"state": ["share"]}, {"state": {}},
    {"State": "share"}, {"level": "share"}, {"state": "share-all"},
    # NFKC lookalikes / zero-width / NUL-suffix / NBSP / non-breaking hyphen:
    # exact-match canaries against a future trim()/normalize() on one side only.
    {"state": "sharedirect"}, "\uff53hare", "sh\u200bare", "share\u0000",
    "direct\u0000", "private\u00a0", "share\u2011all",
]
LOUD_POLICIES = [
    {"global": "share-all", "sources": {"imessage": "share-all"}},
    {"global": "share-all", "sources": {}},
    {"global": "private", "sources": {"imessage": "share-all"}},
    {},
]
LOUD_PROFILES = [None, {"share": "share", "displayName": "Ann"}, {"share": "share"}]


def unknown_override_vectors():
    """Every unrecognized override x loud policy x shared profile x convo shape.

    Tagged expect_private so main() can assert the decision itself, not merely
    that the two implementations agree on it."""
    out = []
    for override in UNKNOWN_OVERRIDES:
        out.append({"kind": "effective_level", "override": override,
                    "expect_private": True})
        for convo in CONVOS[:3] + [CONVOS[7], CONVOS[9], None]:
            for policy in LOUD_POLICIES:
                for profile in LOUD_PROFILES:
                    out.append({"kind": "resolve", "convo": convo, "policy": policy,
                                "override": override, "profile": profile,
                                "expect_private": True})
                    out.append({"kind": "effective_shared", "convo": convo,
                                "policy": policy, "override": override,
                                "profile": profile, "expect_private": True})
    return out


# ------------------------------------------- the per-contact override class
# The contact dimension KEEPS its standing policies, so its fall-through rule is
# the opposite of the conversation dimension's: an unrecognized override VALUE
# inherits from per-source/global rather than resolving private. That makes the
# cross-product of (override value x policy x source) the thing both sides must
# agree on exactly, so it gets a deterministic class of its own rather than
# relying on the fuzzer to reach it.
CONTACT_POLICIES = [
    {"global": "share-all", "sources": {"imessage": "share-all"}},
    {"global": "share-all", "sources": {"imessage": "private-all"}},
    {"global": "private", "sources": {"imessage": "share-all"}},
    {"global": "private", "sources": {}},
    {},
    {"global": "share-all", "sources": ["share-all"]},
]


def contact_override_vectors():
    """Every override value x every contact policy x a few source shapes, plus a
    normalize_contact_overrides vector for every key/value pairing."""
    out = []
    for override in OVERRIDE_VALUES + ["share", "private"]:
        for policy in CONTACT_POLICIES:
            for source in ("imessage", "whatsapp", "", "5", "__proto__", 5, None):
                out.append({"kind": "resolve_contact_share", "source": source,
                            "policy": policy, "override": override})
    for key in OVERRIDE_KEYS:
        for value in OVERRIDE_VALUES:
            out.append({"kind": "normalize_contact_overrides",
                        "raw": {"overrides": {key: value}}})
    for raw in (None, {}, [], "x", 5, {"overrides": None}, {"overrides": []},
                {"overrides": "share"}, {"overrides": 5}, {"overrides": {}},
                {"nope": {"imessage|+1": "share"}},
                {"overrides": {k: "share" for k in OVERRIDE_KEYS}}):
        out.append({"kind": "normalize_contact_overrides", "raw": raw})
    # The entry cap: exactly at it reads, one over it is a READ FAILURE on both
    # sides (never a silently half-honored map).
    at_cap = {"imessage|+1%d" % i: "private" for i in range(consent.CONTACT_OVERRIDES_CAP)}
    over_cap = dict(at_cap)
    over_cap["imessage|+1over"] = "private"
    out.append({"kind": "normalize_contact_overrides", "raw": {"overrides": at_cap}})
    out.append({"kind": "normalize_contact_overrides", "raw": {"overrides": over_cap}})
    return out


# ------------------------------------------------------------------- fuzzing
class Gen:
    def __init__(self, seed):
        self.r = random.Random(seed)

    def pick(self, xs):
        return self.r.choice(xs)

    def token(self):
        return self.pick(TOKENS + TOKENS + JUNK_STR)

    def junk(self, depth=0):
        """Any JSON value, biased toward the tokens the resolvers care about."""
        c = self.r.random()
        if depth > 2 or c < 0.45:
            return self.pick(TOKENS + JUNK_STR + SCALARS)
        if c < 0.7:
            return {self.pick(_key_pool() + ["state", "global", "sources", "share",
                                             "displayName", "id", "sourceId", "sourceLabel"]):
                    self.junk(depth + 1) for _ in range(self.r.randint(0, 3))}
        return [self.junk(depth + 1) for _ in range(self.r.randint(0, 3))]

    def policy(self):
        c = self.r.random()
        if c < 0.15:
            return self.junk()
        p = {}
        if self.r.random() < 0.8:
            p["global"] = self.pick(["share-all", "private"] * 3 + JUNK_STR + SCALARS)
        if self.r.random() < 0.85:
            if self.r.random() < 0.85:
                p["sources"] = {self.pick(_key_pool()): self.pick(TOKENS + JUNK_STR + SCALARS)
                                for _ in range(self.r.randint(0, 4))}
            else:
                p["sources"] = self.junk()
        if self.r.random() < 0.1:
            p[self.pick(JUNK_STR)] = self.junk()
        return p

    def convo(self):
        if self.r.random() < 0.1:
            return self.junk()
        c = {"id": self.pick(ROOM_IDS)}
        if self.r.random() < 0.9:
            c["sourceId"] = self.pick(SOURCE_IDS * 3 + SCALARS + [["x"], {}])
        if self.r.random() < 0.6:
            c["sourceLabel"] = self.pick(["iMessage", "WhatsApp", "", "5"] + SCALARS + [[], {}])
        return c

    def override(self):
        return self.pick([None] * 3 + ["share", "private", "direct"] * 3 + JUNK_STR + SCALARS
                         + [{"state": "share"}, {"state": "private"}, {"state": "direct"},
                            {"state": "junk"}, {"state": ["share"]},
                            {"state": "share", "migrated": True}, {}, []])

    def profile(self):
        if self.r.random() < 0.35:
            return None
        if self.r.random() < 0.15:
            return self.junk()
        p = {}
        if self.r.random() < 0.9:
            p["share"] = self.pick(["share", "private", "inherit"] * 2 + JUNK_STR + SCALARS)
        if self.r.random() < 0.8:
            p["displayName"] = self.pick(["Ann", "Bob", "", "5"] + SCALARS + [[], {}])
        return p

    def contact_override(self):
        """One per-contact override VALUE (the third resolve_contact_share arg)."""
        return self.pick(OVERRIDE_VALUES + ["share", "private"] * 3 + JUNK_STR)

    def contact_overrides_event(self):
        """A whole stored com.jkali.contact_overrides CONTENT."""
        c = self.r.random()
        if c < 0.08:
            return self.junk()                       # not even a dict
        if c < 0.16:
            return {"overrides": self.junk()}        # non-dict field = read failure
        m = {}
        for _ in range(self.r.randint(0, 5)):
            m[self.pick(OVERRIDE_KEYS)] = self.pick(OVERRIDE_VALUES + ["share", "private"] * 2)
        body = {"overrides": m}
        if self.r.random() < 0.1:
            body[self.pick(JUNK_STR)] = self.junk()
        if self.r.random() < 0.08:
            body.pop("overrides")                    # absent field = empty map
        return body

    def sync(self):
        rooms = {}
        for _ in range(self.r.randint(0, 4)):
            rid = self.pick(ROOM_IDS)
            events = []
            for _ in range(self.r.randint(0, 3)):
                e = {"type": self.pick(["com.jkali.share_override"] * 3
                                       + ["m.tag", "com.jkali.share_policy", ""] + SCALARS)}
                if self.r.random() < 0.9:
                    e["content"] = self.pick([{"state": "share"}, {"state": "private"},
                                              {"state": "inherit"}, {"state": "junk"}, {},
                                              "share", "private", None, [], 5])
                if self.r.random() < 0.05:
                    e = self.junk()
                events.append(e)
            ad = {"events": events} if self.r.random() < 0.9 else self.junk()
            rooms[rid] = {"account_data": ad} if self.r.random() < 0.9 else self.junk()
        c = self.r.random()
        if c < 0.05:
            return self.junk()
        if c < 0.1:
            return {"rooms": self.junk()}
        return {"rooms": {"join": rooms}}


def fuzz_vectors(n, seed):
    g = Gen(seed)
    out = []
    for _ in range(n):
        out.append({"kind": "resolve", "convo": g.convo(), "policy": g.policy(),
                    "override": g.override(), "profile": g.profile()})
        out.append({"kind": "effective_shared", "convo": g.convo(), "policy": g.policy(),
                    "override": g.override(), "profile": g.profile()})
        out.append({"kind": "normalize_policy", "p": g.policy()})
        out.append({"kind": "normalize_override", "data": g.override()})
        out.append({"kind": "effective_level", "override": g.override()})
        out.append({"kind": "normalize_contact_policy", "raw": g.policy()})
        out.append({"kind": "resolve_contact_share",
                    "source": g.pick(SOURCE_IDS + SCALARS + [["x"], {}]),
                    "policy": g.policy(),
                    "override": g.contact_override()})
        out.append({"kind": "normalize_contact_overrides", "raw": g.contact_overrides_event()})
        out.append({"kind": "overrides_from_sync", "sync": g.sync()})
        convos = [g.convo() for _ in range(g.r.randint(0, 4))]
        ids = [c.get("id") for c in convos
               if isinstance(c, dict) and isinstance(c.get("id"), str)]
        overrides = ({g.pick(ids or ROOM_IDS): g.override() for _ in range(g.r.randint(0, 3))}
                     if g.r.random() < 0.85 else g.pick([None, [], "share", 5]))
        profiles = ({g.pick(ids or ROOM_IDS): g.profile() for _ in range(g.r.randint(0, 3))}
                    if g.r.random() < 0.85 else g.pick([None, [], "share", 5]))
        out.append({"kind": "resolve_all", "convos": convos, "policy": g.policy(),
                    "overrides": overrides, "profiles": profiles})
    return out


# ---------------------------------------------------------------- evaluators
def eval_py(v):
    try:
        k = v["kind"]
        if k == "resolve":
            return consent.resolve(v["convo"], v["policy"], v["override"], v["profile"])
        if k == "effective_shared":
            return consent.effective_shared(v["convo"], v["policy"], v["override"], v["profile"])
        if k == "resolve_all":
            return consent.resolve_all(v["convos"], v["policy"], v["overrides"], v["profiles"])
        if k == "normalize_policy":
            return consent.normalize_policy(v["p"])
        if k == "normalize_override":
            return consent.normalize_override(v["data"])
        if k == "effective_level":
            return consent.effective_level(v["override"])
        if k == "overrides_from_sync":
            return consent.overrides_from_sync(v["sync"])
        if k == "normalize_contact_policy":
            return consent.normalize_contact_policy(v["raw"])
        if k == "resolve_contact_share":
            # .get(): pre-per-contact-share vectors carry no override field, and
            # None must fall through exactly as JS's `undefined` does.
            return consent.resolve_contact_share(v["source"], v["policy"],
                                                 v.get("override"))
        if k == "normalize_contact_overrides":
            return consent.normalize_contact_overrides(v["raw"])
        raise ValueError("unknown kind " + k)
    except Exception as e:  # a crash is a comparable outcome, not a skip
        return {"__error__": type(e).__name__}


def eval_js(vectors):
    node = os.environ.get("CONSENT_NODE", "docker")
    script = os.path.join("tests", "conformance", "consent_eval.mjs")
    if node == "docker":
        # read-only mount: the evaluator only reads, and the repo holds secrets
        cmd = ["docker", "run", "--rm", "-i", "-v", REPO + ":/w:ro", "-w", "/w",
               "node:20-alpine", "node", script]
    else:
        cmd = [node, script]
    env = dict(os.environ)
    env["PATH"] = "/Applications/Docker.app/Contents/Resources/bin:" + env.get("PATH", "")
    proc = subprocess.run(cmd, input=json.dumps(vectors).encode(), capture_output=True,
                          cwd=REPO, env=env, timeout=600)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace")[-2000:])
        raise SystemExit("node evaluator failed (rc=%d)" % proc.returncode)
    return json.loads(proc.stdout)


def canon(x):
    """Canonical JSON so key order / whitespace never count as a difference.
    Python None <-> JS null; JS undefined was mapped to {__undefined__} by the
    evaluator and Python never returns it, so it shows up as a real difference."""
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def check_invariants(vectors, py, js):
    """Self-check the expect_private vectors on BOTH sides.

    Comparison alone cannot catch a shared invariant violation: if both
    implementations decided an unknown override means 'share', they would agree
    and the run would be green while every junk override leaked a conversation.
    So the unknown-value class asserts the DECISION, not just the agreement."""
    bad = []
    for i, v in enumerate(vectors):
        if not v.get("expect_private"):
            continue
        for side, r in (("py", py[i]), ("js", js[i])):
            if v["kind"] == "effective_level":
                ok = r == "private"
            elif v["kind"] == "effective_shared":
                ok = r is False
            else:  # resolve
                ok = isinstance(r, dict) and r.get("shared") is False
            if not ok:
                bad.append((i, side, v, r))
    return bad


def main():
    unknown = unknown_override_vectors()
    contact = contact_override_vectors()
    vectors = (exhaustive_resolve_vectors() + unknown + contact
               + fuzz_vectors(FUZZ_N, SEED))
    py = [eval_py(v) for v in vectors]
    js = eval_js(vectors)
    assert len(js) == len(vectors), "node returned %d results for %d vectors" % (len(js), len(vectors))

    violations = check_invariants(vectors, py, js)
    if violations:
        print("INVARIANT VIOLATED: an absent/unrecognized override resolved SHARED "
              "on %d vector/side pair(s) — this is the leak class, not a drift"
              % len(violations))
        for i, side, v, r in violations[:10]:
            print("   - #%d [%s] -> %s\n       %s" % (i, side, canon(r), canon(v)[:400]))
        sys.exit(1)

    mismatches, py_err, js_err = [], 0, 0
    for i, v in enumerate(vectors):
        a, b = canon(py[i]), canon(js[i])
        pe = isinstance(py[i], dict) and "__error__" in py[i]
        je = isinstance(js[i], dict) and "__error__" in js[i]
        py_err += pe
        js_err += je
        if a != b or pe or je:
            mismatches.append((i, v, py[i], js[i]))

    n_fuzz = FUZZ_N * 10  # entry points per fuzz iteration (see fuzz_vectors)
    # ACCEPTANCE (per-contact-share C4): the run must actually EXERCISE the new
    # override argument, not merely keep passing without it.
    n_override_bearing = sum(1 for v in vectors
                             if v["kind"] == "resolve_contact_share"
                             and v.get("override") is not None)
    n_override_maps = sum(1 for v in vectors if v["kind"] == "normalize_contact_overrides")
    print("consent conformance: %d vectors (%d exhaustive resolve + %d unknown-value "
          "+ %d per-contact-override + %d fuzz), seed=%d"
          % (len(vectors), len(vectors) - n_fuzz - len(unknown) - len(contact),
             len(unknown), len(contact), n_fuzz, SEED))
    print("  override-bearing resolve_contact_share vectors=%d  "
          "normalize_contact_overrides vectors=%d"
          % (n_override_bearing, n_override_maps))
    if not n_override_bearing or not n_override_maps:
        print("NO OVERRIDE-BEARING VECTORS: the per-contact override argument was "
              "never exercised, so this run proves nothing about it")
        sys.exit(1)
    print("  python errors=%d  js errors=%d  differing/erroring vectors=%d"
          % (py_err, js_err, len(mismatches)))
    if mismatches:
        # Group by (kind, py-output, js-output) so a single root cause is one
        # line. A class where the `shared` BOOLEAN differs is the leak class
        # (one side would mirror what the other showed as private) and is
        # labelled DECISION DIFFERS; a reason-only difference is drift; a crash
        # on either side is a crash. Decision classes are listed first.
        def decision_of(x):
            if isinstance(x, bool):
                return x
            if isinstance(x, dict) and "shared" in x:
                return bool(x["shared"])
            return None

        def label(p, j):
            if (isinstance(p, dict) and "__error__" in p) or (isinstance(j, dict) and "__error__" in j):
                return "CRASH"
            dp, dj = decision_of(p), decision_of(j)
            if dp is not None and dj is not None and dp != dj:
                return "DECISION DIFFERS"
            return "drift"

        groups = {}
        for i, v, p, j in mismatches:
            key = (label(p, j), v["kind"], canon(p), canon(j))
            groups.setdefault(key, []).append((i, v))
        n_decision = sum(len(x) for k, x in groups.items() if k[0] == "DECISION DIFFERS")
        print("  %d distinct divergence classes; DECISION DIFFERS vectors=%d"
              % (len(groups), n_decision))
        order = {"DECISION DIFFERS": 0, "CRASH": 1, "drift": 2}
        for (lab, kind, p, j), items in sorted(
                groups.items(), key=lambda kv: (order[kv[0][0]], -len(kv[1])))[:30]:
            i, v = items[0]
            print("   - [%s] %s x%d\n       py=%s\n       js=%s\n       e.g. #%d %s"
                  % (lab, kind, len(items), p, j, i, canon(v)[:400]))
        sys.exit(1)
    print("OK: JS and Python consent resolvers agree on every vector, no crashes")


if __name__ == "__main__":
    main()
