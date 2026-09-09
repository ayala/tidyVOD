import unittest

from core import (
    ConfigurationError,
    category_override,
    category_target,
    clean_title,
    compile_category_rules,
    compile_title_rules,
    parse_account_names,
    selected_category_mappings,
    selected_hidden_categories,
)


class RulesTests(unittest.TestCase):
    def test_category_rules_are_ordered_and_content_scoped(self):
        rules = compile_category_rules([
            {"pattern": "cinema", "target": "Movies", "content_type": "movie"},
            {"pattern": ".*", "target": "Fallback"},
        ])
        self.assertEqual(category_target("FHD Cinema", "movie", rules), "Movies")
        self.assertEqual(category_target("FHD Cinema", "series", rules), "Fallback")

    def test_title_rules_chain_and_normalize_whitespace(self):
        rules = compile_title_rules([
            {"pattern": r"\[(?:FHD|4K)\]", "replacement": ""},
            {"pattern": r"^UK:\s*", "replacement": ""},
        ])
        self.assertEqual(clean_title("UK:  Dune [4K] ", "movie", rules), "Dune")

    def test_invalid_json_has_a_clear_error(self):
        with self.assertRaisesRegex(ConfigurationError, "not valid JSON"):
            compile_category_rules("[")

    def test_account_names_are_case_insensitive(self):
        self.assertEqual(parse_account_names(" Main, BACKUP "), {"main", "backup"})

    def test_friendly_category_override_uses_plain_clean_name(self):
        settings = {
            "category_override_movie_12": " Netflix ",
            "category_override_movie_13": "Netflix",
        }
        self.assertEqual(category_override(settings, "movie", 12), "Netflix")
        self.assertEqual(category_override(settings, "movie", 13), "Netflix")
        self.assertIsNone(category_override(settings, "series", 12))

    def test_obsolete_clean_suffix_is_removed_from_saved_name(self):
        settings = {"category_override_movie_12": " Netflix [clean] "}
        self.assertEqual(category_override(settings, "movie", 12), "Netflix")

    def test_obsolete_en_suffix_is_removed_from_saved_name(self):
        settings = {"category_override_movie_12": " Foreign Movies [EN] "}
        self.assertEqual(category_override(settings, "movie", 12), "Foreign Movies")

    def test_obsolete_marker_without_a_name_is_ignored(self):
        self.assertIsNone(category_override({"category_override_movie_12": "[CLEAN]"}, "movie", 12))

    def test_selected_category_mappings_only_returns_explicit_clean_names(self):
        settings = {
            "category_override_movie_12": " Netflix ",
            "category_override_movie_13": "Netflix [EN]",
            "category_override_series_22": "Drama",
            "category_override_series_23": "",
            "auto_apply": True,
        }
        self.assertEqual(
            selected_category_mappings(settings),
            {
                "movie": {12: "Netflix", 13: "Netflix"},
                "series": {22: "Drama"},
            },
        )

    def test_hidden_category_is_excluded_from_clean_mappings(self):
        settings = {
            "category_override_movie_12": "Netflix",
            "category_hidden_movie_12": True,
            "category_hidden_series_22": False,
        }
        self.assertEqual(selected_hidden_categories(settings), {"movie": {12}, "series": set()})
        self.assertEqual(selected_category_mappings(settings), {"movie": {}, "series": {}})

if __name__ == "__main__":
    unittest.main()
