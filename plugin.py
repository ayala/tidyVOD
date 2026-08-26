"""Dispatcharr plugin entry point for configurable VOD organization."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path
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
    safe_filename,
    category_override,
    category_override,
    category_target,
    clean_title,
    compile_category_rules,
    compile_title_rules,
    parse_account_names,
)


MARKER = "vodarranger"


@dataclass(frozen=True)
class ExportEntry:
    content_type: str
    source_category_id: int | None
    source_category: str
    export_category: str
    item_id: int
    item_name: str
    stream_url: str
    logo_url: str | None
    tvg_id: str | None


class Plugin:
    name = "tidyVOD"
    version = "0.6.6"
    description = "Rename, combine, and export curated VOD categories in one plugin."
    author = "ayala"
    help_url = "https://github.com/ayala/tidyVOD"

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
            "id": "m3u_export_help",
            "label": "M3U export",
            "type": "info",
            "value": "Export your curated categories into tidy m3u/xmltv files for tools like m3u4u.",
        },
        {
            "id": "output_directory",
            "label": "Output directory",
            "type": "string",
            "default": "/data/plugins/.tidym3u_exports",
            "help_text": "Directory where playlist files are written. One stable name plus timestamped backups are created.",
        },
        {
            "id": "filename_prefix",
            "label": "Filename prefix",
            "type": "string",
            "default": "tidym3u-playlist",
            "help_text": "Prefix for playlist/XMLTV output files.",
        },
        {
            "id": "emit_xmltv",
            "label": "Also write XMLTV stub",
            "type": "boolean",
            "default": True,
        },
        {
            "id": "auto_export",
            "label": "Export after M3U refresh",
            "type": "boolean",
            "default": False,
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
            "description": "Restore category and title values saved by this plugin.",
            "button_label": "Restore",
            "button_color": "red",
            "confirm": {"title": "Restore VOD values?", "message": "This only restores values tracked by tidyVOD and earlier compatible builds."},
        },
        {
            "id": "preview_export",
            "label": "Preview export",
            "description": "Compute counts for the current settings without writing files.",
            "button_label": "Preview export",
            "button_color": "blue",
        },
        {
            "id": "export_playlist",
            "label": "Export playlist",
            "description": "Write M3U (+ optional XMLTV) files to disk.",
            "button_label": "Export",
            "button_color": "green",
            "confirm": {
                "title": "Export curated playlist?",
                "message": "This writes/overwrites the stable output files and creates a timestamped snapshot.",
            },
        },
        {
            "id": "on_m3u_refresh",
            "label": "Auto-apply and export after M3U refresh",
            "description": "Internal event action controlled by the auto-apply and export settings.",
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
        ]
        export_ids = [
            "m3u_export_help", "output_directory", "filename_prefix", "emit_xmltv", "auto_export"
        ]
        return (
            [base["category_editor_help"], base["account_names"]]
            + dynamic
            + [base["portable_mapping_json"], base["auto_apply"]]
            + [base[field_id] for field_id in export_ids]
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
                return self._on_m3u_refresh(settings, logger)
            if action == "preview_export":
                return self._run_export(settings, logger, dry_run=True)
            if action == "export_playlist":
                return self._run_export(settings, logger, dry_run=False)
            if action == "preview":
                return self._apply(settings, logger, dry_run=True)
            if action == "backup_mappings":
                return self._backup_mappings_result(settings, reason="manual")
            if action == "restore_mappings":
                return self._restore_mappings(settings, logger)
            if action == "import_mappings":
                return self._import_mappings(settings, logger)
            if action == "apply":
                return self._apply(settings, logger, dry_run=False)
            if action == "restore":
                return self._restore(settings, logger)
            return {"status": "error", "message": f"Unknown action: {action}"}
        except ConfigurationError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            if logger is not None:
                logger.exception("tidyVOD failed")
            return {"status": "error", "message": str(exc)}

    def _on_m3u_refresh(self, settings: dict[str, Any], logger: Any) -> dict[str, Any]:
        auto_apply = bool(settings.get("auto_apply", False))
        auto_export = bool(settings.get("auto_export", False))

        if not auto_apply and not auto_export:
            return {"status": "ok", "message": "Automatic apply/export is disabled"}

        results = []
        messages = []
        overall_status = "ok"
        for action_name in ("apply", "export"):
            if action_name == "apply" and auto_apply:
                result = self._apply(settings, logger, dry_run=False)
            elif action_name == "export" and auto_export:
                result = self._run_export(settings, logger, dry_run=False)
            else:
                continue
            results.append(result)
            result_status = result.get("status")
            if result_status == "error":
                overall_status = "error"
            messages.append(result.get("message", f"{action_name} completed"))
        if auto_apply and auto_export:
            message = "Auto-refresh completed: " + " | ".join(messages)
        else:
            message = messages[-1] if messages else ""
        return {
            "status": overall_status,
            "message": message,
            "results": results,
        }

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
    def _coerce_url(value: Any) -> str | None:
        if not value:
            return None
        if isinstance(value, (list, tuple)):
            if not value:
                return None
            value = value[0]
        if callable(value):
            try:
                value = value()
            except Exception:
                return None
        if not value:
            return None
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if not value:
            return None
        if value.startswith("/") and not value.lower().startswith("http"):
            return None
        return value

    @staticmethod
    def _tvg_id(item: Any) -> str | None:
        for key in ("tvg_id", "epg_id", "imdb_id", "tmdb_id", "id"):
            value = getattr(item, key, None)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _item_logo_url(item: Any) -> str | None:
        for key in ("logo",):
            value = getattr(item, key, None)
            if not value:
                continue
            logo_url = getattr(value, "url", None)
            if logo_url:
                return str(logo_url)
        for key in ("logo_url", "poster_url", "image_url", "artwork_url"):
            value = getattr(item, key, None)
            if value:
                text = str(value).strip()
                if text:
                    return text
        return None

    @staticmethod
    def _stream_url_from_relation(relation: Any, item: Any, content_type: str) -> str | None:
        for obj in (relation, item):
            if obj is None:
                continue
            for attr in ("stream_url", "url", "play_url", "player_url", "m3u_url", "direct_url"):
                value = Plugin._coerce_url(getattr(obj, attr, None))
                if value:
                    return value

        # Xtream providers commonly expose a stream ID plus account credentials.
        stream_id = getattr(relation, "stream_id", None) or getattr(item, "stream_id", None)
        if stream_id:
            account = getattr(relation, "m3u_account", None)
            if account is not None:
                base = (
                    getattr(account, "base_url", None)
                    or getattr(account, "url", None)
                    or getattr(account, "api_url", None)
                )
                username = getattr(account, "username", None) or getattr(account, "user", None)
                password = getattr(account, "password", None)
                if base and username and password:
                    base = str(base).rstrip("/")
                    segment = "movie" if content_type == "movie" else "series"
                    return f"{base}/{segment}/{int(stream_id)}?username={username}&password={password}"
        return None

    def _collect_entries(self, settings: dict[str, Any]) -> tuple[list[ExportEntry], Counter]:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation

        account_filter = self._account_filter(settings)
        relation_specs = [
            ("movie", M3UMovieRelation, "movie"),
            ("series", M3USeriesRelation, "series"),
        ]
        entries: list[ExportEntry] = []
        counts: Counter[str] = Counter()

        for content_type, relation_model, item_field in relation_specs:
            queryset = relation_model.objects.filter(**account_filter).select_related("category", item_field, "m3u_account")
            for relation in queryset.iterator(chunk_size=1000):
                item = getattr(relation, item_field)
                if item is None:
                    counts["missing_item"] += 1
                    continue
                source_category = relation.category.name if relation.category else "Uncategorized"
                source_category_id = relation.category_id
                target_category = category_override(settings, content_type, source_category_id) or source_category
                if not target_category:
                    target_category = source_category
                url = Plugin._stream_url_from_relation(relation, item, content_type)
                if not url:
                    counts["missing_url"] += 1
                    continue
                item_name = str(getattr(item, "name", "") or "").strip() or str(
                    getattr(item, "original_name", "") or ""
                ).strip()
                if not item_name:
                    item_name = f"{content_type.title()} {item.pk}"
                entry = ExportEntry(
                    content_type=content_type,
                    source_category_id=source_category_id,
                    source_category=source_category,
                    export_category=target_category,
                    item_id=item.pk,
                    item_name=item_name,
                    stream_url=url,
                    logo_url=Plugin._item_logo_url(item),
                    tvg_id=Plugin._tvg_id(item),
                )
                entries.append(entry)
                counts["items"] += 1
                counts[f"category:{target_category}"] += 1

        counts["categories"] = len({entry.export_category for entry in entries})
        return entries, counts

    @staticmethod
    def _m3u_line_escape(value: str) -> str:
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace('"', "\\\"")
            .replace("\n", " ")
            .strip()
        )

    def _write_m3u(self, entries: list[ExportEntry], output: Path) -> int:
        lines = ["#EXTM3U"]
        for entry in sorted(entries, key=lambda item: (item.export_category.casefold(), item.item_name.casefold())):
            tvg_id = entry.tvg_id or f"{entry.content_type}:{entry.item_id}"
            attrs = [
                f'tvg-id="{self._m3u_line_escape(tvg_id)}"',
                f'group-title="{self._m3u_line_escape(entry.export_category)}"',
            ]
            if entry.logo_url:
                attrs.append(f'tvg-logo="{self._m3u_line_escape(entry.logo_url)}"')
            line = f"#EXTINF:-1 {' '.join(attrs)},{self._m3u_line_escape(entry.item_name)}"
            lines.append(line)
            lines.append(entry.stream_url)

        with output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        return len(entries)

    def _write_xmltv(self, entries: list[ExportEntry], output: Path) -> int:
        by_channel = {}
        for index, entry in enumerate(entries):
            key = entry.tvg_id or f"{entry.content_type}:{entry.item_id}:{index}"
            if key in by_channel:
                by_channel[f"{key}-{index}"] = entry
            else:
                by_channel[key] = entry

        with output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            handle.write('<tv generator-info-name="tidyVOD">\n')
            for key, entry in by_channel.items():
                handle.write(f'  <channel id="{self._m3u_line_escape(key)}">\n')
                handle.write(f'    <display-name>{self._m3u_line_escape(entry.item_name)}</display-name>\n')
                if entry.logo_url:
                    handle.write(f'    <icon src="{self._m3u_line_escape(entry.logo_url)}" />\n')
                handle.write("  </channel>\n")
            handle.write("</tv>\n")
        return len(by_channel)

    def _run_export(self, settings: dict[str, Any], logger: Any, dry_run: bool) -> dict[str, Any]:
        entries, counts = self._collect_entries(settings)

        if not entries:
            return {
                "status": "error",
                "changes": dict(counts),
                "message": (
                    "No stream URLs were resolved. Enable VOD entries and check whether items have valid stream links. "
                    f"Missing URLs: {counts.get('missing_url', 0)}"
                ),
            }

        if dry_run:
            grouped = defaultdict(int)
            for entry in entries:
                grouped[entry.export_category] += 1
            return {
                "status": "ok",
                "dry_run": True,
                "changes": dict(counts),
                "samples": [
                    {"type": category, "change": f"{count} items"} for category, count in sorted(grouped.items())[:25]
                ],
                "message": (
                    f"Preview: {counts['items']} items across {counts['categories']} groups, "
                    f"{counts['missing_url']} with unresolved URLs."
                ),
            }

        output_dir = Path(str(settings.get("output_directory", "") or "/data/plugins/.tidym3u_exports")).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = safe_filename(str(settings.get("filename_prefix", "") or "tidym3u-export"))
        timestamp = datetime.now(datetime_timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        m3u_name = f"{prefix}-{timestamp}.m3u"
        stable_m3u = f"{prefix}.m3u"
        m3u_path = output_dir / m3u_name
        stable_m3u_path = output_dir / stable_m3u
        m3u_count = self._write_m3u(entries, m3u_path)
        self._write_m3u(entries, stable_m3u_path)

        xmltv_path = None
        xmltv_count = 0
        if settings.get("emit_xmltv", True):
            xmltv_name = f"{prefix}-{timestamp}.xml"
            stable_xmltv = f"{prefix}.xml"
            xmltv_path = output_dir / xmltv_name
            stable_xmltv_path = output_dir / stable_xmltv
            xmltv_count = self._write_xmltv(entries, xmltv_path)
            self._write_xmltv(entries, stable_xmltv_path)

        manifest = {
            "generated_at": datetime.now(datetime_timezone.utc).isoformat(),
            "plugin": self.name,
            "version": self.version,
            "entries": m3u_count,
            "categories": counts["categories"],
            "xmltv_channels": xmltv_count,
        }
        manifest_path = output_dir / f"{prefix}-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        return {
            "status": "ok",
            "dry_run": False,
            "changes": dict(counts),
            "message": (
                f"Exported {m3u_count} entries ({counts['categories']} groups) to:\n"
                f"- {m3u_path}\n"
                f"- {stable_m3u_path}\n"
                + (f"- {stable_xmltv_path}\n" if xmltv_path else "")
                + f"Manifest: {manifest_path}"
            ),
        }

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

    def _apply(self, settings: dict[str, Any], logger: Any, dry_run: bool) -> dict[str, Any]:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation, Movie, Series, VODCategory

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
                direct_target = category_override(settings, content_type, source_category_id)
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
