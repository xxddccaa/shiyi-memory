"""Backup catalogue contains no credentials; only registered automatic files are pruned."""

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from workmemory.config import ROOT


class BackupFiles:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.directory = self.root / "data/backups"

    def check(self):
        if self.directory.is_symlink() or self.directory.resolve() != self.root / "data/backups":
            raise ValueError("备份目录不属于本项目")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, identifier):
        self.check()
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.dump", identifier):
            raise ValueError("备份标识无效")
        path = self.directory / identifier
        if path.is_symlink() or path.resolve().parent != self.directory:
            raise ValueError("备份路径无效")
        return path

    @contextmanager
    def lock(self):
        self.check()
        fd = os.open(
            self.directory / ".maintenance.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def metadata(self, path, value):
        target = path.with_suffix(".json")
        temp = target.with_suffix(".json.partial")
        if target.is_symlink() or temp.is_symlink():
            raise ValueError("备份清单路径无效")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)

    def list(self):
        self.check()
        values = []
        for path in self.directory.glob("*.dump"):
            if path.is_symlink():
                continue
            try:
                meta = path.with_suffix(".json")
                value = (
                    json.loads(meta.read_text()) if meta.is_file() and not meta.is_symlink() else {}
                )
                if not isinstance(value, dict):
                    value = {}
                values.append(
                    {
                        "id": path.name,
                        "size": path.stat().st_size,
                        "created_at": value.get(
                            "created_at",
                            datetime.fromtimestamp(
                                path.stat().st_mtime, ZoneInfo("UTC")
                            ).isoformat(),
                        ),
                        "automatic": value.get("managed_by") == "workmemory-daily-v1",
                        "verification": value.get("verification"),
                        "sha256": value.get("sha256"),
                    }
                )
            except (OSError, ValueError, TypeError):
                continue
        return sorted(values, key=lambda item: item["created_at"], reverse=True)

    def prune(self, clock, timezone):
        zone = ZoneInfo(timezone)
        keep, days, weeks = set(), set(), set()
        for item in self.list():
            if not item["automatic"]:
                continue
            date = datetime.fromisoformat(item["created_at"]).astimezone(zone).date()
            monday = date - timedelta(days=date.weekday())
            if len(days) < 7 and date not in days:
                keep.add(item["id"])
                days.add(date)
            if len(weeks) < 4 and monday not in weeks:
                keep.add(item["id"])
                weeks.add(monday)
        for item in self.list():
            if item["automatic"] and item["id"] not in keep:
                path = self.path(item["id"])
                path.unlink()
                path.with_suffix(".json").unlink(missing_ok=True)
                path.with_suffix(".owner.json").unlink(missing_ok=True)


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
