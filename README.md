# tidyVOD for Dispatcharr

tidyVOD is a headless Dispatcharr plugin for renaming and combining provider VOD categories. Its main editor deliberately stays simple: every detected movie and series category gets one optional clean-name field.

For example, entering `Netflix` beside `Netflix Movies`, `Netflix HEVC`, and `Netflix Kids` combines all three in Dispatcharr's curated Xtream output. Entering `Foreign Horror` beside `FR Thriller`, `IT Horror`, and `ES Horror` creates a separate combined category at the same time. Blank fields keep their original category names.

tidyVOD can also:

- clean movie and series titles with optional advanced regex replacements;
- automatically back up clean category-name mappings and restore them after mistakes or provider rebuilds;
- preview changes before writing them;
- continuously place newly imported VOD into your saved curated categories;
- restore category and title values it previously changed;
- export curated categories as `.m3u` (and optional `.xml` XMLTV stub) for downstream tools like m3u4u in one click.

The original stream IDs, provider credentials, and playback URLs are never changed.

## Install

For managed installs and GUI updates, add this URL under **Dispatcharr → Settings → Plugins → Manage Repositories**:

```text
https://raw.githubusercontent.com/ayala/tidyVOD/refs/heads/main/manifest.json
```

Install or update tidyVOD from the plugin browser. The internal plugin key remains unchanged, so existing mappings and backups carry forward. Enable it, reload the plugin list so detected category fields appear, then use **Preview** or **Preview export** before applying changes.

For a manual install, upload `releases/v0.7.2/tidyvod-v0.7.2.zip` through **Dispatcharr → Settings → Plugins → Install Plugin**.

Dispatcharr v0.24.0 or newer is required.

## Category editor

Each provider category has one field:

| Provider category | Clean name |
| --- | --- |
| Netflix Movies | Netflix |
| Netflix HEVC | Netflix |
| Netflix Kids | Netflix |
| FR Thriller | Foreign Horror |
| IT Horror | Foreign Horror |
| ES Horror | Foreign Horror |
| Documentaries 4K HEVC | Documentaries |

Matching clean names combine automatically. Different clean names create different combined lists in the same run.

## Category-name backups

**Back up mappings** saves every nonblank clean-name field as a versioned JSON snapshot. Preview and Apply also create an automatic snapshot whenever the mappings have changed. Up to 50 unique snapshots are retained under `/data/plugins/.tidyvod_backups`, outside the replaceable plugin folder and inside Dispatcharr's normal plugin-data backup scope. Existing VOD Cleaner snapshots are copied forward automatically. The manual action reports the JSON file path and fills **Portable category-name backup** after a page reload; copy that JSON into a local `.json` file for an off-server backup.

**Restore mappings** restores the latest server snapshot into plugin settings. To restore a local copy, paste it into **Portable category-name backup** and press **Import pasted backup**. Snapshots include both the database category ID and provider category name, allowing name-based recovery when a provider refresh recreates categories with different IDs. Reload the Plugins page immediately after restoring so the editor displays the restored values.

## Safe workflow

1. Enter clean category names.
2. Save settings.
3. Run **Back up mappings** and keep a local JSON copy.
4. Run **Preview**.
5. Run **Apply**.
6. Leave **Keep curated categories synchronized** enabled so later provider additions follow the same mappings.

**Apply runs immediately. You do not need to press Run afterward.** Keep tidyVOD and **Keep curated categories synchronized** enabled. The watcher starts when Dispatcharr loads the enabled plugin, waits 15 seconds for startup, and checks every minute. The M3U event button is an internal notice, not a start-watching button.

Automatic repair covers new imports, provider-reset assignments, missing categories, and previously curated items whose saved clean name changed. It uses the original source markers in Dispatcharr's `custom_properties` JSON. If a provider recreates a source category with a different ID, exact source names retained in mapping backups allow recovery. Blank/removed mappings are never restored automatically, ambiguous names are not guessed, and account/movie/series scope is preserved. If both the source identity and its backup/restore markers are gone, manual mapping or backup restoration may still be required.

The database query selects assignments that differ from their targets; already-correct items are not loaded or rewritten. Repairs are written in batches, and a shared file lock prevents overlapping synchronization, Apply, and Restore runs. A refresh can briefly show provider categories until the next check; this is repair after import, not interception of the provider import itself. Automatic synchronization applies category-editor mappings, not advanced title-cleanup rules.

Use **Synchronize now** for an immediate category repair. **Show status** reports the last check and warns if there is no recent activity for more than three minutes, or reports a failure, disabled synchronization, or no saved mappings. Status is read on demand, not a push notification. If it stays stale, reload tidyVOD or restart Dispatcharr. Disable automatic synchronization before intentionally using **Restore** to undo category changes.

### 0.7.3 reliability update

- Start the watcher on plugin load, without requiring an action click after restart.
- Retire replaced watcher instances and retry transient database failures.
- Repair changed/missing assignments and recover recreated source IDs by exact backed-up names.
- Preserve source names in backups when their original database rows disappear.
- Show explicit stale/disabled/error status; protect Apply/Restore with the same repair lock.

Update the existing plugin; do not uninstall it. Existing settings and versioned mapping backups remain in place. After upgrading from 0.7.2, a one-time Dispatcharr restart is recommended to clear older watcher code in any long-lived worker (this interrupts active playback). Then, after a minute, use **Show status** to verify a fresh successful check without pressing Apply or Run.

Developer verification: install Django 5.2 in a separate test environment, then run `make test test-orm` (or `make package`) with `PYTHON` pointing to that environment. The ORM tests use an isolated in-memory SQLite database and temporary backup directory, never live Dispatcharr data.

## Immutable releases

Every release is written to its own `releases/vX.Y.Z/` directory with an installable ZIP and `SHA256SUMS`. Packaging refuses to overwrite an existing version. `make clean` never removes releases.

Build a new release only after increasing `VERSION` in the Makefile and matching versions in `plugin.py` and `plugin.json`:

```sh
make package
make verify
```

Run helper tests with:

```sh
make test
```

The Netflix, Hulu, and Prime Video marks shown in the plugin icon belong to their respective owners and are used only to illustrate the provider-category aggregation concept.
