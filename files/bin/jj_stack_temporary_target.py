"""Parsing for the optional, invocation-local temporary target policy."""

from __future__ import annotations

import json
from dataclasses import dataclass

BASE_CONFIG = "stack.temporary-target.base"
STATUSES_CONFIG = "stack.temporary-target.required-statuses"


class Error(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    base: str
    required_statuses: tuple[str, ...]


def parse_policy(base: str | None, statuses: str | None) -> Policy | None:
    if base is None and statuses is None:
        return None
    if base is None or statuses is None:
        raise Error("temporary-target base and required-statuses must be configured together")
    if not base or base.strip() != base or any(character.isspace() for character in base):
        raise Error("temporary-target base must be a nonempty branch name")
    try:
        value = json.loads(statuses)
    except json.JSONDecodeError as exc:
        raise Error("temporary-target required-statuses must be a nonempty string list") from exc
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise Error("temporary-target required-statuses must be a nonempty string list")
    contexts = tuple(item.strip() for item in value)
    if len({item.casefold() for item in contexts}) != len(contexts):
        raise Error("temporary-target required-statuses contains duplicate contexts")
    return Policy(base, contexts)
