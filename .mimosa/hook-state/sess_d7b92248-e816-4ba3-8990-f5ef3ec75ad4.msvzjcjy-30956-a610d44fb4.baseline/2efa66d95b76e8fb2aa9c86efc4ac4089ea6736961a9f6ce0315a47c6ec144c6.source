from __future__ import annotations

import re
from typing import Iterable


GENERIC_FAVORITE_TAG = "ai_favorite"
LEGACY_FAVORITE_TAG = "haven_favorite"
FAVORITE_PREFIX = "flavor_"


def normalize_tag(value: object) -> str:
    return str(value or "").strip().lower()


def ai_favorite_tag(ai_name: str | None = None) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "_", normalize_tag(ai_name)).strip("_")
    return f"{slug}_favorite" if slug else GENERIC_FAVORITE_TAG


def favorite_memory_aliases(ai_name: str | None = None) -> set[str]:
    aliases = {GENERIC_FAVORITE_TAG, LEGACY_FAVORITE_TAG}
    if ai_name:
        aliases.add(ai_favorite_tag(ai_name))
    return aliases


def is_flavor_tag(tag: object) -> bool:
    return normalize_tag(tag).startswith(FAVORITE_PREFIX)


def is_favorite_memory_tag(tag: object, ai_name: str | None = None) -> bool:
    normalized = normalize_tag(tag)
    if is_flavor_tag(normalized):
        return False
    if normalized in favorite_memory_aliases(ai_name):
        return True
    return bool(re.fullmatch(r"[\w\u4e00-\u9fff]+_favorite", normalized))


def is_favorite_policy_tag(tag: object, ai_name: str | None = None) -> bool:
    return is_favorite_memory_tag(tag, ai_name) or is_flavor_tag(tag)


def has_favorite_memory_tag(tags: Iterable[object] | None, ai_name: str | None = None) -> bool:
    return any(is_favorite_memory_tag(tag, ai_name) for tag in tags or [])


def has_favorite_policy_tag(tags: Iterable[object] | None, ai_name: str | None = None) -> bool:
    return any(is_favorite_policy_tag(tag, ai_name) for tag in tags or [])


def favorite_policy_tags(tags: Iterable[object] | None, ai_name: str | None = None) -> list[str]:
    return [
        str(tag).strip()
        for tag in tags or []
        if str(tag or "").strip() and is_favorite_policy_tag(tag, ai_name)
    ]
