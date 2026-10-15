"""Verify native restart, ownership, and nonempty backup restore in an isolated cluster.

No business process, database, or capture preference is changed. Swift spool recovery
is covered separately by SpoolCheck and the isolated collector integration tests.
"""
import json
import socket
import tempfile

from alembic import command
from alembic.config import Config
from migrate_postgres import native_manifest
from native_database import NativePostgres
from workmemory.config import ROOT, get_settings


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = get_settings().model_copy(deep=True)
    settings.database.port = port
    settings.database.name = "workmemory_verify"
    (ROOT / ".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pg-verify-", dir=ROOT / ".cache") as folder:
        pg = NativePostgres(settings, root=folder)
        try:
            pg.prepare()
            pg.start()
            pg.create_database(settings.database.name)
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["database_url"] = settings.database_url
            command.upgrade(config, "head")
            pg.sql("INSERT INTO projects(id,name,state) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','synthetic persistence marker','active')", settings.database.name)
            original = native_manifest(pg, settings.database.name)
            pid = (pg.data / "postmaster.pid").read_text().splitlines()[0]
            pg.start()
            assert (pg.data / "postmaster.pid").read_text().splitlines()[0] == pid
            pg.stop()
            pg.start()
            assert native_manifest(pg, settings.database.name) == original
            dump = pg.backup()
            restored = "workmemory_restore_verification"
            pg.create_database(restored)
            pg.run("pg_restore", "--no-owner", "--exit-on-error", "--dbname", restored, dump)
            assert native_manifest(pg, restored) == original
            pg.restore_check(dump)
            pg.mark_ready(dump)
            pg.check_marker()
            report = {"native_restart_persistence": "passed", "repeated_start_same_process": "passed",
                      "full_table_backup_restore": "passed", "project_ownership": "passed",
                      "business_database_touched": False, "collector_preference_touched": False}
        finally:
            pg.stop()
    output = ROOT / "data/exports/infra-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
