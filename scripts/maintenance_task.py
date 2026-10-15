"""Fixed maintenance subprocess. Arguments are internal identifiers, never shell commands."""

import json
import os
import sys
from datetime import UTC, datetime

from native_database import NativePostgres
from workmemory.maintenance.backups import BackupFiles, checksum


def execute(kind, identifier, automatic=False):
    pg, files = NativePostgres(), BackupFiles()
    with files.lock():
        return execute_locked(pg, files, kind, identifier, automatic)


def execute_locked(pg, files, kind, identifier, automatic):
    path = files.path(identifier)
    if kind == "backup":
        marker = path.with_suffix(".owner.json")
        owner = {"id": identifier, "automatic": automatic}
        # An API crash can leave the subprocess alive. The flock serializes retry.
        if marker.is_symlink():
            raise ValueError("备份归属标记无效")
        if marker.exists():
            if json.loads(marker.read_text()) != owner:
                raise ValueError("备份属于其他任务")
        else:
            if path.exists() or path.with_suffix(".dump.partial").exists():
                raise ValueError("拒绝接管未登记备份")
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(owner, stream)
                stream.flush()
                os.fsync(stream.fileno())
        partial = path.with_suffix(".dump.partial")
        if partial.is_symlink():
            raise ValueError("备份临时路径无效")
        # Only this task's partial file may be removed after obtaining its lock.
        partial.unlink(missing_ok=True)
        if not path.exists():
            pg.backup(path)
        pg.run("pg_restore", "--list", path)
        value = {
            "id": identifier,
            "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            "sha256": checksum(path),
            "size": path.stat().st_size,
            "managed_by": "workmemory-daily-v1" if automatic else "manual",
        }
        files.metadata(path, value)
        if automatic:
            files.prune(datetime.now(UTC), pg.settings.timezone)
        return value
    if kind == "verify":
        if not path.is_file():
            raise ValueError("备份不存在")
        meta = next(item for item in files.list() if item["id"] == identifier)
        digest = checksum(path)
        if meta.get("sha256") and meta["sha256"] != digest:
            raise ValueError("备份校验值不匹配")
        result = {**pg.restore_check(path), "verified_at": datetime.now(UTC).isoformat()}
        value = {
            **meta,
            "sha256": digest,
            "verification": result,
            "managed_by": "workmemory-daily-v1" if meta["automatic"] else "manual",
        }
        files.metadata(path, value)
        return result
    raise ValueError("未知维护操作")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        print(
            json.dumps(
                execute(sys.argv[1], sys.argv[2], len(sys.argv) > 3 and sys.argv[3] == "automatic")
            )
        )
    except Exception as error:
        # Exception messages and pg output can carry sensitive paths/data.
        print(json.dumps({"error": type(error).__name__}))
        raise SystemExit(1) from None
