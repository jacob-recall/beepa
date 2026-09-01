#!/usr/bin/env python3
# Emit a Synapse ed25519 signing-key line:  "ed25519 <key_id> <base64 seed>".
# Pure stdlib, no Docker — a robust fallback for render-hub.sh when the Synapse
# image can't run to produce one (e.g. an amd64 image on arm64 without Rosetta,
# which can exit 0 yet write nothing). Matches signedjson's write_signing_keys
# format: algorithm, a short version/key-id, and the base64 (unpadded) of the
# 32-byte ed25519 seed.
import base64, os, secrets, string

key_id = "a_" + "".join(secrets.choice(string.ascii_letters) for _ in range(4))
seed = base64.b64encode(os.urandom(32)).decode().rstrip("=")
print("ed25519 %s %s" % (key_id, seed))
