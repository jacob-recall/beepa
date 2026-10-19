"""Run CRM SELECTs on synthetic PostgreSQL bridge schemas.

The integration harness supplies run_sql(sql)->stdout using ONLY its manifest's
disposable Postgres. This module has no default connection/credentials and
cannot discover or connect to an installed bridge database.
"""
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents.enrich.crm_resolver import provider_query


def run_fixture(run_sql):
    schema = "crm_resolver_" + uuid.uuid4().hex
    run_sql("CREATE SCHEMA " + schema)
    def execute(sql):
        return run_sql("SET search_path TO " + schema + ";\n" + sql)
    def rows(source):
        raw = execute(provider_query(source))
        # A harness may use psql -qAt; ignore SET command tags if it doesn't.
        return [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
    try:
        execute('''
            CREATE TABLE portal(bridge_id TEXT,id TEXT,receiver TEXT,mxid TEXT,room_type TEXT,other_user_id TEXT);
            CREATE TABLE user_portal(bridge_id TEXT,user_mxid TEXT,login_id TEXT,portal_id TEXT,portal_receiver TEXT);
            CREATE TABLE user_login(bridge_id TEXT,user_mxid TEXT,id TEXT,space_room TEXT,metadata JSONB);
            CREATE TABLE ghost(bridge_id TEXT,id TEXT,identifiers JSONB,metadata JSONB);
            CREATE TABLE whatsmeow_lid_map(lid TEXT,pn TEXT);
            INSERT INTO user_login VALUES ('','@user:fixture','login','!space:fixture','{"credential":"must-not-be-selected"}');
            INSERT INTO ghost VALUES ('','123','[]','{"phone":"+14155552671"}');
            INSERT INTO ghost VALUES ('','lid-456','[]','{}');
            INSERT INTO whatsmeow_lid_map VALUES ('456','14155552672');
            INSERT INTO portal VALUES
                ('','good','login','!good:fixture','dm','123'),
                ('','lid','login','!lid:fixture','dm','lid-456'),
                ('','group','login','!group:fixture','group_dm','123'),
                ('','wrong-owner','login','!wrong-owner:fixture','dm','123'),
                ('','wrong-receiver','login','!wrong-receiver:fixture','dm','123'),
                ('','wrong-bridge','login','!wrong-bridge:fixture','dm','123'),
                ('','orphan-login','login','!orphan:fixture','dm','123'),
                ('','wrong-ghost','login','!wrong-ghost:fixture','dm','foreign-ghost');
            INSERT INTO ghost VALUES ('old','foreign-ghost','[]','{"phone":"+14155552671"}');
            INSERT INTO user_portal VALUES
                ('','@user:fixture','login','good','login'),
                ('','@user:fixture','login','lid','login'),
                ('','@user:fixture','login','group','login'),
                ('','@foreign:fixture','login','wrong-owner','login'),
                ('','@user:fixture','login','wrong-receiver','different'),
                ('old','@user:fixture','login','wrong-bridge','login'),
                ('','@user:fixture','deleted-login','orphan-login','login'),
                ('','@user:fixture','login','wrong-ghost','login');
        ''')
        counts = {}
        for source in ("whatsapp", "gmessages", "instagram", "linkedin", "twitter"):
            result = rows(source)
            assert {row["room_id"] for row in result} == {"!good:fixture", "!lid:fixture"}, source
            assert all(row["owner"] == "@user:fixture" and row["login_id"] == "login" for row in result), source
            assert "must-not-be-selected" not in json.dumps(result), source
            if source == "whatsapp":
                assert next(row["phone"] for row in result if row["room_id"] == "!lid:fixture") == "14155552672"
            else:
                assert next(row["phone"] for row in result if row["room_id"] == "!good:fixture") == "+14155552671"
            counts[source] = len(result)
        execute('''
            DROP TABLE portal;
            CREATE TABLE portal(dcid TEXT,receiver TEXT,type INTEGER,other_user_id TEXT,dc_guild_id TEXT,mxid TEXT);
            CREATE TABLE "user"(mxid TEXT,dcid TEXT,space_room TEXT,discord_token TEXT);
            INSERT INTO "user" VALUES ('@user:fixture','789','!space:fixture','must-not-be-selected');
            INSERT INTO portal VALUES
                ('dm','789',1,'123',NULL,'!dm:fixture'),
                ('dm-empty','789',1,'124','','!dm-empty:fixture'),
                ('group','789',3,'123',NULL,'!group:fixture'),
                ('guild','789',0,'123','456','!guild:fixture'),
                ('wrong-receiver','999',1,'123',NULL,'!foreign:fixture');
        ''')
        result = rows("discord")
        assert {row["room_id"] for row in result} == {"!dm:fixture", "!dm-empty:fixture"}
        assert all(row["owner"] == "@user:fixture" and row["login_id"] == "789" for row in result)
        assert "must-not-be-selected" not in json.dumps(result)
        counts["discord"] = len(result)
        return counts
    finally:
        run_sql("DROP SCHEMA " + schema + " CASCADE")
