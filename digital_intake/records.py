"""Structured records derived from intake envelopes.

Every record keeps the identifier of the source envelope it came from so that
any later statement can be traced back through the chain of custody.  Records
are append-only from the workbench's point of view: a correction arrives as a
new envelope, and the previous revision remains queryable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .models import (
    NOTE_KINDS,
    NOTE_REVIEW_STATES,
    RECORD_KINDS,
    RecordValidationError,
    clean_string,
    parse_date,
    parse_datetime,
    string_list,
)


@dataclass(frozen=True)
class RecordBase:
    source_id: str
    collected_at: datetime
    payload: dict[str, Any]

    @property
    def record_kind(self) -> str:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError


@dataclass(frozen=True)
class CollectionItem(RecordBase):
    item_ref: str
    catalogue_revision: int
    period: str | None
    institution_ref: str | None
    title: str | None
    object_refs: tuple[str, ...]
    replaces_item_ref: str | None

    record_kind = "collection_item"


@dataclass(frozen=True)
class DigitalAsset(RecordBase):
    asset_ref: str
    item_ref: str
    revision: int
    capture_kind: str
    source_team: str | None
    sha256: str | None
    file_name: str | None
    captured_at: datetime | None
    angle: str | None
    precision_note: str | None
    derived_from: str | None

    record_kind = "digital_asset"


@dataclass(frozen=True)
class ResearchNote(RecordBase):
    note_ref: str
    item_ref: str
    note_kind: str | None
    revision: int
    review_state: str
    body: str | None
    author: str | None
    supersedes: str | None

    record_kind = "research_note"


@dataclass(frozen=True)
class License(RecordBase):
    license_ref: str
    asset_ref: str
    allowed_use: tuple[str, ...]
    valid_from: date | None
    valid_until: date | None
    revision: int
    supersedes: str | None
    restrictions: str | None
    attribution: str | None
    revoked: bool

    record_kind = "license"


@dataclass(frozen=True)
class ExhibitionItem(RecordBase):
    exhibition_ref: str
    asset_ref: str
    state: str
    license_ref: str | None
    item_ref: str | None
    channel: str | None
    title: str | None
    note_refs: tuple[str, ...]
    revision: int = 1
    published_at: datetime | None = None
    recorded_catalogue_revision: int | None = None

    record_kind = "exhibition_item"


@dataclass(frozen=True)
class ProvenanceAmendment(RecordBase):
    """A cataloguer's explicit fill-in for previously missing provenance.

    Amendments never rewrite history.  They are consulted when computing
    *current* completeness, while reports about a past release keep using the
    evidence that was available at the time.
    """

    asset_ref: str
    item_ref: str | None
    source_team: str | None
    capture_kind: str | None
    sha256: str | None
    note: str | None
    amended_by: str | None

    record_kind = "provenance_amendment"


RECORD_CLASSES = {
    "collection_item": CollectionItem,
    "digital_asset": DigitalAsset,
    "research_note": ResearchNote,
    "license": License,
    "exhibition_item": ExhibitionItem,
    "provenance_amendment": ProvenanceAmendment,
}


def build_record(source: dict[str, Any]) -> RecordBase:
    """Validate one intake envelope and turn it into a typed record."""

    if not isinstance(source, dict):
        raise RecordValidationError("来源条目必须是 JSON 对象")
    source_id = clean_string(source.get("source_id"))
    if not source_id:
        raise RecordValidationError("来源条目缺少 source_id")
    record_kind = clean_string(source.get("record_kind"))
    if record_kind not in RECORD_KINDS:
        raise RecordValidationError(
            f"来源 {source_id} 的 record_kind 不受支持: {record_kind!r}"
        )
    collected_at = parse_datetime(source.get("collected_at"), f"{source_id}.collected_at")
    payload = source.get("payload")
    if not isinstance(payload, dict):
        raise RecordValidationError(f"来源 {source_id} 的 payload 必须是对象")

    cls = RECORD_CLASSES[record_kind]
    builder = _BUILDERS[record_kind]
    return builder(cls, source_id, collected_at, payload)


def _require(payload: dict[str, Any], key: str, source_id: str) -> str:
    value = clean_string(payload.get(key))
    if not value:
        raise RecordValidationError(f"来源 {source_id} 缺少必填字段 {key}")
    return value


def _build_collection_item(cls, source_id, collected_at, payload):
    item_ref = _require(payload, "item_ref", source_id)
    revision = payload.get("catalogue_revision", 1)
    if not isinstance(revision, int) or revision < 0:
        raise RecordValidationError(
            f"来源 {source_id} 的 catalogue_revision 必须是非负整数"
        )
    return cls(
        source_id=source_id,
        collected_at=collected_at,
        payload=payload,
        item_ref=item_ref,
        catalogue_revision=revision,
        period=clean_string(payload.get("period")),
        institution_ref=clean_string(payload.get("institution_ref")),
        title=clean_string(payload.get("title")),
        object_refs=tuple(string_list(payload.get("object_refs"), "object_refs")),
        replaces_item_ref=clean_string(payload.get("replaces_item_ref")),
    )


def _build_digital_asset(cls, source_id, collected_at, payload):
    asset_ref = _require(payload, "asset_ref", source_id)
    item_ref = _require(payload, "item_ref", source_id)
    capture_kind = _require(payload, "capture_kind", source_id)
    captured_at = None
    if payload.get("captured_at") is not None:
        captured_at = parse_datetime(
            payload.get("captured_at"), f"{source_id}.payload.captured_at"
        )
    sha = clean_string(payload.get("sha256"))
    if sha is not None:
        normalized = sha.lower()
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            raise RecordValidationError(f"来源 {source_id} 的 sha256 格式无效")
        sha = normalized
    revision = payload.get("revision", 1)
    if not isinstance(revision, int) or revision < 0:
        raise RecordValidationError(f"来源 {source_id} 的 revision 必须是非负整数")
    return cls(
        source_id=source_id,
        collected_at=collected_at,
        payload=payload,
        asset_ref=asset_ref,
        item_ref=item_ref,
        revision=revision,
        capture_kind=capture_kind,
        source_team=clean_string(payload.get("source_team")),
        sha256=sha,
        file_name=clean_string(payload.get("file_name")),
        captured_at=captured_at,
        angle=clean_string(payload.get("angle")),
        precision_note=clean_string(payload.get("precision_note")),
        derived_from=clean_string(payload.get("derived_from")),
    )


def _build_research_note(cls, source_id, collected_at, payload):
    note_ref = _require(payload, "note_ref", source_id)
    item_ref = clean_string(payload.get("item_ref"))
    note_kind = clean_string(payload.get("note_kind"))
    if note_kind is not None and note_kind not in NOTE_KINDS:
        raise RecordValidationError(
            f"来源 {source_id} 的 note_kind 不受支持: {note_kind!r}"
        )
    revision = payload.get("revision", 1)
    if not isinstance(revision, int) or revision < 0:
        raise RecordValidationError(f"来源 {source_id} 的 revision 必须是非负整数")
    review_state = clean_string(payload.get("review_state")) or "draft"
    if review_state not in NOTE_REVIEW_STATES:
        raise RecordValidationError(
            f"来源 {source_id} 的 review_state 不受支持: {review_state!r}"
        )
    return cls(
        source_id=source_id,
        collected_at=collected_at,
        payload=payload,
        note_ref=note_ref,
        item_ref=item_ref or "",
        note_kind=note_kind,
        revision=revision,
        review_state=review_state,
        body=clean_string(payload.get("body")),
        author=clean_string(payload.get("author")),
        supersedes=clean_string(payload.get("supersedes")),
    )


def _build_license(cls, source_id, collected_at, payload):
    license_ref = _require(payload, "license_ref", source_id)
    asset_ref = _require(payload, "asset_ref", source_id)
    allowed = string_list(payload.get("allowed_use"), "allowed_use")
    if not allowed:
        raise RecordValidationError(f"来源 {source_id} 的许可必须声明 allowed_use")
    valid_from = None
    valid_until = None
    if payload.get("valid_from") is not None:
        valid_from = parse_date(payload.get("valid_from"), f"{source_id}.payload.valid_from")
    if payload.get("valid_until") is not None:
        valid_until = parse_date(
            payload.get("valid_until"), f"{source_id}.payload.valid_until"
        )
    if valid_from and valid_until and valid_until < valid_from:
        raise RecordValidationError(f"来源 {source_id} 的许可有效期起止颠倒")
    revision = payload.get("revision", 1)
    if not isinstance(revision, int) or revision < 0:
        raise RecordValidationError(f"来源 {source_id} 的 revision 必须是非负整数")
    return cls(
        source_id=source_id,
        collected_at=collected_at,
        payload=payload,
        license_ref=license_ref,
        asset_ref=asset_ref,
        allowed_use=tuple(allowed),
        valid_from=valid_from,
        valid_until=valid_until,
        revision=revision,
        supersedes=clean_string(payload.get("supersedes")),
        restrictions=clean_string(payload.get("restrictions")),
        attribution=clean_string(payload.get("attribution")),
        revoked=payload.get("revoked") is True,
    )


def _build_exhibition_item(cls, source_id, collected_at, payload):
    exhibition_ref = _require(payload, "exhibition_ref", source_id)
    asset_ref = _require(payload, "asset_ref", source_id)
    state = clean_string(payload.get("state")) or "draft"
    revision = payload.get("revision", 1)
    if not isinstance(revision, int) or revision < 0:
        raise RecordValidationError(f"来源 {source_id} 的 revision 必须是非负整数")
    published_at = None
    if payload.get("published_at") is not None:
        published_at = parse_datetime(
            payload.get("published_at"), f"{source_id}.payload.published_at"
        )
    recorded_revision = payload.get("catalogue_revision")
    if recorded_revision is not None and (
        not isinstance(recorded_revision, int) or recorded_revision < 0
    ):
        raise RecordValidationError(
            f"来源 {source_id} 的 catalogue_revision 必须是非负整数"
        )
    return cls(
        source_id=source_id,
        collected_at=collected_at,
        payload=payload,
        exhibition_ref=exhibition_ref,
        asset_ref=asset_ref,
        state=state,
        license_ref=clean_string(payload.get("license_ref")),
        item_ref=clean_string(payload.get("item_ref")),
        channel=clean_string(payload.get("channel")),
        title=clean_string(payload.get("title")),
        note_refs=tuple(string_list(payload.get("note_refs"), "note_refs")),
        revision=revision,
        published_at=published_at,
        recorded_catalogue_revision=recorded_revision,
    )


_BUILDERS = {
    "collection_item": _build_collection_item,
    "digital_asset": _build_digital_asset,
    "research_note": _build_research_note,
    "license": _build_license,
    "exhibition_item": _build_exhibition_item,
}


def _build_provenance_amendment(cls, source_id, collected_at, payload):
    asset_ref = _require(payload, "asset_ref", source_id)
    return cls(
        source_id=source_id,
        collected_at=collected_at,
        payload=payload,
        asset_ref=asset_ref,
        item_ref=clean_string(payload.get("item_ref")),
        source_team=clean_string(payload.get("source_team")),
        capture_kind=clean_string(payload.get("capture_kind")),
        sha256=clean_string(payload.get("sha256")),
        note=clean_string(payload.get("note")),
        amended_by=clean_string(payload.get("amended_by")),
    )


_BUILDERS["provenance_amendment"] = _build_provenance_amendment
