import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta


class FakeQ:
    def __init__(self, **kwargs):
        pass

    def __or__(self, other):
        return self

    def __and__(self, other):
        return self

    def __invert__(self):
        return self


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "tidyvod_reconciliation_test"


def load_plugin_module():
    django = types.ModuleType("django")
    django_db = types.ModuleType("django.db")
    django_db.IntegrityError = type("IntegrityError", (Exception,), {})
    django_db.close_old_connections = lambda: None
    django_db.transaction = types.SimpleNamespace(atomic=contextlib.nullcontext)
    django_db_models = types.ModuleType("django.db.models")
    django_db_models.Count = lambda *args, **kwargs: (args, kwargs)
    django_db_models.Q = FakeQ
    django.db = django_db
    sys.modules.setdefault("django", django)
    sys.modules.setdefault("django.db", django_db)
    sys.modules.setdefault("django.db.models", django_db_models)

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    for module_name in ("core", "plugin"):
        qualified = f"{PACKAGE}.{module_name}"
        spec = importlib.util.spec_from_file_location(qualified, ROOT / f"{module_name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PACKAGE}.plugin"]


class FakeQuerySet:
    def __init__(self, relations):
        self.relations = relations

    def select_related(self, *args):
        return self

    def iterator(self, chunk_size=None):
        return iter(self.relations)

    def filter(self, *args, **kwargs):
        return self


class FakeRelationManager:
    def __init__(self, relations):
        self.relations = relations
        self.updated = []

    def filter(self, *args, **filters):
        # SQL candidate selection is exercised by orm_reconciliation.py.
        return FakeQuerySet(self.relations)

    def bulk_update(self, relations, fields, batch_size=None):
        self.updated.extend(relations)


class FakeCategoryManager:
    def __init__(self):
        self.categories = {}

    def get_or_create(self, *, name, category_type):
        key = (category_type, name)
        created = key not in self.categories
        if created:
            self.categories[key] = types.SimpleNamespace(
                id=900 + len(self.categories),
                pk=900 + len(self.categories),
                name=name,
                category_type=category_type,
            )
        return self.categories[key], created

    def filter(self, *args, **filters):
        return FakeCategoryQuerySet([])


class FakeAccountRelations:
    def filter(self, **filters):
        return self

    def values_list(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def __getitem__(self, item):
        return ["Provider"]


class FakeCategoryQuerySet(list):
    def filter(self, *args, **kwargs):
        return self

    def annotate(self, **annotations):
        return self

    def distinct(self):
        return self

    def order_by(self, *fields):
        return self

    def only(self, *fields):
        return self

    def values_list(self, *fields, **kwargs):
        if kwargs.get("flat") and fields == ("pk",):
            return FakeCategoryQuerySet([item.pk for item in self])
        if fields == ("category_type", "pk"):
            return FakeCategoryQuerySet([
                (item.category_type, item.pk) for item in self
            ])
        return self


class FakeEditorCategoryManager:
    def __init__(self, categories):
        self.categories = categories

    def filter(self, *args, **filters):
        category_type = filters.get("category_type")
        if category_type is None:
            return FakeCategoryQuerySet(self.categories)
        return FakeCategoryQuerySet([
            category for category in self.categories
            if category.category_type == category_type
        ])


class FakeLogoManager:
    def get_or_create(self, *, url, defaults):
        return types.SimpleNamespace(pk=99, url=url, name=defaults["name"]), True


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin_module()

    def test_new_relation_is_moved_and_marked_for_restore(self):
        source = types.SimpleNamespace(id=12, pk=12, name="Netflix HEVC", category_type="movie")
        relation = types.SimpleNamespace(
            category=source,
            category_id=12,
            custom_properties={"basic_data": {"stream_id": 7}},
        )
        movie_manager = FakeRelationManager([relation])
        series_manager = FakeRelationManager([])
        category_manager = FakeCategoryManager()

        vod_models = types.ModuleType("apps.vod.models")
        vod_models.M3UMovieRelation = types.SimpleNamespace(objects=movie_manager)
        vod_models.M3USeriesRelation = types.SimpleNamespace(objects=series_manager)
        vod_models.M3UVODCategoryRelation = types.SimpleNamespace(
            objects=types.SimpleNamespace(filter=lambda *args, **kwargs: FakeQuerySet([]))
        )
        vod_models.VODCategory = types.SimpleNamespace(objects=category_manager)
        sys.modules["apps"] = types.ModuleType("apps")
        sys.modules["apps.vod"] = types.ModuleType("apps.vod")
        sys.modules["apps.vod.models"] = vod_models

        plugin = self.module.Plugin.__new__(self.module.Plugin)
        plugin._backup_mappings = lambda *args, **kwargs: None
        plugin._mapping_entries = lambda settings: []
        plugin._resolved_category_mappings = lambda settings, mappings, **kwargs: mappings
        plugin._account_filter = lambda settings: {}
        result = plugin._perform_category_reconciliation(
            {"category_override_movie_12": "Netflix"},
            {"movie": {12: "Netflix"}, "series": {}},
            Mock(),
        )

        self.assertEqual(relation.category.name, "Netflix")
        self.assertEqual(
            relation.custom_properties["vodarranger"],
            {"original_category_id": 12, "original_category_name": "Netflix HEVC"},
        )
        self.assertEqual(len(movie_manager.updated), 1)
        self.assertEqual(result["changes"]["categories"], 1)

    def test_constructor_starts_watcher_without_action(self):
        with patch.object(self.module.Plugin, "_ensure_reconciler_started") as start:
            plugin = self.module.Plugin()
        start.assert_called_once_with()
        self.assertFalse(plugin._stop_event.is_set())

    def test_reload_retires_old_thread_and_start_is_idempotent(self):
        with patch.object(self.module.threading, "Thread") as thread_cls, patch.object(self.module.threading, "enumerate") as threads:
            previous = Mock()
            previous._tidyvod_path = self.module.__file__
            threads.return_value = [previous]
            plugin = self.module.Plugin()
            previous._tidyvod_stop_event.set.assert_called_once()
            thread_cls.return_value.is_alive.return_value = True
            plugin._ensure_reconciler_started()
            thread_cls.return_value.start.assert_called_once()
            plugin.stop()
            self.assertTrue(plugin._stop_event.is_set())

    def test_loop_retries_after_database_error(self):
        plugin = self.module.Plugin.__new__(self.module.Plugin)
        plugin._stop_event = Mock()
        plugin._stop_event.wait.side_effect = [False, False, True]
        plugin._stop_event.is_set.return_value = False
        config = types.SimpleNamespace(version=plugin.version, settings={"sync_curated_categories": True})
        plugin._plugin_config = Mock(side_effect=[RuntimeError("database starting"), config])
        plugin._reconcile_categories = Mock()
        with self.assertLogs("dispatcharr.plugins.tidyvod", level="ERROR"):
            plugin._reconcile_loop()
        plugin._reconcile_categories.assert_called_once()

    def test_loop_does_not_write_when_disabled_or_old_version(self):
        for version, enabled in [(self.module.Plugin.version, False), ("99.0.0", True)]:
            with self.subTest(version=version, enabled=enabled):
                plugin = self.module.Plugin.__new__(self.module.Plugin)
                plugin._stop_event = Mock()
                plugin._stop_event.wait.side_effect = [False, True]
                plugin._stop_event.is_set.return_value = False
                plugin._plugin_config = Mock(return_value=types.SimpleNamespace(
                    version=version, settings={"sync_curated_categories": enabled}))
                plugin._reconcile_categories = Mock()
                plugin._reconcile_loop()
                plugin._reconcile_categories.assert_not_called()

    def test_status_distinguishes_stale_disabled_idle_error_and_healthy(self):
        plugin = self.module.Plugin.__new__(self.module.Plugin)
        config = types.SimpleNamespace(enabled=True, settings={"category_override_movie_12": "Netflix"})
        plugin._plugin_config = Mock(return_value=config)
        now = datetime.now(timezone.utc)
        plugin._read_reconcile_status = Mock(return_value={
            "status": "ok", "completed_at": (now - timedelta(minutes=10)).isoformat()})
        self.assertEqual(plugin._reconcile_status_result()["health"], "stale")
        plugin._read_reconcile_status.return_value = {"status": "running", "started_at": (now - timedelta(minutes=10)).isoformat()}
        self.assertEqual(plugin._reconcile_status_result()["health"], "stale")
        plugin._read_reconcile_status.return_value = {"status": "error", "error": "database offline", "completed_at": now.isoformat()}
        result = plugin._reconcile_status_result()
        self.assertEqual(result["health"], "error")
        self.assertIn("database offline", result["message"])
        plugin._read_reconcile_status.return_value = {"status": "ok", "completed_at": now.isoformat()}
        self.assertEqual(plugin._reconcile_status_result()["health"], "ok")
        config.enabled = False
        self.assertEqual(plugin._reconcile_status_result()["health"], "disabled")
        config.enabled = True
        config.settings = {}
        self.assertEqual(plugin._reconcile_status_result()["health"], "idle")

    def test_tmdb_watcher_field_shows_state_time_and_counts(self):
        plugin = self.module.Plugin.__new__(self.module.Plugin)
        plugin._reconcile_thread = Mock()
        plugin._reconcile_thread.is_alive.return_value = True
        now = datetime.now(timezone.utc)
        plugin._read_reconcile_status = Mock(return_value={
            "status": "ok",
            "completed_at": now.isoformat(),
            "last_tmdb_run_at": now.isoformat(),
            "last_cleaned_at": now.isoformat(),
            "last_tmdb_result": {
                "changes": {
                    "titles_normalized": 7,
                    "artwork_enriched": 5,
                    "already_clean": 10,
                    "remaining": 3,
                }
            },
        })
        with patch.object(
            plugin, "_automatic_tmdb_categories",
            return_value={"movie": {12}, "series": set()},
        ):
            field = plugin._tmdb_watcher_field(
                {
                    "sync_curated_categories": True,
                    "category_tmdb_cleanup_movie_12": True,
                },
                True,
            )
        self.assertEqual(field["label"], "TMDB watcher — WATCHING")
        self.assertIn("7 titles normalized", field["value"])
        self.assertIn("5 posters enriched", field["value"])
        self.assertIn("3 queued", field["value"])

    def test_category_editor_has_a_gap_between_movies_and_series(self):
        categories = [
            types.SimpleNamespace(
                pk=12,
                name="Movies",
                category_type="movie",
                item_count=10,
                m3u_relations=FakeAccountRelations(),
            ),
            types.SimpleNamespace(
                pk=22,
                name="Series",
                category_type="series",
                item_count=5,
                m3u_relations=FakeAccountRelations(),
            ),
        ]
        sys.modules.setdefault("apps", types.ModuleType("apps"))
        sys.modules.setdefault("apps.vod", types.ModuleType("apps.vod"))
        vod_models = sys.modules.setdefault(
            "apps.vod.models", types.ModuleType("apps.vod.models")
        )
        vod_models.VODCategory = types.SimpleNamespace(
            objects=FakeEditorCategoryManager(categories)
        )

        with patch.object(
            self.module.Plugin,
            "_source_item_counts",
            side_effect=lambda content_type, rows: {
                row.pk: {"total": row.item_count, "original": 0, "moved": row.item_count}
                for row in rows
            },
        ):
            fields = self.module.Plugin._category_editor_fields()
        ids = [field["id"] for field in fields]

        self.assertEqual(
            ids,
            [
                "movie_category_heading",
                "category_override_movie_12",
                "category_hidden_movie_12",
                "category_tmdb_cleanup_movie_12",
                "movie_series_section_gap_1",
                "movie_series_section_gap_2",
                "series_category_heading",
                "category_override_series_22",
                "category_hidden_series_22",
                "category_tmdb_cleanup_series_22",
            ],
        )
        spacers = [field for field in fields if field["id"].startswith("movie_series_section_gap_")]
        self.assertEqual(len(spacers), 2)
        self.assertTrue(all(field["value"] == "\u200c" for field in spacers))
        hide = next(field for field in fields if field["id"] == "category_hidden_movie_12")
        self.assertEqual(hide["label"], "Hide this category")
        self.assertFalse(hide["default"])
        cleanup = next(field for field in fields if field["id"] == "category_tmdb_cleanup_movie_12")
        self.assertEqual(cleanup["label"], "TMDB Artwork")
        self.assertTrue(cleanup["default"])
        self.assertEqual(cleanup["help_text"], "Replace VOD provided artwork.")
        self.assertEqual(
            hide["help_text"],
            "Turn ON to remove category. Turn OFF and refresh VOD to restore available titles.",
        )
        movie = next(field for field in fields if field["id"] == "category_override_movie_12")
        self.assertEqual(
            movie["help_text"],
            "10 movies • 0 in original category → 10 moved by tidyVOD • Provider",
        )

    def test_tmdb_artwork_categories_default_active_movies_on(self):
        categories = [
            types.SimpleNamespace(pk=12, category_type="movie"),
            types.SimpleNamespace(pk=13, category_type="movie"),
        ]
        sys.modules["apps.vod.models"].VODCategory = types.SimpleNamespace(
            objects=FakeEditorCategoryManager(categories)
        )
        selected = self.module.Plugin._selected_tmdb_artwork_categories({
            "category_tmdb_cleanup_movie_13": False,
            "category_tmdb_cleanup_series_17": True,
        })
        self.assertEqual(selected, {"movie": {12}, "series": {17}})

    def test_automatic_tmdb_normalization_includes_movies_and_series(self):
        categories = [
            types.SimpleNamespace(pk=12, category_type="movie"),
            types.SimpleNamespace(pk=22, category_type="series"),
        ]
        sys.modules.setdefault("apps", types.ModuleType("apps"))
        sys.modules.setdefault("apps.vod", types.ModuleType("apps.vod"))
        vod_models = sys.modules.setdefault(
            "apps.vod.models", types.ModuleType("apps.vod.models")
        )
        vod_models.VODCategory = types.SimpleNamespace(
            objects=FakeEditorCategoryManager(categories)
        )
        self.assertEqual(
            self.module.Plugin._automatic_tmdb_categories(),
            {"movie": {12}, "series": {22}},
        )

    def test_tmdb_poster_prefers_requested_language_and_never_textless(self):
        posters = [
            {"file_path": "/textless.jpg", "iso_639_1": None, "vote_count": 100},
            {"file_path": "/english.jpg", "iso_639_1": "en", "vote_count": 50},
            {"file_path": "/spanish.jpg", "iso_639_1": "es", "vote_count": 2},
        ]
        selected = self.module.Plugin._best_tmdb_poster(posters, "es-ES")
        self.assertEqual(selected["file_path"], "/spanish.jpg")
        self.assertIsNone(self.module.Plugin._best_tmdb_poster([
            {"file_path": "/textless.jpg", "iso_639_1": None, "vote_count": 100},
        ], "es"))

    def test_tmdb_language_uses_category_then_title_then_english(self):
        aliases = {"EN": "en", "ES": "es", "FR": "fr"}
        self.assertEqual(
            self.module.Plugin._tmdb_language(
                "ES| Horror", "EN - Rec (2007)", aliases
            ),
            ("ES", "es", "category"),
        )
        self.assertEqual(
            self.module.Plugin._tmdb_language(
                "Horror", "FR - Rec (2007)", aliases
            ),
            ("FR", "fr", "title"),
        )
        self.assertEqual(
            self.module.Plugin._tmdb_language("Horror", "Rec (2007)", aliases),
            ("EN", "en", "default"),
        )

    def test_artwork_off_still_normalizes_title_without_touching_poster(self):
        category = types.SimpleNamespace(pk=12, name="Movies", category_type="movie")
        provider_logo = types.SimpleNamespace(pk=7, url="https://provider/poster.jpg")
        item = types.SimpleNamespace(
            pk=101,
            tmdb_id="123",
            imdb_id="",
            year=1989,
            name="Die Hard 4K (1989)",
            logo=provider_logo,
            logo_id=7,
            custom_properties={},
        )
        item.refresh_from_db = Mock()
        item.save = Mock()
        relation = types.SimpleNamespace(
            pk=201,
            category=category,
            category_id=12,
            movie=item,
            custom_properties={
                "basic_data": {"name": "Die Hard 4K (1989)"},
                "info": {"movie_image": "https://provider/poster.jpg"},
            },
        )
        movie_manager = FakeRelationManager([relation])
        series_manager = FakeRelationManager([])
        sys.modules.setdefault("apps", types.ModuleType("apps"))
        sys.modules.setdefault("apps.vod", types.ModuleType("apps.vod"))
        vod_models = sys.modules.setdefault(
            "apps.vod.models", types.ModuleType("apps.vod.models")
        )
        vod_models.VODCategory = types.SimpleNamespace(
            objects=FakeEditorCategoryManager([category])
        )
        vod_models.M3UMovieRelation = types.SimpleNamespace(objects=movie_manager)
        vod_models.M3USeriesRelation = types.SimpleNamespace(objects=series_manager)
        vod_models.VODLogo = types.SimpleNamespace(objects=FakeLogoManager())
        plugin = self.module.Plugin.__new__(self.module.Plugin)
        plugin._account_filter = Mock(return_value={})
        detail = {
            "id": 123,
            "title": "Die Hard",
            "release_date": "1989-07-15",
            "images": {"posters": [{
                "file_path": "/die-hard.jpg",
                "iso_639_1": "en",
                "vote_count": 20,
            }]},
        }
        with (
            patch.object(plugin, "_automatic_tmdb_categories", return_value={
                "movie": {12}, "series": set(),
            }),
            patch.object(plugin, "_selected_tmdb_artwork_categories", return_value={
                "movie": set(), "series": set(),
            }),
            patch("urllib.request.urlopen", return_value=FakeHTTPResponse(detail)),
        ):
            result = plugin._run_tmdb_cleanup(
                {"tmdb_api_key": "test", "keep_language_prefix": True},
                Mock(),
                limit=10,
            )
        self.assertEqual(item.name, "EN - Die Hard (1989)")
        self.assertIs(item.logo, provider_logo)
        self.assertEqual(
            relation.custom_properties["info"]["movie_image"],
            "https://provider/poster.jpg",
        )
        self.assertEqual(result["changes"]["titles_normalized"], 1)
        self.assertEqual(result["changes"].get("artwork_enriched", 0), 0)

    def test_relation_level_tmdb_artwork_is_detected(self):
        relation = types.SimpleNamespace(custom_properties={
            "detailed_info": {"movie_image": "https://image.tmdb.org/t/p/w780/poster.jpg"}
        })
        self.assertEqual(
            self.module.Plugin._relation_tmdb_artwork_url(relation),
            "https://image.tmdb.org/t/p/w780/poster.jpg",
        )
        refreshed = types.SimpleNamespace(custom_properties={
            "info": {"movie_image": "https://image.tmdb.org/t/p/w780/managed.jpg"},
            "detailed_info": {"movie_image": "https://provider.example/refreshed.jpg"},
        })
        self.assertEqual(
            self.module.Plugin._relation_tmdb_artwork_url(refreshed),
            "https://image.tmdb.org/t/p/w780/managed.jpg",
        )


if __name__ == "__main__":
    unittest.main()
