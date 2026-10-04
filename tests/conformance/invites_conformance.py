#!/usr/bin/env python3
"""Bridge-invite conformance harness: JS (apps/user/invites.js) vs Python
(agents/uplink/invites.py) on the same vectors.

Why this exists: the invite predicate decides which rooms this hub joins
WITHOUT a human in the loop, and it is now implemented twice on purpose (the
browser app joins while it is open; the uplink daemon joins while it is
closed). Two hand-maintained copies drift by construction, and the failure mode
is that the daemon admits an invite the reviewed JS gate refuses — a room
created by a bridge GHOST (whose content a remote contact controls) or a space
mislabelled as another source. So this harness runs the SAME vectors through
BOTH real modules and fails on:
  - any vector where the two full results differ ({join, refusedNonBridge,
    overCap} compared whole, not just `join`), or
  - any vector where either side raises (a crash is not a decision: in the
    daemon it aborts the invites stage, in the UI it strands every invite).

Vector classes:
  1. Every case class in tests/unit/user_invites.test.js, built from the same
     REAL captured /sync payloads (Synapse 1.159.0, room version 11).
  2. The Discord child space: "Direct Messages" is a real m.space created by
     @discordbot:localhost whose name does NOT start with the source spaceName
     ("Discord") — verified live on this hub. It is admitted only via
     childSpaceNames, which is exactly the field shared/source_catalog.py's
     SPACE_SOURCES drops, so this class is the regression that catches a port
     built from the wrong catalog view.
  3. Seeded fuzz: junk of every JSON type at every nesting level, prototype-
     named keys (__proto__ / constructor / toString), and trailing-newline
     canaries that prove both sides anchor at end-of-STRING (Python's `$`
     matches before a final newline; JS's does not).

Deterministic: a fixed seed, so a failure reproduces byte-for-byte.

Run:  python3 tests/conformance/invites_conformance.py   (exit 0 = conformant)
Env:  INVITES_FUZZ_N  random vectors per class (default 4000)
      INVITES_SEED    generator seed (default 20261003)
      CONSENT_NODE    how to run node: "docker" (default) or a node binary path
                      (the same env var tests/run.py already sets)
"""
import copy
import json
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "agents", "uplink"))
import invites  # noqa: E402

SEED = int(os.environ.get("INVITES_SEED", "20261003"))
FUZZ_N = int(os.environ.get("INVITES_FUZZ_N", "4000"))

# ---------------------------------------------------------------- identities
SELF = "@jkali:localhost"
GMSG_BOT = "@gmessagesbot:localhost"
WA_BOT = "@whatsappbot:localhost"
IMSG_BOT = "@imessagebot:localhost"
DISCORD_BOT = "@discordbot:localhost"
BOTS = [GMSG_BOT, WA_BOT, IMSG_BOT, "@instagrambot:localhost",
        "@linkedinbot:localhost", "@twitterbot:localhost", DISCORD_BOT]
# Exactly what apps/user/main.js's bridgeIdentities() feeds the predicate, built
# from the SHARED catalog (childSpaceNames included) rather than retyped.
with open(os.path.join(REPO, "shared", "source_catalog.json")) as f:
    CATALOG = json.load(f)
SPACES = [{"spaceName": s["spaceName"], "botMxid": s["botMxid"],
           "childSpaceNames": s.get("childSpaceNames") or []}
          for s in CATALOG
          if s.get("kind") == "source" and s.get("botMxid") and s.get("spaceName")]

GMSG_DM_ID = "!AzbbJwkTFwmNBWwNWB:localhost"
GMSG_SPACE_ID = "!twUmELsqxnTPpCQpSR:localhost"
WA_DM_ID = "!uLuKqUWBiAFhDUNwgR:localhost"
DISCORD_SPACE_ID = "!dDiscordDirectMsgs:localhost"

# ------------------------------------------- REAL captured invite payloads --
REAL_GMSG_DM = {"invite_state": {"events": [
    {"content": {"room_version": "11"}, "sender": GMSG_BOT, "state_key": "", "type": "m.room.create"},
    {"content": {"join_rule": "invite"}, "sender": GMSG_BOT, "state_key": "", "type": "m.room.join_rules"},
    {"content": {"com.beeper.exclude_from_timeline": True, "topic": ""}, "sender": GMSG_BOT,
     "state_key": "", "type": "m.room.topic"},
    {"content": {"avatar_url": "mxc://maunium.net/yGOdcrJcwqARZqdzbfuxfhzb",
                 "displayname": "Google Messages bridge bot", "membership": "join"},
     "sender": GMSG_BOT, "state_key": GMSG_BOT, "type": "m.room.member"},
    {"content": {"displayname": "jkali", "membership": "invite"},
     "event_id": "$-5Pwx1CljnF703g0Mertv06wOxXBjfMkqpQQaSgc5jA",
     "origin_server_ts": 1787780898034, "sender": GMSG_BOT, "state_key": SELF,
     "type": "m.room.member", "unsigned": {"age": 160754299}},
]}}

REAL_GMSG_SPACE = {"invite_state": {"events": [
    {"content": {"room_version": "11", "type": "m.space"}, "sender": GMSG_BOT,
     "state_key": "", "type": "m.room.create"},
    {"content": {"join_rule": "invite"}, "sender": GMSG_BOT, "state_key": "", "type": "m.room.join_rules"},
    {"content": {"url": "mxc://maunium.net/yGOdcrJcwqARZqdzbfuxfhzb"}, "sender": GMSG_BOT,
     "state_key": "", "type": "m.room.avatar"},
    {"content": {"name": "Google Messages (user@example.com)"}, "sender": GMSG_BOT,
     "state_key": "", "type": "m.room.name"},
    {"content": {"topic": "Your Google Messages bridged chats - user@example.com"},
     "sender": GMSG_BOT, "state_key": "", "type": "m.room.topic"},
    {"content": {"avatar_url": "mxc://maunium.net/yGOdcrJcwqARZqdzbfuxfhzb",
                 "displayname": "Google Messages bridge bot", "membership": "join"},
     "sender": GMSG_BOT, "state_key": GMSG_BOT, "type": "m.room.member"},
    {"content": {"displayname": "jkali", "membership": "invite"},
     "event_id": "$wRWc44oAA8sr4N6V4Vx4CnZGG9jqeaXvTk8s8CVi82E",
     "origin_server_ts": 1787780899154, "sender": GMSG_BOT, "state_key": SELF,
     "type": "m.room.member", "unsigned": {"age": 160753179}},
]}}

REAL_WA_DM = {"invite_state": {"events": [
    {"content": {"room_version": "11"}, "sender": WA_BOT, "state_key": "", "type": "m.room.create"},
    {"content": {"join_rule": "invite"}, "sender": WA_BOT, "state_key": "", "type": "m.room.join_rules"},
    {"content": {"com.beeper.exclude_from_timeline": True, "name": "Contact One"},
     "sender": WA_BOT, "state_key": "", "type": "m.room.name"},
    {"content": {"com.beeper.exclude_from_timeline": True, "topic": "WhatsApp private chat"},
     "sender": WA_BOT, "state_key": "", "type": "m.room.topic"},
    {"content": {"avatar_url": "mxc://maunium.net/NeXNQarUbrlYBiPCpprYsRqr",
                 "displayname": "WhatsApp bridge bot", "membership": "join"},
     "sender": WA_BOT, "state_key": WA_BOT, "type": "m.room.member"},
    {"content": {"com.beeper.exclude_from_timeline": True, "displayname": "jkali",
                 "membership": "invite"},
     "event_id": "$Gw2T1Ftm8Q2mvL6cMLLx11RPVeAIJxDJlFzdtiz27jQ",
     "origin_server_ts": 1787782241971, "sender": WA_BOT, "state_key": SELF,
     "type": "m.room.member", "unsigned": {"age": 159410362}},
]}}


def reshape(fixture, create_sender=Ellipsis, invite_sender=Ellipsis,
            name=Ellipsis, space_type=None):
    """tests/unit/user_invites.test.js's reshape(): a spoof fixture that is
    structurally identical to the genuine one in every other way."""
    events = copy.deepcopy(fixture["invite_state"]["events"])
    for e in events:
        if e.get("type") == "m.room.create" and create_sender is not Ellipsis:
            if create_sender is None:
                e.pop("sender", None)
            else:
                e["sender"] = create_sender
            if space_type is False:
                e["content"].pop("type", None)
            if space_type is True:
                e["content"]["type"] = "m.space"
        if (e.get("type") == "m.room.member" and e.get("state_key") == SELF
                and invite_sender is not Ellipsis):
            e["sender"] = invite_sender
        if e.get("type") == "m.room.name" and name is not Ellipsis:
            e["content"]["name"] = name
    return {"invite_state": {"events": events}}


def drop_type(fixture, type_):
    return {"invite_state": {"events": [e for e in fixture["invite_state"]["events"]
                                        if e.get("type") != type_]}}


def V(section=Ellipsis, bots=Ellipsis, self_=Ellipsis, spaces=Ellipsis, opts=Ellipsis):
    """One bridge_invites_to_join vector. An omitted key means the argument is
    absent on BOTH sides (JS `undefined` / Python's missing .get)."""
    v = {"kind": "bridge_invites_to_join"}
    if section is not Ellipsis:
        v["section"] = section
    if bots is not Ellipsis:
        v["bots"] = bots
    if self_ is not Ellipsis:
        v["self"] = self_
    if spaces is not Ellipsis:
        v["spaces"] = spaces
    if opts is not Ellipsis:
        v["opts"] = opts
    return v


def go(section, bots=BOTS, self_=SELF, spaces=SPACES, opts=None):
    return V(section, bots, self_, spaces, {} if opts is None else opts)


# ------------------------------------------------------------ case vectors --
def case_vectors():
    out = []
    # 1. The real bot invites: create.sender == invite sender == a known bot.
    out += [go({GMSG_DM_ID: REAL_GMSG_DM}),
            go({WA_DM_ID: REAL_WA_DM}),
            go({GMSG_DM_ID: REAL_GMSG_DM, WA_DM_ID: REAL_WA_DM})]
    # 2/3/4. A bridge ghost, the user themself, an unknown local account.
    for who in ("@gmessages_abc:localhost", SELF, "@evil:localhost",
                "@gmessagesbot:other", "@GMessagesBot:localhost", "notanmxid", ""):
        out.append(go({GMSG_DM_ID: reshape(REAL_GMSG_DM, who, who)}))
    out.append(V({GMSG_DM_ID: reshape(REAL_GMSG_DM, "notanmxid", "notanmxid")},
                 ["notanmxid"], SELF, SPACES, {}))
    # 5. No create event / no create sender / no invite_state at all.
    out += [go({GMSG_DM_ID: drop_type(REAL_GMSG_DM, "m.room.create")}),
            go({GMSG_DM_ID: reshape(REAL_GMSG_DM, None)}),
            go({GMSG_DM_ID: {}}),
            go({GMSG_DM_ID: {"invite_state": {}}}),
            go({GMSG_DM_ID: {"invite_state": {"events": "nope"}}}),
            go({GMSG_DM_ID: None}),
            go({GMSG_DM_ID: []})]
    # 6. The invite is addressed to someone else / no member events at all.
    other = {"invite_state": {"events": [
        dict(e, state_key="@someoneelse:localhost")
        if e.get("type") == "m.room.member" and e.get("state_key") == SELF else e
        for e in REAL_GMSG_DM["invite_state"]["events"]]}}
    out += [go({GMSG_DM_ID: other}),
            go({GMSG_DM_ID: drop_type(REAL_GMSG_DM, "m.room.member")})]
    # 7. Multiplicity: two DIFFERENT invite senders -> refused; a duplicate of
    #    the same bot-stamped invite is one distinct sender -> fine.
    doubled = {"invite_state": {"events": REAL_GMSG_DM["invite_state"]["events"] + [
        {"content": {"membership": "invite"}, "sender": "@evil:localhost",
         "state_key": SELF, "type": "m.room.member"}]}}
    dup_same = {"invite_state": {"events": REAL_GMSG_DM["invite_state"]["events"] + [
        {"content": {"membership": "invite"}, "sender": GMSG_BOT,
         "state_key": SELF, "type": "m.room.member"}]}}
    out += [go({GMSG_DM_ID: doubled}), go({GMSG_DM_ID: dup_same})]
    # 8. Cross-bridge laundering (both senders allowlisted, but different).
    out += [go({GMSG_DM_ID: reshape(REAL_GMSG_DM, WA_BOT, GMSG_BOT)}),
            go({GMSG_DM_ID: reshape(REAL_GMSG_DM, GMSG_BOT, WA_BOT)})]
    # 9. Space invites: the name/creator bind.
    out += [go({GMSG_SPACE_ID: REAL_GMSG_SPACE}),
            go({GMSG_SPACE_ID: reshape(REAL_GMSG_SPACE, WA_BOT, WA_BOT,
                                       "WhatsApp (+15551234567)")}),
            go({GMSG_SPACE_ID: reshape(REAL_GMSG_SPACE, name="WhatsApp (+15551234567)")}),
            go({GMSG_SPACE_ID: drop_type(REAL_GMSG_SPACE, "m.room.name")}),
            go({GMSG_SPACE_ID: reshape(REAL_GMSG_SPACE, name="Fake Google Messages")}),
            go({GMSG_SPACE_ID: reshape(REAL_GMSG_SPACE, name="")}),
            go({GMSG_SPACE_ID: reshape(REAL_GMSG_SPACE, name=None)}),
            go({GMSG_SPACE_ID: reshape(REAL_GMSG_SPACE, name=5)}),
            go({GMSG_SPACE_ID: reshape(REAL_GMSG_SPACE, name="Google Messages\n")}),
            go({WA_DM_ID: reshape(REAL_WA_DM, GMSG_BOT, GMSG_BOT, "WhatsApp (+15551234567)")}),
            V({GMSG_SPACE_ID: REAL_GMSG_SPACE}, BOTS, SELF, [], {}),
            V({GMSG_SPACE_ID: REAL_GMSG_SPACE}, BOTS, SELF, None, {}),
            V({GMSG_SPACE_ID: REAL_GMSG_SPACE}, BOTS, SELF,
              [None, 0, "", [], {}, {"spaceName": ""}, {"spaceName": 5},
               {"spaceName": "Google Messages"}], {})]
    # 9b. DISCORD CHILD SPACE: "Direct Messages" is a real m.space created by
    #     @discordbot:localhost (verified live) whose name does NOT start with
    #     the source spaceName. Admitted ONLY via childSpaceNames.
    disc_space = reshape(REAL_GMSG_SPACE, DISCORD_BOT, DISCORD_BOT, "Direct Messages")
    out += [go({DISCORD_SPACE_ID: disc_space}),
            # the same child name claimed by another bridge's bot -> refused
            go({DISCORD_SPACE_ID: reshape(REAL_GMSG_SPACE, WA_BOT, WA_BOT, "Direct Messages")}),
            # a prefix of the child name is not the child name (exact match only)
            go({DISCORD_SPACE_ID: reshape(REAL_GMSG_SPACE, DISCORD_BOT, DISCORD_BOT,
                                          "Direct Messages (2)")}),
            go({DISCORD_SPACE_ID: reshape(REAL_GMSG_SPACE, DISCORD_BOT, DISCORD_BOT, "Discord")}),
            go({DISCORD_SPACE_ID: reshape(REAL_GMSG_SPACE, DISCORD_BOT, DISCORD_BOT, "Discord X")}),
            # childSpaceNames dropped (shared/source_catalog.py's SPACE_SOURCES
            # view): the SAME vector must then be refused on BOTH sides.
            V({DISCORD_SPACE_ID: disc_space}, BOTS, SELF,
              [{"spaceName": s["spaceName"], "botMxid": s["botMxid"]} for s in SPACES], {}),
            # childSpaceNames of the wrong JSON type
            V({DISCORD_SPACE_ID: disc_space}, BOTS, SELF,
              [{"spaceName": "Discord", "botMxid": DISCORD_BOT,
                "childSpaceNames": "Direct Messages"}], {}),
            V({DISCORD_SPACE_ID: disc_space}, BOTS, SELF,
              [{"spaceName": "Discord", "botMxid": DISCORD_BOT,
                "childSpaceNames": [None, 5, "Direct Messages"]}], {})]
    # 10. Backpressure caps + determinism.
    def mk(n):
        return {"!r%03d:localhost" % i: REAL_GMSG_DM for i in range(n)}
    many = mk(120)
    for rid in sorted(many)[:100]:
        many[rid] = reshape(REAL_GMSG_DM, "@evil:localhost", "@evil:localhost")
    out += [go(mk(40)), go(many),
            go(mk(40), opts={"maxJoins": 3}), go(mk(40), opts={"maxExamine": 2}),
            go(mk(40), opts={"maxExamine": 0}), go(mk(40), opts={"maxJoins": 0}),
            go(mk(40), opts={"maxExamine": -5}), go(mk(40), opts={"maxJoins": -5}),
            go(mk(5), opts={"maxExamine": 2.5}), go(mk(5), opts={"maxJoins": 1.5}),
            go(mk(5), opts={"maxExamine": "3"}), go(mk(5), opts={"maxJoins": True}),
            go(mk(5), opts={"maxExamine": None}), go(mk(5), opts=[]),
            go(mk(5), opts="junk"), go(mk(5), opts={"maxExamine": 1e308})]
    # 11. Malformed ids; allowlist shapes; empty/absent inputs.
    bad = {"AzbbJwkTFwmNBWwNWB:localhost": REAL_GMSG_DM,
           "!noserver": REAL_GMSG_DM,
           "!x:example.org": REAL_GMSG_DM,
           "!y:local host": REAL_GMSG_DM,
           "!z:localhost:8008": REAL_GMSG_DM,
           "!trailing:localhost\n": REAL_GMSG_DM,
           "!сyrillic:localhost": REAL_GMSG_DM,
           "__proto__": REAL_GMSG_DM,
           "constructor": REAL_GMSG_DM,
           "toString": REAL_GMSG_DM,
           "": REAL_GMSG_DM}
    good_and_bad = dict(bad)
    good_and_bad[GMSG_DM_ID] = REAL_GMSG_DM
    out += [go(bad), go(good_and_bad),
            V({GMSG_DM_ID: REAL_GMSG_DM}, [GMSG_BOT], SELF, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, [WA_BOT], SELF, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, [None, None, 42, {}, GMSG_BOT], SELF, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, [], SELF, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, [None, None, 42], SELF, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, "notalist", SELF, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, BOTS, "", SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, BOTS, None, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, BOTS, 42, SPACES, {}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, BOTS, "@someoneelse:localhost", SPACES, {}),
            V(), V(None), V({}),
            V({GMSG_DM_ID: REAL_GMSG_DM}, BOTS, SELF)]
    # 12. localpart(), exhaustively, + the room-shape anchoring canaries.
    for m in ["@gmessagesbot:localhost", "@bob.smith_1=/+-:localhost:8008",
              "@bob!smith:localhost", "@bob smith:localhost", "bob:localhost",
              "@bob", "@bob:", "@:localhost", "@Bob:localhost", "", "@bob:localhost\n",
              "@bob\n:localhost", "@bob:local\nhost", "@bob:localhost\x00",
              "\n@bob:localhost", "@@bob:localhost", "@bob::localhost", None, 42,
              True, [], {}, 3.5]:
        out.append({"kind": "localpart", "mxid": m})
    out.append({"kind": "localpart"})
    for s in ["!ok:localhost", "!ok:master", "ok:localhost", "!ok:localhost\n",
              "!ok\n:localhost", "!:localhost", "!ok:localhost ", " !ok:localhost",
              "!o k:localhost", "!ok:localhost:8008", "!__proto__:localhost"]:
        out.append({"kind": "room_shape", "s": s})
    return out


# ------------------------------------------------------------------- fuzz ---
JUNK = [None, True, False, 0, 1, -1, 5, 9007199254740991, "", "junk", "0",
        "__proto__", "constructor", "toString", "prototype", "valueOf",
        "m.room.create", "m.room.member", "m.room.name", "m.space", "invite",
        SELF, GMSG_BOT, "@gmessages_abc:localhost", "@evil:localhost",
        GMSG_BOT + "\n", "Google Messages", "Google Messages\n", "Direct Messages",
        "WhatsApp", [], {}, [1, 2], {"a": 1}]
ROOM_KEYS = [GMSG_DM_ID, WA_DM_ID, GMSG_SPACE_ID, DISCORD_SPACE_ID,
             "!a:localhost", "!b:localhost", "!a:localhost\n", "!a:master",
             "a:localhost", "__proto__", "constructor", "", "5"]
EVENT_TYPES = ["m.room.create", "m.room.member", "m.room.name", "m.room.topic",
               "m.room.join_rules", "__proto__", "", None, 5]
STATE_KEYS = ["", SELF, GMSG_BOT, "@someoneelse:localhost", None, 0, False,
              "__proto__", " "]


class Gen(object):
    def __init__(self, seed):
        self.r = random.Random(seed)

    def pick(self, pool):
        return copy.deepcopy(self.r.choice(pool))

    def value(self, depth=0):
        """Junk of every JSON type at every nesting level."""
        if depth < 2 and self.r.random() < 0.25:
            if self.r.random() < 0.5:
                return [self.value(depth + 1) for _ in range(self.r.randint(0, 3))]
            return {str(self.pick(JUNK + ["name", "type", "sender", "membership",
                                          "state_key", "events", "invite_state"])):
                    self.value(depth + 1)
                    for _ in range(self.r.randint(0, 3))}
        return self.pick(JUNK)

    def event(self):
        if self.r.random() < 0.15:
            return self.value()
        e = {}
        if self.r.random() < 0.95:
            e["type"] = self.pick(EVENT_TYPES)
        if self.r.random() < 0.95:
            e["state_key"] = self.pick(STATE_KEYS)
        if self.r.random() < 0.9:
            e["sender"] = self.pick(JUNK + [GMSG_BOT, WA_BOT, DISCORD_BOT, SELF])
        if self.r.random() < 0.9:
            c = {}
            if self.r.random() < 0.7:
                c["membership"] = self.pick(["invite", "join", "leave", None, 5])
            if self.r.random() < 0.6:
                c["name"] = self.pick(JUNK + ["Google Messages (x)", "Direct Messages",
                                              "Discord", "WhatsApp (+1)"])
            if self.r.random() < 0.5:
                c["type"] = self.pick(["m.space", None, "m.room", 5, "m.space\n"])
            e["content"] = c if self.r.random() < 0.85 else self.value()
        return e

    def entry(self):
        if self.r.random() < 0.12:
            return self.value()
        events = [self.event() for _ in range(self.r.randint(0, 6))]
        if self.r.random() < 0.08:
            return {"invite_state": self.value()}
        return {"invite_state": {"events": events if self.r.random() < 0.92
                                 else self.value()}}

    def section(self):
        if self.r.random() < 0.08:
            return self.value()
        n = self.r.randint(0, 5)
        return {self.r.choice(ROOM_KEYS): self.entry() for _ in range(n)}

    def spaces(self):
        if self.r.random() < 0.15:
            return self.value()
        out = []
        for _ in range(self.r.randint(0, 4)):
            if self.r.random() < 0.2:
                out.append(self.pick(JUNK))
                continue
            s = {}
            if self.r.random() < 0.9:
                s["spaceName"] = self.pick(JUNK + ["Google Messages", "WhatsApp",
                                                   "Discord", "iMessage"])
            if self.r.random() < 0.9:
                s["botMxid"] = self.pick(JUNK + [GMSG_BOT, WA_BOT, DISCORD_BOT])
            if self.r.random() < 0.6:
                s["childSpaceNames"] = self.pick(
                    [["Direct Messages"], [], None, "Direct Messages", 5,
                     [None, "Direct Messages"], [[]]])
            out.append(s)
        if self.r.random() < 0.4:
            out = SPACES + out
        return out

    def bots(self):
        if self.r.random() < 0.15:
            return self.pick(JUNK)
        return [self.pick(JUNK + [GMSG_BOT, WA_BOT, DISCORD_BOT, SELF])
                for _ in range(self.r.randint(0, 4))] + (
            BOTS if self.r.random() < 0.6 else [])

    def opts(self):
        if self.r.random() < 0.2:
            return self.pick(JUNK)
        o = {}
        if self.r.random() < 0.8:
            o["maxExamine"] = self.pick([0, 1, 2, 3, 100, -1, 2.5, "3", None, True])
        if self.r.random() < 0.8:
            o["maxJoins"] = self.pick([0, 1, 2, 30, -1, 1.5, "2", None, False])
        return o


def fuzz_vectors(n, seed):
    g = Gen(seed)
    out = []
    for _ in range(n):
        out.append(V(g.section(), g.bots(),
                     g.pick(JUNK + [SELF, GMSG_BOT]), g.spaces(), g.opts()))
        out.append({"kind": "localpart", "mxid": g.value()})
    return out


# -------------------------------------------------------------- evaluators --
def eval_py(v):
    try:
        k = v["kind"]
        if k == "bridge_invites_to_join":
            return invites.bridge_invites_to_join(
                v.get("section"), v.get("bots"), v.get("self"),
                v.get("spaces"), v.get("opts"))
        if k == "localpart":
            return invites.localpart(v.get("mxid"))
        if k == "room_shape":
            return invites.ROOM_SHAPE_RE.fullmatch(v["s"]) is not None
        raise ValueError("unknown kind " + k)
    except Exception as e:  # a crash is a comparable outcome, not a skip
        return {"__error__": type(e).__name__}


def eval_js(vectors):
    node = os.environ.get("CONSENT_NODE", "docker")
    script = os.path.join("tests", "conformance", "invites_eval.mjs")
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
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def check_invariants(vectors, py, js):
    """Agreement is not enough: BOTH sides agreeing to join a ghost-created
    room would still be a hole. Every admitted id must be a well-shaped room id
    that the vector actually offered, and the counts must stay non-negative."""
    bad = []
    for i, v in enumerate(vectors):
        if v["kind"] != "bridge_invites_to_join":
            continue
        offered = set(v.get("section") or {}) if isinstance(v.get("section"), dict) else set()
        for side, r in (("py", py[i]), ("js", js[i])):
            if not isinstance(r, dict) or "join" not in r:
                continue            # crash/garbage: reported by the diff pass
            for rid in r["join"]:
                if not isinstance(rid, str) or invites.ROOM_SHAPE_RE.fullmatch(rid) is None:
                    bad.append((i, side, v, "admitted a malformed id"))
                elif rid not in offered:
                    bad.append((i, side, v, "admitted an id the vector never offered"))
            if len(set(r["join"])) != len(r["join"]):
                bad.append((i, side, v, "duplicate id in join"))
            if r["refusedNonBridge"] < 0 or r["overCap"] < 0:
                bad.append((i, side, v, "negative counter"))
    return bad


def main():
    cases = case_vectors()
    vectors = cases + fuzz_vectors(FUZZ_N, SEED)
    # Round-trip the Python side through JSON too, so neither implementation
    # sees a value the other could not have received.
    vectors = json.loads(json.dumps(vectors))
    py = [eval_py(v) for v in vectors]
    js = eval_js(vectors)
    assert len(js) == len(vectors), "node returned %d results for %d vectors" % (
        len(js), len(vectors))

    violations = check_invariants(vectors, py, js)
    if violations:
        print("INVARIANT VIOLATED on %d vector/side pair(s)" % len(violations))
        for i, side, v, why in violations[:10]:
            print("   - #%d [%s] %s\n       %s" % (i, side, why, canon(v)[:400]))
        sys.exit(1)

    # ACCEPTANCE: the Discord child-space class must actually be exercised and
    # must actually ADMIT, otherwise a childSpaceNames-dropping port would pass.
    disc = [i for i, v in enumerate(vectors)
            if v["kind"] == "bridge_invites_to_join"
            and isinstance(v.get("section"), dict) and DISCORD_SPACE_ID in v["section"]]
    admitted = [i for i in disc
                if isinstance(py[i], dict) and py[i].get("join") == [DISCORD_SPACE_ID]
                and isinstance(js[i], dict) and js[i].get("join") == [DISCORD_SPACE_ID]]
    if not admitted:
        print("NO ADMITTED DISCORD CHILD-SPACE VECTOR: this run proves nothing "
              "about childSpaceNames")
        sys.exit(1)

    mismatches, py_err, js_err = [], 0, 0
    for i, v in enumerate(vectors):
        pe = isinstance(py[i], dict) and "__error__" in py[i]
        je = isinstance(js[i], dict) and "__error__" in js[i]
        py_err += pe
        js_err += je
        if canon(py[i]) != canon(js[i]) or pe or je:
            mismatches.append((i, v, py[i], js[i]))

    print("invites conformance: %d vectors (%d cases + %d fuzz), seed=%d"
          % (len(vectors), len(cases), len(vectors) - len(cases), SEED))
    print("  discord child-space vectors=%d (admitted on both sides: %d)"
          % (len(disc), len(admitted)))
    print("  python errors=%d  js errors=%d  differing/erroring vectors=%d"
          % (py_err, js_err, len(mismatches)))
    if mismatches:
        groups = {}
        for i, v, p, j in mismatches:
            crash = ((isinstance(p, dict) and "__error__" in p)
                     or (isinstance(j, dict) and "__error__" in j))
            jp = p.get("join") if isinstance(p, dict) else None
            jj = j.get("join") if isinstance(j, dict) else None
            lab = "CRASH" if crash else ("JOIN DIFFERS" if jp != jj else "count drift")
            groups.setdefault((lab, v["kind"], canon(p), canon(j)), []).append((i, v))
        order = {"JOIN DIFFERS": 0, "CRASH": 1, "count drift": 2}
        print("  %d distinct divergence classes" % len(groups))
        for (lab, kind, p, j), items in sorted(
                groups.items(), key=lambda kv: (order[kv[0][0]], -len(kv[1])))[:30]:
            i, v = items[0]
            print("   - [%s] %s x%d\n       py=%s\n       js=%s\n       e.g. #%d %s"
                  % (lab, kind, len(items), p, j, i, canon(v)[:600]))
        sys.exit(1)
    print("OK: JS and Python invite predicates agree on every vector, no crashes")


if __name__ == "__main__":
    main()
