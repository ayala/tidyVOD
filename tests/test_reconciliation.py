import contextlib
import importlib.util
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
    def annotate(self, **annotations):
        return self

    def distinct(self):
        return self

    def order_by(self, *fields):
        return self


class FakeEditorCategoryManager:
    def __init__(self, categories):
        self.categories = categories

    def filter(self, **filters):
        return FakeCategoryQuerySet([
            category for category in self.categories
            if category.category_type == filters["category_type"]
        ])


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

        fields = self.module.Plugin._category_editor_fields()
        ids = [field["id"] for field in fields]

        self.assertEqual(
            ids,
            [
                "movie_category_heading",
                "category_override_movie_12",
                "movie_series_section_gap",
                "series_category_heading",
                "category_override_series_22",
            ],
        )
        spacer = next(field for field in fields if field["id"] == "movie_series_section_gap")
        self.assertEqual(spacer["value"], "\u200c")
        self.assertTrue(spacer["value"])


if __name__ == "__main__":
    unittest.main()
