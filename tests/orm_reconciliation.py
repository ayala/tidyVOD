"""Real Django/SQLite regression tests, run separately from the stub unit suite.

Usage: python tests/orm_reconciliation.py (requires Django 5.2).
No connection to a running Dispatcharr or user data is made.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from django.conf import settings

settings.configure(
    INSTALLED_APPS=["__main__"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    USE_TZ=True,
)
import django
django.setup()
from django.db import connection, models


class VODCategory(models.Model):
    name = models.CharField(max_length=255)
    category_type = models.CharField(max_length=16)


class Account(models.Model):
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)


class M3UMovieRelation(models.Model):
    category = models.ForeignKey(VODCategory, null=True, on_delete=models.SET_NULL)
    custom_properties = models.JSONField(null=True)
    m3u_account = models.ForeignKey(Account, on_delete=models.CASCADE)


class M3USeriesRelation(models.Model):
    category = models.ForeignKey(VODCategory, null=True, on_delete=models.SET_NULL)
    custom_properties = models.JSONField(null=True)
    m3u_account = models.ForeignKey(Account, on_delete=models.CASCADE)


class M3UVODCategoryRelation(models.Model):
    category = models.ForeignKey(VODCategory, on_delete=models.CASCADE, related_name="m3u_relations")
    m3u_account = models.ForeignKey(Account, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)
    custom_properties = models.JSONField(null=True)


with connection.schema_editor() as editor:
    for model in (VODCategory, Account, M3UMovieRelation, M3USeriesRelation, M3UVODCategoryRelation):
        editor.create_model(model)

vod = types.ModuleType("apps.vod.models")
vod.VODCategory = VODCategory
vod.M3UMovieRelation = M3UMovieRelation
vod.M3USeriesRelation = M3USeriesRelation
vod.M3UVODCategoryRelation = M3UVODCategoryRelation
sys.modules["apps"] = types.ModuleType("apps")
sys.modules["apps.vod"] = types.ModuleType("apps.vod")
sys.modules["apps.vod.models"] = vod
ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("tidyvod_orm")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("tidyvod_orm.plugin", ROOT / "plugin.py")
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        M3UMovieRelation.objects.all().delete()
        M3USeriesRelation.objects.all().delete()
        M3UVODCategoryRelation.objects.all().delete()
        VODCategory.objects.all().delete()
        Account.objects.all().delete()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"DISPATCHARR_PLUGINS_DIR": self.directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.plugin = plugin_module.Plugin.__new__(plugin_module.Plugin)
        self.account = Account.objects.create(name="Provider")
        self.plugin._account_filter = lambda settings: {"m3u_account": self.account, "m3u_account__is_active": True}
        self.source = VODCategory.objects.create(name="Netflix HEVC", category_type="movie")
        M3UVODCategoryRelation.objects.create(
            category=self.source, m3u_account=self.account, enabled=True, custom_properties={})
        self.settings = {f"category_override_movie_{self.source.pk}": "Netflix"}

    def relation(self, category=None, marker=False, account=None):
        props = {"basic_data": {"stream_id": 7}}
        if marker:
            props["vodarranger"] = {"original_category_id": self.source.pk,
                                     "original_category_name": self.source.name}
        return M3UMovieRelation.objects.create(
            category=category, custom_properties=props, m3u_account=account or self.account)

    def run_sync(self, force=True):
        return self.plugin._reconcile_categories(self.settings, Mock(), source="test", force=force)

    def test_new_vod_and_provider_reset_are_repaired_idempotently(self):
        relation = self.relation(self.source)
        self.assertEqual(self.run_sync()["changes"]["categories"], 1)
        relation.refresh_from_db()
        self.assertEqual(relation.category.name, "Netflix")
        self.assertEqual(relation.custom_properties["basic_data"], {"stream_id": 7})
        self.assertEqual(self.run_sync()["changes"]["categories"], 0)
        M3UMovieRelation.objects.filter(pk=relation.pk).update(category=self.source)
        self.assertEqual(self.run_sync()["changes"]["categories"], 1)

    def test_source_count_follows_items_moved_to_clean_category(self):
        direct = self.relation(self.source)
        self.relation(self.source)
        self.assertEqual(
            self.plugin._source_item_counts("movie", [self.source]),
            {self.source.pk: {"total": 2, "original": 2, "moved": 0}},
        )
        self.run_sync()
        direct.refresh_from_db()
        self.assertEqual(direct.category.name, "Netflix")
        self.assertEqual(
            self.plugin._source_item_counts("movie", [self.source]),
            {self.source.pk: {"total": 2, "original": 0, "moved": 2}},
        )
        self.relation(self.source)
        self.assertEqual(
            self.plugin._source_item_counts("movie", [self.source]),
            {self.source.pk: {"total": 3, "original": 1, "moved": 2}},
        )

    def test_null_and_unmapped_assignments_recover_from_markers(self):
        wrong = VODCategory.objects.create(name="Wrong category", category_type="movie")
        self.relation(None, marker=True)
        self.relation(wrong, marker=True)
        self.assertEqual(self.run_sync()["changes"]["categories"], 2)

    def test_changed_clean_name_repairs_already_curated_items(self):
        self.relation(self.source)
        self.run_sync()
        self.settings[f"category_override_movie_{self.source.pk}"] = "Netflix Movies"
        self.assertEqual(self.run_sync()["changes"]["categories"], 1)
        self.assertEqual(M3UMovieRelation.objects.get().category.name, "Netflix Movies")

    def test_deleted_target_recovers(self):
        relation = self.relation(self.source)
        self.run_sync()
        relation.refresh_from_db()
        relation.category.delete()
        relation.refresh_from_db()
        self.assertIsNone(relation.category)
        self.assertEqual(self.run_sync()["changes"]["categories"], 1)

    def test_recreated_source_id_recovers_from_backup_without_mutating_settings(self):
        self.plugin._backup_mappings(self.settings, reason="test")
        self.source.delete()
        recreated = VODCategory.objects.create(name="Netflix HEVC", category_type="movie")
        self.relation(recreated)
        before = dict(self.settings)
        self.assertEqual(self.run_sync()["changes"]["categories"], 1)
        self.assertEqual(self.settings, before)
        entries = self.plugin._mapping_entries(self.settings)
        self.assertEqual(entries[0]["provider_name"], "Netflix HEVC")
        self.assertEqual(self.run_sync()["changes"]["categories"], 0)

    def test_blank_mapping_is_not_restored_from_backup(self):
        self.plugin._backup_mappings(self.settings, reason="test")
        self.relation(self.source)
        self.settings = {key: "" for key in self.settings}
        self.assertEqual(self.run_sync()["changes"]["categories"], 0)
        self.assertEqual(M3UMovieRelation.objects.get().category, self.source)

    def test_hidden_category_is_disabled_and_existing_assignments_removed(self):
        relation = self.relation(self.source)
        self.run_sync()  # First move it into its clean category.
        self.settings[f"category_hidden_movie_{self.source.pk}"] = True
        result = self.run_sync()
        self.assertEqual(result["changes"]["hidden_assignments"], 1)
        self.assertFalse(M3UMovieRelation.objects.filter(pk=relation.pk).exists())
        category_relation = M3UVODCategoryRelation.objects.get(category=self.source)
        self.assertFalse(category_relation.enabled)
        self.assertTrue(category_relation.custom_properties["vodarranger"]["hidden_category"])

    def test_unhide_restores_only_plugin_managed_enablement(self):
        self.settings[f"category_hidden_movie_{self.source.pk}"] = True
        self.run_sync()
        self.settings[f"category_hidden_movie_{self.source.pk}"] = False
        result = self.run_sync()
        self.assertEqual(result["changes"]["restored_categories"], 1)
        category_relation = M3UVODCategoryRelation.objects.get(category=self.source)
        self.assertTrue(category_relation.enabled)
        self.assertNotIn("vodarranger", category_relation.custom_properties)

        manually_disabled = VODCategory.objects.create(name="Manual", category_type="movie")
        M3UVODCategoryRelation.objects.create(
            category=manually_disabled, m3u_account=self.account,
            enabled=False, custom_properties={},
        )
        self.run_sync()
        self.assertFalse(M3UVODCategoryRelation.objects.get(category=manually_disabled).enabled)

    def test_hidden_category_without_clean_name_is_backed_up(self):
        self.settings = {f"category_hidden_movie_{self.source.pk}": True}
        payload = self.plugin._backup_mappings(self.settings, reason="test")
        self.assertEqual(payload["mappings"][0]["clean_name"], "")
        self.assertTrue(payload["mappings"][0]["hidden"])

    def test_backup_restore_preserves_clean_name_and_hidden_choice(self):
        self.settings[f"category_hidden_movie_{self.source.pk}"] = True
        self.settings[f"category_tmdb_cleanup_movie_{self.source.pk}"] = True
        payload = self.plugin._backup_mappings(self.settings, reason="test")
        config = Mock(settings={
            f"category_override_movie_{self.source.pk}": "Changed",
            f"category_hidden_movie_{self.source.pk}": False,
            f"category_tmdb_cleanup_movie_{self.source.pk}": False,
        })
        self.plugin._plugin_config = Mock(return_value=config)
        result = self.plugin._restore_mapping_payload(
            config.settings, payload, Mock(), source="test",
        )
        self.assertEqual(result["restored_mappings"], 1)
        self.assertEqual(config.settings[f"category_override_movie_{self.source.pk}"], "Netflix")
        self.assertTrue(config.settings[f"category_hidden_movie_{self.source.pk}"])
        self.assertTrue(config.settings[f"category_tmdb_cleanup_movie_{self.source.pk}"])

    def test_hide_preview_does_not_change_database(self):
        relation = self.relation(self.source)
        self.settings[f"category_hidden_movie_{self.source.pk}"] = True
        result = self.plugin._apply_hidden_categories(self.settings, Mock(), dry_run=True)
        self.assertEqual(result["changes"]["hidden_assignments"], 1)
        self.assertTrue(M3UMovieRelation.objects.filter(pk=relation.pk).exists())
        self.assertTrue(M3UVODCategoryRelation.objects.get(category=self.source).enabled)

    def test_multiple_source_recreations_and_lost_target_recover(self):
        self.plugin._backup_mappings(self.settings, reason="test")
        self.source.delete()
        recreated = VODCategory.objects.create(name="Netflix HEVC", category_type="movie")
        relation = self.relation(recreated)
        self.run_sync()
        recreated.delete()
        relation.refresh_from_db()
        relation.category.delete()
        newest = VODCategory.objects.create(name="Netflix HEVC", category_type="movie")
        self.relation(newest)
        self.assertEqual(self.run_sync()["changes"]["categories"], 2)

    def test_ambiguous_names_are_not_guessed(self):
        entries = [
            {"category_id": 10, "content_type": "movie", "provider_name": "Same"},
            {"category_id": 11, "content_type": "movie", "provider_name": "Same"},
        ]
        mappings = {"movie": {10: "First", 11: "Second"}, "series": {}}
        self.assertEqual(self.plugin._category_name_targets(entries, mappings)["movie"], {})
        category = VODCategory.objects.create(name="Same", category_type="movie")
        resolved = self.plugin._resolved_category_mappings({}, mappings, entries=entries)
        self.assertNotIn(category.pk, resolved["movie"])

    def test_large_repair_is_batched(self):
        M3UMovieRelation.objects.bulk_create([
            M3UMovieRelation(category=self.source, custom_properties={}, m3u_account=self.account)
            for _ in range(1005)
        ])
        self.assertEqual(self.run_sync()["changes"]["categories"], 1005)
        self.assertEqual(self.run_sync()["changes"]["categories"], 0)

    def test_unmapped_and_out_of_scope_items_are_untouched(self):
        other = VODCategory.objects.create(name="Other", category_type="movie")
        self.relation(other)
        self.relation(self.source, account=Account.objects.create(name="Excluded"))
        self.assertEqual(self.run_sync()["changes"]["categories"], 0)

    def test_movie_mapping_does_not_apply_to_series(self):
        series_source = VODCategory.objects.create(name=self.source.name, category_type="series")
        M3USeriesRelation.objects.create(category=series_source, custom_properties={}, m3u_account=self.account)
        self.assertEqual(self.run_sync()["changes"]["categories"], 0)
        self.settings[f"category_override_series_{series_source.pk}"] = "Netflix Series"
        result = self.run_sync()
        self.assertEqual(result["changes"]["series"], 1)
        self.assertEqual(M3USeriesRelation.objects.get().category.name, "Netflix Series")

    def test_shared_lock_and_cooldown_prevent_duplicate_work(self):
        self.relation(self.source)
        with self.plugin._reconcile_lock() as acquired:
            self.assertTrue(acquired)
            self.assertIn("already running", self.run_sync()["message"])
        self.assertEqual(self.run_sync()["changes"]["categories"], 1)
        self.assertIn("recently", self.run_sync(force=False)["message"])

    def test_failures_are_recorded_and_retried(self):
        with patch.object(self.plugin, "_perform_category_reconciliation", side_effect=RuntimeError("test failure")):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                self.run_sync()
        self.assertEqual(self.plugin._read_reconcile_status()["status"], "error")
        self.run_sync()
        self.assertEqual(self.plugin._read_reconcile_status()["status"], "ok")

    def test_correct_assignments_are_not_materialized_or_updated(self):
        self.relation(self.source)
        self.run_sync()
        with patch.object(self.plugin, "_sample") as sample, patch.object(M3UMovieRelation.objects, "bulk_update") as update:
            self.assertEqual(self.run_sync()["changes"]["categories"], 0)
            sample.assert_not_called()
            update.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
