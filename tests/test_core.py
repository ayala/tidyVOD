import unittest

from core import (
    ConfigurationError,
    category_override,
    category_target,
    clean_title,
    category_language,
    compile_category_rules,
    compile_title_rules,
    parse_account_names,
    parse_cleanup_tokens,
    parse_language_aliases,
    provider_title_language,
    normalize_match_title,
    formatted_tmdb_title,
    match_title_candidates,
    selected_category_mappings,
    selected_hidden_categories,
    selected_tmdb_cleanup_categories,
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

    def test_tmdb_cleanup_categories_are_explicit(self):
        settings = {
            "category_tmdb_cleanup_movie_12": True,
            "category_tmdb_cleanup_movie_13": False,
            "category_tmdb_cleanup_series_22": True,
        }
        self.assertEqual(
            selected_tmdb_cleanup_categories(settings),
            {"movie": {12}, "series": {22}},
        )

    def test_language_prefix_and_editable_aliases(self):
        aliases = parse_language_aliases("ESP=es, EN=en-GB")
        self.assertEqual(category_language("ESP| Peliculas 4K", aliases), ("ESP", "es"))
        self.assertEqual(category_language("EN| Movies", aliases), ("EN", "en-gb"))
        self.assertEqual(category_language("Movies", aliases), (None, None))
        self.assertEqual(provider_title_language("EN - Blade 4K (1998)", aliases), ("EN", "en-gb"))
        self.assertEqual(provider_title_language("[ESP] Blade (1998)", aliases), ("ESP", "es"))
        self.assertEqual(provider_title_language("|EN| Blade (1998)", aliases), ("EN", "en-gb"))
        self.assertEqual(provider_title_language("ESP • Blade (1998)", aliases), ("ESP", "es"))

    def test_provider_title_normalization_is_conservative(self):
        tokens = parse_cleanup_tokens("4K, HDR, Blu-ray")
        self.assertEqual(
            normalize_match_title("ES| A Man Called Otto (2022) HDR • 4K", tokens),
            ("A Man Called Otto", 2022),
        )
        self.assertEqual(
            normalize_match_title("EN - Blade 4K (1998)", tokens),
            ("Blade", 1998),
        )
        self.assertEqual(
            formatted_tmdb_title("El peor vecino del mundo", 2022, "ES", True),
            "ES| El peor vecino del mundo (2022)",
        )

    def test_trailing_actor_candidate_requires_title_case(self):
        self.assertEqual(
            match_title_candidates("A Man Called Otto TOM HANKS"),
            ["A Man Called Otto TOM HANKS", "A Man Called Otto"],
        )
        self.assertEqual(
            match_title_candidates("A MAN CALLED OTTO TOM HANKS"),
            ["A MAN CALLED OTTO TOM HANKS"],
        )

if __name__ == "__main__":
    unittest.main()
