#!/usr/bin/env python3
"""Run all unit scripts and consent parity with the installed runtimes."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-only", action="store_true")
    args = parser.parse_args()
    env = dict(os.environ, CONSENT_NODE=os.environ.get("CONSENT_NODE", "node"))
    for command in ([sys.executable, "--version"], ["node", "--version"]):
        subprocess.run(command, check=True)
    failed = []
    files = sorted((ROOT / "tests/unit").glob("*.test.*"))
    for path in files:
        if path.suffix not in (".js", ".py"):
            continue
        print("\n==", path.name, "==", flush=True)
        command = ["node" if path.suffix == ".js" else sys.executable, str(path)]
        if subprocess.call(command, cwd=ROOT, env=env):
            failed.append(path.name)
    if not args.unit_only:
        if subprocess.call([sys.executable, "tests/conformance/consent_conformance.py"], cwd=ROOT, env=env):
            failed.append("consent_conformance")
    print("\nFailed: " + ", ".join(failed) if failed else "\nAll discovered checks passed.")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
