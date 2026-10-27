"""Independent checks on a patched backup, run before anyone restores it to a phone."""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wa2ios.ios.backup import (
    CHATSTORAGE, FLAG_FILE, WHATSAPP_DOMAIN, BackupReader, _mbfile, file_id, read_info,
)
from wa2ios.util import Wa2iosError, info


class _Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def check(self, ok: bool, what: str) -> None:
        info(f"  [{'ok' if ok else 'FAIL'}] {what}")
        if not ok:
            self.failed.append(what)


@dataclass(frozen=True)
class FileMismatch:
    relative_path: str
    kind: str  # "fileID" | "size" | "missing"
    detail: str

    def describe(self) -> str:
        return f"{self.relative_path}: {self.detail}"


def find_whatsapp_mismatches(reader: BackupReader) -> list[FileMismatch]:
    """Compare every WhatsApp file record in Manifest.db to the payload on disk."""
    problems: list[FileMismatch] = []
    for fid, rel, blob in reader.db.execute(
        "SELECT fileID, relativePath, file FROM Files WHERE domain = ? AND flags = 1",
        (WHATSAPP_DOMAIN,),
    ):
        expected = file_id(WHATSAPP_DOMAIN, rel)
        if fid != expected:
            problems.append(FileMismatch(
                rel, "fileID",
                f"fileID mismatch (index {fid}, expected {expected})",
            ))
            continue
        payload = reader.payload(fid)
        size = int(_mbfile(blob)[1]["Size"])
        if payload.exists() and payload.stat().st_size != size:
            actual = payload.stat().st_size
            problems.append(FileMismatch(
                rel, "size",
                f"Size {size} in index, {actual} on disk",
            ))
        elif not payload.exists() and size > 0:
            problems.append(FileMismatch(
                rel, "missing",
                f"missing on disk (index Size {size})",
            ))
    return problems


def _report_mismatches(problems: list[FileMismatch], *, limit: int = 30) -> None:
    for p in problems[:limit]:
        info(f"    - {p.describe()}")
    if len(problems) > limit:
        info(f"    ... and {len(problems) - limit} more")


def _count(db: sqlite3.Connection, sql: str, *args) -> int:
    return db.execute(sql, args).fetchone()[0]


def _store_message_count(backup: Path) -> int | None:
    reader = BackupReader(backup)
    try:
        with tempfile.TemporaryDirectory(prefix="wa2ios-verify-") as tmp:
            p = Path(tmp) / CHATSTORAGE
            if not reader.extract(WHATSAPP_DOMAIN, CHATSTORAGE, p):
                return None
            for side in ("-wal", "-shm"):
                reader.extract(WHATSAPP_DOMAIN, CHATSTORAGE + side, Path(tmp) / (CHATSTORAGE + side))
            db = sqlite3.connect(str(p))
            try:
                return _count(db, "SELECT count(*) FROM ZWAMESSAGE")
            finally:
                db.close()
    finally:
        reader.close()


def verify_backup(patched: Path, *, baseline: Path, expected_added: int, new_paths: list[str]) -> None:
    expected_new_files = len(new_paths)
    c = _Checks()
    meta = read_info(patched)
    c.check(not meta.encrypted, "backup is unencrypted and readable")

    reader = BackupReader(patched)
    try:
        mdb = reader.db
        c.check(mdb.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Manifest.db integrity check")
        c.check(not (patched / "Manifest.db-wal").exists(), "Manifest.db has no leftover write-ahead log")

        base = BackupReader(baseline)
        try:
            base_files = _count(base.db, "SELECT count(*) FROM Files WHERE flags = 1")
        finally:
            base.close()
        new_files = _count(mdb, "SELECT count(*) FROM Files WHERE flags = 1")
        c.check(new_files == base_files + expected_new_files,
                f"file count: {base_files} original + {expected_new_files} media = {new_files}")

        problems = find_whatsapp_mismatches(reader)
        c.check(len(problems) == 0, "every WhatsApp file in the index matches its contents")
        if problems:
            _report_mismatches(problems)
            size_only = all(p.kind == "size" for p in problems)
            if size_only:
                info("    Hint: re-run with --fix-manifest-sizes to rewrite those Size fields on the copy.")

        if new_paths:
            absent = sum(1 for p in new_paths if (reader.find(WHATSAPP_DOMAIN, p) or (0, 0))[1] != FLAG_FILE)
            c.check(absent == 0, f"all {len(new_paths)} added media files are indexed ({absent} missing)")

        wal = reader.find(WHATSAPP_DOMAIN, CHATSTORAGE + "-wal")
        if wal and wal[1] == FLAG_FILE:
            c.check(reader.payload(wal[0]).stat().st_size == 0, "no stale WhatsApp write-ahead log in the backup")

        with tempfile.TemporaryDirectory(prefix="wa2ios-verify-") as tmp:
            store_path = Path(tmp) / CHATSTORAGE
            c.check(reader.extract(WHATSAPP_DOMAIN, CHATSTORAGE, store_path), "WhatsApp database present in the copy")
            db = sqlite3.connect(f"{store_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                c.check(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "WhatsApp database integrity check")
                total = _count(db, "SELECT count(*) FROM ZWAMESSAGE")
                before = _store_message_count(baseline)
                if before is not None:
                    c.check(total == before + expected_added,
                            f"message count: {before} original + {expected_added} added = {total}")
                c.check(_count(db, "SELECT count(*) FROM ZWAMESSAGE m LEFT JOIN ZWACHATSESSION s "
                                   "ON s.Z_PK = m.ZCHATSESSION WHERE s.Z_PK IS NULL") == 0,
                        "every message belongs to a conversation")
                c.check(_count(db, "SELECT count(*) FROM (SELECT ZCHATSESSION, ZSORT FROM ZWAMESSAGE "
                                   "GROUP BY 1, 2 HAVING count(*) > 1)") == 0,
                        "display order (ZSORT) is unique within each conversation")
                stale = 0
                for name, highest in db.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME IS NOT NULL"):
                    table = "Z" + name.upper()
                    try:
                        top = _count(db, f"SELECT coalesce(max(Z_PK), 0) FROM {table}")
                    except sqlite3.OperationalError:
                        continue
                    if top > (highest or 0):
                        stale += 1
                c.check(stale == 0, "Core Data id counters are ahead of every row")
            finally:
                db.close()
    finally:
        reader.close()

    if c.failed:
        raise Wa2iosError("The patched backup failed these checks, so it was discarded: " + "; ".join(c.failed))
