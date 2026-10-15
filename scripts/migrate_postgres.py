"""One-way default deployment migration. Original container data and dump remain intact."""

import json
import os
import subprocess

from native_database import NativePostgres
from workmemory.config import ROOT

# psql executes one generated SELECT per table. Only counts and SHA256 digests leave
# PostgreSQL; canonical JSON plus sorted row hashes makes physical row order irrelevant.
MANIFEST_SQL = r"""
SET timezone = 'UTC';
SET extra_float_digits = 3;
SELECT format(
  $query$SELECT jsonb_build_object('table', %L, 'count', count(*), 'sha256', encode(sha256(convert_to(coalesce(string_agg(h, '' ORDER BY h), ''), 'UTF8')), 'hex')) FROM (SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex') h FROM %I.%I t) records;$query$,
  schemaname || '.' || tablename, schemaname, tablename)
FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename
\gexec
SELECT jsonb_build_object('sequence', sequencename, 'last_value', last_value,
    'start', start_value, 'increment', increment_by, 'max', max_value, 'min', min_value, 'cycle', cycle)
FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename;
SELECT jsonb_build_object('columns', jsonb_agg(x ORDER BY table_name, ordinal_position))
FROM (SELECT table_name, column_name, ordinal_position, data_type, udt_name, is_nullable, column_default
      FROM information_schema.columns WHERE table_schema='public') x;
"""


def parse_manifest(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def source_manifest(manager):
    result = subprocess.run(
        manager.compose_args(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -XAtq -v ON_ERROR_STOP=1',
        ),
        input=MANIFEST_SQL.encode(),
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError("旧数据库内容校验失败，未进行切换")
    return parse_manifest(result.stdout.decode())


def native_manifest(pg, database):
    return parse_manifest(pg.sql(MANIFEST_SQL, database))


def migrate():
    import manager

    pg = NativePostgres()
    if pg.marker.exists():
        pg.check_marker()
        pg.start()
        print("原生数据库已经就绪，保留现有数据")
        return
    old = ROOT / "data/postgres/PG_VERSION"
    if not old.exists():
        pg.prepare()
        pg.start()
        if not pg.database_exists(pg.settings.database.name):
            pg.create_database(pg.settings.database.name)
        pg.mark_ready()
        print("已初始化本项目原生 PostgreSQL")
        return
    # Refuse an active API during data cutover. The management operation stops it first.
    if manager.owns_process("backend"):
        raise RuntimeError("迁移前请先停止本项目后端，避免迁移过程中产生写入")
    if pg.initialized() and pg.running():
        pg.stop()
    manager.docker_ready()
    manager.assert_container_ownership()
    manager.run(
        manager.compose_args("up", "-d", "--wait", "postgres"), "临时启动旧 PostgreSQL 导出数据"
    )
    from datetime import datetime

    backup = (
        ROOT
        / "data/backups"
        / ("docker-migration-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".dump")
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    partial = backup.with_suffix(".partial")
    with partial.open("xb") as stream:
        result = subprocess.run(
            manager.compose_args(
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner',
            ),
            stdout=stream,
            stderr=subprocess.PIPE,
        )
    if result.returncode:
        raise RuntimeError("旧数据库导出失败；原始数据保留，未进行切换")
    pg.run("pg_restore", "--list", partial)
    partial.chmod(0o600)
    partial.rename(backup)
    expected = source_manifest(manager)
    manager.run(manager.compose_args("stop", "postgres"), "停止旧数据库，释放端口")
    pg.prepare(migration=True)
    pg.start()
    verification = pg.restore_check(backup)
    name = pg.settings.database.name
    if pg.database_exists(name):
        # A previous interrupted restore must be inspected; never drop a nonempty destination.
        tables = int(
            pg.sql(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'", name
            )
        )
        if tables:
            actual = native_manifest(pg, name)
            if actual != expected:
                raise RuntimeError("目标库已有不同内容，拒绝覆盖；备份已保留")
        else:
            pg.run(
                "pg_restore",
                "--no-owner",
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                name,
                backup,
                timeout=300,
            )
    else:
        pg.create_database(name)
        pg.run(
            "pg_restore",
            "--no-owner",
            "--exit-on-error",
            "--single-transaction",
            "--dbname",
            name,
            backup,
            timeout=300,
        )
    actual = native_manifest(pg, name)
    if actual != expected:
        raise RuntimeError("恢复前后完整内容校验不一致，未标记迁移完成")
    pg.mark_ready(backup, {**verification, "tables": actual})
    print(f"原生 PostgreSQL 迁移完成：{sum(1 for row in actual if 'table' in row)} 张表内容及序列校验一致；旧数据和备份保留")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        import manager

        with manager.operation_lock():
            migrate()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(
            str(exc)
            if isinstance(exc, RuntimeError)
            else "迁移未完成，原始数据及已生成备份保留，请检查服务状态"
        )
        raise SystemExit(1) from None
