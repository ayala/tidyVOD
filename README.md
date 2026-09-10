# tidyVOD for Dispatcharr

tidyVOD is a headless Dispatcharr plugin for renaming, combining, hiding, and normalizing provider VOD categories. Every detected movie and series category gets an optional clean-name field plus **Hide this category** and **TMDB Artwork** switches.

For example, entering `Netflix` beside `Netflix Movies`, `Netflix HEVC`, and `Netflix Kids` combines all three in Dispatcharr's curated Xtream output. Entering `Foreign Horror` beside `FR Thriller`, `IT Horror`, and `ES Horror` creates a separate combined category at the same time. Blank fields keep their original category names.

tidyVOD can also:

- clean movie and series titles with optional advanced regex replacements;
- automatically back up clean category-name mappings and restore them after mistakes or provider rebuilds;
- preview changes before writing them;
- continuously place newly imported VOD into your saved curated categories;
- stop importing selected categories and remove their existing provider assignments;
- opt individual categories into conservative, language-aware TMDB title and poster cleanup;
- restore category and title values it previously changed;
- export curated categories as `.m3u` (and optional `.xml` XMLTV stub) for downstream tools like m3u4u in one click.

The original stream IDs, provider credentials, and playback URLs are never changed.

## Install

For managed installs and GUI updates, add this URL under **Dispatcharr → Settings → Plugins → Manage Repositories**:

```text
https://raw.githubusercontent.com/ayala/tidyVOD/refs/heads/main/manifest.json
```

Install or update tidyVOD from the plugin browser. The internal plugin key remains unchanged, so existing mappings and backups carry forward. Enable it, reload the plugin list so detected category fields appear, then use **Preview** or **Preview export** before applying changes.

For a manual install, upload `releases/v0.8.12/tidyvod-v0.8.12.zip` through **Dispatcharr → Settings → Plugins → Install Plugin**.

Dispatcharr v0.24.0 or newer is required.

## Category editor

Each provider category has a clean-name field followed by hide and TMDB cleanup switches:

| Provider category | Clean name | Hide | TMDB Artwork |
| --- | --- | --- | --- |
| EN\| Netflix Movies 4K | Netflix | Off | On |
| ES\| Netflix Movies 4K | Netflix ES | Off | On |
| Netflix Kids | Netflix Kids | Off | Off |
| Unwanted PPV | *(blank)* | On | Off |

Matching clean names combine automatically. Different clean names create different combined lists in the same run.

Hidden categories remain visible in tidyVOD settings. **Preview** reports what would be removed without changing data. **Apply** disables the provider category and removes its existing movie or series assignments; the watcher keeps it disabled after later refreshes. Turning Hide off restores the category's previous enabled state. Run a provider VOD refresh to reimport titles that are still available. If a title also has a relation from another visible category or provider, that copy remains.

## Automatic TMDB titles and optional artwork

Official TMDB title normalization runs automatically for every active VOD category. The per-category **TMDB Artwork** switch controls only whether tidyVOD replaces poster art. A prefix such as `ES|`, `EN|`, `FR|`, or `IT|` selects the localized title and poster language; unprefixed categories and titles default to English. Prefix mappings are editable under **Language prefix mappings**. By default the visible title retains that identity, for example `ES| El peor vecino del mundo (2022)`; turn off **Keep language prefix in cleaned titles** only if the category name alone is enough to distinguish languages.

Matching is deliberately conservative: an existing TMDB ID is preferred, then an IMDb ID, then one exact normalized title-and-year result. The category prefix is authoritative; when it is absent, a provider title prefix such as `EN -` is accepted as a fallback, then English is used. Missing years, ambiguous results, and shared titles selected by conflicting language categories are left unchanged. The final title comes from the confirmed TMDB record, so actor names and provider-added wording disappear. **Remove these provider title tags** is an editable comma-separated list used only to form searches; remove a token if it is meaningful for your provider.

Normalization runs automatically in small batches during the one-minute watcher cycle. Large libraries require multiple passes. The Settings status shows whether the watcher is running, the last completed pass, and how many items remain queued.

Artwork selection accepts only posters tagged with the category language or English, never untagged/textless artwork. The requested language wins, followed by the highest-voted English poster. This avoids provider-generated `4K`, `HDR`, codec, audio, and source overlays in normal cases. TMDB does not expose a machine-readable “contains a 4K badge” flag, so no plugin can absolutely certify the pixels without an OCR/image-analysis dependency; a rare incorrectly uploaded TMDB poster may still require manual correction.

Enter a TMDB API key (or configure `TMDB_API_KEY` in Dispatcharr), save settings, enable the category switch, and use **Clean selected VOD now** for an immediate pass. The normal one-minute watcher then processes up to 25 new/reset items per pass and reapplies managed names and posters after provider refreshes. Already-correct items make no network request. The manual action processes up to 250 items per run.

Dispatcharr stores one title and one poster on each shared Movie/Series record. If the same record is attached to selected categories with different language prefixes, tidyVOD skips it as a language conflict instead of allowing one category to overwrite the other.

## Category-name backups

**Back up mappings** saves every nonblank clean name and every hidden-category choice as a versioned JSON snapshot. Preview and Apply also create an automatic snapshot whenever these choices change. Up to 50 unique snapshots are retained under `/data/plugins/.tidyvod_backups`, outside the replaceable plugin folder and inside Dispatcharr's normal plugin-data backup scope. Existing VOD Cleaner snapshots are copied forward automatically. The manual action reports the JSON file path and fills **Portable category-name backup** after a page reload; copy that JSON into a local `.json` file for an off-server backup.

**Restore mappings** restores the latest server snapshot into plugin settings. To restore a local copy, paste it into **Portable category-name backup** and press **Import pasted backup**. Snapshots include both the database category ID and provider category name, allowing name-based recovery when a provider refresh recreates categories with different IDs. Reload the Plugins page immediately after restoring so the editor displays the restored values.

## Safe workflow

1. Enter clean category names.
2. Save settings.
3. Run **Back up mappings** and keep a local JSON copy.
4. Run **Preview**.
5. Run **Apply**.
6. Leave **Keep curated categories synchronized** enabled so later provider additions follow the same mappings.

Movie and Series settings are separated by two blank spacer rows for easier scanning.

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

### 0.7.4 category visibility update

- Add a clear **Hide this category** switch beneath every category.
- Keep hidden categories available in settings for later restoration.
- Disable future provider imports and remove existing assignments for hidden categories.
- Preserve and restore hide choices through automatic and portable backups.
- Double the visual separation between Movie and Series settings.

### 0.7.5 source-count correction

- Count VOD by its original provider category after tidyVOD moves it into a clean category.
- Continue showing zero for a hidden category after its provider assignments are removed.

### 0.7.6 split source counts

- Show totals as `256 movies • 0 in original category → 256 moved by tidyVOD`.
- Use the natural plural labels `movies` and `series` instead of `movie(s)` and `series(s)`.

### 0.8.0 language-aware TMDB cleanup

- Add a **TMDB Clean-up** switch directly beneath each category's Hide switch.
- Preserve category language prefixes in cleaned titles by default.
- Make prefix mappings and removable provider tags editable.
- Prefer IDs, require exact title-and-year fallback matches, and leave ambiguity unchanged.
- Select localized or English TMDB posters while excluding untagged/textless art.
- Reapply managed names and posters through the existing one-minute watcher.
- Preserve TMDB cleanup selections in server and portable mapping backups.

### 0.8.1 provider-credit matching

- Safely retry title matching without a trailing two-to-four-word all-caps actor credit, such as `A Man Called Otto TOM HANKS`.
- Refuse that heuristic when the entire provider title is uppercase.

### 0.8.2 embedded-tag parsing

- Remove configured tags regardless of whether they appear before or after the year.
- Recognize provider title prefixes written as `EN|`, `EN:`, `EN -`, or with an en/em dash.

### 0.8.3 synchronization results

- Replace the misleading `Repaired 0` wording when categories are already correct.
- Include TMDB cleaned, already-clean, uncertain, missing-key, and remaining results in Synchronize and Show status messages.
- Prefer a category language prefix but fall back to a recognized prefix on each provider title.
- Add a backed-up per-category language override for categories whose original prefix was already removed.
- Report missing language prefixes, shared-language conflicts, missing posters, and request errors instead of presenting unexplained zeros.

### 0.8.4 visible artwork and reporting

- Apply selected TMDB artwork to the provider relation that Dispatcharr displays, not only the shared Movie/Series logo.
- Preserve the original relation artwork for Restore and automatically repair provider-refresh overwrites without repeating TMDB requests.
- Add **Show cleaned titles** with managed movie/series counts, poster-repair detection, and the first 20 cleaned names.

### 0.8.5 provider release-label matching

- Recognize configurable leading release labels such as `SD/CAM –`, `HDCAM -`, and `TS |` before searching TMDB.
- Require an explicit separator so genuine titles such as `Cam (2018)` are not damaged.
- Leave unmatched titles and their existing provider artwork unchanged.

### 0.8.6 player-safe TMDB titles

- Store matched TMDB names in a player-safe form by default, using language prefixes such as `ES -`.
- Preserve real bracketed names in clients that otherwise treat them as tags by substituting visually equivalent fullwidth brackets; `[REC]²` becomes `ES - ［REC］² (2009)`.
- Normalize Unicode sequel digits only during matching, so `[REC]²` compares as `REC2` while the official superscript remains in the displayed title.
- Allow exact TMDB title styling by turning off **Player-safe TMDB titles**.
- Keep selected TMDB posters in a relation field that survives Dispatcharr's on-demand provider metadata refresh.
- Report both shared-item and provider-relation poster assignments that need repair.

### 0.8.13 TMDB queue progression

- Prevents unmatched, ambiguous, request-error, and no-poster records from monopolizing every batch.
- Retries transient request failures after one hour and uncertain matches after seven days.
- Falls back to a conservative title/year search when a provider supplies a stale TMDB ID.
- Applies built-in language and provider-tag defaults even when Dispatcharr has not persisted untouched settings.

### 0.8.12 concise status copy

- Rename the status heading to **tidyVOD Status**, simplify the explanation heading, and remove the misleading TMDB environment-key hint.
- Default TMDB Artwork to off for both movie and series categories; saved per-category choices remain authoritative.

### 0.8.11 concise category controls

- Shorten the Hide and TMDB Artwork descriptions without changing their behavior.

### 0.8.10 separate titles from artwork

- Normalize official TMDB titles automatically across all active VOD categories.
- Rename each per-category switch to **TMDB Artwork** and limit it strictly to poster enrichment.
- Remove the per-category language override; category/title prefixes are detected automatically and unprefixed media defaults to English.
- Explain the one-minute batch cycle and remaining backlog directly in Settings.

### 0.8.9 movie cleanup defaults

- Movie-category **TMDB Clean-up** switches now default to on, including newly discovered categories.
- Each visible category switch remains authoritative; turning one off excludes that category.
- Removes the short-lived global override so the visible settings always describe the actual category choices.

### 0.8.8 all-movie TMDB switch

- Adds **Enable TMDB Clean-up for all** directly below the **Movie categories** heading.
- Turning it on includes every active movie category in TMDB cleanup. Turning it off returns to the individual category switches without erasing those choices.

### 0.8.7 visible watcher status and full report

- Show **TMDB watcher — WATCHING**, **RUNNING**, or **OFF** in Settings with last-pass, last-change, and result counts.
- Save every managed title and its item/relation poster status to `/data/plugins/.tidyvod_backups/tmdb-cleanup-report.csv` when **Show cleaned titles** runs.
- Show the report path beneath the Actions list instead of relying only on the short notification popup.
- Expose the player-safe title and leading release-label settings that were previously omitted from the generated Settings panel.

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
