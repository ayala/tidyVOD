"""Dispatcharr plugin entry point for configurable VOD organization."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json
import os
import re
import shutil
import tempfile
from typing import Any

from django.db import IntegrityError, transaction

from .core import (
    ConfigurationError,
    category_override,
    category_override_details,
    category_target,
    clean_title,
    compile_category_rules,
    compile_title_rules,
    parse_account_names,
)


MARKER = "vodarranger"


class Plugin:
    name = "tidyVOD"
    version = "0.5.1"
    description = "Rename, combine, back up, and optionally clean artwork for curated VOD categories."
    author = "tidyVOD contributors"

    BASE_FIELDS = [
        {
            "id": "category_editor_help",
            "label": "Category editor",
            "type": "info",
            "value": "Enter a clean name beside any provider category. Give several categories the same clean name to combine them. Leave a field blank to keep the original name.",
        },
        {
            "id": "account_names",
            "label": "Limit to provider accounts",
            "type": "string",
            "default": "",
            "help_text": "Optional comma-separated Dispatcharr M3U account names. Blank means all active accounts.",
        },
        {
            "id": "clean_art_help",
            "label": "Cover artwork (category opt-in)",
            "type": "info",
            "value": "Use [CLEAN] for language-neutral/textless art or [EN] for a cleaner English-title poster. Markers are stripped from exported category names, and only marked source categories are eligible.",
        },
        {
            "id": "tmdb_api_key",
            "label": "TMDB API key for cover artwork",
            "type": "string",
            "input_type": "password",
            "default": "",
            "help_text": "Optional when Dispatcharr already has TMDB_API_KEY configured. Used only by Prepare selected covers.",
        },
        {
            "id": "advanced_rules_help",
            "label": "Advanced rules (optional)",
            "type": "info",
            "value": "The simple category rows above are usually all you need. Regex rules remain available for bulk patterns and title cleanup.",
        },
        {
            "id": "category_rules",
            "label": "Advanced category rules (JSON)",
            "type": "text",
            "default": "[]",
            "help_text": "Ordered rules. Example: [{\"pattern\": \"^(UK: )?Movies\", \"target\": \"Movies\", \"content_type\": \"movie\"}]",
        },
        {
            "id": "title_rules",
            "label": "Title cleanup rules (JSON)",
            "type": "text",
            "default": "[]",
            "help_text": "Ordered regex replacements. Example: [{\"pattern\": \"\\\\s*\\\\[(?:FHD|4K)\\\\]\\\\s*\", \"replacement\": \" \"}]",
        },
        {
            "id": "portable_mapping_json",
            "label": "Portable category-name backup (JSON)",
            "type": "text",
            "default": "",
            "help_text": "Back up mappings fills this after a page reload. Copy it into a local .json file, or paste a saved backup here and use Import pasted backup.",
        },
        {
            "id": "auto_apply",
            "label": "Apply automatically after M3U refresh",
            "type": "boolean",
            "default": False,
        },
    ]

    actions = [
        {
            "id": "backup_mappings",
            "label": "Back up category names",
            "description": "Save a durable snapshot of every clean category name you entered.",
            "button_label": "Back up mappings",
            "button_color": "blue",
        },
        {
            "id": "restore_mappings",
            "label": "Restore category names",
            "description": "Restore the latest saved clean-name snapshot into the category editor.",
            "button_label": "Restore mappings",
            "button_color": "red",
            "confirm": {
                "title": "Restore saved category names?",
                "message": "Current clean-name fields will be backed up first, then replaced by the latest saved snapshot.",
            },
        },
        {
            "id": "import_mappings",
            "label": "Import portable category-name backup",
            "description": "Restore clean-name fields from JSON pasted into the portable backup field.",
            "button_label": "Import pasted backup",
            "button_color": "red",
            "confirm": {
                "title": "Import pasted category names?",
                "message": "Current clean-name fields will be backed up first, then replaced by the pasted snapshot.",
            },
        },
        {
            "id": "prepare_clean_art",
            "label": "Prepare selected category artwork",
            "description": "Find TMDB covers for up to 250 titles in [CLEAN] or [EN] categories.",
            "button_label": "Prepare selected covers",
            "button_color": "blue",
        },
        {
            "id": "preview",
            "label": "Preview changes",
            "description": "Count and sample changes without writing to the database.",
            "button_label": "Preview",
            "button_color": "blue",
        },
        {
            "id": "apply",
            "label": "Apply changes",
            "description": "Apply configured category and title rules.",
            "button_label": "Apply",
            "button_color": "green",
            "confirm": {"title": "Apply VOD changes?", "message": "A restore marker will be saved before each change."},
        },
        {
            "id": "restore",
            "label": "Restore plugin changes",
            "description": "Restore category and title values saved by this plugin, plus any legacy artwork changes.",
            "button_label": "Restore",
            "button_color": "red",
            "confirm": {"title": "Restore VOD values?", "message": "This only restores values tracked by tidyVOD and earlier compatible builds."},
        },
        {
            "id": "on_m3u_refresh",
            "label": "Auto-apply after M3U refresh",
            "description": "Internal event action controlled by the auto-apply setting.",
            "events": ["m3u_refresh"],
        },
    ]

    @property
    def fields(self) -> list[dict[str, Any]]:
        """Add one direct clean-name field per detected provider category."""
        base = {field["id"]: field for field in self.BASE_FIELDS}
        try:
            dynamic = self._category_editor_fields()
        except Exception:
            dynamic = [{
                "id": "category_editor_unavailable",
                "label": "Category list unavailable",
                "type": "info",
                "value": "Refresh an Xtream provider with VOD scanning enabled, then reload plugins to populate the editor.",
            }]
        advanced_ids = [
            "advanced_rules_help", "category_rules", "title_rules",
            "portable_mapping_json",
        ]
        return (
            [base["category_editor_help"], base["account_names"]]
            + [base["clean_art_help"], base["tmdb_api_key"]]
            + dynamic
            + [base["auto_apply"]]
            + [base[field_id] for field_id in advanced_ids]
        )

    @staticmethod
    def _category_editor_fields() -> list[dict[str, Any]]:
        from django.db.models import Count
        from apps.vod.models import VODCategory

        fields: list[dict[str, Any]] = []
        for content_type, relation_name, label in (
            ("movie", "m3umovierelation", "Movie categories"),
            ("series", "m3useriesrelation", "Series categories"),
        ):
            categories = (
                VODCategory.objects.filter(
                    category_type=content_type,
                    m3u_relations__m3u_account__is_active=True,
                    m3u_relations__enabled=True,
                )
                .annotate(item_count=Count(relation_name, distinct=True))
                .distinct()
                .order_by("name")
            )
            rows = list(categories)
            if not rows:
                continue
            fields.append({
                "id": f"{content_type}_category_heading",
                "label": label,
                "type": "info",
                "value": f"{len(rows)} detected. Matching clean names are combined automatically.",
            })
            for category in rows:
                account_names = list(
                    category.m3u_relations.filter(m3u_account__is_active=True)
                    .values_list("m3u_account__name", flat=True)
                    .distinct()[:4]
                )
                provider_text = ", ".join(account_names)
                if len(account_names) == 4:
                    provider_text += ", …"
                description = f"{category.item_count} {content_type}(s)"
                if provider_text:
                    description += f" • {provider_text}"
                fields.append({
                    "id": f"category_override_{content_type}_{category.pk}",
                    "label": category.name,
                    "type": "string",
                    "default": "",
                    "placeholder": "Clean name (blank = unchanged)",
                    "help_text": description,
                })
        if not fields:
            fields.append({
                "id": "no_categories_detected",
                "label": "No VOD categories detected",
                "type": "info",
                "value": "Enable VOD scanning on an Xtream provider, refresh it, then reload plugins.",
            })
        return fields

    def run(self, action: str, params: dict, context: dict) -> dict[str, Any]:
        settings = context.get("settings", {})
        logger = context.get("logger")
        try:
            if action == "on_m3u_refresh":
                if not settings.get("auto_apply", False):
                    return {"status": "ok", "message": "Automatic apply is disabled"}
                return self._apply(settings, logger, dry_run=False)
            if action == "preview":
                return self._apply(settings, logger, dry_run=True)
            if action == "backup_mappings":
                return self._backup_mappings_result(settings, reason="manual")
            if action == "restore_mappings":
                return self._restore_mappings(settings, logger)
            if action == "import_mappings":
                return self._import_mappings(settings, logger)
            if action == "prepare_clean_art":
                return self._prepare_clean_art(settings, logger)
            if action == "apply":
                return self._apply(settings, logger, dry_run=False)
            if action == "restore":
                return self._restore(settings, logger)
            return {"status": "error", "message": f"Unknown action: {action}"}
        except ConfigurationError as exc:
            return {"status": "error", "message": str(exc)}

    def _account_filter(self, settings: dict[str, Any]) -> dict[str, Any]:
        from apps.m3u.models import M3UAccount

        names = parse_account_names(settings.get("account_names", ""))
        filters: dict[str, Any] = {"m3u_account__is_active": True}
        if names:
            # Django's __in lookup is case-sensitive on PostgreSQL. Resolve IDs
            # explicitly so the user-facing account list remains case-insensitive.
            account_ids = [
                account.pk
                for account in M3UAccount.objects.filter(is_active=True).only("pk", "name")
                if account.name.casefold() in names
            ]
            filters["m3u_account_id__in"] = account_ids
        return filters

    @staticmethod
    def _mapping_backup_dir() -> str:
        plugins_dir = (
            os.environ.get("DISPATCHARR_PLUGINS_DIR")
            or os.environ.get("PLUGINS_DIR")
            or "/data/plugins"
        )
        backup_dir = os.path.join(plugins_dir, ".tidyvod_backups")
        legacy_dir = os.path.join(plugins_dir, ".vod_cleaner_backups")
        if not os.path.exists(backup_dir) and os.path.isdir(legacy_dir):
            try:
                shutil.copytree(legacy_dir, backup_dir)
            except OSError:
                return legacy_dir
        return backup_dir

    @staticmethod
    def _mapping_entries(settings: dict[str, Any]) -> list[dict[str, Any]]:
        from apps.vod.models import VODCategory

        selected = []
        category_ids = []
        pattern = re.compile(r"^category_override_(movie|series)_(\d+)$")
        for key, value in settings.items():
            match = pattern.fullmatch(str(key))
            clean_name = str(value or "").strip()
            if not match or not clean_name:
                continue
            content_type, category_id = match.group(1), int(match.group(2))
            selected.append((content_type, category_id, clean_name))
            category_ids.append(category_id)
        categories = {
            category.pk: category
            for category in VODCategory.objects.filter(pk__in=category_ids).only("pk", "name", "category_type")
        }
        entries = []
        for content_type, category_id, clean_name in selected:
            category = categories.get(category_id)
            entries.append({
                "content_type": content_type,
                "category_id": category_id,
                "provider_name": category.name if category else None,
                "clean_name": clean_name,
            })
        return sorted(
            entries,
            key=lambda entry: (
                entry["content_type"],
                str(entry.get("provider_name") or "").casefold(),
                entry["category_id"],
            ),
        )

    @staticmethod
    def _atomic_json_write(path: str, payload: dict[str, Any]) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix=".mapping-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise

    def _backup_mappings(self, settings: dict[str, Any], reason: str) -> dict[str, Any]:
        entries = self._mapping_entries(settings)
        backup_dir = self._mapping_backup_dir()
        latest_path = os.path.join(backup_dir, "latest.json")
        digest = hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        if os.path.isfile(latest_path):
            try:
                with open(latest_path, "r", encoding="utf-8") as handle:
                    latest = json.load(handle)
                if latest.get("mapping_sha256") == digest:
                    return latest
            except (OSError, ValueError, TypeError):
                pass

        timestamp = datetime.now(datetime_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = {
            "format": 1,
            "created_at": datetime.now(datetime_timezone.utc).isoformat(),
            "reason": reason,
            "plugin_version": self.version,
            "mapping_sha256": digest,
            "mappings": entries,
        }
        archive_path = os.path.join(backup_dir, f"mappings-{timestamp}-{digest[:8]}.json")
        self._atomic_json_write(archive_path, payload)
        self._atomic_json_write(latest_path, payload)

        archives = sorted(
            name for name in os.listdir(backup_dir)
            if name.startswith("mappings-") and name.endswith(".json")
        )
        for old_name in archives[:-50]:
            try:
                os.unlink(os.path.join(backup_dir, old_name))
            except OSError:
                pass
        return payload

    def _backup_mappings_result(self, settings: dict[str, Any], reason: str) -> dict[str, Any]:
        from apps.plugins.models import PluginConfig

        payload = self._backup_mappings(settings, reason)
        count = len(payload.get("mappings", []))
        with transaction.atomic():
            config = PluginConfig.objects.select_for_update().get(key=MARKER)
            updated_settings = dict(config.settings or {})
            updated_settings["portable_mapping_json"] = json.dumps(payload, indent=2, sort_keys=True)
            config.settings = updated_settings
            config.save(update_fields=["settings", "updated_at"])
        latest_path = os.path.join(self._mapping_backup_dir(), "latest.json")
        return {
            "status": "ok",
            "backup": {"created_at": payload.get("created_at"), "mappings": count},
            "file": latest_path,
            "message": (
                f"Backed up {count} clean category names. Reload this page to copy the portable JSON, "
                f"or retrieve {latest_path} from the Dispatcharr data volume."
            ),
        }

    def _restore_mappings(self, settings: dict[str, Any], logger: Any) -> dict[str, Any]:
        backup_dir = self._mapping_backup_dir()
        latest_path = os.path.join(backup_dir, "latest.json")
        if not os.path.isfile(latest_path):
            return {"status": "error", "message": "No category-name backup exists yet."}

        # Capture the intended restore target before the safety snapshot updates latest.json.
        with open(latest_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return self._restore_mapping_payload(settings, payload, logger, source="latest saved")

    def _import_mappings(self, settings: dict[str, Any], logger: Any) -> dict[str, Any]:
        raw = str(settings.get("portable_mapping_json", "") or "").strip()
        if not raw:
            return {
                "status": "error",
                "message": "Paste a tidyVOD JSON backup into Portable category-name backup first.",
            }
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {"status": "error", "message": f"The pasted backup is not valid JSON: {exc.msg}."}
        return self._restore_mapping_payload(settings, payload, logger, source="pasted")

    def _restore_mapping_payload(
        self, settings: dict[str, Any], payload: Any, logger: Any, source: str,
    ) -> dict[str, Any]:
        from apps.plugins.models import PluginConfig
        from apps.vod.models import VODCategory

        if not isinstance(payload, dict) or payload.get("format") != 1:
            return {"status": "error", "message": "This is not a supported tidyVOD backup."}
        mappings = payload.get("mappings")
        if not isinstance(mappings, list):
            return {"status": "error", "message": "The category-name backup is invalid."}

        # Preserve the current editor state after capturing the target snapshot.
        self._backup_mappings(settings, reason=f"before_{source.replace(' ', '_')}_restore")

        categories = list(VODCategory.objects.only("pk", "name", "category_type"))
        by_id = {(category.category_type, category.pk): category for category in categories}
        by_name = {
            (category.category_type, category.name.casefold()): category
            for category in categories
        }
        restored = {}
        missing = 0
        for entry in mappings:
            if not isinstance(entry, dict):
                continue
            content_type = entry.get("content_type")
            category = by_id.get((content_type, entry.get("category_id")))
            if category is None and entry.get("provider_name"):
                category = by_name.get((content_type, str(entry["provider_name"]).casefold()))
            clean_name = str(entry.get("clean_name") or "").strip()
            if category is None or not clean_name:
                missing += 1
                continue
            restored[f"category_override_{content_type}_{category.pk}"] = clean_name

        with transaction.atomic():
            config = PluginConfig.objects.select_for_update().get(key=MARKER)
            updated_settings = {
                key: value
                for key, value in (config.settings or {}).items()
                if not str(key).startswith("category_override_")
            }
            updated_settings.update(restored)
            config.settings = updated_settings
            config.save(update_fields=["settings", "updated_at"])

        logger.info("tidyVOD restored %s saved mapping fields", len(restored))
        return {
            "status": "ok",
            "restored_mappings": len(restored),
            "missing_categories": missing,
            "message": (
                f"Restored {len(restored)} clean category names from the {source} backup; "
                f"{missing} unavailable categories were skipped. "
                "Reload the Plugins page now to show the restored fields."
            ),
        }

    def _art_item_modes(self, settings: dict[str, Any]) -> dict[str, dict[int, str]]:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation

        account_filter = self._account_filter(settings)
        scoped: dict[str, dict[int, str]] = {"movie": {}, "series": {}}
        for content_type, relation_model, item_field in (
            ("movie", M3UMovieRelation, "movie"),
            ("series", M3USeriesRelation, "series"),
        ):
            relations = relation_model.objects.filter(**account_filter).select_related("category")
            for relation in relations.iterator(chunk_size=1000):
                props = relation.custom_properties or {}
                marker = props.get(MARKER, {}) if isinstance(props, dict) else {}
                original_category_id = marker.get("original_category_id") if isinstance(marker, dict) else None
                source_category_id = original_category_id or relation.category_id
                _, art_mode = category_override_details(settings, content_type, source_category_id)
                if art_mode:
                    item_id = getattr(relation, f"{item_field}_id")
                    current_mode = scoped[content_type].get(item_id)
                    # A Movie/Series has one global logo in Dispatcharr. When
                    # relations request both modes, explicit English wins.
                    if current_mode is None or art_mode == "en":
                        scoped[content_type][item_id] = art_mode
        return scoped

    @staticmethod
    def _cached_clean_cover(item: Any, art_mode: str) -> str | None:
        props = item.custom_properties or {}
        marker = props.get(MARKER, {}) if isinstance(props, dict) else {}
        if not isinstance(marker, dict):
            return None
        if marker.get("clean_cover_tmdb_id") != str(item.tmdb_id or ""):
            return None
        if marker.get("clean_cover_mode") != art_mode:
            return None
        value = str(marker.get("clean_cover_url") or "").strip()
        return value or None

    def _prepare_clean_art(self, settings: dict[str, Any], logger: Any) -> dict[str, Any]:
        import requests
        from apps.vod.models import Movie, Series

        scoped = self._art_item_modes(settings)
        total = sum(len(items) for items in scoped.values())
        if not total:
            return {
                "status": "error",
                "message": "No categories are marked [CLEAN] or [EN]. Add a marker after a clean category name and save settings.",
            }

        candidates: list[tuple[str, Any, str]] = []
        counts = Counter(total=total)
        for media_type, model, item_modes in (
            ("movie", Movie, scoped["movie"]),
            ("tv", Series, scoped["series"]),
        ):
            for item in model.objects.filter(pk__in=item_modes).iterator(chunk_size=500):
                art_mode = item_modes[item.pk]
                if self._cached_clean_cover(item, art_mode):
                    counts["ready"] += 1
                    continue
                if not item.tmdb_id:
                    counts["missing_tmdb"] += 1
                    continue
                props = item.custom_properties or {}
                marker = props.get(MARKER, {}) if isinstance(props, dict) else {}
                if (
                    isinstance(marker, dict)
                    and marker.get("clean_cover_checked") == str(item.tmdb_id)
                    and marker.get("clean_cover_mode") == art_mode
                ):
                    counts["no_cover"] += 1
                    continue
                if len(candidates) < 250:
                    candidates.append((media_type, item, art_mode))
                else:
                    counts["remaining"] += 1

        if not candidates:
            return {
                "status": "ok",
                "prepared": dict(counts),
                "message": (
                    f"{counts['ready']} clean covers are ready; {counts['missing_tmdb']} titles lack TMDB IDs; "
                    f"{counts['no_cover']} have no usable TMDB poster. Run Preview."
                ),
            }

        api_key = str(settings.get("tmdb_api_key", "") or os.environ.get("TMDB_API_KEY", "")).strip()
        if not api_key:
            return {
                "status": "error",
                "message": "Enter a TMDB API key or configure Dispatcharr's TMDB_API_KEY first.",
            }

        def fetch(candidate: tuple[str, Any, str]) -> tuple[str | None, str, str]:
            media_type, item, art_mode = candidate
            try:
                response = requests.get(
                    f"https://api.themoviedb.org/3/{media_type}/{item.tmdb_id}/images",
                    params={"api_key": api_key, "language": "en", "include_image_language": "null"},
                    timeout=12,
                )
                if response.status_code == 401:
                    return None, art_mode, "unauthorized"
                response.raise_for_status()
                posters = (response.json() or {}).get("posters") or []
            except (requests.RequestException, ValueError) as exc:
                logger.warning("TMDB image lookup failed for %s %s: %s", media_type, item.tmdb_id, exc)
                return None, art_mode, "error"
            usable = [poster for poster in posters if poster.get("file_path")]
            preferred = [
                poster for poster in usable
                if (poster.get("iso_639_1") is None if art_mode == "clean" else poster.get("iso_639_1") == "en")
            ]
            pool = preferred or usable
            if not pool:
                return None, art_mode, "ok"
            best = max(
                pool,
                key=lambda poster: (
                    float(poster.get("vote_average") or 0),
                    int(poster.get("vote_count") or 0),
                    int(poster.get("width") or 0) * int(poster.get("height") or 0),
                ),
            )
            selected_mode = art_mode if preferred else "fallback"
            return f"https://image.tmdb.org/t/p/w780{best['file_path']}", selected_mode, "ok"

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch, candidates))
        if any(status == "unauthorized" for _, _, status in results):
            return {"status": "error", "message": "TMDB rejected the API key."}

        for (_, item, art_mode), (url, selected_mode, status) in zip(candidates, results):
            if status == "error":
                counts["request_errors"] += 1
                continue
            props = dict(item.custom_properties or {})
            existing_marker = props.get(MARKER, {})
            marker = dict(existing_marker) if isinstance(existing_marker, dict) else {}
            marker["clean_cover_checked"] = str(item.tmdb_id)
            marker["clean_cover_tmdb_id"] = str(item.tmdb_id)
            marker["clean_cover_mode"] = art_mode
            if url:
                marker["clean_cover_url"] = url
                marker["clean_cover_textless"] = selected_mode == "clean"
                counts["prepared"] += 1
                counts[selected_mode] += 1
            else:
                marker.pop("clean_cover_url", None)
                counts["no_cover"] += 1
            props[MARKER] = marker
            item.custom_properties = props
            item.save(update_fields=["custom_properties", "updated_at"])

        counts["ready"] += counts["prepared"]
        return {
            "status": "ok",
            "prepared": dict(counts),
            "message": (
                f"Prepared {counts['prepared']} covers: {counts['clean']} language-neutral, "
                f"{counts['en']} English, and {counts['fallback']} fallbacks; "
                f"{counts['ready']} are now ready, {counts['missing_tmdb']} lack TMDB IDs, "
                f"and {counts['remaining']} remain for another run. Run Preview next."
            ),
        }

    def _apply(self, settings: dict[str, Any], logger: Any, dry_run: bool) -> dict[str, Any]:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation, Movie, Series, VODCategory, VODLogo

        category_rules = compile_category_rules(settings.get("category_rules", "[]"))
        title_rules = compile_title_rules(settings.get("title_rules", "[]"))
        relation_specs = [
            ("movie", M3UMovieRelation, "movie"),
            ("series", M3USeriesRelation, "series"),
        ]
        account_filter = self._account_filter(settings)
        try:
            self._backup_mappings(settings, reason="automatic_preview" if dry_run else "automatic_apply")
        except Exception as exc:
            logger.warning("Could not create automatic category-name backup: %s", exc)
        counts = Counter()
        samples: list[dict[str, str]] = []
        relation_changes = []
        title_candidates: dict[tuple[str, int], tuple[Any, str, str]] = {}
        art_item_modes: dict[str, dict[int, str]] = {"movie": {}, "series": {}}

        for content_type, relation_model, item_field in relation_specs:
            queryset = relation_model.objects.filter(**account_filter).select_related(
                "category", item_field, "m3u_account"
            )
            for relation in queryset.iterator(chunk_size=1000):
                item = getattr(relation, item_field)
                props = relation.custom_properties or {}
                marker = props.get(MARKER, {}) if isinstance(props, dict) else {}
                source_category = marker.get("original_category_name") or (
                    relation.category.name if relation.category else "Uncategorized"
                )
                source_category_id = marker.get("original_category_id") or relation.category_id
                direct_target, art_mode = category_override_details(
                    settings, content_type, source_category_id
                )
                if art_mode:
                    current_mode = art_item_modes[content_type].get(item.pk)
                    if current_mode is None or art_mode == "en":
                        art_item_modes[content_type][item.pk] = art_mode
                target = direct_target or category_target(source_category, content_type, category_rules)
                if target and (not relation.category or relation.category.name != target):
                    relation_changes.append((content_type, relation, source_category, target))
                    counts["categories"] += 1
                    self._sample(samples, "category", f"{source_category} -> {target}")

                item_props = item.custom_properties or {}
                item_marker = item_props.get(MARKER, {}) if isinstance(item_props, dict) else {}
                source_name = item_marker.get("original_name") or item.name
                cleaned = clean_title(source_name, content_type, title_rules)
                if cleaned and cleaned != item.name:
                    title_candidates[(content_type, item.pk)] = (item, source_name, cleaned)

        title_changes = []
        planned_keys = set()
        for (content_type, _), (item, source_name, cleaned) in title_candidates.items():
            model = Movie if content_type == "movie" else Series
            collision_key = (content_type, cleaned.casefold(), item.year)
            collision = collision_key in planned_keys
            if not item.tmdb_id and not item.imdb_id:
                collision = collision or model.objects.filter(name=cleaned, year=item.year).exclude(pk=item.pk).exists()
            if collision:
                counts["title_conflicts"] += 1
                self._sample(samples, "title conflict", f"{source_name} -> {cleaned}")
                continue
            planned_keys.add(collision_key)
            title_changes.append((item, source_name, cleaned))
            counts["titles"] += 1
            self._sample(samples, "title", f"{source_name} -> {cleaned}")

        cover_changes = []
        for model, content_type in ((Movie, "movie"), (Series, "series")):
            modes = art_item_modes[content_type]
            for item in model.objects.filter(pk__in=modes).select_related("logo"):
                url = self._cached_clean_cover(item, modes[item.pk])
                if not url:
                    counts["covers_unprepared"] += 1
                    continue
                if not item.logo_id or item.logo.url != url:
                    cover_changes.append((item, item.logo_id, url))
                    counts["covers"] += 1
                    self._sample(samples, "clean cover", f"{item.name} -> TMDB artwork")

        if dry_run:
            return self._result(counts, samples, dry_run=True)

        with transaction.atomic():
            category_cache = {}
            changed_relations = []
            for content_type, relation, source_category, target in relation_changes:
                key = (target, content_type)
                target_category = category_cache.get(key)
                if target_category is None:
                    target_category, _ = VODCategory.objects.get_or_create(name=target, category_type=content_type)
                    category_cache[key] = target_category
                props = dict(relation.custom_properties or {})
                marker = dict(props.get(MARKER, {}))
                marker.setdefault("original_category_id", relation.category_id)
                marker.setdefault("original_category_name", source_category)
                props[MARKER] = marker
                relation.custom_properties = props
                relation.category = target_category
                changed_relations.append(relation)
            for content_type, relation_model, _ in relation_specs:
                batch = [r for r in changed_relations if r.category.category_type == content_type]
                if batch:
                    relation_model.objects.bulk_update(batch, ["category", "custom_properties"], batch_size=1000)

            for item, source_name, cleaned in title_changes:
                props = dict(item.custom_properties or {})
                marker = dict(props.get(MARKER, {}))
                marker.setdefault("original_name", source_name)
                props[MARKER] = marker
                item.name = cleaned
                item.custom_properties = props
                try:
                    # The nested block creates a savepoint; a rare concurrent
                    # uniqueness conflict does not poison the outer transaction.
                    with transaction.atomic():
                        item.save(update_fields=["name", "custom_properties", "updated_at"])
                except IntegrityError:
                    counts["title_conflicts"] += 1
                    counts["titles"] -= 1

            for item, original_logo_id, url in cover_changes:
                logo, _ = VODLogo.objects.get_or_create(
                    url=url,
                    defaults={"name": f"tidyVOD clean cover - {item.name}"[:255]},
                )
                item.refresh_from_db(fields=["custom_properties", "logo"])
                props = dict(item.custom_properties or {})
                existing_marker = props.get(MARKER, {})
                marker = dict(existing_marker) if isinstance(existing_marker, dict) else {}
                marker.setdefault("original_logo_id", original_logo_id)
                marker["clean_cover_managed"] = True
                props[MARKER] = marker
                item.logo = logo
                item.custom_properties = props
                item.save(update_fields=["logo", "custom_properties", "updated_at"])

        logger.info("tidyVOD applied: %s", dict(counts))
        return self._result(counts, samples, dry_run=False)

    def _restore(self, settings: dict[str, Any], logger: Any) -> dict[str, Any]:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation, Movie, Series, VODCategory, VODLogo

        counts = Counter()
        account_filter = self._account_filter(settings)
        with transaction.atomic():
            for relation_model in (M3UMovieRelation, M3USeriesRelation):
                changed = []
                for relation in relation_model.objects.filter(**account_filter).iterator(chunk_size=1000):
                    props = dict(relation.custom_properties or {})
                    marker = props.get(MARKER)
                    if not isinstance(marker, dict) or "original_category_id" not in marker:
                        continue
                    original = VODCategory.objects.filter(pk=marker.get("original_category_id")).first()
                    if original is None and marker.get("original_category_name"):
                        original, _ = VODCategory.objects.get_or_create(
                            name=marker["original_category_name"],
                            category_type="movie" if relation_model is M3UMovieRelation else "series",
                        )
                    relation.category = original
                    props.pop(MARKER, None)
                    relation.custom_properties = props
                    changed.append(relation)
                if changed:
                    relation_model.objects.bulk_update(changed, ["category", "custom_properties"], batch_size=1000)
                    counts["categories"] += len(changed)

            scoped_movie_ids = M3UMovieRelation.objects.filter(**account_filter).values_list("movie_id", flat=True)
            scoped_series_ids = M3USeriesRelation.objects.filter(**account_filter).values_list("series_id", flat=True)
            for model, ids in ((Movie, scoped_movie_ids), (Series, scoped_series_ids)):
                for item in model.objects.filter(pk__in=ids).iterator(chunk_size=1000):
                    props = dict(item.custom_properties or {})
                    marker = props.get(MARKER)
                    if not isinstance(marker, dict):
                        continue
                    fields = ["custom_properties", "updated_at"]
                    if marker.get("original_name"):
                        name_collision = (
                            not item.tmdb_id
                            and not item.imdb_id
                            and model.objects.filter(name=marker["original_name"], year=item.year)
                            .exclude(pk=item.pk)
                            .exists()
                        )
                        if name_collision:
                            counts["title_conflicts"] += 1
                        else:
                            item.name = marker["original_name"]
                            fields.append("name")
                            counts["titles"] += 1
                    if marker.get("poster_managed") or marker.get("clean_cover_managed"):
                        item.logo = VODLogo.objects.filter(pk=marker.get("original_logo_id")).first()
                        fields.append("logo")
                        counts["covers"] += 1
                    props.pop(MARKER, None)
                    item.custom_properties = props
                    try:
                        with transaction.atomic():
                            item.save(update_fields=fields)
                    except IntegrityError:
                        # New provider data may now occupy the old (name, year)
                        # key. Keep the customized value instead of aborting all
                        # unrelated restores.
                        counts["title_conflicts"] += 1
                        if "name" in fields:
                            counts["titles"] -= 1
                        if "logo" in fields:
                            counts["covers"] -= 1
                        counts["save_conflicts"] += 1

        logger.info("tidyVOD restored: %s", dict(counts))
        return {
            "status": "ok",
            "restored": dict(counts),
            "message": (
                f"Restored {counts['categories']} category assignments, "
                f"{counts['titles']} titles, and {counts['covers']} cover artwork items"
            ),
        }

    @staticmethod
    def _sample(samples: list[dict[str, str]], kind: str, change: str) -> None:
        if len(samples) < 25:
            samples.append({"type": kind, "change": change})

    @staticmethod
    def _result(counts: Counter, samples: list[dict[str, str]], dry_run: bool) -> dict[str, Any]:
        summary = (
            f"{counts['categories']} category assignments and {counts['titles']} titles"
        )
        conflicts = counts["title_conflicts"]
        if conflicts:
            summary += f" ({conflicts} title conflicts skipped)"
        if counts["covers"] or counts["covers_unprepared"]:
            summary += (
                f". Clean covers: {counts['covers']} replacements and "
                f"{counts['covers_unprepared']} unprepared titles"
            )
        return {
            "status": "ok",
            "dry_run": dry_run,
            "changes": dict(counts),
            "samples": samples,
            "message": (
                f"Preview: {summary}. No data changed."
                if dry_run
                else f"Applied {summary}."
            ),
        }
