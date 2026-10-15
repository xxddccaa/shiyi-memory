"""Project-owned PostgreSQL 17, without Docker or global brew services."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from workmemory.config import ROOT, get_settings


def postgres_bin():
    candidates = []
    brew = shutil.which("brew")
    if brew:
        result = subprocess.run([brew, "--prefix", "postgresql@17"], capture_output=True, text=True)
        if result.returncode == 0:
            candidates.append(Path(result.stdout.strip()) / "bin")
    candidates += [
        Path("/opt/homebrew/opt/postgresql@17/bin"),
        Path("/usr/local/opt/postgresql@17/bin"),
    ]
    for path in candidates:
        if (path / "postgres").is_file():
            version = subprocess.run(
                [str(path / "postgres"), "--version"], capture_output=True, text=True
            )
            if re.search(r"\b17\.", version.stdout):
                return path.resolve()
    raise RuntimeError(
        "未安装原生 PostgreSQL 17。请执行 brew install postgresql@17，或选择安装／修复。"
    )


class NativePostgres:
    def __init__(self, settings=None, root=ROOT):
        self.settings = settings or get_settings()
        self.root = Path(root).resolve()
        self.data = self.root / "data/postgres-native"
        self.owner = self.data / "workmemory-owner.json"
        self.marker = self.root / "config/database.local.json"
        self.bin = postgres_bin()
        self.env = {
            **os.environ,
            "PGPASSWORD": self.settings.postgres_password,
            "PGHOST": "127.0.0.1",
            "PGPORT": str(self.settings.database.port),
            "PGUSER": self.settings.database.user,
            "PGDATABASE": self.settings.database.name,
            "PGCONNECT_TIMEOUT": "5",
        }
        if self.settings.database.host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("原生数据库仅管理本项目的本机地址")

    def run(self, name, *args, **kwargs):
        result = subprocess.run(
            [str(self.bin / name), *map(str, args)],
            env=self.env,
            capture_output=True,
            timeout=kwargs.pop("timeout", 60),
            **kwargs,
        )
        if result.returncode:
            # PostgreSQL diagnostics can contain SQL/data; retain no unredacted command/error.
            raise RuntimeError(
                f"PostgreSQL {name} 未完成（退出码 {result.returncode}），请检查本项目数据库状态"
            )
        return result.stdout

    def check_path(self):
        if (
            self.data.is_symlink()
            or self.data.resolve() != self.root / "data/postgres-native"
            or self.owner.is_symlink()
        ):
            raise RuntimeError("数据库目录不属于本项目，拒绝操作")

    def check_owned(self):
        self.check_path()
        try:
            owner = json.loads(self.owner.read_text())
        except (OSError, ValueError):
            raise RuntimeError("数据库缺少项目归属标记，拒绝接管") from None
        if (
            owner != {"root": str(self.root), "data_directory": str(self.data), "major": 17}
            or (self.data / "PG_VERSION").read_text().strip() != "17"
        ):
            raise RuntimeError("数据库归属或版本不匹配，拒绝操作")

    def initialized(self):
        return (self.data / "PG_VERSION").exists()

    def prepare(self, *, migration=False):
        self.check_path()
        if (self.root / "data/postgres/PG_VERSION").exists() and not migration and not self.marker.exists():
            raise RuntimeError("发现旧 Docker 数据。请先执行数据库迁移，不能自动建立空库覆盖使用")
        if self.initialized():
            self.check_owned()
            return
        if self.data.exists() and (self.data.is_symlink() or any(self.data.iterdir())):
            raise RuntimeError("原生数据库目录非空但未初始化，请检查后重试")
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.data.resolve() != self.root / "data/postgres-native":
            raise RuntimeError("数据库路径不能跨出项目")
        secret = self.root / ".run" / ("pg-init-" + uuid.uuid4().hex)
        secret.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(self.settings.postgres_password + "\n")
            self.run(
                "initdb",
                "-D",
                self.data,
                "-U",
                self.settings.database.user,
                "--pwfile",
                secret,
                "--auth-local=scram-sha-256",
                "--auth-host=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            )
        finally:
            secret.unlink(missing_ok=True)
        self.owner.write_text(
            json.dumps({"root": str(self.root), "data_directory": str(self.data), "major": 17})
        )
        self.owner.chmod(0o600)
        (self.data / "postgresql.conf").write_text(
            "\n".join(
                [
                    "# Managed only for this project. Durability settings retain PostgreSQL defaults.",
                    "listen_addresses = '127.0.0.1'",
                    f"port = {self.settings.database.port}",
                    "unix_socket_directories = ''",
                    "shared_buffers = '128MB'",
                    "work_mem = '4MB'",
                    "maintenance_work_mem = '64MB'",
                    "max_connections = 32",
                    "max_parallel_workers_per_gather = 0",
                    "max_parallel_workers = 0",
                    "max_worker_processes = 2",
                    "autovacuum_max_workers = 2",
                    "max_wal_size = '256MB'",
                    "min_wal_size = '80MB'",
                    "jit = off",
                    "log_statement = 'none'",
                    "log_min_error_statement = 'panic'",
                    "log_parameter_max_length_on_error = 0",
                ]
            )
            + "\n"
        )

    def running(self):
        if not self.initialized():
            return False
        self.check_owned()
        result = subprocess.run(
            [str(self.bin / "pg_ctl"), "-D", str(self.data), "status"], capture_output=True
        )
        if result.returncode != 0:
            return False
        pid = int((self.data / "postmaster.pid").read_text().splitlines()[0])
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
        ).stdout
        command = shlex.split(process.strip())
        if (
            len(command) != 3
            or Path(command[0]).name != "postgres"
            or command[1:] != ["-D", str(self.data)]
        ):
            raise RuntimeError("数据库 PID 与项目路径不匹配，拒绝操作")
        return True

    def sql(self, query, database="postgres"):
        return (
            self.run(
                "psql",
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                database,
                input=query.encode(),
            )
            .decode()
            .strip()
        )

    def assert_connection(self):
        self.check_owned()
        if self.sql("SHOW data_directory") != str(self.data):
            raise RuntimeError("数据库端口属于其他实例，拒绝操作")

    def check_marker(self):
        if self.marker.is_symlink():
            raise RuntimeError("数据库部署标记不可为符号链接")
        try:
            value = json.loads(self.marker.read_text())
        except (OSError, ValueError):
            raise RuntimeError("原生数据库尚未安装或迁移，请选择安装／修复") from None
        if any(
            value.get(key) != expected
            for key, expected in {
                "mode": "native",
                "root": str(self.root),
                "data_directory": str(self.data),
                "major": 17,
            }.items()
        ):
            raise RuntimeError("数据库部署标记与项目不匹配，拒绝接管")
        self.check_owned()

    def start(self):
        self.check_owned()
        if self.running():
            self.assert_connection()
            return
        config = self.data / "postgresql.conf"
        if config.is_symlink():
            raise RuntimeError("数据库配置不可指向项目外部")
        content = config.read_text()
        configured = re.sub(r"(?m)^port\s*=.*$", f"port = {self.settings.database.port}", content)
        if configured != content:
            config.write_text(configured)
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", self.settings.database.port)) == 0:
                raise RuntimeError(f"端口 {self.settings.database.port} 已被其他服务占用，拒绝接管")
        log = self.root / "logs/postgres.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        self.run("pg_ctl", "-D", self.data, "-l", log, "-w", "-t", "30", "start")
        self.assert_connection()

    def stop(self):
        if self.initialized() and self.running():
            self.assert_connection()
            self.run("pg_ctl", "-D", self.data, "-m", "fast", "-w", "-t", "30", "stop")

    def database_exists(self, database):
        # The identifier is passed as a psql variable, not interpolated SQL.
        return (
            self.run(
                "psql",
                "-XAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                "name=" + database,
                "-d",
                "postgres",
                input=b"SELECT 1 FROM pg_database WHERE datname = :'name';",
            ).strip()
            == b"1"
        )

    def create_database(self, name):
        self.assert_connection()
        self.run("createdb", "--encoding=UTF8", name)

    def backup(self, path=None):
        self.assert_connection()
        path = (
            Path(path)
            if path
            else self.root
            / "data/backups"
            / ("workmemory-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".dump")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        if path.exists() or partial.exists():
            raise RuntimeError("备份路径已存在，拒绝覆盖")
        try:
            self.run("pg_dump", "--format=custom", "--no-owner", "--file", partial, timeout=300)
            self.run("pg_restore", "--list", partial)
            partial.chmod(0o600)
            partial.rename(path)
        finally:
            partial.unlink(missing_ok=True)
        return path

    def restore_check(self, path):
        self.assert_connection()
        database = "workmemory_restore_" + uuid.uuid4().hex[:16]
        self.create_database(database)
        try:
            self.run(
                "pg_restore",
                "--no-owner",
                "--exit-on-error",
                "--dbname",
                database,
                path,
                timeout=300,
            )
            version = self.sql("SELECT version_num FROM alembic_version", database)
            count = self.sql("SELECT count(*) FROM episodes", database)
            # Inspect every restored public table and verify all validated constraints
            # survived pg_restore. Counts describe the backup, never today's live DB.
            names = self.sql("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename", database).splitlines()
            tables = {}
            for name in names:
                quoted = '"' + name.replace('"', '""') + '"'
                tables[name] = int(self.sql(f"SELECT count(*) FROM public.{quoted}", database))
            invalid = int(self.sql("SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND NOT convalidated", database))
            if invalid:
                raise RuntimeError("恢复库含未验证约束")
            return {"schema": version, "episodes": int(count), "tables": tables, "constraints_valid": True}
        finally:
            self.run("dropdb", "--if-exists", database)

    def mark_ready(self, backup=None, verification=None):
        self.assert_connection()
        value = {
            "mode": "native",
            "root": str(self.root),
            "data_directory": str(self.data),
            "major": 17,
            "migrated_at": datetime.now(UTC).isoformat(),
            "backup": str(backup) if backup else None,
            "backup_sha256": hashlib.sha256(Path(backup).read_bytes()).hexdigest()
            if backup
            else None,
            "verification": verification,
        }
        partial = self.marker.with_suffix(".tmp")
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text(json.dumps(value, indent=2))
        partial.chmod(0o600)
        partial.replace(self.marker)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="拾忆原生 PostgreSQL")
    parser.add_argument(
        "action", choices=["init", "start", "stop", "status", "backup", "restore-check"]
    )
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    pg = NativePostgres()
    if args.action == "init" and pg.marker.exists():
        pg.check_marker()
        pg.start()
    elif args.action == "init":
        pg.prepare()
        pg.start()
        if not pg.database_exists(pg.settings.database.name):
            pg.create_database(pg.settings.database.name)
        pg.mark_ready()
    elif args.action == "start":
        pg.check_marker()
        pg.start()
    elif args.action == "stop":
        pg.stop()
    elif args.action == "status":
        running = pg.running()
        ready = False
        if running:
            pg.assert_connection()
            ready = True
        print(
            json.dumps(
                {
                    "installed": pg.initialized(),
                    "running": running,
                    "ready": ready,
                    "mode": "native",
                }
            )
        )
    elif args.action == "backup":
        print(pg.backup(args.path))
    else:
        print(json.dumps(pg.restore_check(Path(args.path))))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(
            str(exc)
            if isinstance(exc, RuntimeError)
            else "原生数据库操作失败，请检查安装与项目路径"
        )
        raise SystemExit(1) from None
