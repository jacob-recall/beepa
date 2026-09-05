#!/usr/bin/env python3
"""Disposable local AND master Synapse; never discovers production credentials."""
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[2]


def load_manifest():
    raw = os.environ.get("SYNCTEST_MANIFEST")
    if not raw:
        raise RuntimeError("Run tests/integration/run.sh; a disposable SYNCTEST_MANIFEST is required")
    path = Path(raw).resolve()
    data = json.loads(path.read_text())
    marker = json.loads((path.parent / ".beepa-test-root").read_text())
    if (not re.fullmatch(r"beepa-test-[0-9a-f]{12}", data.get("project", ""))
            or marker.get("project") != data["project"]):
        raise RuntimeError("Invalid disposable test-root marker")
    for key in ("local_url", "master_url"):
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", data[key]):
            raise RuntimeError("Test URLs must be allocated loopback ports")
        if int(data[key].rsplit(":", 1)[1]) in (8008, 8018, 8028):
            raise RuntimeError("Refusing production or legacy shared test endpoint")
    for key in ("master_dir", "state_dir"):
        if path.parent not in Path(data[key]).resolve().parents:
            raise RuntimeError("Test state must remain inside the marked root")
    if data.get("master_container") != data["project"] + "-master-1":
        raise RuntimeError("Refusing a container outside the disposable project")
    return data


class Sandbox:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="beepa-test-")
        self.root = Path(self.temporary.name)
        self.project = "beepa-test-" + uuid.uuid4().hex[:12]
        (self.root / ".beepa-test-root").write_text(json.dumps({"project": self.project}))
        self.compose = self.root / "compose.json"
        self.env = dict(os.environ)
        self.env["PATH"] = "/Applications/Docker.app/Contents/Resources/bin:" + self.env.get("PATH", "")

    def docker(self, *args, **kwargs):
        return subprocess.run(["docker", "compose", "-p", self.project, "-f", str(self.compose), *args],
                              env=self.env, check=True, **kwargs)

    def prepare(self):
        images = re.findall(r"image: (\S+)", (REPO / "docker-compose.yml").read_text())
        postgres, synapse = images[:2]
        services, volumes = {}, {}
        local_secret = secrets.token_hex(32)
        port_reservations = []
        for _ in range(2):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port_reservations.append(listener)
        for role, server in (("local", "localhost"), ("master", "master")):
            data = self.root / role / "synapse"
            data.mkdir(parents=True)
            password = secrets.token_hex(32)
            registration = local_secret if role == "local" else secrets.token_hex(32)
            config = {
                "server_name": server, "report_stats": False,
                "listeners": [{"port": 8008, "tls": False, "type": "http", "resources": [{"names": ["client"]}]}],
                "database": {"name": "psycopg2", "args": {"user": "matrix", "password": password,
                    "dbname": "synapse", "host": role + "-db", "cp_min": 1, "cp_max": 5}},
                "media_store_path": "/data/media", "signing_key_path": "/data/signing.key",
                "registration_shared_secret": registration, "enable_registration": False,
                "macaroon_secret_key": secrets.token_hex(32), "form_secret": secrets.token_hex(32),
                "trusted_key_servers": [], "federation_domain_whitelist": [],
                "presence": {"enabled": False}, "caches": {"sync_response_cache_duration": "0s"},
                "rc_login": {k: {"per_second": 100, "burst_count": 1000} for k in ("address", "account", "failed_attempts")},
                "rc_message": {"per_second": 1000, "burst_count": 10000},
                "rc_room_creation": {"per_second": 100, "burst_count": 1000},
                "rc_invites": {k: {"per_second": 100, "burst_count": 1000}
                               for k in ("per_room", "per_user", "per_issuer")},
                "rc_registration": {"per_second": 100, "burst_count": 1000},
                "rc_joins": {"local": {"per_second": 100, "burst_count": 1000}},
            }
            (data / "homeserver.yaml").write_text(json.dumps(config))
            (data / ".secrets.local").write_text("REGISTRATION_SHARED_SECRET='" + registration
                + "'\nTEAMMATE_PASSWORD_KEY='" + secrets.token_hex(32) + "'\n")
            volume = role + "-db"
            volumes[volume] = {}
            services[role + "-db"] = {"image": postgres,
                "environment": {"POSTGRES_USER": "matrix", "POSTGRES_PASSWORD": password,
                    "POSTGRES_DB": "synapse", "POSTGRES_INITDB_ARGS": "--encoding=UTF8 --lc-collate=C --lc-ctype=C"},
                "volumes": [volume + ":/var/lib/postgresql/data"],
                "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U matrix -d synapse"], "interval": "1s", "timeout": "3s", "retries": 60}}
            services[role] = {"image": synapse, "environment": {"UID": str(os.getuid()), "GID": str(os.getgid())},
                "volumes": [str(data) + ":/data"], "ports": ["127.0.0.1:%d:8008" %
                    port_reservations[0 if role == "local" else 1].getsockname()[1]],
                "depends_on": {role + "-db": {"condition": "service_healthy"}}}
        self.compose.write_text(json.dumps({"services": services, "volumes": volumes}))
        for listener in port_reservations:
            listener.close()
        self.docker("up", "-d", stdout=subprocess.DEVNULL)
        urls = {}
        for role in ("local", "master"):
            address = self.docker("port", role, "8008", capture_output=True, text=True).stdout.strip()
            urls[role] = "http://" + address
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(urls[role] + "/health", timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError(role + " test Synapse did not start")
        self.env.update(BEEPA_MASTER_STATE_DIR=str(self.root / "master"), MASTER_CS_BASE=urls["master"],
                        MASTER_PUBLIC_URL=urls["master"], TEAMMATES="alice bob")
        subprocess.run(["bash", str(REPO / "master/provision.sh")], env=self.env, check=True,
                       stdout=subprocess.DEVNULL)
        state = self.root / "state"
        state.mkdir()
        manifest = {"project": self.project, "local_url": urls["local"], "master_url": urls["master"],
                    "local_secret": local_secret, "master_dir": str(self.root / "master"),
                    "state_dir": str(state), "master_container": self.project + "-master-1"}
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        path.chmod(0o600)
        self.env["SYNCTEST_MANIFEST"] = str(path)
        return self.env

    def close(self):
        marker = json.loads((self.root / ".beepa-test-root").read_text())
        if marker.get("project") != self.project or not self.project.startswith("beepa-test-"):
            raise RuntimeError("Refusing cleanup outside disposable test project")
        if self.compose.exists():
            self.docker("down", "-v", stdout=subprocess.DEVNULL)
        self.temporary.cleanup()


def main():
    sandbox = Sandbox()
    try:
        env = sandbox.prepare()
        args = sys.argv[1:]
        targets = {"--enrollment": "test_enroll.py", "--roster": "test_roster.py", "--recovery": "test_recovery.py",
                   "--lifecycle": "test_lifecycle.py"}
        target = targets.get(args[0] if args else "", "harness.py")
        if target != "harness.py":
            args = args[1:]
        return subprocess.call([sys.executable, str(Path(__file__).with_name(target)), *args], env=env)
    finally:
        sandbox.close()


if __name__ == "__main__":
    sys.exit(main())
