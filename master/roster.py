"""Additive roster resolution; removing an account is an explicit admin action."""
import re


def resolve_roster(persisted, recorded, additions):
    result = []
    for name in (persisted + " " + recorded + " " + additions).split():
        if not re.fullmatch(r"[a-z0-9]{1,64}", name) or name == "manager":
            raise ValueError("Invalid teammate localpart; use lowercase letters and digits, excluding manager")
        if name not in result:
            result.append(name)
    return result


if __name__ == "__main__":
    import sys
    print(" ".join(resolve_roster(*sys.argv[1:])))
