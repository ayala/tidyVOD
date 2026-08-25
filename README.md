# tidyVOD for Dispatcharr

tidyVOD is a headless Dispatcharr plugin for renaming and combining provider VOD categories. Its main editor deliberately stays simple: every detected movie and series category gets one optional clean-name field.

For example, entering `Netflix` beside `Netflix Movies`, `Netflix HEVC`, and `Netflix Kids` combines all three in Dispatcharr's curated Xtream output. Entering `Foreign Horror` beside `FR Thriller`, `IT Horror`, and `ES Horror` creates a separate combined category at the same time. Blank fields keep their original category names.

tidyVOD can also:

- clean movie and series titles with optional advanced regex replacements;
- automatically back up clean category-name mappings and restore them after mistakes or provider rebuilds;
- replace provider artwork only for categories explicitly marked `[CLEAN]` or `[EN]`;
- preview changes before writing them;
- reapply mappings automatically after an M3U refresh;
- restore category and title values it previously changed;
- export curated categories as `.m3u` (and optional `.xml` XMLTV stub) for downstream tools like m3u4u in one click.

The original stream IDs, provider credentials, and playback URLs are never changed.

## Install

For managed installs and GUI updates, add this URL under **Dispatcharr → Settings → Plugins → Manage Repositories**:

```text
https://raw.githubusercontent.com/ayala/tidyVOD/refs/heads/main/manifest.json
```

Install or update tidyVOD from the plugin browser. The internal plugin key remains unchanged, so existing mappings and backups carry forward. Enable it, reload the plugin list so detected category fields appear, then use **Preview** or **Preview export** before applying changes.

For a manual install, upload `releases/v0.6.4/tidyvod-v0.6.4.zip` through **Dispatcharr → Settings → Plugins → Install Plugin**.

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

## Clean category artwork

Append `[CLEAN]` or `[EN]` to a clean category name to opt in only that source category. For example, `Netflix Test [CLEAN]` and `Foreign Movies [EN]` are exported as `Netflix Test` and `Foreign Movies`; markers are never included in Xtream category names.

Save settings, then run **Prepare selected covers**. tidyVOD requests image choices only for opted-in titles, processing at most 250 new titles per run. `[CLEAN]` prefers TMDB images without a language tag—the closest reliable metadata signal for textless/key artwork. `[EN]` prefers English-language posters with the title. Either mode falls back to the highest-rated available TMDB poster. Run **Preview** to see the exact replacement count before **Apply**. Existing covers are overwritten only for marked categories and can be restored.

Dispatcharr stores artwork on the shared movie/show record rather than on each category relation. If the same title belongs to both a `[CLEAN]` and `[EN]` category, `[EN]` wins deterministically.

## Category-name backups

**Back up mappings** saves every nonblank clean-name field as a versioned JSON snapshot. Preview and Apply also create an automatic snapshot whenever the mappings have changed. Up to 50 unique snapshots are retained under `/data/plugins/.tidyvod_backups`, outside the replaceable plugin folder and inside Dispatcharr's normal plugin-data backup scope. Existing VOD Cleaner snapshots are copied forward automatically. The manual action reports the JSON file path and fills **Portable category-name backup** after a page reload; copy that JSON into a local `.json` file for an off-server backup.

**Restore mappings** restores the latest server snapshot into plugin settings. To restore a local copy, paste it into **Portable category-name backup** and press **Import pasted backup**. Snapshots include both the database category ID and provider category name, allowing name-based recovery when a provider refresh recreates categories with different IDs. Reload the Plugins page immediately after restoring so the editor displays the restored values.

## Safe workflow

1. Enter clean category names.
2. Save settings.
3. Run **Back up mappings** and keep a local JSON copy.
4. If using `[CLEAN]` or `[EN]`, run **Prepare selected covers**.
5. Run **Preview**.
6. Run **Apply**.
7. Enable automatic application only after confirming the result.

tidyVOD stores restore markers in Dispatcharr's existing `custom_properties` JSON. Provider refreshes may restore upstream categories, which is why the optional M3U-refresh action reapplies the mappings.

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
