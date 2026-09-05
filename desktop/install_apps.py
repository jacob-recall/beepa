#!/usr/bin/env python3
"""Install per-user macOS browser launchers without touching account state."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

APPS = {
    "user": ("Beepa", "org.beepa.launcher.user", "http://127.0.0.1:8011/apps/user/index.html"),
    "master": ("Beepa Master", "org.beepa.launcher.master", "http://127.0.0.1:8011/apps/master/index.html"),
}
VERSION = "4"
ASSETS = Path(__file__).resolve().parent / "assets"


def validate_url(value):
    """URLs are configuration, never credentials or executable source."""
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("App URL must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("App URL must be a complete http:// or https:// web address")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("App URL must not contain login credentials")
    _ = parsed.port  # Reject malformed/out-of-range ports as well.
    return value


def apple_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launcher_source(name, url):
    # No browser automation, Python dependency at launch, secrets, repo paths,
    # service reset or native bridge executable. Use the user's default browser.
    return f'''on run
    set appURL to {apple_string(url)}
    repeat
        try
            do shell script "/usr/bin/curl --globoff --noproxy '*' --fail --silent --output /dev/null --connect-timeout 2 --max-time 4 " & quoted form of appURL
            exit repeat
        on error
            set choice to button returned of (display dialog "The Beepa interface is not responding yet. Check that Docker and Beepa services are running. For a remote master, also check Tailscale." with title {apple_string(name)} buttons {{"Cancel", "Open anyway", "Retry"}} default button "Retry" cancel button "Cancel")
            if choice is "Open anyway" then exit repeat
        end try
    end repeat
    do shell script "/usr/bin/open " & quoted form of appURL
end run
'''


def install_app(role, applications_dir, url=None):
    name, bundle_id, default_url = APPS[role]
    icon = ASSETS / (role + ".icns")
    icon_hash = hashlib.sha256(icon.read_bytes()).hexdigest()
    destination = Path(applications_dir).expanduser() / (name + ".app")
    existing = {}
    if destination.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {destination}")
    if destination.exists():
        try:
            existing = plistlib.loads((destination / "Contents/Info.plist").read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            raise ValueError(f"Not a managed Beepa launcher: {destination}") from exc
        if existing.get("CFBundleIdentifier") != bundle_id or not existing.get("BeepaLauncher"):
            raise ValueError(f"Refusing to replace another application: {destination}")
    # Reinstall keeps a configured tailnet/custom address unless explicitly changed.
    url = validate_url(url or existing.get("BeepaURL") or default_url)
    source = launcher_source(name, url)
    if (existing.get("BeepaURL") == url and existing.get("BeepaLauncherVersion") == VERSION
            and existing.get("BeepaIconHash") == icon_hash
            and (destination / "Contents/Resources/Beepa.icns").is_file()
            and (destination / "Contents/Resources/Scripts/main.scpt").is_file()
            and os.access(destination / "Contents/MacOS/applet", os.X_OK)):
        try:
            subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(destination)], check=True)
            return destination
        except subprocess.CalledProcessError:
            pass  # Rebuild a damaged launcher instead of preserving a broken icon.

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".beepa-app-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        source_file = stage / "launcher.applescript"
        source_file.write_text(source)
        bundle = stage / destination.name
        subprocess.run(["/usr/bin/osacompile", "-o", str(bundle), str(source_file)], check=True)
        info_path = bundle / "Contents/Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update(CFBundleIdentifier=bundle_id, CFBundleName=name,
                    CFBundleDisplayName=name, CFBundleVersion=VERSION,
                    CFBundleShortVersionString="1.1", CFBundleIconFile="Beepa.icns",
                    BeepaIconHash=icon_hash, BeepaLauncher=True,
                    BeepaLauncherVersion=VERSION, BeepaURL=url)
        shutil.copyfile(icon, bundle / "Contents/Resources/Beepa.icns")
        info_path.write_bytes(plistlib.dumps(info))
        # osacompile signs its output; changing Info.plist invalidates that
        # signature. Sign ONLY this freshly generated launcher, never a bridge.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(bundle)], check=True)
        # Keep the installed bundle intact if compilation fails. If activation
        # fails, restore it. Updates reuse the same path and bundle identifier.
        previous = stage / "previous.app"
        if destination.exists():
            destination.rename(previous)
        try:
            bundle.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("user", "master", "both"), default="both")
    parser.add_argument("--applications-dir", type=Path, default=Path.home() / "Applications")
    parser.add_argument("--user-url", default=os.environ.get("BEEPA_USER_APP_URL"))
    parser.add_argument("--master-url", default=os.environ.get("BEEPA_MASTER_APP_URL"))
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("macOS app launchers can only be installed on macOS")
    roles = ("user", "master") if args.role == "both" else (args.role,)
    try:
        for role in roles:
            url = getattr(args, role + "_url")
            if url:
                validate_url(url)
        for role in roles:
            path = install_app(role, args.applications_dir, getattr(args, role + "_url"))
            print(f"Installed: {path}\nOpen it from Finder, or drag it to the Dock.")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"App installation failed: {exc}\n")


if __name__ == "__main__":
    main()
