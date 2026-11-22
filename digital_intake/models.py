"""Domain constants and small value helpers shared by the workbench."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

RECORD_KINDS = frozenset(
    {
        "collection_item",
        "digital_asset",
        "research_note",
        "license",
        "exhibition_item",
        "provenance_amendment",
    }
)

# Fields that every digital carrier must have before it can be considered to
# have a clear chain of custody.  Filenames and content hashes are deliberately
# not identity: two carriers may share both and still be distinct records.
REQUIRED_ASSET_PROVENANCE = (
    "item_ref",
    "capture_kind",
    "source_team",
    "sha256",
)

EXHIBITION_STATES = frozenset(
    {"draft", "preparing", "ready", "publishing", "published", "withdrawn"}
)

# Exhibitions that can still be changed and therefore must be re-checked when
# their inputs move.  Published exhibitions are frozen evidence.
ACTIVE_EXHIBITION_STATES = frozenset(
    {"draft", "preparing", "ready", "publishing"}
)

NOTE_KINDS = frozenset({"observation", "interpretation"})
NOTE_REVIEW_STATES = frozenset(
    {"draft", "checked", "disputed", "superseded", "rejected"}
)

# Channels that reach the general public.  A license must name one of these
# explicitly; research/onsite permissions never imply a public release.
PUBLIC_CHANNELS = frozenset(
    {
        "online-exhibition",
        "public-web",
        "social-media",
        "publication-print",
        "publication-digital",
    }
)


class RecordValidationError(ValueError):
    """A single envelope is structurally invalid."""


def parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RecordValidationError(f"{field_name} 必须是非空 ISO 日期时间字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RecordValidationError(f"{field_name} 不是有效的 ISO 日期时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise RecordValidationError(f"{field_name} 必须包含时区偏移: {value!r}")
    return parsed


def parse_date(value: Any, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        raise RecordValidationError(f"{field_name} 必须是 ISO 日期字符串")
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise RecordValidationError(f"{field_name} 不是有效日期: {value!r}") from exc


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value.isoformat()


def clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise RecordValidationError(f"{field_name} 必须是字符串或字符串列表")
    result: list[str] = []
    for entry in values:
        text = clean_string(entry)
        if text and text not in result:
            result.append(text)
    return result
