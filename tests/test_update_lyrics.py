import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("updater", Path(__file__).parents[1] / "scripts/update_lyrics.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def document(song_id=1, revision=1):
    return {"version": 3, "musicId": song_id, "revision": revision, "updatedAt": "2026-10-01T00:00:00Z",
            "state": "complete", "renditions": [{"key": "sekai", "kind": "sekai", "label": "SEKAI",
            "availableVersions": ["full"], "performers": [], "relation": {"kind": "none"},
            "sourceTabPaths": [["SEKAI"]], "provenance": [{"component": "renditions/sekai/full_text",
            "provider": "sekaipedia", "title": "Example", "revisionId": 1,
            "revisionUrl": "https://www.sekaipedia.org/wiki/Example?oldid=1", "licenseName": "CC BY-SA 4.0",
            "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/"}],
            "full": {"version": {"kind": "sekai", "label": "Full"}, "lines": [
                {"id": "a", "order": 0, "japanese": "テスト", "zh-CN": "测试", "stanzaBreakBefore": True,
                 "segments": [{"text": "テスト", "performerIds": [], "ruby": [{"text": "テスト"}]}],
                 "trailingPerformerIds": []}]}}]}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.calls = []
        self.doc = document()
        self.fail = False

    def fetch(self, url):
        self.calls.append(url)
        if url.endswith("/index.json"):
            return {"version": 3, "songs": [{"musicId": 1, "revision": self.doc["revision"],
                    "updatedAt": self.doc["updatedAt"], "state": "complete"}]}
        if url.endswith("/musics.json"):
            return [{"id": 1, "title": url.split("/")[4]}, {"id": 2, "title": "Unpublished lyrics"}]
        if self.fail:
            raise OSError("Temporary outage")
        return copy.deepcopy(self.doc)

    def run_update(self, day="2026-10-01", mode="auto", **kwargs):
        return updater.update(self.root, mode=mode, delay=0, fetch=self.fetch,
                              today=dt.date.fromisoformat(day), **kwargs)

    def test_daily_missing_then_monthly_full_and_lossless_mirror(self):
        report = self.run_update()
        self.assertEqual(report["fetched"], [1])
        self.assertEqual(updater.read_json(self.root / "lyrics/music_1.json"), self.doc)
        self.assertNotIn("en-US", self.doc["renditions"][0]["full"]["lines"][0])
        self.assertEqual(self.run_update("2026-10-02")["fetched"], [])
        self.assertEqual(self.run_update("2026-11-02")["fetched"], [1])
        index = updater.read_json(self.root / "index.json")
        self.assertEqual(set(index["songs"][0]["regionalTitles"]), set(updater.REGIONS))
        self.assertFalse(index["songs"][1]["hasLyrics"])

    def test_failure_preserves_last_good_data_and_retries_full_refresh(self):
        self.run_update()
        self.doc["revision"] = 2
        self.fail = True
        self.assertTrue(self.run_update("2026-11-01")["failures"])
        self.assertEqual(updater.read_json(self.root / "lyrics/music_1.json")["revision"], 1)
        self.assertEqual(updater.read_json(self.root / "state.json")["lastSuccessfulFullMonth"], "2026-10")
        self.fail = False
        self.assertEqual(self.run_update("2026-11-02")["fetched"], [1])

    def test_filtered_fetch_does_not_mark_month_complete(self):
        self.run_update(song_ids={1})
        self.assertFalse((self.root / "state.json").exists())

    def test_targeted_fetch_refreshes_existing_song_and_rejects_unknown_id(self):
        self.run_update()
        self.doc["revision"] = 2
        self.assertEqual(self.run_update(song_ids={1})["fetched"], [1])
        self.assertEqual(updater.read_json(self.root / "lyrics/music_1.json")["revision"], 2)
        with self.assertRaises(ValueError):
            self.run_update(song_ids={999})

    def test_bad_index_does_not_replace_existing_publication(self):
        self.run_update()
        before = (self.root / "index.json").read_bytes()
        with self.assertRaises(ValueError):
            updater.update(self.root, fetch=lambda _: {"version": 99, "songs": []})
        self.assertEqual(before, (self.root / "index.json").read_bytes())

    def test_wrong_song_revision_and_segment_corruption_are_rejected(self):
        with self.assertRaises(ValueError):
            updater.validate_document(self.doc, 2)
        with self.assertRaises(ValueError):
            updater.validate_document(self.doc, 1, {"revision": 2})
        self.doc["renditions"][0]["full"]["lines"][0]["segments"][0]["text"] = "wrong"
        with self.assertRaises(ValueError):
            updater.validate_document(self.doc, 1)

    def test_v4_translation_editions_and_alignment(self):
        self.doc["version"] = 4
        self.doc["defaultTranslationEditionKey"] = "community"
        self.doc["translationEditions"] = [{"key": "community", "label": "Community", "renditions": [
            {"renditionKey": "sekai", "full": {"translations": ["测试"]}}]}]
        updater.validate_document(self.doc, 1)
        self.doc["translationEditions"][0]["renditions"][0]["full"]["translations"] = []
        with self.assertRaises(ValueError):
            updater.validate_document(self.doc, 1)

    def test_all_three_allowlisted_licenses_and_mixed_components_are_accepted(self):
        credits = self.doc["renditions"][0]["provenance"]
        original = copy.deepcopy(credits[0])
        credits.clear()
        for name, url in updater.ALLOWED_LICENSES:
            credit = {**original, "licenseName": name, "licenseUrl": url}
            credits.append(credit)
        updater.validate_document(self.doc, 1)
        self.assertEqual(self.run_update()["fetched"], [1])

    def test_unapproved_or_mismatched_license_pairs_are_rejected(self):
        cases = [
            ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
            ("CC BY-NC-SA 4.0", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
            ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/3.0/"),
            ("CC BY-SA 4.0", "https://example.com/licenses/by-sa/4.0/"),
            (None, "https://creativecommons.org/licenses/by-sa/4.0/"),
            ("CC BY-SA 4.0", None),
        ]
        for name, url in cases:
            with self.subTest(name=name, url=url):
                self.doc = document()
                self.doc["renditions"][0]["provenance"][0].update(licenseName=name, licenseUrl=url)
                with self.assertRaises(updater.LicenseNotAllowed):
                    updater.validate_document(self.doc, 1)

    def test_one_unapproved_rendition_skips_entire_song_without_failing_run(self):
        alternate = copy.deepcopy(self.doc["renditions"][0])
        alternate["key"] = "alternate"
        alternate["provenance"][0].update(licenseName="All rights reserved", licenseUrl="https://example.com/terms")
        self.doc["renditions"].append(alternate)
        report = self.run_update()
        self.assertEqual(report["fetched"], [])
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["skipped"][0]["id"], 1)
        self.assertFalse((self.root / "lyrics/music_1.json").exists())
        self.assertFalse(updater.read_json(self.root / "index.json")["songs"][0]["hasLyrics"])
        # A policy skip completes the monthly check and is not an endless full-refresh retry.
        self.assertEqual(updater.read_json(self.root / "state.json")["lastSuccessfulFullMonth"], "2026-10")
        self.doc = document(revision=2)
        self.assertEqual(self.run_update("2026-10-02")["fetched"], [1])

    def test_unapproved_new_revision_preserves_previously_approved_document(self):
        self.run_update()
        before = (self.root / "lyrics/music_1.json").read_bytes()
        self.doc["revision"] = 2
        self.doc["renditions"][0]["provenance"][0]["licenseName"] = "CC BY 4.0"
        report = self.run_update("2026-11-01")
        self.assertEqual(report["failures"], [])
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual(before, (self.root / "lyrics/music_1.json").read_bytes())
        index = updater.read_json(self.root / "index.json")
        self.assertEqual(index["songs"][0]["revision"], 1)

    def test_missing_structured_license_is_skipped_for_legacy_and_current_schemas(self):
        for credits in (None, []):
            with self.subTest(credits=credits):
                self.doc = document()
                self.doc["renditions"][0]["provenance"] = credits
                with self.assertRaises(updater.LicenseNotAllowed):
                    updater.validate_document(self.doc, 1)
        legacy = {key: value for key, value in document().items() if key != "renditions"}
        legacy.update(version=1, attribution="Free-text attribution only",
                      lines=document()["renditions"][0]["full"]["lines"])
        with self.assertRaises(updater.LicenseNotAllowed):
            updater.validate_document(legacy, 1)
        legacy["attributions"] = document()["renditions"][0]["provenance"]
        updater.validate_document(legacy, 1)

    def test_additional_nested_license_records_cannot_bypass_allowlist(self):
        self.doc["extraMetadata"] = {"sources": [{"licenseName": "CC0 1.0", "licenseUrl": "https://creativecommons.org/publicdomain/zero/1.0/"}]}
        report = self.run_update()
        self.assertEqual(len(report["skipped"]), 1)
        self.assertFalse((self.root / "lyrics/music_1.json").exists())

    def test_skipping_one_song_does_not_prevent_updating_another(self):
        self.doc["renditions"][0]["provenance"][0]["licenseName"] = "Unknown license"
        def fetch(url):
            if "music_2.json" in url:
                return document(song_id=2)
            result = self.fetch(url)
            if url.endswith("/index.json"):
                result["songs"].append({**result["songs"][0], "musicId": 2})
            return result
        report = updater.update(self.root, delay=0, fetch=fetch, today=dt.date(2026, 10, 1))
        self.assertEqual(report["fetched"], [2])
        self.assertEqual([entry["id"] for entry in report["skipped"]], [1])
        self.assertEqual(report["failures"], [])
        self.assertTrue((self.root / "lyrics/music_2.json").exists())


if __name__ == "__main__":
    unittest.main()
