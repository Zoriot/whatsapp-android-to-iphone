"""Android msgstore.db  ->  a patched copy of an iPhone backup, ready to restore.

The original iPhone backup is never modified. A copy is made next to it, the
WhatsApp database inside the copy is merged with the Android history, the copy's
index (Manifest.db) is updated to match, and the result is verified.
"""

from __future__ import annotations

import mimetypes
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from wa2ios.android.reader import AndroidStore
from wa2ios.ios import backup as ios_backup
from wa2ios.ios.backup import CHATSTORAGE, WHATSAPP_DOMAIN, BackupReader, WritableBackup, unique_media_name
from wa2ios.ios.chatstorage import (
    IOS_AUDIO, IOS_DOCUMENT, IOS_GIF, IOS_IMAGE, IOS_STICKER, IOS_TEXT, IOS_VIDEO, ChatStorage,
)
from wa2ios.model import FILE_KINDS, Chat, Kind, Message
from wa2ios.util import Wa2iosError, android_ms_to_coredata, folder_size, free_space, human_size, info, step, warn
from wa2ios.verify import find_whatsapp_mismatches, verify_backup

NATIVE_TYPE = {
    Kind.IMAGE: IOS_IMAGE, Kind.VIDEO: IOS_VIDEO, Kind.GIF: IOS_GIF, Kind.AUDIO: IOS_AUDIO,
    Kind.VOICE: IOS_AUDIO, Kind.DOCUMENT: IOS_DOCUMENT, Kind.STICKER: IOS_STICKER,
}
DEFAULT_MIME = {
    Kind.IMAGE: "image/jpeg", Kind.VIDEO: "video/mp4", Kind.GIF: "video/mp4",
    Kind.VOICE: "audio/ogg; codecs=opus", Kind.AUDIO: "audio/mpeg",
    Kind.STICKER: "image/webp", Kind.DOCUMENT: "application/octet-stream",
}
LABEL = {
    Kind.IMAGE: "📷 Photo", Kind.VIDEO: "🎥 Video", Kind.GIF: "🎞️ GIF", Kind.AUDIO: "🎵 Audio",
    Kind.VOICE: "🎤 Voice message", Kind.DOCUMENT: "📄 Document", Kind.STICKER: "🌟 Sticker",
    Kind.CONTACT: "👤 Contact", Kind.LOCATION: "📍 Location", Kind.DELETED: "🚫 This message was deleted",
    Kind.POLL: "📊 Poll", Kind.VIEW_ONCE: "👁️ View-once media (cannot be transferred)",
}
SKIPPED_KINDS = {Kind.SYSTEM, Kind.CALL}
DURATION_KINDS = {Kind.VIDEO, Kind.GIF, Kind.AUDIO, Kind.VOICE}


@dataclass
class Options:
    msgstore: Path
    backup: Path | None = None
    chatstorage: Path | None = None  # advanced: patch a standalone ChatStorage.sqlite instead of a backup
    media_root: Path | None = None  # folder that contains "Media" (the Android "WhatsApp" folder)
    with_media: bool = False
    output: Path | None = None
    dry_run: bool = False
    only_chats: list[str] = field(default_factory=list)
    fix_manifest_sizes: bool = False  # rewrite stale Manifest Size fields on the copy only


@dataclass
class Report:
    chats_read: int = 0
    messages_read: int = 0
    skipped: Counter = field(default_factory=Counter)
    placeholders: Counter = field(default_factory=Counter)
    media_copied: int = 0
    media_missing: int = 0
    first: float | None = None
    last: float | None = None


# --------------------------------------------------------------------------- text
def _placeholder(m: Message) -> str | None:
    label = LABEL.get(m.kind)
    if label is None:
        return m.text or None
    detail = None
    if m.kind is Kind.DOCUMENT and m.media and m.media.name:
        detail = m.media.name
    elif m.kind is Kind.CONTACT and m.vcard_name:
        detail = m.vcard_name
    elif m.kind is Kind.LOCATION and m.location and m.location.latitude is not None:
        loc = m.location
        parts = [p for p in (loc.name, loc.address) if p]
        parts.append(f"https://maps.google.com/?q={loc.latitude},{loc.longitude}")
        detail = "\n".join(parts)
    head = f"{label}: {detail}" if detail and "\n" not in detail else label
    body = [head]
    if detail and "\n" in detail:
        body.append(detail)
    if m.text and m.kind not in (Kind.DELETED, Kind.VIEW_ONCE):
        body.append(m.text)
    return "\n".join(body)


# --------------------------------------------------------------------------- media
def resolve_android_file(media_root: Path | None, recorded: str | None) -> Path | None:
    if not media_root or not recorded:
        return None
    rel = recorded.replace("\\", "/")
    idx = rel.find("Media/")
    if idx > 0:
        rel = rel[idx:]
    candidate = media_root / rel
    return candidate if candidate.is_file() else None


def _thumbnail(src: Path, dst: Path) -> bool:
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        return False
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((256, 256))
            im.save(dst, "JPEG", quality=70)
        return True
    except Exception:  # a broken image must not stop the migration
        return False


@dataclass
class PendingFile:
    source: Path
    backup_path: str  # relativePath inside the WhatsApp domain, e.g. "Message/Media/<jid>/a/b/x.jpg"


class MediaPlanner:
    def __init__(self, media_root: Path | None, enabled: bool, workdir: Path, thumbnails: bool = True):
        self.root = media_root
        self.enabled = enabled
        self.workdir = workdir
        self.thumbnails = thumbnails
        self.pending: list[PendingFile] = []

    def plan(self, m: Message, chat_jid: str) -> dict | None:
        """Return the ZWAMEDIAITEM columns for a native media message, or None to use a placeholder."""
        if not self.enabled or m.kind not in FILE_KINDS or m.media is None:
            return None
        src = resolve_android_file(self.root, m.media.file_path)
        if src is None:
            return None
        ext = src.suffix.lower()
        name = unique_media_name(ext)
        ios_rel = f"Media/{chat_jid}/{name[0]}/{name[1]}/{name}"
        self.pending.append(PendingFile(src, f"Message/{ios_rel}"))
        mime = m.media.mime_type or mimetypes.guess_type(src.name)[0] or DEFAULT_MIME[m.kind]
        if m.kind is Kind.VOICE:
            mime = "audio/ogg; codecs=opus"
        row = {
            "ZMEDIALOCALPATH": ios_rel,
            "ZVCARDSTRING": mime,
            "ZFILESIZE": src.stat().st_size,
            "ZMOVIEDURATION": int(m.media.duration or 0) if m.kind in DURATION_KINDS else 0,
            "ZMEDIAORIGIN": 0,
            "ZCLOUDSTATUS": 0,
        }
        if m.kind is Kind.DOCUMENT:
            row["ZTITLE"] = m.media.name or src.name
        if m.kind is Kind.IMAGE and self.thumbnails:
            thumb_name = name.rsplit(".", 1)[0] + ".thumb"
            thumb_local = self.workdir / "thumbs" / thumb_name
            thumb_local.parent.mkdir(parents=True, exist_ok=True)
            if _thumbnail(src, thumb_local):
                thumb_rel = f"Media/{chat_jid}/{name[0]}/{name[1]}/{thumb_name}"
                self.pending.append(PendingFile(thumb_local, f"Message/{thumb_rel}"))
                row["ZXMPPTHUMBPATH"] = thumb_rel
        return row


# --------------------------------------------------------------------------- core
def merge_into(store: ChatStorage, android: AndroidStore, planner: MediaPlanner, opts: Options) -> Report:
    report = Report()
    wanted = {w.lower() for w in opts.only_chats}
    for chat in android.chats():
        if wanted and not _matches(chat, wanted):
            continue
        report.chats_read += 1
        _merge_chat(store, android, planner, chat, report)
    store.finish()
    return report


def _matches(chat: Chat, wanted: set[str]) -> bool:
    keys = {chat.jid.lower(), chat.jid.split("@")[0].lower(), chat.title().lower()}
    keys |= {a.lower() for a in chat.alt_jids}
    return bool(keys & wanted)


def _merge_chat(store: ChatStorage, android: AndroidStore, planner: MediaPlanner, chat: Chat, report: Report) -> None:
    session = store.find_session([chat.jid, *chat.alt_jids])
    created = False
    if session is not None:
        destination = store.db.execute(
            "SELECT ZCONTACTJID FROM ZWACHATSESSION WHERE Z_PK = ?", (session,)
        ).fetchone()[0]
        known = store.existing_ids(session)
    else:
        destination, known = chat.jid, set()

    for m in android.messages(chat):
        report.messages_read += 1
        if m.kind in SKIPPED_KINDS:
            report.skipped[m.kind.value] += 1
            continue
        if m.key_id and m.key_id in known:
            store.stats.duplicates_skipped += 1
            continue

        date = android_ms_to_coredata(m.timestamp_ms)
        if date is None:
            report.skipped["no date"] += 1
            continue

        media_row = planner.plan(m, destination) if m.kind in FILE_KINDS else None
        if media_row is not None:
            msg_type, text = NATIVE_TYPE[m.kind], m.text
            report.media_copied += 1
        elif m.kind is Kind.TEXT:
            msg_type, text = IOS_TEXT, m.text
        else:
            msg_type, text = IOS_TEXT, _placeholder(m)
            if m.kind in FILE_KINDS:
                report.placeholders[m.kind.value] += 1
                if planner.enabled:
                    report.media_missing += 1
        if msg_type == IOS_TEXT and not text:
            report.skipped["empty"] += 1
            continue

        if session is None:
            created_at = android_ms_to_coredata(chat.created_ms)
            session = store.create_session(destination, chat.title(), chat.is_group, chat.archived, created_at)
            created = True

        store.add_message(
            session=session, chat_jid=destination, is_group=chat.is_group, from_me=m.from_me, date=date,
            msg_type=msg_type, text=text, stanza_id=m.key_id, starred=m.starred,
            sender_jid=m.sender_jid, sender_name=m.sender_name, media=media_row,
        )
        if m.key_id:
            known.add(m.key_id)
        report.first = date if report.first is None else min(report.first, date)
        report.last = date if report.last is None else max(report.last, date)

    if session is not None and not created and session in store.stats.touched_sessions:
        store.stats.sessions_merged += 1


# --------------------------------------------------------------------------- driver
def _extract_store(backup: BackupReader, workdir: Path) -> Path:
    target = workdir / CHATSTORAGE
    if not backup.extract(WHATSAPP_DOMAIN, CHATSTORAGE, target):
        raise Wa2iosError(
            "This iPhone backup has no WhatsApp data. Install WhatsApp on the iPhone, register your number, "
            "skip the iCloud restore, then make a new backup with iTunes / Apple Devices."
        )
    for side in ("-wal", "-shm"):
        backup.extract(WHATSAPP_DOMAIN, CHATSTORAGE + side, workdir / (CHATSTORAGE + side))
    return target


def _print_report(report: Report, store: ChatStorage, planner: MediaPlanner) -> None:
    def date(d):
        return datetime.fromtimestamp(d + 978307200).strftime("%Y-%m-%d") if d is not None else "-"

    stats = store.stats
    info(f"  Android chats read ........ {report.chats_read}")
    info(f"  Android messages read ..... {report.messages_read}")
    info(f"  Messages added to iPhone .. {stats.messages_added}  ({date(report.first)} to {date(report.last)})")
    info(f"  Already on iPhone (skipped) {stats.duplicates_skipped}")
    info(f"  New conversations ......... {stats.sessions_created}")
    info(f"  Merged into existing ...... {stats.sessions_merged}")
    if planner.enabled:
        info(f"  Media files copied ........ {report.media_copied}")
        if report.media_missing:
            info(f"  Media not found on PC ..... {report.media_missing} (shown as text lines instead)")
    if report.placeholders:
        info("  Shown as a text line ...... " + ", ".join(f"{k}: {v}" for k, v in report.placeholders.most_common()))
    if report.skipped:
        info("  Not carried over .......... " + ", ".join(f"{k}: {v}" for k, v in report.skipped.most_common()))
    info(f"  Message flags copied from your iPhone: {store.conv.sampled} of 10 (rest use known defaults)")


def _merge(store_path: Path, android: AndroidStore, planner: MediaPlanner, opts: Options) -> tuple[Report, ChatStorage]:
    store = ChatStorage(store_path)
    try:
        report = merge_into(store, android, planner, opts)
    except BaseException:
        store.abort()
        raise
    return report, store


def _find_media_root(opts: Options) -> None:
    if not opts.with_media or opts.media_root:
        return
    for guess in (opts.msgstore.parent.parent, opts.msgstore.parent):
        if (guess / "Media").is_dir():
            opts.media_root = guess
            info(f"Using media from {guess / 'Media'}")
            return
    raise Wa2iosError(
        "--with-media needs --media-dir pointing at the Android 'WhatsApp' folder (the one containing 'Media')."
    )


def run(opts: Options) -> Path | None:
    step("Reading the Android database")
    android = AndroidStore(opts.msgstore)
    _find_media_root(opts)

    if opts.chatstorage:
        return _run_on_store(android, opts)
    if not opts.backup:
        raise Wa2iosError("Tell me which iPhone backup to use with --backup (see `wa2ios backups`).")

    source = ios_backup.read_info(opts.backup)
    info(f"iPhone backup: {source.describe()}")
    if source.encrypted:
        raise Wa2iosError(
            "This backup is encrypted. Make an *encrypted* safety backup and archive it, then turn off "
            "'Encrypt local backup' and make a fresh backup to use here. See README, step 4."
        )
    if source.has_whatsapp is False:
        raise Wa2iosError("This iPhone backup has no WhatsApp data. Install and activate WhatsApp on the iPhone first.")

    scan = BackupReader(opts.backup)
    try:
        stale = [m for m in find_whatsapp_mismatches(scan) if m.kind == "size"]
    finally:
        scan.close()
    if stale and not opts.fix_manifest_sizes:
        warn(
            f"This backup has {len(stale)} WhatsApp file(s) whose Manifest Size does not match the "
            "on-disk payload. The post-migrate check will fail unless you pass --fix-manifest-sizes "
            "(rewrites Size on the copy only; file contents are unchanged)."
        )
        for m in stale[:10]:
            info(f"    - {m.describe()}")
        if len(stale) > 10:
            info(f"    ... and {len(stale) - 10} more")

    with tempfile.TemporaryDirectory(prefix="wa2ios-") as tmp:
        workdir = Path(tmp)

        # 1. Rehearse on a scratch copy of the iPhone store. Quick, and it catches problems
        #    before gigabytes are copied.
        step("Rehearsing the merge on a scratch copy of the iPhone's WhatsApp database")
        reader = BackupReader(opts.backup)
        try:
            store_path = _extract_store(reader, workdir / "rehearsal")
        finally:
            reader.close()
        planner = MediaPlanner(opts.media_root, opts.with_media, workdir / "rehearsal", thumbnails=False)
        report, store = _merge(store_path, android, planner, opts)
        _print_report(report, store, planner)
        if store.stats.messages_added == 0:
            warn("Nothing new to add - the iPhone already has all of these messages.")
            return None
        if opts.dry_run:
            info("\nDry run only: nothing was written. Run again without --dry-run to build the backup.")
            return None

        # 2. The real thing, on a copy of the backup.
        output = opts.output or opts.backup.with_name(f"{opts.backup.name}-wa2ios-{datetime.now():%Y%m%d-%H%M%S}")
        need = folder_size(opts.backup) + sum(p.source.stat().st_size for p in planner.pending)
        have = free_space(output.parent)
        if need > have:
            raise Wa2iosError(f"Not enough disk space: need about {human_size(need)}, have {human_size(have)}.")

        step("Making a copy of the iPhone backup (the original is left untouched)")
        ios_backup.clone(opts.backup, output)
        try:
            patched = WritableBackup(output)
            step("Merging the Android history into the copy")
            store_path = _extract_store(patched, workdir / "final")
            planner = MediaPlanner(opts.media_root, opts.with_media, workdir / "final")
            report, store = _merge(store_path, android, planner, opts)

            step("Writing the merged database into the backup copy")
            patched.replace(WHATSAPP_DOMAIN, CHATSTORAGE, store_path)
            # A stale write-ahead log in the backup would be replayed over the new database on the phone.
            for side in ("-wal", "-shm"):
                found = patched.find(WHATSAPP_DOMAIN, CHATSTORAGE + side)
                if found and found[1] == ios_backup.FLAG_FILE:
                    patched.replace(WHATSAPP_DOMAIN, CHATSTORAGE + side, None)
            if planner.pending:
                step(f"Adding {len(planner.pending)} media files to the backup copy")
                for i, f in enumerate(planner.pending, 1):
                    patched.add_file(WHATSAPP_DOMAIN, f.backup_path, f.source)
                    if i % 500 == 0:
                        info(f"    {i}/{len(planner.pending)}")
            if opts.fix_manifest_sizes:
                step("Refreshing stale Manifest Size fields on the copy")
                n = patched.fix_stale_sizes(WHATSAPP_DOMAIN)
                info(f"  updated Size for {n} file(s)" if n else "  nothing to refresh")
            try:
                patched.mark_as_newest()
            except Exception as e:  # cosmetic only
                warn(f"Could not update the date iTunes shows for the copy ({e}); pick it by folder name.")
            patched.close()

            step("Checking the result")
            verify_backup(output, baseline=opts.backup, expected_added=store.stats.messages_added,
                          new_paths=[f.backup_path for f in planner.pending])
        except BaseException:
            warn(f"Something went wrong; removing the unfinished copy {output}")
            shutil.rmtree(output, ignore_errors=True)
            raise

    step("Done")
    _print_report(report, store, planner)
    info(f"\nPatched backup: {output}")
    return output


def _run_on_store(android: AndroidStore, opts: Options) -> Path | None:
    """Advanced/testing: merge into a standalone ChatStorage.sqlite instead of a backup."""
    src = opts.chatstorage
    out = opts.output or src.with_name(src.stem + ".merged.sqlite")
    if out.exists():
        raise Wa2iosError(f"{out} already exists; refusing to overwrite it.")
    shutil.copyfile(src, out)
    for side in ("-wal", "-shm"):
        if Path(str(src) + side).exists():
            shutil.copyfile(str(src) + side, str(out) + side)
    if opts.with_media:
        warn("--with-media needs a backup to put files into; with --chatstorage, media becomes text lines.")
    with tempfile.TemporaryDirectory(prefix="wa2ios-") as tmp:
        planner = MediaPlanner(None, False, Path(tmp))
        try:
            report, store = _merge(out, android, planner, opts)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
    _print_report(report, store, planner)
    if opts.dry_run:
        out.unlink(missing_ok=True)
        return None
    info(f"\nMerged store written to {out}")
    return out
