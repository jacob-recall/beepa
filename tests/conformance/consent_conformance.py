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
     source ids, non-object convos), and
  2. FUZZES the other entry points (normalize_policy, normalize_override,
     overrides_from_sync, normalize_contact_policy, resolve_contact_share,
     resolve_all, effective_shared) with a seeded random JSON generator that
     mixes valid tokens with junk of every JSON type at every level,

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


def _key_pool():
    return SOURCE_IDS + ["x", "y"]


# ------------------------------------------------------------ exhaustive resolve
# Every structured combination for resolve(convo, policy, override, profile).
OVERRIDES = [None, "share", "private", "inherit", "junk", "", 5, True, {}, [],
             {"state": "share"}]
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
        return self.pick([None] * 3 + ["share", "private"] * 3 + JUNK_STR + SCALARS
                         + [{"state": "share"}, {"state": "private"}, {"state": "junk"}, {}, []])

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
        out.append({"kind": "normalize_contact_policy", "raw": g.policy()})
        out.append({"kind": "resolve_contact_share",
                    "source": g.pick(SOURCE_IDS + SCALARS + [["x"], {}]),
                    "policy": g.policy()})
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
        if k == "overrides_from_sync":
            return consent.overrides_from_sync(v["sync"])
        if k == "normalize_contact_policy":
            return consent.normalize_contact_policy(v["raw"])
        if k == "resolve_contact_share":
            return consent.resolve_contact_share(v["source"], v["policy"])
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


def main():
    vectors = exhaustive_resolve_vectors() + fuzz_vectors(FUZZ_N, SEED)
    py = [eval_py(v) for v in vectors]
    js = eval_js(vectors)
    assert len(js) == len(vectors), "node returned %d results for %d vectors" % (len(js), len(vectors))

    mismatches, py_err, js_err = [], 0, 0
    for i, v in enumerate(vectors):
        a, b = canon(py[i]), canon(js[i])
        pe = isinstance(py[i], dict) and "__error__" in py[i]
        je = isinstance(js[i], dict) and "__error__" in js[i]
        py_err += pe
        js_err += je
        if a != b or pe or je:
            mismatches.append((i, v, py[i], js[i]))

    print("consent conformance: %d vectors (%d exhaustive resolve + %d fuzz), seed=%d"
          % (len(vectors), len(vectors) - FUZZ_N * 8, FUZZ_N * 8, SEED))
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
