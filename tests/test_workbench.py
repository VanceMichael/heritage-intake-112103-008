import json
import unittest
from datetime import date
from pathlib import Path

from digital_intake import IntakeError, Workbench

FIXTURES = Path(__file__).parents[1] / "fixtures"


def load(name="scenario.json"):
    path = FIXTURES / name
    document = json.loads(path.read_text(encoding="utf-8"))
    workbench = Workbench()
    result = workbench.ingest_document(document)
    return workbench, result


def envelope(source_id, kind, payload, collected_at="2026-07-01T00:00:00+08:00"):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected_at,
        "payload": payload,
    }


class IntakeTests(unittest.TestCase):
    def test_all_fixture_envelopes_are_accepted(self):
        _, result = load()
        self.assertEqual(result["rejected"], [])
        self.assertGreaterEqual(len(result["accepted"]), 5)

    def test_duplicate_source_id_is_rejected_not_merged(self):
        workbench, _ = load()
        duplicate = envelope(
            "scan-108-a",
            "digital_asset",
            {
                "asset_ref": "ASSET-108-A",
                "item_ref": "ITEM-SYN-108",
                "capture_kind": "photogrammetry",
            },
        )
        result = workbench.ingest(duplicate)
        self.assertFalse(result["accepted"])
        self.assertIn("source_id", result["reason"])

    def test_malformed_envelope_is_reported_without_stopping_intake(self):
        workbench = Workbench()
        result = workbench.ingest_document(
            {
                "sources": [
                    {"source_id": "bad", "record_kind": "digital_asset"},
                    envelope(
                        "good",
                        "collection_item",
                        {"item_ref": "ITEM-X", "catalogue_revision": 1},
                    ),
                ]
            }
        )
        self.assertEqual(result["accepted"], ["good"])
        self.assertEqual(len(result["rejected"]), 1)

    def test_missing_payload_field_is_rejected_with_reason(self):
        workbench = Workbench()
        result = workbench.ingest(
            envelope("x", "digital_asset", {"asset_ref": "A", "item_ref": "I"})
        )
        self.assertFalse(result["accepted"])
        self.assertIn("capture_kind", result["reason"])

    def test_non_object_document_is_an_intake_format_error(self):
        with self.assertRaises(IntakeError):
            Workbench().ingest_document([])


class CarrierIdentityTests(unittest.TestCase):
    def test_same_filename_resolves_to_multiple_carriers(self):
        workbench, _ = load()
        result = workbench.resolve_asset(file_name="object_front.jpg")
        self.assertEqual(result["status"], "ambiguous")
        refs = {c["asset_ref"] for c in result["candidates"]}
        self.assertEqual(refs, {"ASSET-108-A", "ASSET-108-B", "ASSET-108-C"})

    def test_distinct_assets_on_one_item_are_never_merged(self):
        workbench, _ = load()
        assets = workbench.assets_for_item("ITEM-SYN-108")
        self.assertEqual(len(assets), 3)
        self.assertEqual(len({a.asset_ref for a in assets}), 3)

    def test_asset_ref_resolves_unambiguously(self):
        workbench, _ = load()
        result = workbench.resolve_asset(asset_ref="ASSET-108-C")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["candidates"]), 1)

    def test_resolve_requires_a_key(self):
        workbench, _ = load()
        with self.assertRaises(IntakeError):
            workbench.resolve_asset()

    def test_same_checksum_alone_is_not_identity(self):
        workbench = Workbench()
        for index in (1, 2):
            workbench.ingest(
                envelope(
                    f"scan-{index}",
                    "digital_asset",
                    {
                        "asset_ref": f"ASSET-{index}",
                        "item_ref": "ITEM-X",
                        "capture_kind": "photogrammetry",
                        "source_team": "TEAM",
                        "sha256": "a" * 64,
                    },
                )
            )
        result = workbench.resolve_asset(sha256="a" * 64)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)


class ProvenanceTests(unittest.TestCase):
    def test_incomplete_asset_is_not_cleared_by_complete_siblings(self):
        workbench, _ = load()
        report = workbench.completeness_report()
        incomplete = {row["asset_ref"] for row in report["incomplete"]}
        self.assertIn("ASSET-108-C", incomplete)
        # Its siblings are complete, but that must not leak across carriers.
        self.assertNotIn("ASSET-108-A", incomplete)

    def test_release_blocked_for_incomplete_asset(self):
        workbench, _ = load()
        # ASSET-108-C has an online exhibition license but missing provenance.
        result = workbench.check_release(
            "ASSET-108-C", "online-exhibition", date(2026, 11, 1)
        )
        self.assertFalse(result["ready"])
        self.assertTrue(
            any("来源信息不完整" in reason for reason in result["reasons"])
        )

    def test_amendments_fill_gaps_without_rewriting_original(self):
        workbench, _ = load()
        before = workbench.provenance("ASSET-108-C")
        self.assertFalse(before["complete"])
        result = workbench.amend_provenance(
            "ASSET-108-C",
            source_team="TEAM-SYN-1",
            sha256="b" * 64,
            note="依据采集日志补录",
            amended_by="CATALOGUER-SYN-1",
        )
        self.assertTrue(result["accepted"])
        after = workbench.provenance("ASSET-108-C")
        self.assertTrue(after["complete"])
        self.assertEqual(after["source_team"], "TEAM-SYN-1")
        self.assertEqual(len(after["amendments"]), 1)
        # Original envelope payload is untouched.
        self.assertIsNone(workbench.asset("ASSET-108-C").source_team)

    def test_derivation_chain_traces_origin(self):
        workbench = Workbench()
        workbench.ingest(
            envelope(
                "orig",
                "digital_asset",
                {
                    "asset_ref": "ASSET-ORIG",
                    "item_ref": "ITEM-X",
                    "capture_kind": "photogrammetry",
                    "source_team": "TEAM",
                    "sha256": "c" * 64,
                },
            )
        )
        workbench.ingest(
            envelope(
                "derived",
                "digital_asset",
                {
                    "asset_ref": "ASSET-DERIVED",
                    "item_ref": "ITEM-X",
                    "capture_kind": "derivative",
                    "source_team": "TEAM",
                    "sha256": "d" * 64,
                    "derived_from": "ASSET-ORIG",
                },
            )
        )
        report = workbench.provenance("ASSET-DERIVED")
        self.assertEqual(report["derivation_chain"][0]["asset_ref"], "ASSET-ORIG")
        self.assertEqual(report["derivation_chain"][0]["status"], "present")

    def test_missing_origin_is_reported(self):
        workbench = Workbench()
        workbench.ingest(
            envelope(
                "derived",
                "digital_asset",
                {
                    "asset_ref": "ASSET-DERIVED",
                    "item_ref": "ITEM-X",
                    "capture_kind": "derivative",
                    "source_team": "TEAM",
                    "sha256": "d" * 64,
                    "derived_from": "ASSET-UNKNOWN",
                },
            )
        )
        report = workbench.provenance("ASSET-DERIVED")
        self.assertEqual(report["derivation_chain"][0]["status"], "missing")


class RightsTests(unittest.TestCase):
    def test_research_only_license_blocks_online_exhibition(self):
        workbench, _ = load()
        decision = workbench.rights_for_asset(
            "ASSET-108-B", "online-exhibition", date(2026, 11, 1)
        )
        self.assertFalse(decision.allowed)
        self.assertIn("online-exhibition", decision.reason)

    def test_expired_license_blocks_release(self):
        workbench, _ = load()
        decision = workbench.rights_for_asset(
            "ASSET-108-C", "online-exhibition", date(2026, 12, 1)
        )
        self.assertFalse(decision.allowed)
        self.assertIn("到期", decision.reason)

    def test_license_valid_on_boundary_date(self):
        workbench, _ = load()
        decision = workbench.rights_for_asset(
            "ASSET-108-A", "online-exhibition", date(2026, 12, 31)
        )
        self.assertTrue(decision.allowed)

    def test_license_not_yet_valid_blocks_release(self):
        workbench, _ = load()
        decision = workbench.rights_for_asset(
            "ASSET-108-A", "research", date(2026, 1, 1)
        )
        self.assertFalse(decision.allowed)

    def test_channel_outside_scope_is_refused(self):
        workbench, _ = load()
        decision = workbench.rights_for_asset(
            "ASSET-108-A", "social-media", date(2026, 11, 1)
        )
        self.assertFalse(decision.allowed)

    def test_unknown_asset_is_not_releasable(self):
        workbench, _ = load()
        result = workbench.check_release("ASSET-NOPE", "research", date(2026, 11, 1))
        self.assertFalse(result["ready"])

    def test_rights_matrix_lists_public_channels(self):
        workbench, _ = load()
        matrix = workbench.rights_matrix("ASSET-108-A", date(2026, 11, 1))
        self.assertTrue(matrix["online-exhibition"]["allowed"])
        self.assertFalse(matrix["social-media"]["allowed"])

    def test_new_license_revision_supersedes_old(self):
        workbench, _ = load()
        workbench.ingest(
            envelope(
                "rights-44-r2",
                "license",
                {
                    "license_ref": "LIC-44-R2",
                    "asset_ref": "ASSET-108-A",
                    "allowed_use": ["research"],
                    "valid_from": "2026-11-01",
                    "valid_until": "2027-06-30",
                    "revision": 2,
                    "supersedes": "LIC-44",
                },
            )
        )
        decision = workbench.rights_for_asset(
            "ASSET-108-A", "online-exhibition", date(2026, 11, 20)
        )
        self.assertFalse(decision.allowed)

    def test_revoked_license_is_refused(self):
        workbench, _ = load()
        workbench.ingest(
            envelope(
                "revoke-44",
                "license",
                {
                    "license_ref": "LIC-44-REV",
                    "asset_ref": "ASSET-108-A",
                    "allowed_use": ["online-exhibition"],
                    "revision": 3,
                    "supersedes": "LIC-44",
                    "revoked": True,
                },
            )
        )
        decision = workbench.rights_for_asset(
            "ASSET-108-A", "online-exhibition", date(2026, 11, 1)
        )
        self.assertFalse(decision.allowed)
        self.assertIn("撤销", decision.reason)


class ResearchTests(unittest.TestCase):
    def test_observation_and_interpretation_are_distinguishable(self):
        workbench, _ = load()
        notes = workbench.research_notes(item_ref="ITEM-SYN-108")
        kinds = {note["note_ref"]: note["note_kind"] for note in notes}
        self.assertEqual(kinds["NOTE-27"], "interpretation")
        self.assertEqual(kinds["NOTE-28"], "observation")
        interpretation = next(n for n in notes if n["note_ref"] == "NOTE-27")
        self.assertTrue(interpretation["interpretation"])

    def test_corrected_note_supersedes_earlier_revision(self):
        workbench, _ = load()
        workbench.ingest(
            envelope(
                "research-27-r3",
                "research_note",
                {
                    "note_ref": "NOTE-27",
                    "item_ref": "ITEM-SYN-108",
                    "note_kind": "interpretation",
                    "revision": 3,
                    "review_state": "checked",
                    "body": "复核后更正的解释。",
                    "supersedes": "NOTE-27",
                },
            )
        )
        notes = workbench.research_notes(note_ref="NOTE-27")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["revision"], 3)
        self.assertEqual(notes[0]["body"], "复核后更正的解释。")

    def test_unknown_note_kind_is_rejected(self):
        workbench, _ = load()
        result = workbench.ingest(
            envelope(
                "bad-note",
                "research_note",
                {"note_ref": "NOTE-X", "note_kind": "guess"},
            )
        )
        self.assertFalse(result["accepted"])


class ExhibitionLifecycleTests(unittest.TestCase):
    def test_draft_is_flagged_when_item_attribution_changes(self):
        workbench, _ = load()
        # Re-confirm the item this asset belongs to.
        workbench.ingest(
            envelope(
                "reattribute",
                "digital_asset",
                {
                    "asset_ref": "ASSET-108-A",
                    "item_ref": "ITEM-SYN-109",
                    "capture_kind": "photogrammetry",
                    "source_team": "TEAM-SYN-1",
                    "sha256": "7cc9f49113279091b7f22a1ac662fedacb83fb18f88c48364147201a236b99b1",
                    "revision": 2,
                },
            )
        )
        results = workbench.recheck_exhibition("EX-SYN-9", date(2026, 11, 20))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["requires_attention"])
        self.assertTrue(results[0]["attribution_drift"])

    def test_draft_is_flagged_when_license_updates(self):
        workbench, _ = load()
        workbench.ingest(
            envelope(
                "rights-new",
                "license",
                {
                    "license_ref": "LIC-44-NEW",
                    "asset_ref": "ASSET-108-A",
                    "allowed_use": ["research"],
                    "revision": 2,
                    "supersedes": "LIC-44",
                    "valid_until": "2027-12-31",
                },
            )
        )
        results = workbench.recheck_exhibition("EX-SYN-9", date(2026, 11, 1))
        self.assertTrue(results[0]["requires_attention"])
        self.assertTrue(results[0]["attribution_drift"])

    def test_publishing_freezes_snapshot_and_later_changes_do_not_alter_it(self):
        workbench, _ = load()
        published = workbench.set_exhibition_state(
            "EX-SYN-9",
            "ASSET-108-A",
            "published",
            channel="online-exhibition",
            on=date(2026, 10, 1),
        )
        self.assertTrue(published["published"])
        snapshot = workbench.published_snapshot("EX-SYN-9", "ASSET-108-A")
        self.assertEqual(snapshot["item_ref"], "ITEM-SYN-108")
        self.assertEqual(snapshot["catalogue_revision"], 5)
        self.assertEqual(snapshot["license"]["license_ref"], "LIC-44")

        # Re-attribute the asset and change the license afterwards.
        workbench.ingest(
            envelope(
                "reattribute-late",
                "digital_asset",
                {
                    "asset_ref": "ASSET-108-A",
                    "item_ref": "ITEM-SYN-109",
                    "capture_kind": "photogrammetry",
                    "source_team": "TEAM-SYN-1",
                    "sha256": "7cc9f49113279091b7f22a1ac662fedacb83fb18f88c48364147201a236b99b1",
                    "revision": 2,
                },
            )
        )
        frozen = workbench.published_snapshot("EX-SYN-9", "ASSET-108-A")
        self.assertEqual(frozen, snapshot)

        results = workbench.recheck_exhibition("EX-SYN-9", date(2026, 12, 1))
        self.assertEqual(results[0]["action"], "frozen")
        self.assertEqual(results[0]["snapshot"], snapshot)

    def test_publishing_is_blocked_when_rights_expired(self):
        workbench, _ = load()
        result = workbench.set_exhibition_state(
            "EX-SYN-9",
            "ASSET-108-A",
            "published",
            channel="online-exhibition",
            on=date(2027, 1, 10),
        )
        self.assertFalse(result["published"])
        self.assertIn("blocked", result)

    def test_publishing_is_blocked_without_channel(self):
        workbench, _ = load()
        workbench.ingest(
            envelope(
                "exhibit-nochannel",
                "exhibition_item",
                {
                    "exhibition_ref": "EX-NOCHAN",
                    "asset_ref": "ASSET-108-A",
                    "state": "draft",
                    "license_ref": "LIC-44",
                },
            )
        )
        result = workbench.set_exhibition_state(
            "EX-NOCHAN", "ASSET-108-A", "published", on=date(2026, 11, 1)
        )
        self.assertFalse(result["published"])

    def test_published_entry_cannot_be_edited_in_place(self):
        workbench, _ = load()
        workbench.set_exhibition_state(
            "EX-SYN-9",
            "ASSET-108-A",
            "published",
            channel="online-exhibition",
            on=date(2026, 10, 1),
        )
        with self.assertRaises(IntakeError):
            workbench.set_exhibition_state(
                "EX-SYN-9", "ASSET-108-A", "draft", on=date(2026, 10, 2)
            )

    def test_research_only_asset_cannot_be_published_online(self):
        workbench, _ = load()
        result = workbench.set_exhibition_state(
            "EX-SYN-10",
            "ASSET-108-B",
            "published",
            channel="online-exhibition",
            on=date(2026, 10, 1),
        )
        self.assertFalse(result["published"])

    def test_failed_publish_attempt_does_not_record_snapshot(self):
        workbench, _ = load()
        workbench.set_exhibition_state(
            "EX-SYN-10",
            "ASSET-108-B",
            "published",
            channel="online-exhibition",
            on=date(2026, 10, 1),
        )
        self.assertIsNone(workbench.published_snapshot("EX-SYN-10", "ASSET-108-B"))

    def test_publishing_blocked_when_license_narrowed_after_draft(self):
        workbench, _ = load()
        # A newer, narrower license supersedes LIC-44 before this entry publishes.
        workbench.ingest(
            envelope(
                "rights-narrow",
                "license",
                {
                    "license_ref": "LIC-44-NARROW",
                    "asset_ref": "ASSET-108-A",
                    "allowed_use": ["research"],
                    "revision": 2,
                    "supersedes": "LIC-44",
                },
            )
        )
        result = workbench.set_exhibition_state(
            "EX-SYN-9",
            "ASSET-108-A",
            "published",
            channel="online-exhibition",
            on=date(2026, 11, 1),
        )
        self.assertFalse(result["published"])
        self.assertIn("online-exhibition", " ".join(result["blocked"]["reasons"]))
        self.assertIsNone(workbench.published_snapshot("EX-SYN-9", "ASSET-108-A"))

    def test_publish_uses_current_license_not_stale_reference(self):
        workbench, _ = load()
        # Give the item a broader license, then update the draft's reference.
        workbench.ingest(
            envelope(
                "rights-broad",
                "license",
                {
                    "license_ref": "LIC-44-BROAD",
                    "asset_ref": "ASSET-108-A",
                    "allowed_use": ["online-exhibition"],
                    "revision": 2,
                    "supersedes": "LIC-44",
                    "valid_until": "2027-12-31",
                },
            )
        )
        published = workbench.set_exhibition_state(
            "EX-SYN-9",
            "ASSET-108-A",
            "published",
            channel="online-exhibition",
            on=date(2026, 11, 1),
        )
        self.assertTrue(published["published"])
        snapshot = workbench.published_snapshot("EX-SYN-9", "ASSET-108-A")
        # The snapshot cites the license in force at publication time.
        self.assertEqual(snapshot["license"]["license_ref"], "LIC-44-BROAD")

    def test_imported_published_entry_freezes_regardless_of_batch_order(self):
        # The published row arrives before the license and catalogue rows.
        workbench = Workbench()
        workbench.ingest(
            envelope(
                "exhibit-legacy",
                "exhibition_item",
                {
                    "exhibition_ref": "EX-LEGACY",
                    "asset_ref": "ASSET-LEGACY",
                    "item_ref": "ITEM-LEGACY",
                    "state": "published",
                    "channel": "online-exhibition",
                    "license_ref": "LIC-LEGACY",
                    "published_at": "2026-05-01T12:00:00+08:00",
                },
            )
        )
        workbench.ingest(
            envelope(
                "scan-legacy",
                "digital_asset",
                {
                    "asset_ref": "ASSET-LEGACY",
                    "item_ref": "ITEM-LEGACY",
                    "capture_kind": "photograph",
                    "source_team": "TEAM-LEGACY",
                    "sha256": "e" * 64,
                },
            )
        )
        workbench.ingest(
            envelope(
                "catalog-legacy",
                "collection_item",
                {
                    "item_ref": "ITEM-LEGACY",
                    "catalogue_revision": 2,
                    "title": "旧藏（虚构）",
                },
            )
        )
        workbench.ingest(
            envelope(
                "rights-legacy",
                "license",
                {
                    "license_ref": "LIC-LEGACY",
                    "asset_ref": "ASSET-LEGACY",
                    "allowed_use": ["online-exhibition"],
                    "valid_until": "2026-06-01",
                },
            )
        )
        snapshot = workbench.published_snapshot("EX-LEGACY", "ASSET-LEGACY")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["license"]["license_ref"], "LIC-LEGACY")
        self.assertTrue(snapshot["license"]["allowed"])
        self.assertEqual(snapshot["catalogue_revision"], 2)

    def test_freeze_prefers_license_recorded_by_exhibition(self):
        workbench, _ = load()
        published = workbench.set_exhibition_state(
            "EX-SYN-9",
            "ASSET-108-A",
            "published",
            channel="online-exhibition",
            on=date(2026, 10, 1),
        )
        self.assertTrue(published["published"])
        # A newer license arrives afterwards.
        workbench.ingest(
            envelope(
                "rights-later",
                "license",
                {
                    "license_ref": "LIC-44-LATER",
                    "asset_ref": "ASSET-108-A",
                    "allowed_use": ["research"],
                    "revision": 9,
                    "supersedes": "LIC-44",
                },
            )
        )
        snapshot = workbench.published_snapshot("EX-SYN-9", "ASSET-108-A")
        self.assertEqual(snapshot["license"]["license_ref"], "LIC-44")
        self.assertTrue(snapshot["license"]["allowed"])


class PersistenceTests(unittest.TestCase):
    def test_save_and_load_roundtrip_preserves_structure(self):
        import tempfile

        workbench, _ = load()
        workbench.set_exhibition_state(
            "EX-SYN-9",
            "ASSET-108-A",
            "published",
            channel="online-exhibition",
            on=date(2026, 10, 1),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workbench.json"
            workbench.save(path)
            reloaded = Workbench.load(path)
        self.assertEqual(
            reloaded.completeness_report(), workbench.completeness_report()
        )
        snapshot = reloaded.published_snapshot("EX-SYN-9", "ASSET-108-A")
        self.assertEqual(snapshot["item_ref"], "ITEM-SYN-108")
        self.assertEqual(len(reloaded.accepted_source_ids()), len(workbench.accepted_source_ids()))

    def test_load_missing_path_yields_empty_workbench(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            workbench = Workbench.load(Path(temp) / "absent.json")
        self.assertEqual(workbench.accepted_source_ids(), [])

    def test_continuous_intake_accumulates_into_saved_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workbench.json"
            workbench = Workbench.load(path)
            workbench.ingest_file(FIXTURES / "sources.json")
            workbench.save(path)
            workbench = Workbench.load(path)
            workbench.ingest_file(FIXTURES / "scenario.json")
            workbench.save(path)
            workbench = Workbench.load(path)
        self.assertIn("scan-top-3", workbench.accepted_source_ids())
        self.assertIn("scan-108-a", workbench.accepted_source_ids())


class ItemRevisionTests(unittest.TestCase):
    def test_current_item_uses_highest_catalogue_revision(self):
        workbench, _ = load()
        workbench.ingest(
            envelope(
                "catalog-108-r6",
                "collection_item",
                {
                    "item_ref": "ITEM-SYN-108",
                    "catalogue_revision": 6,
                    "title": "青釉双耳瓶（重新定名）",
                    "institution_ref": "MUSEUM-SYN-2",
                },
            )
        )
        current = workbench.item("ITEM-SYN-108")
        self.assertEqual(current.catalogue_revision, 6)
        self.assertEqual(current.title, "青釉双耳瓶（重新定名）")
        history = workbench.item_history("ITEM-SYN-108")
        self.assertEqual([h.catalogue_revision for h in history], [5, 6])

    def test_re_identified_item_resolves_forward(self):
        workbench, _ = load()
        workbench.ingest(
            envelope(
                "catalog-merge",
                "collection_item",
                {
                    "item_ref": "ITEM-SYN-108-MERGED",
                    "catalogue_revision": 1,
                    "title": "合并后的藏品（虚构）",
                    "replaces_item_ref": "ITEM-SYN-108",
                },
            )
        )
        self.assertEqual(workbench.item("ITEM-SYN-108").item_ref, "ITEM-SYN-108-MERGED")
        # Assets that still cite the old ref surface under the surviving item.
        self.assertEqual(len(workbench.assets_for_item("ITEM-SYN-108")), 3)


if __name__ == "__main__":
    unittest.main()
