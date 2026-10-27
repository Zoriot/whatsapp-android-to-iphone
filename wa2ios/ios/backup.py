"""Read and patch a local (unencrypted) iPhone backup made by iTunes / Apple Devices / Finder.

Layout of a backup folder:
  Info.plist, Manifest.plist, Status.plist  - metadata
  Manifest.db                               - SQLite index, table Files(fileID, domain, relativePath, flags, file)
  ab/abcdef...                              - file contents, named by fileID = sha1(domain + "-" + relativePath)

`file` is an NSKeyedArchiver plist ("MBFile") with Size, Mode, timestamps, etc.
flags = 1 for a file, 2 for a directory.

This module never modifies a backup in place: `clone()` copies it first and all
patching happens on the copy.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wa2ios.util import Wa2iosError, human_size, info

WHATSAPP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
CHATSTORAGE = "ChatStorage.sqlite"
REQUIRED_FILES = ("Info.plist", "Manifest.plist", "Manifest.db", "Status.plist")
FLAG_FILE, FLAG_DIR = 1, 2
UNFINISHED = ".wa2ios-unfinished"


def file_id(domain: str, relative_path: str) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


def default_backup_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform == "win32":
        if os.environ.get("USERPROFILE"):
            roots.append(Path(os.environ["USERPROFILE"]) / "Apple" / "MobileSync" / "Backup")
        if os.environ.get("APPDATA"):
            roots.append(Path(os.environ["APPDATA"]) / "Apple Computer" / "MobileSync" / "Backup")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup")
    return [r for r in roots if r.is_dir()]


@dataclass
class BackupInfo:
    path: Path
    device_name: str
    product: str
    ios_version: str
    last_backup: datetime | None
    encrypted: bool
    has_whatsapp: bool | None  # None when it cannot be checked (encrypted)

    def describe(self) -> str:
        when = self.last_backup.astimezone().strftime("%Y-%m-%d %H:%M") if self.last_backup else "unknown date"
        flags = []
        if self.encrypted:
            flags.append("ENCRYPTED")
        if self.has_whatsapp is False:
            flags.append("no WhatsApp")
        extra = f"  [{', '.join(flags)}]" if flags else ""
        return f"{self.device_name} ({self.product}, iOS {self.ios_version}) - {when}{extra}\n    {self.path}"


def is_backup(path: Path) -> bool:
    return path.is_dir() and all((path / f).is_file() for f in REQUIRED_FILES)


def read_info(path: Path) -> BackupInfo:
    if not is_backup(path):
        raise Wa2iosError(
            f"{path} is not an iPhone backup folder (it needs {', '.join(REQUIRED_FILES)})."
        )
    with open(path / "Info.plist", "rb") as f:
        info_plist = plistlib.load(f)
    with open(path / "Manifest.plist", "rb") as f:
        manifest = plistlib.load(f)
    encrypted = bool(manifest.get("IsEncrypted"))
    has_wa = None
    if not encrypted:
        try:
            with _open_manifest_readonly(path) as db:
                has_wa = db.execute(
                    "SELECT 1 FROM Files WHERE domain = ? AND relativePath = ? AND flags = 1",
                    (WHATSAPP_DOMAIN, CHATSTORAGE),
                ).fetchone() is not None
        except sqlite3.DatabaseError:
            has_wa = None
    date = info_plist.get("Last Backup Date")
    if isinstance(date, datetime) and date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return BackupInfo(
        path=path,
        device_name=str(info_plist.get("Device Name") or info_plist.get("Display Name") or "iPhone"),
        product=str(info_plist.get("Product Type", "?")),
        ios_version=str(info_plist.get("Product Version", "?")),
        last_backup=date if isinstance(date, datetime) else None,
        encrypted=encrypted,
        has_whatsapp=has_wa,
    )


def list_backups(roots: list[Path] | None = None) -> list[BackupInfo]:
    found = []
    for root in roots if roots is not None else default_backup_roots():
        for child in sorted(root.iterdir()):
            if child.name.endswith(UNFINISHED):
                continue
            if is_backup(child):
                try:
                    found.append(read_info(child))
                except (Wa2iosError, plistlib.InvalidFileException, OSError):
                    continue
    found.sort(key=lambda b: b.last_backup or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return found


class _ReadOnly:
    def __init__(self, path: Path):
        manifest = path / "Manifest.db"
        wal = path / "Manifest.db-wal"
        # immutable=1 stops SQLite creating -wal/-shm files inside somebody's backup,
        # but it would ignore a pending log, so only use it when there is none.
        immutable = "&immutable=1" if not (wal.exists() and wal.stat().st_size > 0) else ""
        self.db = sqlite3.connect(f"{manifest.resolve().as_uri()}?mode=ro{immutable}", uri=True)

    def __enter__(self) -> sqlite3.Connection:
        return self.db

    def __exit__(self, *exc) -> None:
        self.db.close()


def _open_manifest_readonly(path: Path) -> _ReadOnly:
    return _ReadOnly(path)


# --------------------------------------------------------------------- MBFile plist
def _mbfile(blob: bytes) -> tuple[dict, dict]:
    archive = plistlib.loads(blob)
    root = archive["$top"]["root"]
    obj = archive["$objects"][root.data if isinstance(root, plistlib.UID) else int(root)]
    if not isinstance(obj, dict) or "Size" not in obj:
        raise Wa2iosError("Unexpected file record in Manifest.db (no Size).")
    return archive, obj


def _resized_blob(blob: bytes, size: int, now: int) -> bytes:
    archive, obj = _mbfile(blob)
    obj["Size"] = int(size)
    obj["LastModified"] = now
    obj["LastStatusChange"] = now
    out = plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)
    if _mbfile(out)[1]["Size"] != size:
        raise Wa2iosError("Internal error: re-encoded file record has the wrong size.")
    return out


def _new_blob(relative_path: str, size: int, template: dict | None, is_dir: bool, inode: int) -> bytes:
    now = int(time.time())
    defaults = {
        "Mode": 0o40755 if is_dir else 0o100644,
        "UserID": 501,
        "GroupID": 501,
        "ProtectionClass": 0 if is_dir else 3,
        "Flags": 0,
    }
    record = {
        "$class": plistlib.UID(3),
        "RelativePath": plistlib.UID(2),
        "Size": 0 if is_dir else int(size),
        "LastModified": now,
        "LastStatusChange": now,
        "Birth": now,
        "InodeNumber": inode,
    }
    for key, value in defaults.items():
        tv = template.get(key) if template else None
        record[key] = tv if isinstance(tv, int) else value
    archive = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null", record, relative_path, {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]}],
    }
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


# --------------------------------------------------------------------- cloning
def clone(source: Path, destination: Path) -> Path:
    """Copy a whole backup folder. Built under a temporary name and renamed when complete,
    so an interrupted copy is never mistaken for a backup."""
    if destination.exists():
        raise Wa2iosError(f"{destination} already exists; refusing to overwrite it.")
    making = destination.with_name(destination.name + UNFINISHED)
    if making.exists():
        shutil.rmtree(making)

    files = [p for p in source.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    info(f"Copying the backup ({len(files)} files, {human_size(total)}) to {destination.name} ...")
    done = 0
    last_print = 0.0
    try:
        for p in files:
            target = making / p.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            done += p.stat().st_size
            if time.time() - last_print > 2:
                last_print = time.time()
                pct = 100 * done / total if total else 100
                print(f"\r    {pct:5.1f}%  {human_size(done)} / {human_size(total)}", end="", flush=True)
        print(f"\r    100.0%  {human_size(total)} / {human_size(total)}", flush=True)
        os.replace(making, destination)
    except BaseException:
        shutil.rmtree(making, ignore_errors=True)
        raise
    return destination


# --------------------------------------------------------------------- reading
class BackupReader:
    """Read files out of an unencrypted backup without changing anything in it."""

    def __init__(self, path: Path, *, writable: bool = False):
        info_ = read_info(path)
        if info_.encrypted:
            raise Wa2iosError(
                "This backup is encrypted. Turn off 'Encrypt local backup' in iTunes / Apple Devices, "
                "make a new backup, and use that one (see README: make an encrypted safety backup first)."
            )
        self.path = path
        if writable:
            self.db = sqlite3.connect(str(path / "Manifest.db"))
        else:
            self.db = _open_manifest_readonly(path).db
        ok = self.db.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":
            raise Wa2iosError(f"The backup's Manifest.db is damaged: {ok}")

    def close(self) -> None:
        self.db.close()

    def payload(self, fid: str) -> Path:
        return self.path / fid[:2] / fid

    def find(self, domain: str, relative_path: str) -> tuple[str, int, bytes] | None:
        row = self.db.execute(
            "SELECT fileID, flags, file FROM Files WHERE domain = ? AND relativePath = ?",
            (domain, relative_path),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def extract(self, domain: str, relative_path: str, destination: Path) -> bool:
        found = self.find(domain, relative_path)
        if not found or found[1] != FLAG_FILE:
            return False
        src = self.payload(found[0])
        if not src.is_file():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, destination)
        return True


# --------------------------------------------------------------------- writable backup
class WritableBackup(BackupReader):
    """A backup *copy* opened for patching. Call close() to flush Manifest.db."""

    def __init__(self, path: Path):
        super().__init__(path, writable=True)
        self.now = int(time.time())
        self._inode = 0x7A2105000000 + int.from_bytes(os.urandom(3), "big")
        self.added = 0
        self.added_bytes = 0

    def replace(self, domain: str, relative_path: str, source: Path | None) -> int:
        """Replace an existing file's contents (source=None empties it). Returns the new size."""
        found = self.find(domain, relative_path)
        if not found or found[1] != FLAG_FILE:
            raise Wa2iosError(f"{relative_path} is not in the backup.")
        fid, _flags, blob = found
        target = self.payload(fid)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source is None:
            target.write_bytes(b"")
        else:
            shutil.copyfile(source, target)
        size = target.stat().st_size
        self.db.execute("UPDATE Files SET file = ? WHERE fileID = ?", (_resized_blob(blob, size, self.now), fid))
        return size

    def _template(self, domain: str, flags: int, prefix: str) -> dict | None:
        for like in (prefix + "%", "%"):
            row = self.db.execute(
                "SELECT file FROM Files WHERE domain = ? AND flags = ? AND relativePath LIKE ? LIMIT 1",
                (domain, flags, like),
            ).fetchone()
            if row:
                try:
                    return _mbfile(row[0])[1]
                except (Wa2iosError, plistlib.InvalidFileException, KeyError, IndexError, TypeError):
                    continue
        return None

    def ensure_dir(self, domain: str, relative_path: str) -> None:
        parts = relative_path.strip("/").split("/")
        template = None
        for i in range(1, len(parts) + 1):
            rel = "/".join(parts[:i])
            found = self.find(domain, rel)
            if found:
                if found[1] != FLAG_DIR:
                    raise Wa2iosError(f"{rel} exists in the backup but is not a folder.")
                continue
            if template is None:
                template = self._template(domain, FLAG_DIR, "Message/Media")
            self._inode += 1
            blob = _new_blob(rel, 0, template, True, self._inode)
            self.db.execute(
                "INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, ?, ?)",
                (file_id(domain, rel), domain, rel, FLAG_DIR, blob),
            )

    def add_file(self, domain: str, relative_path: str, source: Path) -> None:
        if self.find(domain, relative_path):
            raise Wa2iosError(f"{relative_path} already exists in the backup.")
        parent = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
        if parent:
            self.ensure_dir(domain, parent)
        fid = file_id(domain, relative_path)
        target = self.payload(fid)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        size = target.stat().st_size
        self._inode += 1
        blob = _new_blob(relative_path, size, self._template(domain, FLAG_FILE, "Message/Media"), False, self._inode)
        self.db.execute(
            "INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, ?, ?)",
            (fid, domain, relative_path, FLAG_FILE, blob),
        )
        self.added += 1
        self.added_bytes += size

    def fix_stale_sizes(self, domain: str = WHATSAPP_DOMAIN) -> int:
        """Rewrite Manifest Size fields that disagree with the on-disk payload.

        File contents are not changed. Returns how many records were corrected.
        Opt-in only: some iPhone backups ship with stale Size values for WhatsApp
        SQLite databases, which makes the post-migrate integrity check fail.
        """
        fixed = 0
        rows = list(self.db.execute(
            "SELECT fileID, relativePath, file FROM Files WHERE domain = ? AND flags = ?",
            (domain, FLAG_FILE),
        ))
        for fid, rel, blob in rows:
            payload = self.payload(fid)
            if not payload.is_file():
                continue
            try:
                recorded = int(_mbfile(blob)[1]["Size"])
            except (Wa2iosError, plistlib.InvalidFileException, KeyError, IndexError, TypeError):
                continue
            actual = payload.stat().st_size
            if recorded == actual:
                continue
            self.db.execute(
                "UPDATE Files SET file = ? WHERE fileID = ?",
                (_resized_blob(blob, actual, self.now), fid),
            )
            fixed += 1
            info(f"    {rel}: Size {recorded} -> {actual}")
        return fixed

    def mark_as_newest(self) -> None:
        """Set Info.plist's date to now so the patched copy is the newest one listed in iTunes."""
        p = self.path / "Info.plist"
        with open(p, "rb") as f:
            data = plistlib.load(f)
        fmt = plistlib.FMT_BINARY if p.read_bytes()[:6] == b"bplist" else plistlib.FMT_XML
        data["Last Backup Date"] = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        with open(p, "wb") as f:
            plistlib.dump(data, f, fmt=fmt)

    def close(self) -> None:
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = self.db.execute("PRAGMA integrity_check").fetchone()[0]
        self.db.close()
        for side in ("-wal", "-shm"):
            sidecar = self.path / f"Manifest.db{side}"
            if sidecar.exists():
                sidecar.unlink()
        if result != "ok":
            raise Wa2iosError(f"Manifest.db failed its integrity check after patching: {result}")


def unique_media_name(ext: str) -> str:
    return f"{uuid.uuid4()}{ext}".lower() if ext else str(uuid.uuid4())
