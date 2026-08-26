import contextlib
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "tidyvod_reconciliation_test"


def load_plugin_module():
    django = types.ModuleType("django")
    django_db = types.ModuleType("django.db")
    django_db.IntegrityError = type("IntegrityError", (Exception,), {})
    django_db.close_old_connections = lambda: None
    django_db.transaction = types.SimpleNamespace(atomic=contextlib.nullcontext)
    django.db = django_db
    sys.modules.setdefault("django", django)
    sys.modules.setdefault("django.db", django_db)

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

    def filter(self, **filters):
        category_ids = set(filters["category_id__in"])
        return FakeQuerySet([
            relation for relation in self.relations
            if relation.category_id in category_ids
        ])

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


if __name__ == "__main__":
    unittest.main()
