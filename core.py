"""Pure transformation helpers for the Dispatcharr tidyVOD plugin."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


VALID_CONTENT_TYPES = {"movie", "series", "both"}
CATEGORY_OVERRIDE_PATTERN = re.compile(r"^category_override_(movie|series)_(\d+)$")
CATEGORY_HIDDEN_PATTERN = re.compile(r"^category_hidden_(movie|series)_(\d+)$")


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
