"""The intake/catalogue workbench.

Design rules this module enforces:

* **Append-only intake.** A source envelope is ingested once. Corrected
  research notes and updated licenses arrive as new envelopes; earlier
  revisions and every statement made against them stay queryable.
* **Carrier identity, not content identity.** Every ``asset_ref`` is its own
  digital record. Two records sharing a filename, a checksum or an item are
  never merged; ``resolve_asset`` by filename returns every candidate and
  demands an explicit choice when the result is ambiguous.
* **Chain of custody.** Attribution flows item → asset → license/exhibition.
  Missing provenance on one asset is reported per asset and never waived
  because sibling assets look complete.
* **Time-aware rights.** Licenses carry channel scopes and validity windows.
  A license update changes what *new* releases may use; an already-published
  exhibition keeps the rights snapshot it was published with.
* **Frozen releases.** Publishing records an immutable snapshot of each
  asset's item attribution, license, and cited research. Later
  re-attribution or re-licensing sets a review flag on unpublished
  exhibitions and never edits a published one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .errors import IntakeError, IntakeFormatError
from .models import (
    ACTIVE_EXHIBITION_STATES,
    EXHIBITION_STATES,
    PUBLIC_CHANNELS,
    REQUIRED_ASSET_PROVENANCE,
    RecordValidationError,
    iso,
    parse_date,
    utc_now,
)
from .records import (
    CollectionItem,
    DigitalAsset,
    ExhibitionItem,
    License,
    ProvenanceAmendment,
    RecordBase,
    ResearchNote,
    build_record,
)
from .rights import evaluate_license, select_effective_license


@dataclass
class _MergedAsset:
    """A carrier's effective view: original envelope plus later amendments.

    The original payload is never mutated; amendment fields are layered on top
    only for *current* completeness reporting.
    """

    asset: DigitalAsset
    amendments: list[ProvenanceAmendment] = field(default_factory=list)

    def effective(self, field_name: str):
        for amendment in reversed(self.amendments):
            value = getattr(amendment, field_name)
            if value:
                return value
        return getattr(self.asset, field_name)

    def is_complete(self) -> bool:
        return all(self.effective(name) for name in REQUIRED_ASSET_PROVENANCE)


class Workbench:
    """In-memory, append-only workbench over synthetic intake envelopes."""

    def __init__(self, clock=None):
        self._clock = clock or utc_now
        self._sources: dict[str, RecordBase] = {}
        self._items: list[CollectionItem] = []
        self._assets: list[DigitalAsset] = []
        self._notes: list[ResearchNote] = []
        self._licenses: list[License] = []
        self._exhibitions: list[ExhibitionItem] = []
        self._amendments: list[ProvenanceAmendment] = []
        self._snapshots: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._rejected: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ intake

    def ingest_document(self, document: Any) -> dict[str, Any]:
        """Ingest an envelope document, collecting errors instead of aborting."""

        if not isinstance(document, dict) or not isinstance(
            document.get("sources"), list
        ):
            raise IntakeFormatError("文档必须是包含 sources 列表的对象")
        accepted: list[str] = []
        rejected: list[dict[str, Any]] = []
        for raw in document["sources"]:
            result = self.ingest(raw)
            if result["accepted"]:
                accepted.append(result["source_id"])
            else:
                rejected.append(result)
        # Freezing of published entries happens lazily on first read (see
        # ``published_snapshot``), so a published record and the licence or
        # catalogue rows it relies on can arrive in any order.
        return {"accepted": accepted, "rejected": rejected}

    def ingest(self, source: Any) -> dict[str, Any]:
        try:
            record = build_record(source)
        except RecordValidationError as exc:
            payload = {
                "accepted": False,
                "source_id": (source or {}).get("source_id") if isinstance(source, dict) else None,
                "reason": str(exc),
            }
            self._rejected.append(payload)
            return payload

        if record.source_id in self._sources:
            payload = {
                "accepted": False,
                "source_id": record.source_id,
                "reason": "source_id 已存在；请使用新的来源编号重新提交更正",
            }
            self._rejected.append(payload)
            return payload

        if isinstance(record, ExhibitionItem) and record.state not in EXHIBITION_STATES:
            payload = {
                "accepted": False,
                "source_id": record.source_id,
                "reason": f"未知展览状态 {record.state!r}",
            }
            self._rejected.append(payload)
            return payload
        if isinstance(record, ExhibitionItem) and record.state not in ACTIVE_EXHIBITION_STATES and record.state != "published":
            payload = {
                "accepted": False,
                "source_id": record.source_id,
                "reason": f"展览状态 {record.state!r} 不能通过导入创建",
            }
            self._rejected.append(payload)
            return payload

        self._sources[record.source_id] = record
        if isinstance(record, CollectionItem):
            self._items.append(record)
        elif isinstance(record, DigitalAsset):
            self._assets.append(record)
        elif isinstance(record, ResearchNote):
            self._notes.append(record)
        elif isinstance(record, License):
            self._licenses.append(record)
        elif isinstance(record, ExhibitionItem):
            self._exhibitions.append(record)
        elif isinstance(record, ProvenanceAmendment):
            self._amendments.append(record)

        return {"accepted": True, "source_id": record.source_id, "kind": record.record_kind}

    def _freeze_pending(self) -> None:
        for item in self._exhibitions:
            if item.state != "published":
                continue
            key = (item.exhibition_ref, item.asset_ref, item.source_id)
            if key in self._snapshots:
                continue
            self._freeze(item)

    def ingest_file(self, path: str | Path) -> dict[str, Any]:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.ingest_document(document)

    # --------------------------------------------------------------- persistence

    def export_sources(self) -> dict[str, Any]:
        """Serialise every accepted envelope in intake order.

        This is the durable record of the workbench.  Derived views (snapshots,
        amendment merges) are rebuilt on load, so the export never contains
        state that could drift from the source envelopes.
        """

        return {
            "version": 1,
            "sources": [
                {
                    "source_id": record.source_id,
                    "record_kind": record.record_kind,
                    "collected_at": iso(record.collected_at),
                    "payload": record.payload,
                }
                for record in self._sources.values()
            ],
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.export_sources(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)

    @classmethod
    def load(cls, path: str | Path, clock=None) -> "Workbench":
        target = Path(path)
        workbench = cls(clock=clock)
        if not target.exists():
            return workbench
        workbench.ingest_file(target)
        return workbench

    # ------------------------------------------------------------------ lookups

    @property
    def rejected(self) -> list[dict[str, Any]]:
        return list(self._rejected)

    def accepted_source_ids(self) -> list[str]:
        return list(self._sources)

    def source(self, source_id: str) -> RecordBase | None:
        return self._sources.get(source_id)

    def asset(self, asset_ref: str) -> DigitalAsset | None:
        """Return the latest revision of one digital carrier.

        Multiple envelopes may describe the same ``asset_ref`` over time; the
        highest revision (then latest collection time) is the current one.
        """

        matches = [a for a in self._assets if a.asset_ref == asset_ref]
        if not matches:
            return None
        return max(matches, key=lambda a: (a.revision, a.collected_at))

    def current_assets(self) -> list[DigitalAsset]:
        by_ref: dict[str, DigitalAsset] = {}
        for asset in self._assets:
            existing = by_ref.get(asset.asset_ref)
            if existing is None or (asset.revision, asset.collected_at) > (
                existing.revision,
                existing.collected_at,
            ):
                by_ref[asset.asset_ref] = asset
        return sorted(by_ref.values(), key=lambda a: a.asset_ref)

    def item(self, item_ref: str) -> CollectionItem | None:
        """Return the current catalogue revision, honouring re-identification.

        If a catalogue record declares ``replaces_item_ref``, older references
        resolve forward to the surviving item so that a re-confirmed
        attribution is visible to every caller.
        """

        return self._latest_item_record(self._resolve_item_ref(item_ref) or item_ref)

    def _latest_item_record(self, item_ref: str) -> CollectionItem | None:
        matches = [it for it in self._items if it.item_ref == item_ref]
        if not matches:
            return None
        return max(matches, key=lambda it: (it.catalogue_revision, it.collected_at))

    def _resolve_item_ref(self, item_ref: str | None) -> str | None:
        """Follow "replaced by" links to the surviving catalogue ref.

        A later catalogue record carries ``replaces_item_ref`` naming the ref it
        supersedes, so the link is read in the reverse direction: find the
        record that replaces the current ref and continue from there.
        """

        if item_ref is None:
            return None
        seen: set[str] = set()
        ref = item_ref
        while ref not in seen:
            seen.add(ref)
            replacements = [
                it
                for it in self._items
                if it.replaces_item_ref == ref and it.item_ref != ref
            ]
            if not replacements:
                return ref
            ref = max(
                replacements, key=lambda it: (it.catalogue_revision, it.collected_at)
            ).item_ref
        return ref

    def item_history(self, item_ref: str) -> list[CollectionItem]:
        return sorted(
            (it for it in self._items if it.item_ref == item_ref),
            key=lambda it: (it.catalogue_revision, it.collected_at),
        )

    def assets_for_item(self, item_ref: str) -> list[DigitalAsset]:
        # Include refs that were merged into the surviving item, so assets that
        # still cite the old identifier are not silently orphaned.
        refs = {item_ref}
        canonical = self._resolve_item_ref(item_ref)
        if canonical:
            refs.add(canonical)
        for record in self._items:
            if record.replaces_item_ref in refs:
                refs.add(record.item_ref)
        return [a for a in self.current_assets() if a.item_ref in refs]

    def resolve_asset(
        self,
        file_name: str | None = None,
        sha256: str | None = None,
        asset_ref: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a scanned file to carrier(s) without ever guessing.

        Filenames repeat between captures. This returns every matching carrier
        and refuses to collapse them; the caller must supply a distinct key.
        """

        if asset_ref:
            found = [a for a in self.current_assets() if a.asset_ref == asset_ref]
        else:
            found = list(self.current_assets())
            if file_name is not None:
                found = [a for a in found if a.file_name == file_name]
            if sha256 is not None:
                normalized = sha256.lower()
                found = [a for a in found if a.sha256 == normalized]
            if file_name is None and sha256 is None:
                raise IntakeError("必须提供 asset_ref、file_name 或 sha256 之一")

        merged = [self._merged(a) for a in found]
        complete = [m for m in merged if m.is_complete()]
        if len(found) > 1:
            status = "ambiguous"
            note = "多个数字记录匹配；文件名或校验值不能作为唯一身份，请按 asset_ref 指定"
        elif len(found) == 1:
            status = "unique" if complete else "incomplete"
            note = None if complete else "该数字记录缺少来源信息"
        else:
            status = "not_found"
            note = "没有匹配的数字记录"

        return {
            "status": status,
            "note": note,
            "candidates": [self._asset_view(m) for m in merged],
        }

    def _merged(self, asset: DigitalAsset) -> _MergedAsset:
        amendments = sorted(
            (a for a in self._amendments if a.asset_ref == asset.asset_ref),
            key=lambda a: a.collected_at,
        )
        return _MergedAsset(asset=asset, amendments=amendments)

    # -------------------------------------------------------------- provenance

    def provenance(self, asset_ref: str) -> dict[str, Any]:
        asset = self.asset(asset_ref)
        if asset is None:
            raise IntakeError(f"未知数字记录 {asset_ref!r}")
        merged = self._merged(asset)
        missing = [
            name for name in REQUIRED_ASSET_PROVENANCE if not merged.effective(name)
        ]
        item_ref = merged.effective("item_ref")
        current_item = self.item(item_ref) if item_ref else None

        chain = []
        origin = asset.derived_from
        seen: set[str] = set()
        while origin and origin not in seen:
            seen.add(origin)
            parent = self.asset(origin)
            if parent is None:
                chain.append({"asset_ref": origin, "status": "missing"})
                break
            chain.append(
                {
                    "asset_ref": parent.asset_ref,
                    "source_id": parent.source_id,
                    "capture_kind": parent.capture_kind,
                    "collected_at": iso(parent.collected_at),
                    "status": "present",
                }
            )
            origin = parent.derived_from

        return {
            "asset_ref": asset.asset_ref,
            "source_id": asset.source_id,
            "item_ref": item_ref,
            "item_found": current_item is not None,
            "institution_ref": current_item.institution_ref if current_item else None,
            "capture_kind": merged.effective("capture_kind"),
            "source_team": merged.effective("source_team"),
            "sha256": merged.effective("sha256"),
            "file_name": asset.file_name,
            "collected_at": iso(asset.collected_at),
            "missing_fields": missing,
            "complete": not missing,
            "amendments": [
                {
                    "source_id": a.source_id,
                    "collected_at": iso(a.collected_at),
                    "amended_by": a.amended_by,
                    "note": a.note,
                }
                for a in merged.amendments
            ],
            "derivation_chain": list(reversed(chain)),
            "sibling_assets": [
                a.asset_ref
                for a in self.assets_for_item(item_ref)
                if a.asset_ref != asset.asset_ref
            ]
            if item_ref
            else [],
        }

    def completeness_report(self) -> dict[str, Any]:
        """Per-carrier completeness. No asset inherits another's clear state."""

        rows = []
        for asset in self.current_assets():
            merged = self._merged(asset)
            rows.append(
                {
                    "asset_ref": asset.asset_ref,
                    "item_ref": merged.effective("item_ref"),
                    "complete": merged.is_complete(),
                    "missing_fields": [
                        name
                        for name in REQUIRED_ASSET_PROVENANCE
                        if not merged.effective(name)
                    ],
                }
            )
        return {
            "assets": rows,
            "incomplete": [r for r in rows if not r["complete"]],
        }

    def amend_provenance(
        self,
        asset_ref: str,
        *,
        item_ref: str | None = None,
        source_team: str | None = None,
        capture_kind: str | None = None,
        sha256: str | None = None,
        note: str | None = None,
        amended_by: str | None = None,
    ) -> dict[str, Any]:
        if self.asset(asset_ref) is None:
            raise IntakeError(f"未知数字记录 {asset_ref!r}")
        counter = len(self._amendments) + 1
        source_id = f"amend-{asset_ref}-{counter}"
        while source_id in self._sources:
            counter += 1
            source_id = f"amend-{asset_ref}-{counter}"
        payload = {
            "asset_ref": asset_ref,
            "item_ref": item_ref,
            "source_team": source_team,
            "capture_kind": capture_kind,
            "sha256": sha256,
            "note": note,
            "amended_by": amended_by,
        }
        return self.ingest(
            {
                "source_id": source_id,
                "record_kind": "provenance_amendment",
                "collected_at": self._clock().isoformat(),
                "payload": payload,
            }
        )

    # ---------------------------------------------------------------- research

    def research_notes(self, item_ref: str | None = None, note_ref: str | None = None):
        candidates = self._notes
        if item_ref is not None:
            candidates = [n for n in candidates if n.item_ref == item_ref]
        if note_ref is not None:
            candidates = [n for n in candidates if n.note_ref == note_ref]
        return [self._note_view(n) for n in self._current_notes(candidates)]

    def _current_notes(self, notes: Iterable[ResearchNote]) -> list[ResearchNote]:
        notes = list(notes)

        def superseded_by_newer(note: ResearchNote) -> bool:
            for other in notes:
                if other.supersedes != note.note_ref:
                    continue
                if (other.revision, other.collected_at) > (
                    note.revision,
                    note.collected_at,
                ):
                    return True
            return False

        # One note_ref may have several revisions; keep the highest revision,
        # and drop any note that a newer correction supersedes.
        best: dict[str, ResearchNote] = {}
        for note in notes:
            if superseded_by_newer(note):
                continue
            existing = best.get(note.note_ref)
            if existing is None or (note.revision, note.collected_at) > (
                existing.revision,
                existing.collected_at,
            ):
                best[note.note_ref] = note
        return sorted(best.values(), key=lambda n: (n.item_ref, n.note_ref))

    @staticmethod
    def _note_view(note: ResearchNote) -> dict[str, Any]:
        return {
            "note_ref": note.note_ref,
            "item_ref": note.item_ref,
            "note_kind": note.note_kind,
            "revision": note.revision,
            "review_state": note.review_state,
            "author": note.author,
            "body": note.body,
            "source_id": note.source_id,
            "collected_at": iso(note.collected_at),
            "interpretation": note.note_kind == "interpretation",
        }

    # ------------------------------------------------------------------ rights

    def rights_for_asset(self, asset_ref: str, channel: str, on: date | str | None = None):
        """Judge one carrier against one channel.

        A missing ``on`` means "today" and is echoed back in ``evaluated_on``
        so the caller can see which day the answer is about.
        """

        evaluation_date = self._coerce_date(on) or self._clock().date()
        license = select_effective_license(self._licenses, asset_ref, evaluation_date)
        return evaluate_license(license, channel, evaluation_date)

    def check_release(
        self,
        asset_ref: str,
        channel: str,
        on: date | str | None = None,
    ) -> dict[str, Any]:
        """Full readiness check for putting one carrier on one channel."""

        evaluation_date = self._coerce_date(on) or self._clock().date()
        merged = self._merged(self.asset(asset_ref)) if self.asset(asset_ref) else None
        if merged is None:
            return {
                "asset_ref": asset_ref,
                "channel": channel,
                "evaluated_on": evaluation_date.isoformat(),
                "ready": False,
                "reasons": [f"未知数字记录 {asset_ref!r}"],
                "rights": None,
                "provenance_complete": False,
            }
        reasons: list[str] = []
        if not merged.is_complete():
            reasons.append("来源信息不完整：" + ", ".join(
                name for name in REQUIRED_ASSET_PROVENANCE if not merged.effective(name)
            ))
        item_ref = merged.effective("item_ref")
        if item_ref and self.item(item_ref) is None:
            reasons.append(f"关联藏品 {item_ref!r} 不在现行目录中")
        license = select_effective_license(self._licenses, asset_ref, evaluation_date)
        decision = evaluate_license(license, channel, evaluation_date)
        if not decision.allowed:
            reasons.append(decision.reason)
        return {
            "asset_ref": asset_ref,
            "channel": channel,
            "evaluated_on": evaluation_date.isoformat() if evaluation_date else None,
            "ready": not reasons,
            "reasons": reasons,
            "rights": decision.as_dict(),
            "provenance_complete": merged.is_complete(),
        }

    # ------------------------------------------------------------- exhibitions

    def exhibition_item(self, exhibition_ref: str, asset_ref: str) -> ExhibitionItem | None:
        matches = [
            e
            for e in self._exhibitions
            if e.exhibition_ref == exhibition_ref and e.asset_ref == asset_ref
        ]
        if not matches:
            return None
        return max(matches, key=lambda e: (e.revision, e.collected_at))

    def exhibitions(self, exhibition_ref: str) -> list[ExhibitionItem]:
        return [
            item
            for item in self._exhibitions
            if item.exhibition_ref == exhibition_ref
        ]

    def set_exhibition_state(
        self,
        exhibition_ref: str,
        asset_ref: str,
        state: str,
        *,
        channel: str | None = None,
        on: date | str | None = None,
    ) -> dict[str, Any]:
        """Advance an exhibition item; publishing validates and freezes."""

        if state not in EXHIBITION_STATES:
            raise IntakeError(f"未知展览状态 {state!r}")
        current = self.exhibition_item(exhibition_ref, asset_ref)
        if current is None:
            raise IntakeError(f"展览 {exhibition_ref!r} 中没有素材 {asset_ref!r}")
        if current.state == "published":
            raise IntakeError("已发布展览条目不可修改；请建立新的展览版本")
        if current.state == "withdrawn" and state != "withdrawn":
            raise IntakeError("已撤下的展览条目不能恢复")

        evaluation_date = self._coerce_date(on)
        effective_license_ref = None
        if state == "published":
            # Publishing must judge rights as of a concrete day; default to the
            # workbench clock rather than leaving the evaluation undated.
            if evaluation_date is None:
                evaluation_date = self._clock().date()
            release_channel = channel or current.channel or ""
            result = self.check_release(asset_ref, release_channel, evaluation_date)
            if not result["ready"]:
                return {"published": False, "blocked": result}
            # A license update governs content that is still being published,
            # so cite the license actually in force today, not the one the
            # draft happened to reference when it was written.
            effective = select_effective_license(
                self._licenses, asset_ref, evaluation_date
            )
            if effective is not None:
                effective_license_ref = effective.license_ref

        new_item = ExhibitionItem(
            source_id=f"{current.source_id}-state-{state}-{current.revision + 1}",
            collected_at=self._clock(),
            payload={**current.payload, "state": state},
            exhibition_ref=exhibition_ref,
            asset_ref=asset_ref,
            state=state,
            license_ref=effective_license_ref or current.license_ref,
            item_ref=current.item_ref,
            channel=channel or current.channel,
            title=current.title,
            note_refs=current.note_refs,
            revision=current.revision + 1,
            published_at=self._clock() if state == "published" else None,
        )
        self._sources[new_item.source_id] = new_item
        self._exhibitions.append(new_item)
        if state == "published":
            self._freeze(new_item, on=evaluation_date)
        return {"published": state == "published", "item": self._exhibition_view(new_item)}

    def _freeze(self, item: ExhibitionItem, on: date | None = None) -> None:
        """Capture the evidence an exhibition entry was published with.

        The snapshot is written once and never recomputed, so later
        re-attribution or re-licensing cannot rewrite what a past release
        actually used.
        """

        evaluation_date = on or item.collected_at.date()
        merged = self._merged(self.asset(item.asset_ref)) if self.asset(item.asset_ref) else None
        # Prefer the license the entry itself recorded; that is the grant the
        # exhibition relied on at the time.
        license = self._license_by_ref(item.license_ref) if item.license_ref else None
        if license is None:
            license = select_effective_license(
                self._licenses, item.asset_ref, evaluation_date
            )
        decision = evaluate_license(license, item.channel, evaluation_date)
        item_ref = (
            item.item_ref
            or (merged.effective("item_ref") if merged else None)
        )
        catalogue = self.item(item_ref) if item_ref else None
        recorded_revision = item.recorded_catalogue_revision
        if recorded_revision is not None:
            # Imported legacy record: trust the revision it recorded, even if
            # the catalogue has since moved on.
            matching = [
                it
                for it in self._items
                if it.item_ref == item_ref
                and it.catalogue_revision == recorded_revision
            ]
            if matching:
                catalogue = max(matching, key=lambda it: it.collected_at)
        notes = []
        for note_ref in item.note_refs:
            for note in self._current_notes(
                [n for n in self._notes if n.note_ref == note_ref]
            ):
                notes.append(self._note_view(note))
        self._snapshots[(item.exhibition_ref, item.asset_ref, item.source_id)] = {
            "exhibition_ref": item.exhibition_ref,
            "asset_ref": item.asset_ref,
            "channel": item.channel,
            "published_at": iso(item.published_at or item.collected_at),
            "evaluated_on": evaluation_date.isoformat(),
            "item_ref": item_ref,
            "item_title": catalogue.title if catalogue else None,
            "catalogue_revision": catalogue.catalogue_revision if catalogue else None,
            "attribution": merged.effective("source_team") if merged else None,
            "file_name": merged.asset.file_name if merged else None,
            "license": decision.as_dict(),
            "notes": notes,
        }

    def published_snapshot(self, exhibition_ref: str, asset_ref: str) -> dict[str, Any] | None:
        published = [
            e
            for e in self.exhibitions(exhibition_ref)
            if e.asset_ref == asset_ref and e.state == "published"
        ]
        if not published:
            return None
        latest = max(published, key=lambda e: (e.revision, e.collected_at))
        key = (exhibition_ref, asset_ref, latest.source_id)
        if key not in self._snapshots:
            # Lazy freeze covers published rows that arrived by import, and
            # lets the license/catalogue rows land in any order.
            self._freeze(
                latest,
                on=latest.published_at.date()
                if latest.published_at is not None
                else latest.collected_at.date(),
            )
        return self._snapshots.get(key)

    def recheck_exhibition(self, exhibition_ref: str, on: date | str | None = None):
        """Re-evaluate active entries after items, licenses or assets changed."""

        evaluation_date = self._coerce_date(on) or self._clock().date()
        results = []
        for item in self._current_exhibitions(exhibition_ref):
            if item.state == "published":
                results.append(
                    {
                        "asset_ref": item.asset_ref,
                        "state": item.state,
                        "action": "frozen",
                        "note": "已发布展览保留发布时的资料快照，不随更新回改",
                        "snapshot": self.published_snapshot(
                            exhibition_ref, item.asset_ref
                        ),
                    }
                )
                continue
            result = self.check_release(
                item.asset_ref, item.channel or "", evaluation_date
            )
            drift = self._attribution_drift(item, evaluation_date)
            if drift:
                result["reasons"].extend(drift)
                result["ready"] = False
            result.update(
                {
                    "state": item.state,
                    "action": "recheck",
                    "requires_attention": not result["ready"],
                    "attribution_drift": drift,
                }
            )
            results.append(result)
        return results

    def _attribution_drift(
        self, item: ExhibitionItem, evaluation_date: date | None = None
    ) -> list[str]:
        """Differences between what an active entry recorded and current facts."""

        on = evaluation_date or self._clock().date()
        drift: list[str] = []
        asset = self.asset(item.asset_ref)
        if asset is None:
            return [f"数字记录 {item.asset_ref!r} 已不在馆藏中"]
        merged = self._merged(asset)
        current_item_ref = merged.effective("item_ref")
        if item.item_ref and current_item_ref and item.item_ref != current_item_ref:
            drift.append(
                f"藏品归属已重新确认：登记为 {item.item_ref!r}，现为 {current_item_ref!r}"
            )
        current_license = select_effective_license(
            self._licenses, item.asset_ref, on
        )
        if (
            item.license_ref
            and current_license is not None
            and item.license_ref != current_license.license_ref
        ):
            drift.append(
                f"许可已更新：登记为 {item.license_ref!r}，现行为 {current_license.license_ref!r}"
            )
        return drift

    def _license_by_ref(self, license_ref: str) -> License | None:
        matches = [lic for lic in self._licenses if lic.license_ref == license_ref]
        if not matches:
            return None
        return max(matches, key=lambda lic: (lic.revision, lic.collected_at))

    def _current_exhibitions(self, exhibition_ref: str) -> list[ExhibitionItem]:
        entries: dict[str, ExhibitionItem] = {}
        for item in self._exhibitions:
            if item.exhibition_ref != exhibition_ref:
                continue
            existing = entries.get(item.asset_ref)
            if existing is None or (item.revision, item.collected_at) > (
                existing.revision,
                existing.collected_at,
            ):
                entries[item.asset_ref] = item
        return list(entries.values())

    # ---------------------------------------------------------------- reports

    def asset_report(self, asset_ref: str) -> dict[str, Any]:
        provenance = self.provenance(asset_ref)
        licenses = sorted(
            (lic for lic in self._licenses if lic.asset_ref == asset_ref),
            key=lambda lic: (lic.revision, lic.collected_at),
        )
        exhibitions = [
            self._exhibition_view(e)
            for e in self._exhibitions
            if e.asset_ref == asset_ref
        ]
        return {
            "provenance": provenance,
            "licenses": [self._license_view(lic) for lic in licenses],
            "exhibitions": exhibitions,
        }

    def rights_matrix(self, asset_ref: str, on: date | str | None = None):
        evaluation_date = self._coerce_date(on) or self._clock().date()
        channels = sorted(
            {
                use
                for lic in self._licenses
                if lic.asset_ref == asset_ref
                for use in lic.allowed_use
            }
            | set(PUBLIC_CHANNELS)
        )
        return {
            channel: self.rights_for_asset(asset_ref, channel, evaluation_date).as_dict()
            for channel in channels
        }

    def _asset_view(self, merged: _MergedAsset) -> dict[str, Any]:
        return {
            "asset_ref": merged.asset.asset_ref,
            "source_id": merged.asset.source_id,
            "item_ref": merged.effective("item_ref"),
            "capture_kind": merged.effective("capture_kind"),
            "source_team": merged.effective("source_team"),
            "sha256": merged.effective("sha256"),
            "file_name": merged.asset.file_name,
            "angle": merged.asset.angle,
            "precision_note": merged.asset.precision_note,
            "complete": merged.is_complete(),
        }

    @staticmethod
    def _license_view(license: License) -> dict[str, Any]:
        return {
            "license_ref": license.license_ref,
            "asset_ref": license.asset_ref,
            "revision": license.revision,
            "allowed_use": list(license.allowed_use),
            "valid_from": license.valid_from.isoformat() if license.valid_from else None,
            "valid_until": license.valid_until.isoformat() if license.valid_until else None,
            "supersedes": license.supersedes,
            "revoked": license.revoked,
            "restrictions": license.restrictions,
            "attribution": license.attribution,
            "source_id": license.source_id,
        }

    @staticmethod
    def _exhibition_view(item: ExhibitionItem) -> dict[str, Any]:
        return {
            "exhibition_ref": item.exhibition_ref,
            "asset_ref": item.asset_ref,
            "state": item.state,
            "channel": item.channel,
            "license_ref": item.license_ref,
            "item_ref": item.item_ref,
            "title": item.title,
            "revision": item.revision,
            "source_id": item.source_id,
            "collected_at": iso(item.collected_at),
        }

    @staticmethod
    def _coerce_date(value: date | str | None) -> date | None:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return parse_date(value, "date")
            except RecordValidationError as exc:
                raise IntakeError(str(exc)) from exc
        raise IntakeError("日期必须是 date 或 ISO 日期字符串")
