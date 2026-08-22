from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_QUERY_INTENTS_PATH = Path(__file__).resolve().parent / "resources" / "query_intents.json"


@lru_cache(maxsize=1)
def query_intent_lexicon() -> dict[str, Any]:
    with DEFAULT_QUERY_INTENTS_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def query_intent_value(path: str, default: Any = None) -> Any:
    current: Any = query_intent_lexicon()
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def query_intent_terms(path: str) -> tuple[str, ...]:
    value = query_intent_value(path, [])
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item or "").strip())


def query_intent_term_set(path: str) -> frozenset[str]:
    return frozenset(query_intent_terms(path))


def query_intent_rules(path: str) -> tuple[dict[str, Any], ...]:
    value = query_intent_value(path, [])
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))
