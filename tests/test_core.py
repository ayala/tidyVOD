import unittest

from core import (
    ConfigurationError,
    category_override,
    category_override_details,
    category_target,
    clean_title,
    compile_category_rules,
    compile_title_rules,
    parse_account_names,
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

    def test_clean_suffix_is_opt_in_and_not_part_of_category_name(self):
        settings = {"category_override_movie_12": " Netflix [clean] "}
        self.assertEqual(category_override_details(settings, "movie", 12), ("Netflix", "clean"))
        self.assertEqual(category_override(settings, "movie", 12), "Netflix")

    def test_clean_suffix_requires_a_clean_name(self):
        with self.assertRaises(ConfigurationError):
            category_override_details({"category_override_movie_12": "[CLEAN]"}, "movie", 12)

    def test_en_suffix_selects_english_art_and_is_stripped(self):
        settings = {"category_override_movie_12": " Foreign Movies [EN] "}
        self.assertEqual(
            category_override_details(settings, "movie", 12),
            ("Foreign Movies", "en"),
        )

if __name__ == "__main__":
    unittest.main()
