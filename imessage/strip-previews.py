#!/usr/bin/env python3
# Remove SwiftUI #Preview blocks from a Swift source tree.
#
# Xcode's preview macros (#Preview / @Previewable, from module PreviewsMacros)
# are not available to a headless command-line `swift build`, so any #Preview
# block fails compilation with "plugin for module 'PreviewsMacros' not found".
# The blocks are editor-only (Xcode canvas) and never part of the built product,
# so removing them is always safe. build-cli.sh runs this on the freshly-checked-
# out Beeper source before `swift build`, so every teammate's build succeeds
# without hand-editing vendored source.
#
# Usage: strip-previews.py <source-dir>
import os, re, sys

root = sys.argv[1]
changed = 0
for dp, _, files in os.walk(root):
    for fn in files:
        if not fn.endswith(".swift"):
            continue
        path = os.path.join(dp, fn)
        with open(path) as f:
            lines = f.read().split("\n")
        out, i, removed = [], 0, False
        while i < len(lines):
            if re.match(r'^\s*#Preview\b', lines[i]):
                # drop a preceding @available attribute + any blank line(s)
                while out and re.match(r'^\s*@available\b', out[-1]):
                    out.pop()
                while out and out[-1].strip() == "":
                    out.pop()
                # consume through the brace-balanced close of the #Preview block
                depth, started, j = 0, False, i
                while j < len(lines):
                    depth += lines[j].count("{") - lines[j].count("}")
                    if "{" in lines[j]:
                        started = True
                    if started and depth <= 0:
                        break
                    j += 1
                i = j + 1
                removed = True
                continue
            out.append(lines[i])
            i += 1
        if removed:
            with open(path, "w") as f:
                f.write("\n".join(out))
            changed += 1
print(f"[strip-previews] removed #Preview block(s) from {changed} file(s)")
