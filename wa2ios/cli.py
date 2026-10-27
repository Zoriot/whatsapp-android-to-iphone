"""Command line for wa2ios. Run `wa2ios --help` or `wa2ios wizard`."""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from wa2ios import __version__
from wa2ios.util import Wa2iosError, ask_yes_no, info, make_console_safe, step, warn

DEFAULT_WORKDIR = Path.home() / "wa2ios"


# ------------------------------------------------------------------------ commands
def cmd_pull(a) -> int:
    from wa2ios.android.adb import pull

    pull(Path(a.out), adb_path=a.adb, with_media=not a.no_media)
    return 0


def _get_key(a) -> str:
    if a.key:
        return a.key
    return getpass.getpass("Paste your 64-digit WhatsApp backup key (input is hidden): ")


def cmd_decrypt(a) -> int:
    from wa2ios.android.crypt import decrypt

    src, out = Path(a.input), Path(a.output)
    if out.exists() and not ask_yes_no(f"{out} exists. Overwrite?", a.yes):
        return 1
    step(f"Decrypting {src.name}")
    decrypt(_get_key(a), src, out)
    info(f"Decrypted database written to {out}")
    info("Keep this file private: it contains all your messages in readable form.")
    return 0


def cmd_inspect(a) -> int:
    from wa2ios.android.reader import AndroidStore

    s = AndroidStore(Path(a.msgstore)).summarise()

    def d(ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else "-"

    info(f"Chats: {s.chats} ({s.groups} groups)   Messages: {s.messages}   From {d(s.first_ms)} to {d(s.last_ms)}")
    for kind, n in s.kinds.most_common():
        info(f"  {kind:<10} {n}")
    if s.skipped_chats:
        info("Not transferable (status updates, channels, broadcast lists): "
             + ", ".join(f"{k}: {v}" for k, v in s.skipped_chats.items()))
    return 0


def cmd_backups(a) -> int:
    from wa2ios.ios.backup import default_backup_roots, list_backups

    roots = [Path(a.dir)] if a.dir else default_backup_roots()
    if not roots:
        info("No iTunes / Apple Devices backup folder found on this computer.")
        return 1
    found = list_backups(roots)
    if not found:
        info("No iPhone backups found in: " + ", ".join(str(r) for r in roots))
        return 1
    for i, b in enumerate(found, 1):
        info(f"{i}. {b.describe()}")
    return 0


def _migrate_options(a):
    from wa2ios.migrate import Options

    return Options(
        msgstore=Path(a.msgstore),
        backup=Path(a.backup) if a.backup else None,
        chatstorage=Path(a.chatstorage) if a.chatstorage else None,
        media_root=Path(a.media_dir) if a.media_dir else None,
        with_media=a.with_media,
        output=Path(a.output) if a.output else None,
        dry_run=a.dry_run,
        only_chats=a.only or [],
        fix_manifest_sizes=bool(getattr(a, "fix_manifest_sizes", False)),
    )


def cmd_migrate(a) -> int:
    from wa2ios.migrate import run

    if not a.dry_run and not a.chatstorage:
        info(
            "This builds a NEW, patched copy of your iPhone backup next to the original. The original is not "
            "touched. Nothing is sent to your phone until YOU restore the copy."
        )
        if not ask_yes_no("Continue?", a.yes):
            return 1
    out = run(_migrate_options(a))
    if out and not a.chatstorage:
        _print_next_steps(out)
    return 0


def _print_next_steps(out: Path) -> None:
    info(
        "\nNEXT STEPS (see README for details):\n"
        "  1. On the iPhone: Settings > [your name] > Find My > Find My iPhone > OFF.\n"
        "  2. In Apple Devices / iTunes: select the iPhone > 'Restore Backup...' and pick the NEWEST backup\n"
        f"     (the copy wa2ios just made: {out.name}).\n"
        "     Or run:  wa2ios restore --backup \"" + str(out) + "\"\n"
        "  3. Keep the phone connected until it restarts and finishes. Open WhatsApp, verify your number if asked,\n"
        "     and DECLINE any offer to restore chats from iCloud.\n"
        "  4. Turn Find My iPhone back on."
    )


def cmd_restore(a) -> int:
    backup = Path(a.backup).resolve()
    from wa2ios.ios.backup import read_info

    meta = read_info(backup)
    info(f"About to restore: {meta.describe()}")
    info("This REPLACES everything on the connected iPhone with this backup. Find My iPhone must be OFF.")
    if not ask_yes_no("Restore now?", a.yes):
        return 1
    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        raise Wa2iosError(
            "Restoring from the command line needs pymobiledevice3:  pip install pymobiledevice3\n"
            "Or use Apple Devices / iTunes: select the iPhone > Restore Backup > pick the newest backup."
        )
    cmd = [sys.executable, "-m", "pymobiledevice3", "backup2", "restore", str(backup.parent),
           "--source", backup.name, "--system", "--settings", "--reboot"]
    info("Running: " + " ".join(cmd))
    return subprocess.call(cmd)


def cmd_wizard(a) -> int:
    """Guided run of every step, for people who would rather not remember commands."""
    from wa2ios.android.crypt import decrypt
    from wa2ios.ios.backup import default_backup_roots, list_backups
    from wa2ios.migrate import Options, run

    work = Path(a.workdir)
    work.mkdir(parents=True, exist_ok=True)
    info(f"wa2ios {__version__} - WhatsApp Android -> iPhone. Working folder: {work}")
    info("Everything stays on this computer. Read README.md first; this replaces the iPhone's contents "
         "with a backup of itself plus your Android chats.\n")

    android_root = work / "WhatsApp"
    crypt = android_root / "Databases" / "msgstore.db.crypt15"
    if crypt.is_file() and ask_yes_no(f"Use the Android files already in {android_root}?", False):
        pass
    else:
        step("Step 1/4: copy WhatsApp's backup off the Android phone (USB debugging on, phone unlocked)")
        from wa2ios.android.adb import pull

        pull(work, adb_path=a.adb, with_media=a.with_media)

    step("Step 2/4: decrypt it with your 64-digit key")
    msgstore = work / "msgstore.db"
    decrypt(_get_key(a), crypt, msgstore)
    info(f"Decrypted to {msgstore}")
    cmd_inspect(argparse.Namespace(msgstore=str(msgstore)))

    step("Step 3/4: choose the iPhone backup")
    backups = [b for b in list_backups(default_backup_roots()) if "-wa2ios-" not in b.path.name]
    if not backups:
        raise Wa2iosError("No iPhone backups found. Make one in Apple Devices / iTunes (unencrypted) and run again.")
    for i, b in enumerate(backups, 1):
        info(f"{i}. {b.describe()}")
    choice = input(f"Which one? [1-{len(backups)}, default 1]: ").strip() or "1"
    chosen = backups[int(choice) - 1]

    step("Step 4/4: build the patched backup")
    out = run(Options(
        msgstore=msgstore,
        backup=chosen.path,
        media_root=android_root if a.with_media else None,
        with_media=a.with_media,
        fix_manifest_sizes=a.fix_manifest_sizes,
    ))
    if out:
        _print_next_steps(out)
    warn(f"When you are done, delete {msgstore} (your messages, decrypted) and {android_root} (your media).")
    return 0


# ------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wa2ios",
        description="Move WhatsApp chats from Android to iPhone, entirely on your own computer.",
    )
    p.add_argument("--version", action="version", version=f"wa2ios {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("wizard", help="guided, step-by-step transfer (recommended)")
    s.add_argument("--workdir", default=str(DEFAULT_WORKDIR), help=f"working folder (default {DEFAULT_WORKDIR})")
    s.add_argument("--key", help="64-digit key (omit to be asked, which keeps it out of your shell history)")
    s.add_argument("--adb", help="path to adb.exe if it is not on PATH")
    s.add_argument("--with-media", action="store_true", help="also copy photos, videos, voice notes, documents")
    s.add_argument(
        "--fix-manifest-sizes",
        action="store_true",
        help="on the copy only, rewrite Manifest Size fields that do not match the on-disk WhatsApp files",
    )
    s.set_defaults(func=cmd_wizard)

    s = sub.add_parser("pull", help="copy WhatsApp's backup (and media) off the Android phone via adb")
    s.add_argument("--out", default=str(DEFAULT_WORKDIR), help="folder to copy into")
    s.add_argument("--adb", help="path to adb.exe if it is not on PATH")
    s.add_argument("--no-media", action="store_true", help="only copy the database, not photos/videos")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("decrypt", help="decrypt msgstore.db.crypt15 with your 64-digit key")
    s.add_argument("--in", dest="input", default=str(DEFAULT_WORKDIR / "WhatsApp/Databases/msgstore.db.crypt15"))
    s.add_argument("--out", dest="output", default=str(DEFAULT_WORKDIR / "msgstore.db"))
    s.add_argument("--key", help="the key, or a file containing it (omit to be asked)")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_decrypt)

    s = sub.add_parser("inspect", help="summarise a decrypted Android msgstore.db")
    s.add_argument("--msgstore", default=str(DEFAULT_WORKDIR / "msgstore.db"))
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("backups", help="list iPhone backups on this computer")
    s.add_argument("--dir", help="a folder that contains backups (default: the iTunes/Apple Devices folder)")
    s.set_defaults(func=cmd_backups)

    s = sub.add_parser("migrate", help="build a patched copy of an iPhone backup with your Android chats")
    s.add_argument("--msgstore", default=str(DEFAULT_WORKDIR / "msgstore.db"), help="decrypted Android database")
    s.add_argument("--backup", help="iPhone backup folder (see `wa2ios backups`)")
    s.add_argument("--with-media", action="store_true", help="also copy media files (experimental)")
    s.add_argument("--media-dir", help="the Android 'WhatsApp' folder that contains 'Media'")
    s.add_argument("--only", nargs="+", metavar="CHAT", help="only these chats (phone number, group name, or JID)")
    s.add_argument("--output", help="where to write the patched backup (default: next to the original)")
    s.add_argument("--dry-run", action="store_true", help="rehearse and report, write nothing")
    s.add_argument(
        "--fix-manifest-sizes",
        action="store_true",
        help="on the copy only, rewrite Manifest Size fields that do not match the on-disk WhatsApp files "
             "(needed when the source backup already has stale sizes)",
    )
    s.add_argument("--chatstorage", help=argparse.SUPPRESS)  # advanced: patch a bare ChatStorage.sqlite
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("restore", help="restore a patched backup to the connected iPhone (needs pymobiledevice3)")
    s.add_argument("--backup", required=True)
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    make_console_safe()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Wa2iosError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
