# WoWSync

Review-first World of Warcraft **Interface** and **WTF** synchronization between two or more computers over LAN or Tailscale. Run one small app on each computer, pair trusted machines, preview the delta, and pull only what you choose.

**No database, cloud account, subscription, or Python dependencies.** Private settings, pairing tokens, sync history, and backups stay outside the repository. MIT licensed.

## What works

- Separate profiles for Retail, Forever, Classic, Classic Beta, and other `_version_` directories under an installation root. Versions never mix.
- File-level selection for addons, SavedVariables, bindings, macros, account folders, realms, and characters. Filter by path to target an addon or character.
- Individual `WTF/Config.wtf` setting selection and merging. Display, audio, input, UI scale, and unknown CVars stay local by default. Account identity and locale keys in this file cannot be selected.
- Unified text deltas for UTF-8 files; hashes and sizes for binary or large files. Lua is treated as data and never evaluated by WoWSync.
- Three-way comparison against the last accepted shared value: incoming changes, local edits, and conflicts. Different pre-existing files are conflicts on first pairing; timestamps never decide the winner.
- Backups before overwrite, integrity checks, stale-preview rejection, ordinary write-error rollback, and restore with newer-edit protection.
- Multiple paired machines, token rotation, and a local browser management interface. Peer tokens grant read access only; writes happen through the receiving machine’s local UI.

This first release uses **manual, reviewed pulls**. It does not run unattended sync jobs, merge Lua tables, propagate deletions, or map different account/character folder names. Addons are copied as selected files; select all files in an addon when installing or updating it. Removed upstream files remain locally. Use an addon manager for clean upgrades requiring deletions.

## Run on Windows

Requires Python 3.11 or newer. Clone or download this repository, then from its directory:

```powershell
python -m wowsync --install 'D:\Games\World of Warcraft' --open
```

Or use the launcher:

```powershell
.\scripts\start.ps1
```

The terminal prints a private local management URL. Keep the app running while using it. The default listener is loopback only. Nothing is sent to GitHub or any online service.

For access from other machines on your LAN or tailnet:

```powershell
python -m wowsync --install 'D:\Games\World of Warcraft' --listen 0.0.0.0 --open
```

You can also pass `-Listen 0.0.0.0` to `scripts/start.ps1`. If Windows Firewall asks, allow only the networks you intend to use. The app does not create firewall rules or configure Tailscale itself. Management remains restricted to loopback clients even when peer access is enabled.

### Pair another computer

1. Close WoW on both computers. Run WoWSync on the second computer with its own `--install` path and `--listen 0.0.0.0`.
2. On that computer’s local UI, click **Show this machine’s token**.
3. On the receiving computer, add its address, for example `http://cube:8765`, or a Tailscale hostname/IP, and its token.
4. Choose the game version and source machine. Click **Compare changes**.
5. Select files or individual config settings, inspect **View** deltas, and click **Back up & sync selected**. Bulk **Select incoming** respects the folder/search filters and excludes conflicts and machine-local settings.
6. To pull in the other direction, pair the receiving computer on the other computer too. Repeat for additional machines.

Both peers must have the same `_version_` directory name. The parent installation paths may differ. Account/realm/character paths are copied literally; confirm they match on both computers before syncing. If the source is offline, busy, or running WoW, retry when it is ready. On first pairing, explicitly choose a winner for existing differences.

### How local settings and conflicts work

`Config.wtf` is merged by CVar, preserving unselected lines. Recognized gameplay CVars can be selected with incoming changes. Hardware settings and unknown CVars require individual opt-in. If you intentionally select one, the UI asks before overriding it. Account identity and locale keys are hidden in previews and never applied by config merging.

If either config file has duplicate setting names or unsupported text encoding, it is marked **manual review** and excluded from selection. Other files in that version can still sync. WoWSync does not guess which duplicate setting you intended or rewrite your original config to fix it.

For ordinary files, the accepted shared hash is the baseline. A source-only change is incoming; a destination-only change stays local. If both differ from the baseline, the preview reports a conflict and leaves it unselected. Choosing a conflict means taking the source file in full. A successful pull records baselines for selected and already-matching entries. Each peer/version has its own baseline. Restore deliberately keeps the previous sync baseline: restored differences require review on the next pull.

WoWSync blocks comparisons and writes when it detects WoW running. Exit the game before syncing and keep it closed until completion. File hashes are rechecked before writes, but an external program opening or changing files during the final write window can still race the operation.

## Private storage and recovery

Default state locations:

- Windows: `%LOCALAPPDATA%\WoWSync`
- Linux/macOS: `~/.local/share/WoWSync`
- Override with `--state <directory>`. Use a separate state directory for each independently managed installation.

`settings.json` contains pairing credentials. `baselines/` stores accepted shared hashes and config values. `backups/<batch>/files/` contains original files and a `journal.json` records before/after hashes. **Treat the entire state directory as private. Never commit, upload, or share it.** Backups are retained until you remove them yourself; there is no automatic pruning in this release.

Use **Backup history → Restore** to reverse a batch. WoWSync checks every affected file before restoring and refuses to overwrite newer edits. Interrupted/failed batches are also available for recovery: unchanged original files are skipped, files still matching the sync output are restored, and ambiguous edits block recovery. Writes are atomic per file, not across an entire directory. A power loss may leave a partially completed batch; use its history entry before retrying. Preserve the state directory for recovery.

## Network and trust

- Use **Tailscale** for encrypted transport. LAN HTTP sends tokens and game data without encryption. Do not expose the HTTP port directly to the internet. HTTPS peer addresses are supported when placed behind your own trusted TLS endpoint, but WoWSync does not provision certificates.
- Pair only computers you control and trust. Tokens allow reading supported game files, including SavedVariables that may contain private addon data. Root config identity values are excluded from application, but the peer protocol still transmits the raw source file when selected config settings are fetched.
- The local management URL carries a temporary session secret in its fragment. The UI moves it to session storage and removes the fragment from the address bar. Restarting the app invalidates that management session. Do not publish the original URL or terminal output containing it.
- **Rotate token** revokes the current inbound peer token. Remove and re-add that machine with the new token on its readers. Removing a peer locally forgets its outbound credentials; it does not revoke that peer’s access to this computer. Rotate this computer’s token to revoke inbound access.
- Paths are constrained to Interface/WTF, symlinks and junctions are excluded, and common executable/archive/backup files are excluded. Snapshots are limited to 100,000 files / 1 GiB, each file to 128 MiB. Text deltas are limited to 512 KiB combined input and 120,000 output characters. Oversized snapshots fail visibly rather than silently syncing partial data.

The intended host is each game machine. The Cube can run its own peer when it is also a game machine. A database host, cloud relay, NAS hub, and always-on central server are unnecessary. This release does not implement a standalone relay or offline delivery.

## Development

```powershell
python -m unittest discover -s tests -v
node --check wowsync/static/app.js
```

Tests use temporary synthetic game installations, including a real two-service HTTP flow. They do not touch installed games. CI runs Windows and Linux with Python 3.11 and 3.14. Windows is the primary platform; the Linux process guard supports common WoW executable names but has not been validated against every Wine/Proton setup.

For a disposable two-machine UI demo:

```powershell
python scripts/demo.py
```

Open the demo URL printed in the terminal. It uses ports 8775/8776 and temporary game folders removed on normal exit. This does not connect to your real installation.

Optional installation as a command: `python -m pip install .`, then `wowsync --install <path> --open`.

This is an independent community project, not affiliated with Blizzard Entertainment.
