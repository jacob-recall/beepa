#!/usr/bin/env python3
# Minimal, portable envsubst-with-allowlist (macOS has no gettext/envsubst).
# Replaces ONLY the named ${VAR} placeholders with their env values, leaving
# mautrix's own ${...} config references untouched.
#   argv: <template> <dest> VAR1 VAR2 ...
import os, sys
tmpl, dest = sys.argv[1], sys.argv[2]
with open(tmpl) as f:
    text = f.read()
for v in sys.argv[3:]:
    if v not in os.environ:
        sys.exit(f"render: ${{{v}}} not in environment")
    text = text.replace("${%s}" % v, os.environ[v])
with open(dest, "w") as f:
    f.write(text)
