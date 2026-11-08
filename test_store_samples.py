"""样本累计层测试：CSV 去重追加、批次台账幂等、总行数统计。"""

import csv
import os
import tempfile
import unittest

import store_samples as ss


def row(match_id, home="Home", away="Away", date="2026-11-08"):
    return {"date": date, "batch": "B", "match_id": match_id,
            "league": "英超", "home": home, "away": away,
            "kickoff": "2026-11-08 20:00", "rq_line": "-1",
            "history_source": "goal", "odds_source": "apify:flashscore",
            "m1_spf": "主胜", "m1_spf_first": "True", "ft_home": 2,
            "ft_away": 1}


class TestAppendSamples(unittest.TestCase):
    def test_header_and_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path, added = ss.append_samples("2026-11-08", "B", [row("m1"), row("m2")], td)
            self.assertEqual(added, 2)
            with open(path, encoding="utf-8-sig") as fp:
                reader = csv.DictReader(fp)
                self.assertEqual(reader.fieldnames, ss.SAMPLE_COLUMNS)
                self.assertEqual(len(list(reader)), 2)

    def test_dedupe_by_date_batch_match_id(self):
        with tempfile.TemporaryDirectory() as td:
            ss.append_samples("2026-11-08", "B", [row("m1")], td)
            _, added = ss.append_samples("2026-11-08", "B", [row("m1"), row("m2")], td)
            self.assertEqual(added, 1)
            self.assertEqual(ss.count_samples(td), 2)

    def test_same_match_different_batch_kept(self):
        with tempfile.TemporaryDirectory() as td:
            ss.append_samples("2026-11-08", "A", [row("m1")], td)
            _, added = ss.append_samples("2026-11-08", "B", [row("m1")], td)
            self.assertEqual(added, 1)
            self.assertEqual(ss.count_samples(td), 2)

    def test_batch_forced_even_if_row_says_otherwise(self):
        with tempfile.TemporaryDirectory() as td:
            path, _ = ss.append_samples("2026-11-08", "A", [row("m1")], td)
            with open(path, encoding="utf-8-sig") as fp:
                rec = list(csv.DictReader(fp))[0]
            self.assertEqual(rec["batch"], "A")

    def test_generated_at_filled(self):
        with tempfile.TemporaryDirectory() as td:
            path, _ = ss.append_samples("2026-11-08", "B", [row("m1")], td)
            with open(path, encoding="utf-8-sig") as fp:
                rec = list(csv.DictReader(fp))[0]
            self.assertTrue(rec["generated_at"])

    def test_count_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(ss.count_samples(td), 0)


class TestAppendBatch(unittest.TestCase):
    def test_creates_ledger_with_header(self):
        with tempfile.TemporaryDirectory() as td:
            path = ss.append_batch("2026-11-08", "B", 22, note="测试", data_dir=td)
            with open(path, encoding="utf-8") as fp:
                text = fp.read()
            self.assertIn("A 批", text)
            self.assertIn("| B | 2026-11-08 | 22 | Apify Flashscore | 测试 |", text)

    def test_idempotent_same_batch_date(self):
        with tempfile.TemporaryDirectory() as td:
            ss.append_batch("2026-11-08", "B", 22, data_dir=td)
            path = ss.append_batch("2026-11-08", "B", 30, data_dir=td)
            with open(path, encoding="utf-8") as fp:
                text = fp.read()
            self.assertEqual(text.count("| B | 2026-11-08 |"), 1)

    def test_second_date_appends(self):
        with tempfile.TemporaryDirectory() as td:
            ss.append_batch("2026-11-07", "B", 20, data_dir=td)
            path = ss.append_batch("2026-11-08", "B", 22, data_dir=td)
            with open(path, encoding="utf-8") as fp:
                text = fp.read()
            self.assertIn("| B | 2026-11-07 |", text)
            self.assertIn("| B | 2026-11-08 |", text)


class TestSampleColumns(unittest.TestCase):
    def test_column_layout(self):
        self.assertEqual(len(ss.SAMPLE_COLUMNS), len(set(ss.SAMPLE_COLUMNS)))
        cols = ss.SAMPLE_COLUMNS
        self.assertIn("m1_spf_first", cols)
        self.assertIn("m2_crs_any", cols)
        for col in ("ft_home", "ft_away", "ht_home", "ht_away"):
            self.assertIn(col, cols)


if __name__ == "__main__":
    unittest.main()
