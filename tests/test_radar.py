import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import radar

CONFIG = radar.read_json(radar.ROOT / "config/watchlist.json")


class RadarTests(unittest.TestCase):
    def record(self):
        return copy.deepcopy(radar.read_json(radar.ROOT / "tests/fixtures/demo.json")["records"][0])

    def test_repeat_is_not_a_change(self):
        first, events = radar.merge([], [self.record()], "t1")
        second, changes = radar.merge(first, [self.record()], "t2")
        self.assertEqual(len(events), 1)
        self.assertEqual(changes, [])
        self.assertEqual(second[0]["first_seen"], "t1")
        self.assertEqual(second[0]["last_seen"], "t2")

    def test_update_and_missing_archive(self):
        first, _ = radar.merge([], [self.record()], "t1")
        updated = self.record()
        updated["title"] = "Changed title"
        second, changes = radar.merge(first, [updated], "t2")
        self.assertEqual(changes[0]["kind"], "updated")
        retained, changes = radar.merge(second, [], "t3")
        self.assertEqual(retained, second)
        self.assertEqual(changes, [])

    def test_source_namespaces_do_not_collide(self):
        a, b = self.record(), self.record()
        a["id"], b["id"] = "pubmed:1", "ctgov:1"
        merged, _ = radar.merge([], [a, b], "t1")
        self.assertEqual(len(merged), 2)

    def test_query_aliases_and_provisional_tags(self):
        tags = radar.classify("LY3437943 versus semaglutide safety", "", CONFIG)
        self.assertIn("retatrutide", tags["drugs"])
        self.assertIn("Safety", tags["topics"])
        self.assertTrue(all(a.isascii() for a in radar.aliases(CONFIG)))

    def test_trial_pagination_and_cap(self):
        config = {**CONFIG, "max_records_per_source": 2}
        def trial(n):
            return {"protocolSection": {
                "identificationModule": {"nctId": "NCT" + str(n), "briefTitle": "Semaglutide"},
                "statusModule": {"overallStatus": "RECRUITING"}}}
        calls = []
        def fetch(url, params):
            calls.append(dict(params))
            return {"studies": [trial(len(calls))], "nextPageToken": str(len(calls)), "totalCount": 3}
        records, meta = radar.trials(config, fetch)
        self.assertEqual(len(records), 2)
        self.assertEqual(calls[1]["pageToken"], "1")
        self.assertTrue(meta["truncated"])
        self.assertFalse(records[0]["has_results"])

    def test_pubmed_metadata_and_cap_warning(self):
        def fetch(url, params):
            if "esearch" in url:
                return {"esearchresult": {"idlist": ["1"], "count": "5"}}
            return {"result": {"1": {"title": "Semaglutide safety", "pubdate": "2026 Nov", "pubtype": ["Review"]}}}
        rows, info = radar.pubmed(CONFIG, fetch)
        self.assertEqual(rows[0]["publication_types"], ["Review"])
        self.assertTrue(info["truncated"])

    def test_source_failure_preserves_archive_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            records, _ = radar.merge([], [self.record()], "t1")
            radar.write_json(path / "data/catalog.json", {"records": records, "history": [], "sources": {}})
            args = SimpleNamespace(config=radar.ROOT / "config/watchlist.json", data=path / "data", output=path / "reports", demo=False)
            with patch.object(radar, "pubmed", side_effect=ValueError("bad response")), patch.object(radar, "trials", return_value=([], {"returned": 0})):
                code = radar.run(args)
            state = radar.read_json(path / "data/catalog.json")
            self.assertEqual(code, 1)
            self.assertEqual(state["records"], records)
            self.assertEqual(state["sources"]["PubMed"]["status"], "error")

    def test_html_payload_cannot_end_script(self):
        record = self.record()
        record["title"] = "</script><script>alert(1)</script>"
        state = {"records": [record], "last_run": "t1", "sources": {}, "latest_changes": []}
        with tempfile.TemporaryDirectory() as directory:
            radar.render(state, Path(directory))
            page = (Path(directory) / "index.html").read_text()
            self.assertNotIn(record["title"], page)
            self.assertIn("\\u003c/script>", page)


if __name__ == "__main__":
    unittest.main()
