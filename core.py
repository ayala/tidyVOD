"""Pure transformation helpers for the Dispatcharr tidyVOD plugin."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


VALID_CONTENT_TYPES = {"movie", "series", "both"}
CATEGORY_OVERRIDE_PATTERN = re.compile(r"^category_override_(movie|series)_(\d+)$")
CATEGORY_HIDDEN_PATTERN = re.compile(r"^category_hidden_(movie|series)_(\d+)$")
CATEGORY_TMDB_PATTERN = re.compile(r"^category_tmdb_cleanup_(movie|series)_(\d+)$")
LANGUAGE_PREFIX_PATTERN = re.compile(
    r"^\s*(?:"
    r"[\[(]\s*([A-Za-z]{2,3})(?:[-_]([A-Za-z]{2}))?\s*[\])]\s*"
    r"|"
    r"\|?\s*([A-Za-z]{2,3})(?:[-_]([A-Za-z]{2}))?\s*"
    r"(?:[|:/•·]|[-–—]\s+)\s*"
    r")"
)
PROVIDER_TITLE_LANGUAGE_PATTERN = LANGUAGE_PREFIX_PATTERN
DEFAULT_LANGUAGE_ALIASES = {
    "AR": "ar", "DE": "de", "EN": "en", "ENG": "en", "ES": "es",
    "FR": "fr", "FRE": "fr", "IT": "it", "JA": "ja", "JP": "ja",
    "KO": "ko", "NL": "nl", "PL": "pl", "PT": "pt", "RU": "ru",
    "TR": "tr", "ZH": "zh",
}


class ConfigurationError(ValueError):
    """Raised when a user-supplied rule document is invalid."""


@dataclass(frozen=True)
class CategoryRule:
    pattern: re.Pattern[str]
    target: str
    content_type: str = "both"

    def applies_to(self, value: str, content_type: str) -> bool:
        return self.content_type in {"both", content_type} and bool(self.pattern.search(value))


@dataclass(frozen=True)
class TitleRule:
    pattern: re.Pattern[str]
    replacement: str = ""
    content_type: str = "both"

    def apply(self, value: str, content_type: str) -> str:
        if self.content_type not in {"both", content_type}:
            return value
        return self.pattern.sub(self.replacement, value)


def _load_json_list(raw: Any, label: str) -> list[dict[str, Any]]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, list):
        raise ConfigurationError(f"{label} must be a JSON array")
    if not all(isinstance(item, dict) for item in raw):
        raise ConfigurationError(f"every {label} entry must be an object")
    return raw


def _content_type(item: dict[str, Any], label: str, index: int) -> str:
    value = str(item.get("content_type", "both")).lower()
    if value not in VALID_CONTENT_TYPES:
        raise ConfigurationError(
            f"{label}[{index}].content_type must be movie, series, or both"
        )
    return value


def compile_category_rules(raw: Any) -> list[CategoryRule]:
    rules = []
    for index, item in enumerate(_load_json_list(raw, "category_rules")):
        pattern = str(item.get("pattern", "")).strip()
        target = str(item.get("target", "")).strip()
        if not pattern or not target:
            raise ConfigurationError(
                f"category_rules[{index}] requires non-empty pattern and target"
            )
        if len(target) > 255:
            raise ConfigurationError(f"category_rules[{index}].target exceeds 255 characters")
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ConfigurationError(
                f"category_rules[{index}].pattern is invalid: {exc}"
            ) from exc
        rules.append(CategoryRule(compiled, target, _content_type(item, "category_rules", index)))
    return rules


def compile_title_rules(raw: Any) -> list[TitleRule]:
    rules = []
    for index, item in enumerate(_load_json_list(raw, "title_rules")):
        pattern = str(item.get("pattern", "")).strip()
        if not pattern:
            raise ConfigurationError(f"title_rules[{index}] requires a non-empty pattern")
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ConfigurationError(f"title_rules[{index}].pattern is invalid: {exc}") from exc
        rules.append(
            TitleRule(
                compiled,
                str(item.get("replacement", "")),
                _content_type(item, "title_rules", index),
            )
        )
    return rules


def category_target(
    name: str, content_type: str, rules: Iterable[CategoryRule]
) -> str | None:
    """Return the first matching target; rule order is significant."""
    for rule in rules:
        if rule.applies_to(name, content_type):
            return rule.target
    return None


def category_override(
    settings: dict[str, Any], content_type: str, category_id: Any
) -> str | None:
    """Read a clean name, stripping obsolete artwork suffixes from saved settings."""
    value = str(
        settings.get(f"category_override_{content_type}_{category_id}", "") or ""
    ).strip()
    value = re.sub(r"\s*\[(?:CLEAN|EN)\]\s*$", "", value, flags=re.IGNORECASE).strip()
    if len(value) > 255:
        raise ConfigurationError("a category clean name exceeds 255 characters")
    return value or None


def selected_category_mappings(
    settings: dict[str, Any],
) -> dict[str, dict[int, str]]:
    """Return only explicit category-editor mappings, grouped by content type."""
    mappings: dict[str, dict[int, str]] = {"movie": {}, "series": {}}
    for key in settings:
        match = CATEGORY_OVERRIDE_PATTERN.fullmatch(str(key))
        if not match:
            continue
        content_type, raw_category_id = match.groups()
        if settings.get(f"category_hidden_{content_type}_{raw_category_id}", False):
            continue
        target = category_override(settings, content_type, raw_category_id)
        if target:
            mappings[content_type][int(raw_category_id)] = target
    return mappings


def selected_hidden_categories(settings: dict[str, Any]) -> dict[str, set[int]]:
    """Return explicitly hidden category IDs, grouped by content type."""
    hidden: dict[str, set[int]] = {"movie": set(), "series": set()}
    for key, value in settings.items():
        match = CATEGORY_HIDDEN_PATTERN.fullmatch(str(key))
        if match and value:
            content_type, raw_category_id = match.groups()
            hidden[content_type].add(int(raw_category_id))
    return hidden


def selected_tmdb_cleanup_categories(settings: dict[str, Any]) -> dict[str, set[int]]:
    """Return categories explicitly opted into conservative TMDB cleanup."""
    selected: dict[str, set[int]] = {"movie": set(), "series": set()}
    for key, value in settings.items():
        match = CATEGORY_TMDB_PATTERN.fullmatch(str(key))
        if match and value:
            content_type, raw_category_id = match.groups()
            selected[content_type].add(int(raw_category_id))
    return selected


def parse_language_aliases(raw: Any) -> dict[str, str]:
    """Parse editable PREFIX=language mappings while retaining safe defaults."""
    aliases = dict(DEFAULT_LANGUAGE_ALIASES)
    if not raw:
        return aliases
    if not isinstance(raw, str):
        raise ConfigurationError("language_prefix_mappings must be comma-separated text")
    for entry in re.split(r"[,\n]+", raw):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ConfigurationError(
                f"invalid language mapping {entry!r}; use PREFIX=language"
            )
        prefix, language = (part.strip() for part in entry.split("=", 1))
        if not re.fullmatch(r"[A-Za-z]{2,3}", prefix) or not re.fullmatch(
            r"[A-Za-z]{2,3}(?:-[A-Za-z]{2})?", language
        ):
            raise ConfigurationError(
                f"invalid language mapping {entry!r}; example: ES=es"
            )
        aliases[prefix.upper()] = language.lower()
    return aliases


def category_language(name: str, aliases: dict[str, str]) -> tuple[str | None, str | None]:
    """Return the visible source prefix and TMDB image language for a category."""
    match = LANGUAGE_PREFIX_PATTERN.match(name or "")
    if not match:
        return None, None
    prefix = (match.group(1) or match.group(3)).upper()
    region = match.group(2) or match.group(4)
    language = aliases.get(prefix)
    if language and region and "-" not in language:
        language = f"{language}-{region.upper()}"
    return prefix, language


def provider_title_language(name: str, aliases: dict[str, str]) -> tuple[str | None, str | None]:
    """Read a language prefix from a provider title as a secondary hint."""
    match = PROVIDER_TITLE_LANGUAGE_PATTERN.match(name or "")
    if not match:
        return None, None
    prefix = (match.group(1) or match.group(3)).upper()
    region = match.group(2) or match.group(4)
    language = aliases.get(prefix)
    if language and region and "-" not in language:
        language = f"{language}-{region.upper()}"
    return prefix, language


def parse_cleanup_tokens(raw: Any) -> list[str]:
    if not raw:
        return []
    if not isinstance(raw, str):
        raise ConfigurationError("removable_title_tags must be comma-separated text")
    return [token.strip() for token in re.split(r"[,\n]+", raw) if token.strip()]


def normalize_match_title(
    name: str,
    removable_tokens: Iterable[str],
    leading_release_labels: Iterable[str] = (),
) -> tuple[str, int | None]:
    """Create a conservative TMDB search title and extract a reliable year."""
    value = PROVIDER_TITLE_LANGUAGE_PATTERN.sub("", name or "", count=1)
    # Providers commonly prepend availability/quality labels such as
    # ``SD/CAM –``. Only remove configured labels at the beginning and only
    # when followed by an explicit separator, so a real title such as ``Cam``
    # remains searchable.
    for label in sorted(set(leading_release_labels), key=len, reverse=True):
        escaped = re.escape(label).replace(r"\ ", r"[\s._-]*")
        stripped = re.sub(
            rf"^\s*{escaped}\s*(?:[|:•]|[-–—]\s+)\s*",
            "",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
        if stripped != value:
            value = stripped
            break
    years = list(re.finditer(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2}|21\d{2})(?!\d)", value))
    year = int(years[-1].group(1)) if years else None
    if years:
        match = years[-1]
        value = value[:match.start()] + " " + value[match.end():]
    for token in sorted(set(removable_tokens), key=len, reverse=True):
        escaped = re.escape(token).replace(r"\ ", r"[\s._-]*")
        value = re.sub(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[\[\]{}()]", " ", value)
    value = re.sub(r"[._|•]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_")
    return value, year


def comparable_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"[^a-z0-9]+", "", normalized.casefold())


def match_title_candidates(value: str) -> list[str]:
    """Offer a safe alternate without a trailing all-caps actor/credit name."""
    candidates = [value]
    words = value.split()
    suffix_start = len(words)
    while suffix_start and len(words) - suffix_start < 4:
        word = words[suffix_start - 1].strip("-'’.")
        if len(word) < 2 or not any(character.isalpha() for character in word) or word != word.upper():
            break
        suffix_start -= 1
    suffix_size = len(words) - suffix_start
    title_part = " ".join(words[:suffix_start]).strip()
    # Require a two-word credit and mixed/lower case in the retained title.
    # This refuses to reinterpret an entirely upper-case provider title.
    if 2 <= suffix_size <= 4 and title_part and any(character.islower() for character in title_part):
        candidates.append(title_part)
    return candidates


def formatted_tmdb_title(
    title: str,
    year: int | None,
    prefix: str | None,
    keep_prefix: bool,
    player_safe: bool = False,
) -> str:
    value = title or ""
    if player_safe:
        # Some IPTV clients interpret square-bracketed parts of real titles
        # (notably [REC]) as provider tags and hide them. Fullwidth brackets
        # remain visually faithful while avoiding that metadata parser.
        value = value.translate(str.maketrans({"[": "［", "]": "］"}))
    value = re.sub(r"\s+", " ", value).strip()
    if year:
        value = f"{value} ({year})"
    if keep_prefix and prefix:
        value = f"{prefix} - {value}" if player_safe else f"{prefix}| {value}"
    return value[:255]


def clean_title(name: str, content_type: str, rules: Iterable[TitleRule]) -> str:
    value = name
    for rule in rules:
        value = rule.apply(value, content_type)
    return re.sub(r"\s+", " ", value).strip(" -_.")


def parse_account_names(raw: Any) -> set[str]:
    if not raw:
        return set()
    if isinstance(raw, str):
        return {part.strip().casefold() for part in raw.split(",") if part.strip()}
    raise ConfigurationError("account_names must be a comma-separated string")


def safe_filename(value: str, fallback: str = "tidym3u-export") -> str:
    """Create a safe file name stem from user input."""
    value = value.strip()
    if not value:
        return fallback
    value = re.sub(r"[^a-zA-Z0-9._-]", "-", value)
    value = re.sub(r"-+", "-", value).strip(".-")
    return value or fallback
