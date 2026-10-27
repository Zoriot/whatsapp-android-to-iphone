# wa2ios: WhatsApp from Android to iPhone, privately

A small, open, offline tool that moves your WhatsApp history from an Android phone to an
iPhone, on your own Windows PC. It does the same job as iCareFone Transfer or MobileTrans,
but every step is code you can read, nothing is uploaded anywhere, and there's no account.

> **Status: experimental.** The tool has been tested end to end on realistic synthetic
> databases and backups (decrypt → merge → patch → verify), but **not yet restored onto a
> real iPhone**. Follow the safety steps. They let you undo everything.

## How it works (one paragraph)

Your Android phone makes an encrypted local backup (`msgstore.db.crypt15`), and your
64-digit key decrypts it. Your iPhone gets a fresh **local** backup in iTunes / Apple Devices.
wa2ios copies that backup, opens the WhatsApp database inside the copy (`ChatStorage.sqlite`)
and adds your Android chats in the iPhone's own format. It then fixes the backup's index so
everything matches and checks the result. You restore that copy to the iPhone. Because it's
a backup of *the same iPhone* taken minutes earlier, everything else on the phone comes back
as it was, plus your old chats. More detail: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

## What transfers

| | |
|---|---|
| ✅ Text messages, 1:1 and group, with the correct dates, direction, and starred flag | |
| ✅ Group chats: name, and who wrote each message | |
| ✅ Merging: chats that already exist on the iPhone get the old messages added in date order, with no duplicates | |
| ✅ With `--with-media` (experimental): photos (with thumbnails), videos, GIFs, voice notes, audio, documents, stickers | |
| ➖ Locations, contact cards, deleted-message markers, polls, and media not found on the PC | arrive as a readable text line, e.g. `📍 Location: … https://maps.google.com/?q=…` |
| ❌ Not transferred: call history, system notices ("X joined"), status updates, channels, broadcast lists, view-once media, reactions, and reply quotes (the reply arrives, but without the quote bubble) | |

## You need

- A Windows 10/11 PC with enough free disk space for **one more copy of your iPhone backup**, plus your WhatsApp media.
- **Python 3.10+** from [python.org](https://www.python.org/downloads/windows/). Tick "Add python.exe to PATH" when installing.
- **Apple Devices** (Microsoft Store) or **iTunes**, to back up and restore the iPhone.
- **Android SDK Platform-Tools** (`adb`), to copy files off the Android phone: [download](https://developer.android.com/tools/releases/platform-tools). Optional, since you can also copy the files by hand.
- Both phones' USB cables, and the iPhone's SIM card (WhatsApp will text you a code).

## Install

```powershell
git clone https://github.com/Inzyz/whatsapp-android-to-iphone.git
cd whatsapp-android-to-iphone
py -m venv .venv
.venv\Scripts\activate
pip install ".[media]"
wa2ios --help
```

## Step by step

Do the steps **in this order**. Registering WhatsApp on the iPhone (step 3) logs WhatsApp
out on the Android phone, so the Android backup must come first.

### 1. On the Android phone: make a backup you can decrypt

1. WhatsApp → **Settings → Chats → Chat backup → End-to-end encrypted backup → Turn on**.
2. Choose **"Use 64-digit encryption key instead"**. **Write the key down** and keep it private.
   (A *password* won't work. Only the 64-digit key can be used offline.)
3. Tap **Back up** and wait for it to finish.
4. Turn on **Developer options** (Settings → About phone → tap *Build number* 7 times), then
   **Developer options → USB debugging → On**.

### 2. Copy WhatsApp off the Android phone and decrypt it

Connect the Android phone by USB, unlock it, and accept the "Allow USB debugging?" prompt. Then:

```powershell
wa2ios pull              # copies WhatsApp\Databases and WhatsApp\Media to %USERPROFILE%\wa2ios
wa2ios decrypt           # asks for the 64-digit key (spaces are fine); writes %USERPROFILE%\wa2ios\msgstore.db
wa2ios inspect           # shows how many chats and messages were found
```

<details><summary>No adb? Copy by hand instead</summary>

With the phone in "File transfer" mode, open it in File Explorer and copy
`Internal storage\Android\media\com.whatsapp\WhatsApp` (the folder containing `Databases`
and `Media`) to `%USERPROFILE%\wa2ios\WhatsApp`. Then run `wa2ios decrypt`.
</details>

### 3. On the iPhone: install WhatsApp

Install WhatsApp from the App Store and register **the same phone number**. When it offers
to restore from iCloud, tap **Skip**. You should see an empty (or nearly empty) chat list.

### 4. On the PC: make the iPhone backups

Connect the iPhone and open **Apple Devices** (or iTunes).

1. **Safety backup (your way back):** tick **"Encrypt local backup"**, set a password you
   won't forget, then click **Back Up Now**. When it finishes, open
   `%USERPROFILE%\Apple\MobileSync\Backup` (iTunes from apple.com uses
   `%APPDATA%\Apple Computer\MobileSync\Backup`) and **copy the backup folder somewhere
   else**, e.g. `D:\iPhone-safety-backup`. Windows has no "archive" button, and the next
   backup would overwrite this one.
2. **Working backup:** untick **"Encrypt local backup"** (it asks for the password), then
   **Back Up Now** again. wa2ios can only read unencrypted backups.
3. From now until the restore is finished, don't use the iPhone. Anything new would be lost.

### 5. Build the patched backup

```powershell
wa2ios backups                                          # lists backups; copy the path of the newest
wa2ios migrate --backup "C:\Users\you\Apple\MobileSync\Backup\0000XXXX-..." --dry-run
wa2ios migrate --backup "C:\Users\you\Apple\MobileSync\Backup\0000XXXX-..." --with-media
```

The dry run shows exactly what would be added and writes nothing. The real run copies the
backup into a new folder next to it (`…-wa2ios-YYYYMMDD-HHMMSS`), merges, and runs a list
of consistency checks. If any check fails, the copy is deleted. **Your original backup is
never modified.**

Prefer one guided command? `wa2ios wizard --with-media` runs steps 2 and 5 for you.

### 6. Restore the patched backup to the iPhone

1. On the iPhone: **Settings → [your name] → Find My → Find My iPhone → Off**.
2. In Apple Devices / iTunes: select the iPhone → **Restore Backup…** → pick the **newest**
   one, which is the wa2ios copy (it carries today's date and time).
   *(Alternative: `pip install pymobiledevice3`, then `wa2ios restore --backup "<path to the -wa2ios- folder>"`.)*
3. Leave the phone connected. It restarts and may sit on the Apple logo for a long time.
   That's normal, so don't unplug it.
4. Open WhatsApp. If asked, verify your number by SMS. **If it offers to restore from
   iCloud, decline.** Accepting would replace everything you just transferred.
5. Turn **Find My iPhone** back on, and turn **encrypted backups** back on in Apple Devices.

### 7. Clean up

Delete `%USERPROFILE%\wa2ios`. It holds your decrypted messages and media. Once you're
happy, you can also delete the `…-wa2ios-…` backup folder.

## If something goes wrong: undo

Copy your safety backup folder back into `…\MobileSync\Backup\`, then in Apple Devices
choose **Restore Backup…** → the encrypted one (it asks for its password). The phone returns
exactly to how it was before step 5, including Health data and saved passwords.

**What an unencrypted restore doesn't bring back:** saved passwords, Wi-Fi networks, and
Health data aren't in unencrypted backups. If you use iCloud Keychain and iCloud Health,
they sync back by themselves. If not, restoring the safety backup is the only way to get
them back.

## Troubleshooting

| Problem | Fix |
|---|---|
| `crypt14` error | Your backup isn't end-to-end encrypted with a 64-digit key. Redo step 1. |
| "key does not match" | Make sure it's the key for *this* backup. A new key makes old backups unreadable. |
| "This backup is encrypted" | Untick "Encrypt local backup" and back up again (step 4.2). |
| "no WhatsApp data" in the iPhone backup | Install and register WhatsApp on the iPhone (step 3), then back up again. |
| Restore error mentioning Find My | Turn off Find My iPhone (step 6.1). |
| The wa2ios copy doesn't appear in "Restore Backup" | Close and reopen Apple Devices. If it still doesn't appear, move the original backup folder out of `MobileSync\Backup` and rename the `…-wa2ios-…` folder to the original name (the part before `-wa2ios-`). |
| Photos show as "📷 Photo" text | The file wasn't found on the PC. Check that `--media-dir` points at the `WhatsApp` folder that contains `Media`. |
| `every WhatsApp file in the index matches its contents` | The source backup already has stale `Size` values in `Manifest.db` (common for WhatsApp SQLite files). Re-run with `--fix-manifest-sizes` (rewrites Size on the **copy** only; file contents are unchanged). |

## Privacy

- No network access at all: no telemetry, no update checks, no accounts.
- Your key is read from a hidden prompt (not saved or logged) unless you pass `--key`.
- Everything sensitive stays in folders you choose. `.gitignore` blocks databases, keys
  and backups from ever being committed.

## Risks you should know

- Transferring this way isn't supported by WhatsApp or Apple. Future WhatsApp versions may
  change their databases. wa2ios copies conventions from your own iPhone's store and only
  writes columns that exist, but a format change can still break things. Do a dry run first.
- The restore replaces the iPhone's contents with the patched backup. The safety backup in
  step 4.1 is what makes this reversible, so don't skip it.

## Credits

- [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) (GPL-3.0) decrypts crypt15.
- The format findings in [amberkeep's notes](https://github.com/jferrl/amberkeep/tree/main/docs/formats)
  (iPhone media paths, message-type evidence, Core Data conventions), plus
  [watoi](https://github.com/residentsummer/watoi), [mwatoi](https://github.com/mukulkadel/mwatoi) and
  [WhatsApp-Chat-Exporter](https://github.com/KnugiHK/WhatsApp-Chat-Exporter). No code was copied;
  only documented facts about the file formats were used.

Not affiliated with WhatsApp, Meta, or Apple. License: GPL-3.0-or-later.
