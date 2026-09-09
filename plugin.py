"""Dispatcharr plugin entry point for configurable VOD organization."""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Any

from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Q

from .core import (
    ConfigurationError,
    category_language,
    comparable_title,
    formatted_tmdb_title,
    match_title_candidates,
    normalize_match_title,
    parse_cleanup_tokens,
    parse_language_aliases,
    provider_title_language,
    safe_filename,
    category_override,
    category_target,
    clean_title,
    compile_category_rules,
    compile_title_rules,
    parse_account_names,
    selected_category_mappings,
    selected_hidden_categories,
    selected_tmdb_cleanup_categories,
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
    version = "0.8.10"
    description = "Rename, combine, and export curated VOD categories in one plugin."
    author = "ayala"
    help_url = "https://github.com/ayala/tidyVOD"

    BASE_FIELDS = [
        {
            "id": "category_editor_help",
            "label": "Category editor",
            "type": "info",
            "value": "Enter a clean name beside any provider category, or use Hide this category to stop importing it and remove its existing VOD. Hidden categories remain here so they can be restored.",
        },
        {
            "id": "account_names",
            "label": "Limit to provider accounts",
            "type": "string",
            "default": "",
            "help_text": "Optional comma-separated Dispatcharr M3U account names. Blank means all active accounts.",
        },
        {
            "id": "tmdb_cleanup_help",
            "label": "Automatic TMDB titles and optional artwork",
            "type": "info",
            "value": "Official TMDB titles are applied automatically. Each category's TMDB Artwork switch controls poster replacement only. Language comes from the category/title prefix; unprefixed titles use English. Work runs in small batches during the one-minute watcher cycle, so large libraries take multiple passes; the watcher status shows how many remain. Uncertain matches are left unchanged.",
        },
        {
            "id": "tmdb_api_key",
            "label": "TMDB API key",
            "type": "string",
            "input_type": "password",
            "default": "",
            "help_text": "Optional when TMDB_API_KEY is already configured for Dispatcharr.",
        },
        {
            "id": "keep_language_prefix",
            "label": "Keep language prefix in cleaned titles",
            "type": "boolean",
            "default": True,
            "help_text": "Recommended. Produces titles such as ES| Die Hard (1989) so localized copies remain recognizable.",
        },
        {
            "id": "player_safe_tmdb_titles",
            "label": "Player-safe TMDB titles",
            "type": "boolean",
            "default": True,
            "help_text": "Recommended. Uses a language prefix such as ES - and substitutes visually equivalent brackets that IPTV apps do not hide; for example [REC]² becomes ES - ［REC］² (2009).",
        },
        {
            "id": "language_prefix_mappings",
            "label": "Language prefix mappings",
            "type": "string",
            "default": "EN=en, ES=es, FR=fr, IT=it, DE=de, PT=pt, PL=pl, TR=tr, AR=ar, JA=ja, KO=ko",
            "help_text": "Editable PREFIX=TMDB-language pairs. Add provider-specific prefixes here.",
        },
        {
            "id": "removable_title_tags",
            "label": "Remove these provider title tags",
            "type": "text",
            "default": "4K, UHD, HDR, HDR10, HDR10+, Dolby Vision, DV, 2160p, 1080p, 720p, FHD, HEVC, H.265, H265, x265, AV1, BluRay, Blu-ray, WEB-DL, WEBRip, BDRip, REMUX, Dolby Atmos, Atmos, DDP5.1, AAC",
            "help_text": "Comma-separated and editable. These tokens are used only to form safe searches; the final title comes from a confirmed TMDB record.",
        },
        {
            "id": "leading_release_labels",
            "label": "Remove leading release labels",
            "type": "text",
            "default": "SD/CAM, CAM, HDCAM, HD-CAM, HDTS, HD-TS, TS, TELESYNC, TC, SCR, SCREENER",
            "help_text": "Comma-separated and editable. These are removed only when they begin a provider title and are followed by a separator, such as SD/CAM – Moana (2026).",
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
            "label": "Export after synchronized changes",
            "type": "boolean",
            "default": False,
            "help_text": "When synchronization moves new VOD items, refresh the stable M3U/XMLTV export too.",
        },
        {
            "id": "sync_curated_categories",
            "label": "Keep curated categories synchronized",
            "type": "boolean",
            "default": True,
            "help_text": "Automatically starts with Dispatcharr. Every minute, repair mapped category assignments for new and previously curated movies and series.",
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
            "confirm": {"title": "Apply VOD changes?", "message": "A backup is saved first. Hidden categories will be disabled and their existing provider assignments removed."},
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
            "id": "reconcile_now",
            "label": "Repair curated categories now",
            "description": "Check new and previously curated VOD and repair category assignments using your saved mappings.",
            "button_label": "Synchronize now",
            "button_color": "green",
        },
        {
            "id": "tmdb_cleanup_now",
            "label": "Run TMDB normalization now",
            "description": "Normalize titles in all active VOD categories and enrich posters only where TMDB Artwork is enabled.",
            "button_label": "Normalize VOD now",
            "button_color": "green",
        },
        {
            "id": "tmdb_cleanup_report",
            "label": "TMDB clean-up report",
            "description": "Show cleaned titles and detect poster assignments that need repair.",
            "button_label": "Show cleaned titles",
            "button_color": "blue",
        },
        {
            "id": "sync_status",
            "label": "Synchronization status",
            "description": "Check watcher health, including disabled, stale, or failed synchronization, and the last result.",
            "button_label": "Show status",
            "button_color": "blue",
        },
        {
            "id": "on_m3u_refresh",
            "label": "Watch for VOD after M3U refresh",
            "description": "Internal event notice; continuous synchronization waits for the separate VOD import.",
            "events": ["m3u_refresh"],
        },
    ]

    RECONCILE_INTERVAL_SECONDS = 60
    RECONCILE_COOLDOWN_SECONDS = 45
    RECONCILE_STALE_SECONDS = 180

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_start_lock = threading.Lock()
        # Dispatcharr constructs enabled plugins at startup; no action click is
        # required. The thread delays ORM work until discovery has completed.
        self._ensure_reconciler_started()

    def _ensure_reconciler_started(self) -> None:
        with self._reconcile_start_lock:
            if self._stop_event.is_set():
                return
            if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
                return
            # Discovery can replace an instance without calling stop(). Retire
            # previous instances in this process; the file lock covers workers.
            for thread in threading.enumerate():
                if getattr(thread, "_tidyvod_path", None) == __file__:
                    thread._tidyvod_stop_event.set()
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop,
                name="tidyvod-reconciler",
                daemon=True,
            )
            self._reconcile_thread._tidyvod_path = __file__
            self._reconcile_thread._tidyvod_stop_event = self._stop_event
            self._reconcile_thread.start()

    def stop(self, context: dict | None = None) -> None:
        self._stop_event.set()
        if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
            self._reconcile_thread.join(timeout=5)

    @property
    def fields(self) -> list[dict[str, Any]]:
        """Add one direct clean-name field per detected provider category."""
        base = {field["id"]: field for field in self.BASE_FIELDS}
        try:
            config = self._plugin_config(enabled_only=False)
            current_settings = dict(config.settings or {}) if config else {}
            dynamic = self._category_editor_fields(current_settings)
            watcher_status = self._tmdb_watcher_field(
                current_settings, bool(config and config.enabled)
            )
        except Exception:
            current_settings = {}
            watcher_status = {
                "id": "tmdb_watcher_status",
                "label": "TMDB watcher — status unavailable",
                "type": "info",
                "value": "Reload the plugin or use Show status for diagnostics.",
            }
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
            + [watcher_status]
            + [
                base["tmdb_cleanup_help"], base["tmdb_api_key"],
                base["keep_language_prefix"], base["player_safe_tmdb_titles"],
                base["language_prefix_mappings"], base["removable_title_tags"],
                base["leading_release_labels"],
            ]
            + dynamic
            + [base["portable_mapping_json"], base["sync_curated_categories"]]
            + [base[field_id] for field_id in export_ids]
            + [base[field_id] for field_id in advanced_ids]
        )

    def _tmdb_watcher_field(
        self, settings: dict[str, Any], plugin_enabled: bool
    ) -> dict[str, Any]:
        status = self._read_reconcile_status()
        normalization_categories = self._automatic_tmdb_categories()
        selected = sum(len(values) for values in normalization_categories.values())
        artwork_selected = sum(
            len(values) for values in self._selected_tmdb_artwork_categories(settings).values()
        )
        thread_alive = bool(
            self._reconcile_thread is not None and self._reconcile_thread.is_alive()
        )
        enabled = (
            plugin_enabled
            and bool(settings.get("sync_curated_categories", True))
            and selected > 0
            and thread_alive
        )
        state = "OFF"
        if enabled:
            if not status:
                state = "STARTING"
            elif status.get("status") == "running":
                state = "RUNNING"
            elif status.get("status") == "error":
                state = "ERROR"
            else:
                timestamp = status.get("completed_at") or status.get("started_at")
                try:
                    age = time.time() - datetime.fromisoformat(str(timestamp)).timestamp()
                except (TypeError, ValueError):
                    age = float("inf")
                state = "STALE" if age > self.RECONCILE_STALE_SECONDS else "WATCHING"

        def local_time(value: Any) -> str:
            try:
                parsed = datetime.fromisoformat(str(value))
                return parsed.astimezone().strftime("%b %-d, %-I:%M %p")
            except (TypeError, ValueError):
                return "never"

        last_run = status.get("last_tmdb_run_at") or status.get("completed_at")
        last_cleaned = status.get("last_cleaned_at")
        tmdb = status.get("last_tmdb_result") or status.get("tmdb_cleanup") or {}
        changes = tmdb.get("changes") or {}
        value = (
            f"{selected} active categories normalize automatically • last pass {local_time(last_run)} • "
            f"last changed artwork/titles {local_time(last_cleaned)}. "
            f"{artwork_selected} artwork categories enabled. Last result: "
            f"{changes.get('titles_normalized', 0)} titles normalized, "
            f"{changes.get('artwork_enriched', 0)} posters enriched, "
            f"{changes.get('already_clean', 0)} already clean, "
            f"{changes.get('ambiguous', 0) + changes.get('unmatched', 0)} uncertain, "
            f"{changes.get('no_clean_poster', 0)} without a suitable poster, "
            f"{changes.get('request_error', 0)} request errors, "
            f"{changes.get('remaining', 0)} queued. "
            "Reopen or reload this panel to refresh the status."
        )
        return {
            "id": "tmdb_watcher_status",
            "label": f"TMDB watcher — {state}",
            "type": "info",
            "value": value,
        }

    @staticmethod
    def _category_editor_fields(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from apps.vod.models import VODCategory

        settings = settings or {}
        hidden = selected_hidden_categories(settings)
        fields: list[dict[str, Any]] = []
        for content_type, label in (
            ("movie", "Movie categories"),
            ("series", "Series categories"),
        ):
            categories = (
                VODCategory.objects.filter(category_type=content_type).filter(
                    Q(m3u_relations__m3u_account__is_active=True, m3u_relations__enabled=True)
                    | Q(pk__in=hidden[content_type])
                )
                .distinct()
                .order_by("name")
            )
            rows = list(categories)
            if not rows:
                continue
            if content_type == "series" and fields:
                # An empty info row collapses to zero height. A zero-width
                # non-joiner survives API whitespace trimming and forces one
                # invisible text line, creating a clearly visible blank band.
                for index in (1, 2):
                    fields.append({
                        "id": f"movie_series_section_gap_{index}",
                        "label": "",
                        "type": "info",
                        "value": "\u200c",
                    })
            fields.append({
                "id": f"{content_type}_category_heading",
                "label": label,
                "type": "info",
                "value": f"{len(rows)} detected. Matching clean names are combined automatically.",
            })
            item_counts = Plugin._source_item_counts(content_type, rows)
            for category in rows:
                account_names = list(
                    category.m3u_relations.filter(m3u_account__is_active=True)
                    .values_list("m3u_account__name", flat=True)
                    .distinct()[:4]
                )
                provider_text = ", ".join(account_names)
                if len(account_names) == 4:
                    provider_text += ", …"
                count = item_counts.get(category.pk, {"total": 0, "original": 0, "moved": 0})
                noun = "movies" if content_type == "movie" else "series"
                description = (
                    f"{count['total']} {noun} • {count['original']} in original category "
                    f"→ {count['moved']} moved by tidyVOD"
                )
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
                fields.append({
                    "id": f"category_hidden_{content_type}_{category.pk}",
                    "label": "Hide this category",
                    "type": "boolean",
                    "default": False,
                    "help_text": "Stops future imports and removes this provider category's existing VOD. Turn it off and refresh VOD to restore available titles.",
                })
                prefix, language = category_language(
                    category.name,
                    parse_language_aliases(settings.get("language_prefix_mappings", "")),
                )
                language_help = (
                    f"Detected {prefix}| and will request {language} poster artwork."
                    if language
                    else "No supported language prefix detected; English poster artwork will be requested."
                )
                fields.append({
                    "id": f"category_tmdb_cleanup_{content_type}_{category.pk}",
                    "label": "TMDB Artwork",
                    "type": "boolean",
                    "default": content_type == "movie",
                    "help_text": language_help + " Replaces poster artwork only; official TMDB title normalization runs automatically whether this is on or off.",
                })
        if not fields:
            fields.append({
                "id": "no_categories_detected",
                "label": "No VOD categories detected",
                "type": "info",
                "value": "Enable VOD scanning on an Xtream provider, refresh it, then reload plugins.",
            })
        return fields

    @staticmethod
    def _selected_tmdb_artwork_categories(
        settings: dict[str, Any],
    ) -> dict[str, set[int]]:
        selected = selected_tmdb_cleanup_categories(settings)
        from apps.vod.models import VODCategory

        active_movie_ids = VODCategory.objects.filter(
            category_type="movie",
            m3u_relations__m3u_account__is_active=True,
            m3u_relations__enabled=True,
        ).values_list("pk", flat=True).distinct()
        selected["movie"].update(
            category_id for category_id in active_movie_ids
            if bool(settings.get(f"category_tmdb_cleanup_movie_{category_id}", True))
        )
        return selected

    @staticmethod
    def _automatic_tmdb_categories() -> dict[str, set[int]]:
        from apps.vod.models import VODCategory

        rows = VODCategory.objects.filter(
            m3u_relations__m3u_account__is_active=True,
            m3u_relations__enabled=True,
        ).values_list("category_type", "pk").distinct()
        selected = {"movie": set(), "series": set()}
        for content_type, category_id in rows:
            if content_type in selected:
                selected[content_type].add(category_id)
        return selected

    @staticmethod
    def _source_item_counts(content_type: str, categories: list[Any]) -> dict[int, dict[str, int]]:
        """Split each source's relations into original and tidyVOD-moved counts."""
        from django.db.models import Count
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation

        if not categories:
            return {}
        relation_model = M3UMovieRelation if content_type == "movie" else M3USeriesRelation
        category_ids = {category.pk for category in categories}
        category_ids_by_name = {category.name: category.pk for category in categories}
        marker_id = f"custom_properties__{MARKER}__original_category_id"
        marker_name = f"custom_properties__{MARKER}__original_category_name"
        rows = (
            relation_model.objects.filter(m3u_account__is_active=True)
            .filter(
                Q(category_id__in=category_ids)
                | Q(**{f"{marker_id}__in": category_ids})
                | Q(**{f"{marker_name}__in": list(category_ids_by_name)})
            )
            .values("category_id", marker_id, marker_name)
            .annotate(total=Count("pk"))
        )
        counts: dict[int, Counter] = defaultdict(Counter)
        for row in rows:
            original_id = row.get(marker_id)
            try:
                original_id = int(original_id)
            except (TypeError, ValueError):
                original_id = None
            original_name = row.get(marker_name)
            source_id = (
                original_id if original_id in category_ids
                else category_ids_by_name.get(original_name)
                or (row.get("category_id") if row.get("category_id") in category_ids else None)
            )
            if source_id is not None:
                total = row["total"]
                counts[source_id]["total"] += total
                location = "original" if row.get("category_id") == source_id else "moved"
                counts[source_id][location] += total
        return {
            source_id: {
                "total": values["total"],
                "original": values["original"],
                "moved": values["moved"],
            }
            for source_id, values in counts.items()
        }

    def run(self, action: str, params: dict, context: dict) -> dict[str, Any]:
        settings = context.get("settings", {})
        logger = context.get("logger")
        self._ensure_reconciler_started()
        try:
            if action == "on_m3u_refresh":
                return self._on_m3u_refresh(settings, logger)
            if action == "reconcile_now":
                return self._reconcile_categories(settings, logger, source="manual", force=True)
            if action == "sync_status":
                return self._reconcile_status_result()
            if action == "tmdb_cleanup_now":
                with self._reconcile_lock() as acquired:
                    if not acquired:
                        return {"status": "error", "message": "Synchronization is running. Please retry shortly."}
                    result = self._run_tmdb_cleanup(settings, logger, limit=250)
                    self._record_manual_tmdb_status(result)
                    return result
            if action == "tmdb_cleanup_report":
                return self._tmdb_cleanup_report(settings)
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
                with self._reconcile_lock() as acquired:
                    if not acquired:
                        return {"status": "error", "message": "Synchronization is running. Please retry Apply shortly."}
                    return self._apply(settings, logger, dry_run=False)
            if action == "restore":
                with self._reconcile_lock() as acquired:
                    if not acquired:
                        return {"status": "error", "message": "Synchronization is running. Please retry Restore shortly."}
                    return self._restore(settings, logger)
            return {"status": "error", "message": f"Unknown action: {action}"}
        except ConfigurationError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            if logger is not None:
                logger.exception("tidyVOD failed")
            return {"status": "error", "message": str(exc)}

    def _on_m3u_refresh(self, settings: dict[str, Any], logger: Any) -> dict[str, Any]:
        enabled = bool(settings.get("sync_curated_categories", True))
        return {
            "status": "ok",
            "message": (
                "tidyVOD will synchronize new VOD after the provider import finishes."
                if enabled
                else "Continuous curated-category synchronization is disabled."
            ),
        }

    def _record_manual_tmdb_status(self, result: dict[str, Any]) -> None:
        now = datetime.now(datetime_timezone.utc).isoformat()
        status = self._read_reconcile_status()
        status["last_tmdb_run_at"] = now
        status["last_tmdb_result"] = result
        changes = result.get("changes") or {}
        if any(changes.get(key, 0) for key in (
            "titles_normalized", "artwork_enriched", "artwork_restored"
        )):
            status["last_cleaned_at"] = now
        self._atomic_json_write(self._reconcile_status_path(), status)

    def _reconcile_loop(self) -> None:
        """Continuously catch VOD relations created after Dispatcharr's M3U event."""
        logger = logging.getLogger("dispatcharr.plugins.tidyvod")
        # Plugin discovery happens before its database row is always available.
        if self._stop_event.wait(15):
            return
        while not self._stop_event.is_set():
            try:
                close_old_connections()
                config = self._plugin_config(enabled_only=True)
                if config is not None:
                    installed = tuple(int(part) for part in re.findall(r"\d+", config.version or ""))
                    running = tuple(int(part) for part in self.version.split("."))
                    if installed > running:
                        return  # Retired code must not keep writing after an upgrade.
                    settings = dict(config.settings or {})
                    if bool(settings.get("sync_curated_categories", True)):
                        self._reconcile_categories(
                            settings, logger, source="automatic", force=False
                        )
            except Exception:
                logger.exception("tidyVOD background synchronization failed")
            finally:
                close_old_connections()
            if self._stop_event.wait(self.RECONCILE_INTERVAL_SECONDS):
                return

    @staticmethod
    def _plugin_config(*, enabled_only: bool, for_update: bool = False) -> Any:
        from apps.plugins.models import PluginConfig

        # Upgrades from VOD Arranger retain the legacy key; new managed installs
        # normally use tidyvod. Prefer whichever matching row is enabled.
        queryset = PluginConfig.objects.select_for_update() if for_update else PluginConfig.objects
        if enabled_only:
            queryset = queryset.filter(enabled=True)
        for lookup in (
            {"key": MARKER},
            {"key": "tidyvod"},
            {"slug": "tidyvod"},
            {"name": "tidyVOD"},
        ):
            config = queryset.filter(**lookup).first()
            if config is not None:
                return config
        return None

    @classmethod
    def _reconcile_status_path(cls) -> str:
        return os.path.join(cls._mapping_backup_dir(), "synchronization-status.json")

    @classmethod
    def _read_reconcile_status(cls) -> dict[str, Any]:
        try:
            with open(cls._reconcile_status_path(), "r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @classmethod
    @contextmanager
    def _reconcile_lock(cls):
        backup_dir = cls._mapping_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        lock_path = os.path.join(backup_dir, "synchronization.lock")
        with open(lock_path, "a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reconcile_categories(
        self,
        settings: dict[str, Any],
        logger: Any,
        *,
        source: str,
        force: bool,
    ) -> dict[str, Any]:
        mappings = selected_category_mappings(settings)
        hidden = selected_hidden_categories(settings)
        tmdb_selected = self._automatic_tmdb_categories()
        mapping_count = sum(len(group) for group in mappings.values())
        hidden_count = sum(len(group) for group in hidden.values())
        tmdb_count = sum(len(group) for group in tmdb_selected.values())

        logger = logger or logging.getLogger("dispatcharr.plugins.tidyvod")
        with self._reconcile_lock() as acquired:
            if not acquired:
                return {
                    "status": "ok",
                    "message": "A tidyVOD synchronization is already running.",
                }

            previous = self._read_reconcile_status()
            elapsed = time.time() - float(previous.get("completed_epoch", 0) or 0)
            if not force and elapsed < self.RECONCILE_COOLDOWN_SECONDS:
                return {
                    "status": "ok",
                    "message": "Curated categories were synchronized recently.",
                }

            started_at = datetime.now(datetime_timezone.utc).isoformat()
            running_status = {
                "status": "running",
                "source": source,
                "started_at": started_at,
                "mapped_categories": mapping_count,
                "hidden_categories": hidden_count,
                "tmdb_categories": tmdb_count,
            }
            for key in ("last_tmdb_run_at", "last_tmdb_result", "last_cleaned_at"):
                if previous.get(key):
                    running_status[key] = previous[key]
            self._atomic_json_write(self._reconcile_status_path(), running_status)
            try:
                result = self._perform_category_reconciliation(settings, mappings, logger)
                completed_at = datetime.now(datetime_timezone.utc).isoformat()
                status = {
                    "status": "ok",
                    "source": source,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "completed_epoch": time.time(),
                    "mapped_categories": mapping_count,
                    "hidden_categories": hidden_count,
                    "tmdb_categories": tmdb_count,
                    "changes": result.get("changes", {}),
                    "tmdb_cleanup": result.get("tmdb_cleanup", {}),
                }
                tmdb_result = result.get("tmdb_cleanup") or {}
                status["last_tmdb_run_at"] = completed_at
                status["last_tmdb_result"] = tmdb_result
                tmdb_changes = tmdb_result.get("changes") or {}
                if any(tmdb_changes.get(key, 0) for key in (
                    "titles_normalized", "artwork_enriched", "artwork_restored"
                )):
                    status["last_cleaned_at"] = completed_at
                elif previous.get("last_cleaned_at"):
                    status["last_cleaned_at"] = previous["last_cleaned_at"]
                self._atomic_json_write(self._reconcile_status_path(), status)
                result["last_run"] = completed_at
                return result
            except Exception as exc:
                self._atomic_json_write(self._reconcile_status_path(), {
                    "status": "error",
                    "source": source,
                    "started_at": started_at,
                    "completed_at": datetime.now(datetime_timezone.utc).isoformat(),
                    "completed_epoch": time.time(),
                    "mapped_categories": mapping_count,
                    "hidden_categories": hidden_count,
                    "tmdb_categories": tmdb_count,
                    "error": str(exc),
                })
                raise

    def _perform_category_reconciliation(
        self,
        settings: dict[str, Any],
        mappings: dict[str, dict[int, str]],
        logger: Any,
    ) -> dict[str, Any]:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation, VODCategory

        # Exact-name recovery is limited to currently selected mappings. Never
        # resurrect a removed/blank mapping from an old backup.
        entries = self._mapping_entries(settings)
        name_targets = self._category_name_targets(entries, mappings)
        mappings = self._resolved_category_mappings(settings, mappings, entries=entries)
        try:
            self._backup_mappings(settings, reason="automatic_synchronization")
        except Exception as exc:
            logger.warning("Could not back up tidyVOD mappings before synchronization: %s", exc)

        account_filter = self._account_filter(settings)
        relation_specs = (
            ("movie", M3UMovieRelation),
            ("series", M3USeriesRelation),
        )
        counts = Counter(categories=0, movie=0, series=0)
        samples: list[dict[str, str]] = []

        hidden_result = self._apply_hidden_categories(settings, logger, dry_run=False, entries=entries)
        counts.update(hidden_result["changes"])
        samples.extend(hidden_result.get("samples", []))

        with transaction.atomic():
            category_cache: dict[tuple[str, str], Any] = {}
            for content_type, relation_model in relation_specs:
                content_mappings = mappings[content_type]
                if not content_mappings:
                    continue
                # Only fetch assignments that differ from their target. Include
                # restore markers so moved/null categories and renamed targets
                # recover too, without loading every already-correct VOD row.
                by_target: dict[str, list[int]] = defaultdict(list)
                for source_id, target in content_mappings.items():
                    by_target[target].append(source_id)
                candidates = Q(pk__in=[])
                marker_key = f"custom_properties__{MARKER}__original_category_id"
                for target, source_ids in by_target.items():
                    source_names = [name for name, value in name_targets[content_type].items() if value == target]
                    candidates |= (
                        Q(category_id__in=source_ids)
                        | Q(**{f"{marker_key}__in": source_ids})
                        | Q(**{f"custom_properties__{MARKER}__original_category_name__in": source_names})
                    ) & ~Q(category__name=target)
                relations = relation_model.objects.filter(
                    candidates, **account_filter,
                ).select_related("category")
                changed = []
                for relation in relations.iterator(chunk_size=1000):
                    props = dict(relation.custom_properties or {})
                    marker = dict(props.get(MARKER, {}))
                    source_category_id = marker.get("original_category_id") or relation.category_id
                    target_name = (
                        content_mappings.get(source_category_id)
                        or name_targets[content_type].get(marker.get("original_category_name"))
                        or content_mappings.get(relation.category_id)
                    )
                    if not target_name or (
                        relation.category is not None and relation.category.name == target_name
                    ):
                        continue
                    cache_key = (content_type, target_name)
                    target_category = category_cache.get(cache_key)
                    if target_category is None:
                        target_category, _ = VODCategory.objects.get_or_create(
                            name=target_name,
                            category_type=content_type,
                        )
                        category_cache[cache_key] = target_category
                    source_name = marker.get("original_category_name") or (
                        relation.category.name if relation.category else "Uncategorized"
                    )
                    marker.setdefault("original_category_id", source_category_id)
                    marker.setdefault("original_category_name", source_name)
                    props[MARKER] = marker
                    relation.category = target_category
                    relation.custom_properties = props
                    changed.append(relation)
                    counts["categories"] += 1
                    counts[content_type] += 1
                    self._sample(samples, "category", f"{source_name} -> {target_name}")
                    if len(changed) >= 1000:
                        relation_model.objects.bulk_update(
                            changed, ["category", "custom_properties"], batch_size=1000,
                        )
                        changed = []
                if changed:
                    relation_model.objects.bulk_update(
                        changed,
                        ["category", "custom_properties"],
                        batch_size=1000,
                    )

        tmdb_result = self._run_tmdb_cleanup(settings, logger, limit=25)
        export_result = None
        if (counts["categories"] or counts["hidden_assignments"]) and bool(settings.get("auto_export", False)):
            export_result = self._run_export(settings, logger, dry_run=False)
        logger.info("tidyVOD synchronized newly imported VOD: %s", dict(counts))
        if counts["categories"] or counts["hidden_categories"] or counts["hidden_assignments"]:
            category_message = (
                f"Repaired {counts['categories']} category assignments "
                f"({counts['movie']} movies, {counts['series']} series); "
                f"hid {counts['hidden_categories']} categories and removed "
                f"{counts['hidden_assignments']} hidden assignments."
            )
        else:
            category_message = "No category assignments needed repair."
        if any(self._automatic_tmdb_categories().values()):
            category_message += f" {tmdb_result.get('message', 'TMDB normalization did not return a result.')}"
        result = {
            "status": "ok",
            "changes": dict(counts),
            "samples": samples,
            "message": category_message,
        }
        if export_result is not None:
            result["export"] = export_result
        result["tmdb_cleanup"] = tmdb_result
        return result

    def _apply_hidden_categories(self, settings, logger, *, dry_run, entries=None):
        """Disable selected provider categories and remove their VOD assignments."""
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation, M3UVODCategoryRelation

        hidden = selected_hidden_categories(settings)
        entries = self._mapping_entries(settings) if entries is None else entries
        clean_placeholders = {kind: {category_id: "__hidden__" for category_id in ids}
                              for kind, ids in hidden.items()}
        resolved = self._resolved_category_mappings(
            settings, clean_placeholders, entries=entries,
        )
        name_targets = self._category_name_targets(entries, clean_placeholders)
        account_filter = self._account_filter(settings)
        counts = Counter(hidden_categories=0, restored_categories=0,
                         hidden_assignments=0, hidden_movies=0, hidden_series=0)
        samples = []

        relation_filter = dict(account_filter)
        category_relations = M3UVODCategoryRelation.objects.filter(**relation_filter).select_related("category")
        category_updates = []
        for relation in category_relations.iterator(chunk_size=1000):
            props = dict(relation.custom_properties or {})
            marker = dict(props.get(MARKER, {}))
            desired = relation.category_id in resolved.get(relation.category.category_type, set())
            managed = bool(marker.get("hidden_category"))
            if desired and not managed:
                marker["hidden_category"] = True
                marker["was_enabled"] = bool(relation.enabled)
                props[MARKER] = marker
                relation.enabled = False
                relation.custom_properties = props
                category_updates.append(relation)
                counts["hidden_categories"] += 1
                self._sample(samples, "hide category", relation.category.name)
            elif desired and relation.enabled:
                relation.enabled = False
                category_updates.append(relation)
            elif not desired and managed:
                relation.enabled = bool(marker.pop("was_enabled", True))
                marker.pop("hidden_category", None)
                if marker:
                    props[MARKER] = marker
                else:
                    props.pop(MARKER, None)
                relation.custom_properties = props
                category_updates.append(relation)
                counts["restored_categories"] += 1
                self._sample(samples, "restore category", relation.category.name)

        relation_specs = (("movie", M3UMovieRelation), ("series", M3USeriesRelation))
        assignments = []
        for kind, model in relation_specs:
            ids = set(resolved[kind])
            names = set(name_targets[kind])
            if not ids and not names:
                continue
            query = Q(category_id__in=ids)
            if names:
                query |= Q(**{f"custom_properties__{MARKER}__original_category_name__in": names})
            matched = model.objects.filter(query, **account_filter)
            count = matched.count()
            counts["hidden_assignments"] += count
            counts[f"hidden_{kind}s"] += count
            assignments.append(matched)

        if not dry_run:
            with transaction.atomic():
                if category_updates:
                    M3UVODCategoryRelation.objects.bulk_update(
                        category_updates, ["enabled", "custom_properties"], batch_size=1000,
                    )
                for matched in assignments:
                    matched.delete()

        return {"changes": dict(counts), "samples": samples}

    @staticmethod
    def _category_name_targets(entries, mappings):
        grouped = {kind: defaultdict(set) for kind in mappings}
        for entry in entries:
            kind = entry["content_type"]
            target = mappings[kind].get(entry["category_id"])
            if target and entry.get("provider_name"):
                grouped[kind][entry["provider_name"]].add(target)
        return {kind: {name: next(iter(targets)) for name, targets in names.items() if len(targets) == 1}
                for kind, names in grouped.items()}

    def _resolved_category_mappings(self, settings, mappings, *, entries=None):
        """Keep old marker IDs and add exact-name aliases for recreated sources."""
        from apps.vod.models import VODCategory

        resolved = {kind: dict(values) for kind, values in mappings.items()}
        entries = self._mapping_entries(settings) if entries is None else entries
        for kind in resolved:
            names = {entry["provider_name"] for entry in entries
                     if entry["content_type"] == kind and entry.get("provider_name")}
            if not names:
                continue
            categories = VODCategory.objects.filter(category_type=kind, name__in=names)
            by_name = defaultdict(list)
            for category in categories:
                by_name[category.name].append(category.pk)
            targets_by_name = defaultdict(set)
            for entry in entries:
                if entry["content_type"] == kind:
                    target = mappings[kind].get(entry["category_id"])
                    if target:
                        targets_by_name[entry.get("provider_name")].add(target)
            for entry in entries:
                if entry["content_type"] != kind:
                    continue
                target = mappings[kind].get(entry["category_id"])
                matches = by_name.get(entry.get("provider_name"), [])
                if target and len(matches) == 1 and len(targets_by_name[entry.get("provider_name")]) == 1:
                    # An explicit mapping for the current ID always wins.
                    resolved[kind].setdefault(matches[0], target)
        return resolved

    def _reconcile_status_result(self) -> dict[str, Any]:
        status = self._read_reconcile_status()
        config = self._plugin_config(enabled_only=False)
        if config is None or not config.enabled or not bool((config.settings or {}).get("sync_curated_categories", True)):
            return {"status": "ok", "health": "disabled", "synchronization": status,
                    "message": "Automatic synchronization is disabled. Enable tidyVOD and Keep curated categories synchronized to protect your categories."}
        if not (any(selected_category_mappings(config.settings or {}).values())
                or any(selected_hidden_categories(config.settings or {}).values())
                or any(self._automatic_tmdb_categories().values())):
            return {"status": "ok", "health": "idle", "synchronization": status,
                    "message": "No category mappings are saved; nothing to synchronize."}
        if not status:
            return {
                "status": "ok",
                "health": "waiting",
                "message": "WARNING: Automatic synchronization has not reported a check yet. Wait one minute after enabling; if unchanged, reload the plugin or restart Dispatcharr.",
            }
        timestamp = status.get("completed_at") or status.get("started_at")
        try:
            checked_at = datetime.fromisoformat(timestamp).timestamp()
            age = time.time() - checked_at
        except (ValueError, TypeError):
            age = float("inf")
        if age > self.RECONCILE_STALE_SECONDS:
            return {"status": "ok", "health": "stale", "synchronization": status,
                    "message": f"WARNING: No completed/recent synchronization check for over 3 minutes (last activity: {timestamp}). A check may be stalled or the watcher stopped. Reload tidyVOD or restart Dispatcharr. Last error: {status.get('error') or 'none recorded'}."}
        if status.get("status") == "running":
            message = f"Synchronization is running (started {status.get('started_at', 'recently')})."
        elif status.get("status") == "error":
            message = (
                f"Last synchronization failed at {status.get('completed_at', 'an unknown time')}: "
                f"{status.get('error', 'unknown error')}"
            )
        else:
            changes = status.get("changes") or {}
            message = (
                f"Last synchronized {status.get('completed_at', 'recently')}: "
                f"{changes.get('categories', 0)} assignments moved "
                f"({changes.get('movie', 0)} movies, {changes.get('series', 0)} series)."
            )
            tmdb = status.get("tmdb_cleanup") or {}
            if tmdb.get("message"):
                message += f" {tmdb['message']}"
        return {"status": "ok", "health": status.get("status", "unknown"), "synchronization": status, "message": message}

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

    @classmethod
    def _mapping_entries(cls, settings: dict[str, Any]) -> list[dict[str, Any]]:
        from apps.vod.models import VODCategory

        selected: dict[tuple[str, int], dict[str, Any]] = {}
        override_pattern = re.compile(r"^category_override_(movie|series)_(\d+)$")
        hidden_pattern = re.compile(r"^category_hidden_(movie|series)_(\d+)$")
        tmdb_pattern = re.compile(r"^category_tmdb_cleanup_(movie|series)_(\d+)$")
        for key, value in settings.items():
            match = override_pattern.fullmatch(str(key))
            if match:
                content_type, category_id = match.group(1), int(match.group(2))
                selected.setdefault((content_type, category_id), {})["clean_name"] = str(value or "").strip()
                continue
            match = hidden_pattern.fullmatch(str(key))
            if match:
                content_type, category_id = match.group(1), int(match.group(2))
                selected.setdefault((content_type, category_id), {})["hidden"] = bool(value)
                continue
            match = tmdb_pattern.fullmatch(str(key))
            if match:
                content_type, category_id = match.group(1), int(match.group(2))
                selected.setdefault((content_type, category_id), {})["tmdb_cleanup"] = bool(value)
                continue
        selected = {key: values for key, values in selected.items()
                    if values.get("clean_name") or values.get("hidden") or values.get("tmdb_cleanup")}
        category_ids = [category_id for _, category_id in selected]
        categories = {
            category.pk: category
            for category in VODCategory.objects.filter(pk__in=category_ids).only("pk", "name", "category_type")
        }
        # Preserve the source identity if a refresh deleted the original row.
        # Otherwise the next automatic backup would erase our recovery key.
        try:
            with open(os.path.join(cls._mapping_backup_dir(), "latest.json"), encoding="utf-8") as handle:
                previous = json.load(handle).get("mappings", [])
            previous_names = {(entry["content_type"], entry["category_id"]): entry.get("provider_name")
                              for entry in previous}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            previous_names = {}
        entries = []
        for (content_type, category_id), values in selected.items():
            category = categories.get(category_id)
            entries.append({
                "content_type": content_type,
                "category_id": category_id,
                "provider_name": category.name if category else previous_names.get((content_type, category_id)),
                "clean_name": values.get("clean_name", ""),
                "hidden": bool(values.get("hidden", False)),
                "tmdb_cleanup": bool(values.get("tmdb_cleanup", False)),
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
        payload = self._backup_mappings(settings, reason)
        count = len(payload.get("mappings", []))
        with transaction.atomic():
            config = self._plugin_config(enabled_only=False, for_update=True)
            if config is None:
                raise ConfigurationError("tidyVOD plugin settings could not be found.")
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
                f"Backed up {count} category choices (clean names and hidden categories). Reload this page to copy the portable JSON, "
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
        restored_categories = set()
        missing = 0
        for entry in mappings:
            if not isinstance(entry, dict):
                continue
            content_type = entry.get("content_type")
            category = by_id.get((content_type, entry.get("category_id")))
            if category is None and entry.get("provider_name"):
                category = by_name.get((content_type, str(entry["provider_name"]).casefold()))
            clean_name = str(entry.get("clean_name") or "").strip()
            hidden = bool(entry.get("hidden", False))
            tmdb_cleanup = bool(entry.get("tmdb_cleanup", False))
            if category is None or (not clean_name and not hidden and not tmdb_cleanup):
                missing += 1
                continue
            if clean_name:
                restored[f"category_override_{content_type}_{category.pk}"] = clean_name
            if hidden:
                restored[f"category_hidden_{content_type}_{category.pk}"] = True
            if tmdb_cleanup:
                restored[f"category_tmdb_cleanup_{content_type}_{category.pk}"] = True
            restored_categories.add((content_type, category.pk))

        with transaction.atomic():
            config = self._plugin_config(enabled_only=False, for_update=True)
            if config is None:
                raise ConfigurationError("tidyVOD plugin settings could not be found.")
            updated_settings = {
                key: value
                for key, value in (config.settings or {}).items()
                if not str(key).startswith(("category_override_", "category_hidden_", "category_tmdb_cleanup_"))
            }
            updated_settings.update(restored)
            config.settings = updated_settings
            config.save(update_fields=["settings", "updated_at"])

        logger.info("tidyVOD restored %s saved category choices", len(restored_categories))
        return {
            "status": "ok",
            "restored_mappings": len(restored_categories),
            "missing_categories": missing,
            "message": (
                f"Restored {len(restored_categories)} category choices from the {source} backup; "
                f"{missing} unavailable categories were skipped. "
                "Reload the Plugins page now to show the restored fields."
            ),
        }

    @staticmethod
    def _relation_provider_title(relation: Any, item: Any) -> str:
        props = relation.custom_properties or {}
        for container_name in ("basic_data", "detailed_info"):
            container = props.get(container_name, {}) if isinstance(props, dict) else {}
            if isinstance(container, dict):
                for key in ("name", "title", "original_name"):
                    value = str(container.get(key) or "").strip()
                    if value:
                        return value
        item_props = item.custom_properties or {}
        marker = item_props.get(MARKER, {}) if isinstance(item_props, dict) else {}
        return str(marker.get("original_name") or item.name or "").strip()

    @staticmethod
    def _best_tmdb_poster(posters: list[dict[str, Any]], language: str) -> dict[str, Any] | None:
        """Prefer localized, well-voted posters; never select untagged/textless art."""
        language = language.split("-", 1)[0].lower()
        usable = [
            poster for poster in posters
            if poster.get("file_path") and str(poster.get("iso_639_1") or "").lower() in {language, "en"}
        ]
        if not usable:
            return None
        return max(
            usable,
            key=lambda poster: (
                str(poster.get("iso_639_1") or "").lower() == language,
                int(poster.get("vote_count") or 0),
                float(poster.get("vote_average") or 0),
                int(poster.get("width") or 0) * int(poster.get("height") or 0),
            ),
        )

    @staticmethod
    def _tmdb_language(
        source_name: str, provider_title: str, aliases: dict[str, str]
    ) -> tuple[str, str, str]:
        prefix, language = category_language(source_name, aliases)
        if language:
            return prefix, language, "category"
        prefix, language = provider_title_language(provider_title, aliases)
        if language:
            return prefix, language, "title"
        return "EN", "en", "default"

    @staticmethod
    def _relation_tmdb_artwork_url(relation: Any) -> str | None:
        props = relation.custom_properties or {}
        if not isinstance(props, dict):
            return None
        # Dispatcharr checks ``info`` before provider-refreshed
        # ``detailed_info``. tidyVOD owns the former on movie/series relations
        # so an on-demand advanced refresh cannot replace the selected poster.
        for container_name in ("info", "detailed_info"):
            container = props.get(container_name, {})
            if isinstance(container, dict):
                value = str(container.get("movie_image") or "").strip()
                if value:
                    return value
        return None

    def _run_tmdb_cleanup(
        self, settings: dict[str, Any], logger: Any, *, limit: int
    ) -> dict[str, Any]:
        """Normalize every active title; enrich artwork only for opted-in categories."""
        from urllib.error import HTTPError, URLError
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        logger = logger or logging.getLogger("dispatcharr.plugins.tidyvod")
        artwork_selected = self._selected_tmdb_artwork_categories(settings)
        selected = self._automatic_tmdb_categories()
        total_selected = sum(len(values) for values in selected.values())
        total_artwork_selected = sum(len(values) for values in artwork_selected.values())
        if not total_selected:
            return {"status": "ok", "changes": {}, "message": "No active VOD categories are available for TMDB normalization."}
        api_key = str(settings.get("tmdb_api_key", "") or os.environ.get("TMDB_API_KEY", "")).strip()
        if not api_key:
            return {
                "status": "error",
                "changes": {},
                "message": "Automatic TMDB normalization needs a TMDB API key, but none is configured.",
            }
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation, VODCategory, VODLogo

        aliases = parse_language_aliases(settings.get("language_prefix_mappings", ""))
        removable = parse_cleanup_tokens(settings.get("removable_title_tags", ""))
        leading_release_labels = parse_cleanup_tokens(settings.get(
            "leading_release_labels",
            "SD/CAM, CAM, HDCAM, HD-CAM, HDTS, HD-TS, TS, TELESYNC, TC, SCR, SCREENER",
        ))
        keep_prefix = bool(settings.get("keep_language_prefix", True))
        player_safe_titles = bool(settings.get("player_safe_tmdb_titles", True))
        source_categories = VODCategory.objects.filter(
            pk__in=set().union(*selected.values())
        ).only("pk", "name", "category_type")
        names = {"movie": set(), "series": set()}
        for category in source_categories:
            if category.category_type in names:
                names[category.category_type].add(category.name)
        account_filter = self._account_filter(settings)
        candidates: dict[tuple[str, int], dict[str, Any]] = {}
        conflicts: set[tuple[str, int]] = set()
        counts = Counter(
            normalization_categories=total_selected,
            artwork_categories=total_artwork_selected,
        )

        for kind, relation_model, item_field in (
            ("movie", M3UMovieRelation, "movie"),
            ("series", M3USeriesRelation, "series"),
        ):
            ids = selected[kind]
            source_names = names[kind]
            query = Q(category_id__in=ids) | Q(
                **{f"custom_properties__{MARKER}__original_category_id__in": ids}
            )
            if source_names:
                query |= Q(**{
                    f"custom_properties__{MARKER}__original_category_name__in": source_names
                })
            relations = relation_model.objects.filter(query, **account_filter).select_related(
                "category", item_field, f"{item_field}__logo"
            )
            for relation in relations.iterator(chunk_size=500):
                props = relation.custom_properties or {}
                marker = props.get(MARKER, {}) if isinstance(props, dict) else {}
                source_id = marker.get("original_category_id") or relation.category_id
                source_name = marker.get("original_category_name") or (
                    relation.category.name if relation.category else ""
                )
                if source_id not in ids and source_name not in source_names:
                    continue
                item = getattr(relation, item_field)
                provider_title = self._relation_provider_title(relation, item)
                prefix, language, language_source = self._tmdb_language(
                    source_name, provider_title, aliases
                )
                if language_source == "title":
                    counts["title_language_fallback"] += 1
                elif language_source == "default":
                    counts["english_language_default"] += 1
                key = (kind, item.pk)
                previous = candidates.get(key)
                if previous and previous["language"].split("-", 1)[0] != language.split("-", 1)[0]:
                    conflicts.add(key)
                    candidates.pop(key, None)
                    continue
                if previous and key not in conflicts:
                    previous["relations"].append(relation)
                    previous["artwork_enabled"] = (
                        previous["artwork_enabled"]
                        or source_id in artwork_selected[kind]
                    )
                elif key not in conflicts:
                    candidates[key] = {
                        "kind": kind,
                        "media_type": "movie" if kind == "movie" else "tv",
                        "item": item,
                        "prefix": prefix,
                        "language": language,
                        "artwork_enabled": source_id in artwork_selected[kind],
                        "provider_title": provider_title,
                        "relations": [relation],
                    }

        counts["language_conflicts"] = len(conflicts)
        pending = []
        for candidate in candidates.values():
            item = candidate["item"]
            props = item.custom_properties or {}
            marker = props.get(MARKER, {}) if isinstance(props, dict) else {}
            current_logo = getattr(getattr(item, "logo", None), "url", None)
            relation_artwork_managed = any(
                bool(((relation.custom_properties or {}).get(MARKER) or {}).get(
                    "tmdb_relation_artwork_managed"
                ))
                for relation in candidate["relations"]
            )
            relation_artwork_ready = (
                candidate["artwork_enabled"]
                and bool(marker.get("tmdb_cleanup_poster_url"))
                and all(
                    self._relation_tmdb_artwork_url(relation)
                    == marker.get("tmdb_cleanup_poster_url")
                    for relation in candidate["relations"]
                )
            )
            artwork_state_ready = (
                relation_artwork_ready
                and marker.get("tmdb_cleanup_poster_url") == current_logo
                if candidate["artwork_enabled"]
                else not relation_artwork_managed
                and not bool(marker.get("tmdb_artwork_enabled", False))
            )
            if (
                marker.get("tmdb_cleanup_managed")
                and marker.get("tmdb_cleanup_language") == candidate["language"]
                and marker.get("tmdb_cleanup_keep_prefix") == keep_prefix
                and marker.get("tmdb_cleanup_player_safe") == player_safe_titles
                and marker.get("tmdb_cleanup_name") == item.name
                and artwork_state_ready
            ):
                counts["already_clean"] += 1
                continue
            if (
                marker.get("tmdb_cleanup_managed")
                and marker.get("tmdb_cleanup_name")
                and (
                    not candidate["artwork_enabled"]
                    or marker.get("tmdb_cleanup_poster_url")
                )
                and marker.get("tmdb_cleanup_language") == candidate["language"]
                and marker.get("tmdb_cleanup_keep_prefix") == keep_prefix
            ):
                cached_name = marker["tmdb_cleanup_name"]
                if player_safe_titles:
                    cached_name = re.sub(
                        r"^\s*[A-Za-z]{2,3}\s*(?:\||:|[-–—])\s*",
                        "",
                        cached_name,
                        count=1,
                    )
                    cached_name = formatted_tmdb_title(
                        re.sub(r"\s*\((?:18|19|20|21)\d{2}\)\s*$", "", cached_name),
                        item.year,
                        candidate["prefix"],
                        keep_prefix,
                        True,
                    )
                candidate["cached_result"] = {
                    "status": "ok",
                    "tmdb_id": marker.get("tmdb_cleanup_tmdb_id", ""),
                    "name": cached_name,
                    "poster_url": (
                        marker["tmdb_cleanup_poster_url"]
                        if candidate["artwork_enabled"] else None
                    ),
                    "poster_language": marker.get("tmdb_cleanup_poster_language"),
                    "cached": True,
                }
            if len(pending) < limit:
                pending.append(candidate)
            else:
                counts["remaining"] += 1

        def get_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
            url = f"https://api.themoviedb.org/3/{path}?{urlencode(params)}"
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "tidyVOD/0.8"})
            try:
                with urlopen(request, timeout=12) as response:
                    value = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 401:
                    raise PermissionError("TMDB rejected the API key") from exc
                raise OSError(f"TMDB returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError) as exc:
                raise OSError(f"TMDB request failed: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError("TMDB returned an invalid response")
            return value

        def fetch(candidate: dict[str, Any]) -> dict[str, Any]:
            if candidate.get("cached_result"):
                return candidate["cached_result"]
            item = candidate["item"]
            media_type = candidate["media_type"]
            language = candidate["language"]
            language_code = language.split("-", 1)[0]
            tmdb_id = str(item.tmdb_id or "").strip()
            try:
                if not tmdb_id and item.imdb_id:
                    payload = get_json(
                        f"find/{item.imdb_id}",
                        {"api_key": api_key, "external_source": "imdb_id", "language": language},
                    )
                    key = "movie_results" if media_type == "movie" else "tv_results"
                    matches = payload.get(key) or []
                    if len(matches) == 1:
                        tmdb_id = str(matches[0].get("id") or "")
                if not tmdb_id:
                    query_title, parsed_year = normalize_match_title(
                        candidate["provider_title"], removable, leading_release_labels
                    )
                    year = item.year or parsed_year
                    if not query_title or not year:
                        return {"status": "unmatched"}
                    exact = []
                    any_matches = []
                    for search_title in match_title_candidates(query_title):
                        params = {"api_key": api_key, "query": search_title, "language": language}
                        params["primary_release_year" if media_type == "movie" else "first_air_date_year"] = year
                        matches = get_json(f"search/{media_type}", params).get("results") or []
                        any_matches.extend(matches)
                        searched = comparable_title(search_title)
                        for match in matches:
                            date = str(match.get("release_date") if media_type == "movie" else match.get("first_air_date") or "")
                            if not date.startswith(str(year)):
                                continue
                            titles = (
                                (match.get("title"), match.get("original_title"))
                                if media_type == "movie"
                                else (match.get("name"), match.get("original_name"))
                            )
                            if searched in {comparable_title(str(value or "")) for value in titles}:
                                exact.append(match)
                        if exact:
                            break
                    if len(exact) != 1:
                        return {"status": "ambiguous" if exact or any_matches else "unmatched"}
                    tmdb_id = str(exact[0].get("id") or "")
                detail = get_json(
                    f"{media_type}/{tmdb_id}",
                    {
                        "api_key": api_key,
                        "language": language,
                        "append_to_response": "images",
                        "include_image_language": f"{language_code},en",
                    },
                )
                title = detail.get("title") if media_type == "movie" else detail.get("name")
                date = detail.get("release_date") if media_type == "movie" else detail.get("first_air_date")
                try:
                    year = int(str(date)[:4])
                except (TypeError, ValueError):
                    year = item.year
                poster = self._best_tmdb_poster((detail.get("images") or {}).get("posters") or [], language)
                if not title:
                    return {"status": "unmatched"}
                if candidate["artwork_enabled"] and not poster:
                    return {
                        "status": "ok",
                        "tmdb_id": tmdb_id,
                        "name": formatted_tmdb_title(
                            str(title), year, candidate["prefix"], keep_prefix, player_safe_titles
                        ),
                        "poster_url": None,
                        "poster_language": None,
                        "no_clean_poster": True,
                    }
                return {
                    "status": "ok",
                    "tmdb_id": tmdb_id,
                    "name": formatted_tmdb_title(
                        str(title), year, candidate["prefix"], keep_prefix, player_safe_titles
                    ),
                    "poster_url": (
                        f"https://image.tmdb.org/t/p/w780{poster['file_path']}"
                        if poster and candidate["artwork_enabled"] else None
                    ),
                    "poster_language": poster.get("iso_639_1") if poster else None,
                }
            except PermissionError:
                return {"status": "unauthorized"}
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("TMDB cleanup failed for %s: %s", item.pk, exc)
                return {"status": "request_error"}

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch, pending))
        if any(result.get("status") == "unauthorized" for result in results):
            return {"status": "error", "changes": dict(counts), "message": "TMDB rejected the configured API key."}

        relation_updates: dict[str, list[Any]] = {"movie": [], "series": []}
        with transaction.atomic():
            for candidate, result in zip(pending, results):
                status = result.get("status", "request_error")
                if status != "ok":
                    counts[status] += 1
                    continue
                item = candidate["item"]
                poster_url = result.get("poster_url")
                artwork_enabled = bool(candidate["artwork_enabled"] and poster_url)
                logo = None
                if artwork_enabled:
                    logo, _ = VODLogo.objects.get_or_create(
                        url=poster_url,
                        defaults={"name": f"tidyVOD TMDB - {result['name']}"[:255]},
                    )
                item.refresh_from_db(fields=["name", "logo", "custom_properties"])
                props = dict(item.custom_properties or {})
                existing = props.get(MARKER, {})
                marker = dict(existing) if isinstance(existing, dict) else {}
                marker.setdefault("original_name", item.name)
                marker.setdefault("original_logo_id", item.logo_id)
                previously_managed_artwork = bool(
                    marker.get("tmdb_artwork_enabled")
                    or marker.get("tmdb_cleanup_poster_url")
                )
                marker.update({
                    "tmdb_cleanup_managed": True,
                    "tmdb_cleanup_tmdb_id": result["tmdb_id"],
                    "tmdb_cleanup_language": candidate["language"],
                    "tmdb_cleanup_keep_prefix": keep_prefix,
                    "tmdb_cleanup_player_safe": player_safe_titles,
                    "tmdb_cleanup_name": result["name"],
                    "tmdb_artwork_enabled": artwork_enabled,
                })
                if artwork_enabled:
                    marker["tmdb_cleanup_poster_language"] = result["poster_language"]
                    marker["tmdb_cleanup_poster_url"] = poster_url
                else:
                    marker.pop("tmdb_cleanup_poster_language", None)
                    marker.pop("tmdb_cleanup_poster_url", None)
                props[MARKER] = marker
                previous_name = item.name
                item.name = result["name"]
                fields = ["name", "custom_properties", "updated_at"]
                if artwork_enabled:
                    item.logo = logo
                    fields.append("logo")
                    counts["artwork_enriched"] += 1
                elif previously_managed_artwork:
                    item.logo_id = marker.get("original_logo_id")
                    fields.append("logo")
                    counts["artwork_restored"] += 1
                item.custom_properties = props
                try:
                    with transaction.atomic():
                        item.save(update_fields=fields)
                except IntegrityError:
                    counts["save_conflicts"] += 1
                    continue
                counts["cleaned"] += 1
                if previous_name != result["name"]:
                    counts["titles_normalized"] += 1
                if result.get("cached") and artwork_enabled:
                    counts["artwork_repairs"] += 1
                if result.get("no_clean_poster"):
                    counts["no_clean_poster"] += 1
                for relation in candidate["relations"]:
                    relation_props = dict(relation.custom_properties or {})
                    relation_marker_value = relation_props.get(MARKER, {})
                    relation_marker = (
                        dict(relation_marker_value)
                        if isinstance(relation_marker_value, dict) else {}
                    )
                    detail_value = relation_props.get("detailed_info", {})
                    detail = dict(detail_value) if isinstance(detail_value, dict) else {}
                    info_value = relation_props.get("info", {})
                    info = dict(info_value) if isinstance(info_value, dict) else {}
                    if artwork_enabled:
                        if not relation_marker.get("tmdb_relation_artwork_managed"):
                            relation_marker["tmdb_original_relation_movie_image_present"] = "movie_image" in detail
                            relation_marker["tmdb_original_relation_movie_image"] = detail.get("movie_image")
                            relation_marker["tmdb_original_relation_info_movie_image_present"] = "movie_image" in info
                            relation_marker["tmdb_original_relation_info_movie_image"] = info.get("movie_image")
                        relation_marker["tmdb_relation_artwork_managed"] = True
                        relation_marker["tmdb_relation_artwork_url"] = poster_url
                        detail["movie_image"] = poster_url
                        info["movie_image"] = poster_url
                    elif relation_marker.get("tmdb_relation_artwork_managed"):
                        if relation_marker.get("tmdb_original_relation_movie_image_present"):
                            detail["movie_image"] = relation_marker.get("tmdb_original_relation_movie_image")
                        else:
                            detail.pop("movie_image", None)
                        if relation_marker.get("tmdb_original_relation_info_movie_image_present"):
                            info["movie_image"] = relation_marker.get("tmdb_original_relation_info_movie_image")
                        else:
                            info.pop("movie_image", None)
                        for key in (
                            "tmdb_relation_artwork_managed",
                            "tmdb_relation_artwork_url",
                            "tmdb_original_relation_movie_image_present",
                            "tmdb_original_relation_movie_image",
                            "tmdb_original_relation_info_movie_image_present",
                            "tmdb_original_relation_info_movie_image",
                        ):
                            relation_marker.pop(key, None)
                    relation_props["detailed_info"] = detail
                    relation_props["info"] = info
                    relation_props[MARKER] = relation_marker
                    relation.custom_properties = relation_props
                    relation_updates[candidate["kind"]].append(relation)
            if relation_updates["movie"]:
                M3UMovieRelation.objects.bulk_update(
                    relation_updates["movie"], ["custom_properties"], batch_size=500
                )
            if relation_updates["series"]:
                M3USeriesRelation.objects.bulk_update(
                    relation_updates["series"], ["custom_properties"], batch_size=500
                )

        return {
            "status": "ok",
            "changes": dict(counts),
            "message": (
                f"TMDB: {counts['normalization_categories']} categories normalized automatically; "
                f"{counts['artwork_categories']} artwork categories enabled, "
                f"{counts['titles_normalized']} titles changed, "
                f"{counts['artwork_enriched']} posters enriched, "
                f"{counts['artwork_restored']} posters restored after opt-out, "
                f"{counts['already_clean']} already current, "
                f"{counts['artwork_repairs']} existing poster assignments repaired, "
                f"{counts['ambiguous'] + counts['unmatched']} uncertain, "
                f"{counts['english_language_default']} used the English default, "
                f"{counts['language_conflicts']} shared-language conflicts, "
                f"{counts['no_clean_poster']} without a localized/English poster, "
                f"{counts['request_error']} request errors, and {counts['remaining']} remaining."
            ),
        }

    def _tmdb_cleanup_report(self, settings: dict[str, Any]) -> dict[str, Any]:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation, Movie, Series

        account_filter = self._account_filter(settings)
        specs = (
            ("movie", Movie, M3UMovieRelation, "movie_id"),
            ("series", Series, M3USeriesRelation, "series_id"),
        )
        counts = Counter()
        titles: list[str] = []
        report_rows: list[dict[str, Any]] = []
        for kind, model, relation_model, id_field in specs:
            ids = relation_model.objects.filter(**account_filter).values_list(id_field, flat=True)
            items = model.objects.filter(
                pk__in=ids,
                **{f"custom_properties__{MARKER}__tmdb_cleanup_managed": True},
            ).select_related("logo")
            managed_items = list(items.iterator(chunk_size=500))
            expected_by_id = {
                item.pk: (item.custom_properties or {}).get(MARKER, {}).get(
                    "tmdb_cleanup_poster_url"
                )
                for item in managed_items
            }
            relation_summary: dict[int, Counter] = defaultdict(Counter)
            relations = relation_model.objects.filter(
                **account_filter, **{f"{id_field}__in": list(expected_by_id)}
            ).only("custom_properties", id_field)
            for relation in relations.iterator(chunk_size=500):
                item_id = getattr(relation, id_field)
                relation_summary[item_id]["total"] += 1
                counts["managed_relations"] += 1
                if self._relation_tmdb_artwork_url(relation) != expected_by_id.get(item_id):
                    relation_summary[item_id]["repairs"] += 1
                    counts["relation_posters_need_repair"] += 1

            for item in managed_items:
                marker = (item.custom_properties or {}).get(MARKER, {})
                counts[kind] += 1
                expected = marker.get("tmdb_cleanup_poster_url")
                current = getattr(getattr(item, "logo", None), "url", None)
                if not expected or current != expected:
                    counts["item_posters_need_repair"] += 1
                if len(titles) < 20:
                    titles.append(item.name)
                relation_count = relation_summary[item.pk]["total"]
                relation_repairs = relation_summary[item.pk]["repairs"]
                report_rows.append({
                    "content_type": kind,
                    "item_id": item.pk,
                    "title": item.name,
                    "tmdb_id": marker.get("tmdb_cleanup_tmdb_id", ""),
                    "language": marker.get("tmdb_cleanup_language", ""),
                    "poster_url": expected or "",
                    "item_poster": "ok" if expected and current == expected else "needs repair",
                    "provider_relations": relation_count,
                    "relation_posters_needing_repair": relation_repairs,
                })
        total = counts["movie"] + counts["series"]
        examples = ", ".join(titles) if titles else "none yet"
        report_path = os.path.join(self._mapping_backup_dir(), "tmdb-cleanup-report.csv")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=".tmdb-report-", suffix=".csv", dir=os.path.dirname(report_path)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                columns = [
                    "content_type", "item_id", "title", "tmdb_id", "language",
                    "poster_url", "item_poster", "provider_relations",
                    "relation_posters_needing_repair",
                ]
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(report_rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, report_path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
        return {
            "status": "ok",
            "file": report_path,
            "changes": dict(counts),
            "samples": [{"type": "TMDB-normalized title", "change": title} for title in titles],
            "message": (
                f"{total} TMDB-managed titles ({counts['movie']} movies, {counts['series']} series); "
                f"{counts['item_posters_need_repair']} item-level and "
                f"{counts['relation_posters_need_repair']} of {counts['managed_relations']} relation-level "
                "poster assignments need repair. "
                f"Full {total}-title report saved to {report_path}. "
                f"Popup sample ({len(titles)}): {examples}."
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
        hidden_result = self._apply_hidden_categories(settings, logger, dry_run=dry_run)
        counts.update(hidden_result["changes"])
        samples.extend(hidden_result.get("samples", []))
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
                    if not isinstance(marker, dict):
                        continue
                    touched = False
                    if "original_category_id" in marker:
                        original = VODCategory.objects.filter(pk=marker.get("original_category_id")).first()
                        if original is None and marker.get("original_category_name"):
                            original, _ = VODCategory.objects.get_or_create(
                                name=marker["original_category_name"],
                                category_type="movie" if relation_model is M3UMovieRelation else "series",
                            )
                        relation.category = original
                        counts["categories"] += 1
                        touched = True
                    if marker.get("tmdb_relation_artwork_managed"):
                        detail_value = props.get("detailed_info", {})
                        detail = dict(detail_value) if isinstance(detail_value, dict) else {}
                        info_value = props.get("info", {})
                        info = dict(info_value) if isinstance(info_value, dict) else {}
                        if marker.get("tmdb_original_relation_movie_image_present"):
                            detail["movie_image"] = marker.get("tmdb_original_relation_movie_image")
                        else:
                            detail.pop("movie_image", None)
                        if marker.get("tmdb_original_relation_info_movie_image_present"):
                            info["movie_image"] = marker.get("tmdb_original_relation_info_movie_image")
                        else:
                            info.pop("movie_image", None)
                        props["detailed_info"] = detail
                        if info or "info" in props:
                            props["info"] = info
                        counts["relation_artwork"] += 1
                        touched = True
                    if not touched:
                        continue
                    props.pop(MARKER, None)
                    relation.custom_properties = props
                    changed.append(relation)
                if changed:
                    relation_model.objects.bulk_update(changed, ["category", "custom_properties"], batch_size=1000)

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
                    if (marker.get("poster_managed") or marker.get("clean_cover_managed")
                            or marker.get("tmdb_cleanup_managed")):
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
        if counts["hidden_categories"] or counts["restored_categories"] or counts["hidden_assignments"]:
            summary += (
                f"; {counts['hidden_categories']} categories hidden, "
                f"{counts['restored_categories']} restored, and "
                f"{counts['hidden_assignments']} hidden VOD assignments removed"
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
