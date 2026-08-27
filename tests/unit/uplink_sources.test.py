#!/usr/bin/env python3
"""Regression test for uplink source detection (sources_from_sync).

Real bridge spaces are named with a suffix — "WhatsApp (+14146149941)",
"Google Messages (a@b.com)" — while the synthetic integration harness used bare
labels ("iMessage", "LinkedIn"). The uplink originally matched space names
EXACTLY, so it recognized the harness spaces but NOT real ones (found live
2026-08-27). It must prefix-match, mirroring shared/ui/sources.js
(buildConvos uses name.startsWith(spaceName)). Run: python3 this_file.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import uplink

sf = uplink.Uplink.sources_from_sync

def space(name, child):
    return {"state": {"events": [
        {"type": "m.room.name", "content": {"name": name}},
        {"type": "m.space.child", "state_key": child, "content": {"via": ["x"]}}]}}
def conv():
    return {"state": {"events": [{"type": "m.room.name", "content": {"name": "chat"}}]}}

sync = {"rooms": {"join": {
    "!ex:localhost":    space("iMessage", "!c1:localhost"),                  # exact (harness)
    "!wa:localhost":    space("WhatsApp (+14146149941)", "!c2:localhost"),   # real suffixed
    "!gm:localhost":    space("Google Messages (a@b.com)", "!c3:localhost"), # real suffixed
    "!li:localhost":    space("LinkedIn", "!c4:localhost"),                  # exact
    "!other:localhost": space("Zero2One @ UIUC", "!c5:localhost"),           # NOT a source
    "!c1:localhost": conv(), "!c2:localhost": conv(), "!c3:localhost": conv(),
    "!c4:localhost": conv(), "!c5:localhost": conv(),
}}}

got = sf(sync)
expected = {"!c1:localhost": "imessage", "!c2:localhost": "whatsapp",
            "!c3:localhost": "gmessages", "!c4:localhost": "linkedin"}
fails = []
if got != expected:
    fails.append("source map %r != expected %r" % (got, expected))
if "!c5:localhost" in got:
    fails.append("over-match: non-source space 'Zero2One @ UIUC' assigned a source")

if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("PASS: exact + real-suffixed source spaces resolve; no over-match (5 checks)")
