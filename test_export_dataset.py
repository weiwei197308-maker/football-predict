"""export_dataset 单测：合并口径、赛果优先级、日期过滤与列完整性。"""

import csv
import json
import os
import tempfile
import unittest

import export_dataset as ex


def _record(mid, home="Home", away="Away", spf=(2.0, 3.5, 3.8)):
    return {
        "match_id": mid, "league_name": "英超", "league_display": "ENGLAND: Premier League",
        "home": home, "away": away, "kickoff_cst": "2026-11-08T21:00+08:00",
        "status": "scheduled", "ft_home": None, "ft_away": None,
        "ht_home": None, "ht_away": None, "history_source": "goal",
        "odds_mapped": {
            "spf": {"主胜": spf[0], "平": spf[1], "客胜": spf[2]},
            "rq": {"让球数": "-1", "主胜": 3.4, "平": 5.2, "客胜": 1.3},
            "ttg": {n: 10.0 for n in ("0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球")},
            "hafu": {"半主胜-全主胜": 3.7, "半主胜-全平": 13.0, "半主胜-全客胜": 23.0,
                     "半平-全主胜": 6.0, "半平-全平": 6.5, "半平-全客胜": 7.0,
                     "半客胜-全主胜": 21.0, "半客胜-全平": 13.0, "半客胜-全客胜": 4.3},
            "crs": {"1:0": 12.0, "2:1": 9.5},
        },
        "odds_flags": {"odds_source": "apify:flashscore"},
    }


class ExportDatasetTest(unittest.TestCase):
    def _prepare(self, td, records_by_date, sample_rows=None):
        os.makedirs(os.path.join(td, "odds"))
        for d, records in records_by_date.items():
            with open(os.path.join(td, "odds", f"{d}.jsonl"), "w", encoding="utf-8") as fp:
                for rec in records:
                    fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if sample_rows:
            cols = list(ex.SAMPLE_REC_COLUMNS) + list(ex.SAMPLE_HIT_COLUMNS)
            with open(os.path.join(td, "samples.csv"), "w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=cols + ["date", "match_id", "batch",
                                                              "ft_home", "ft_away", "ht_home",
                                                              "ht_away"], extrasaction="ignore")
                writer.writeheader()
                writer.writerows(sample_rows)

    def test_join_sample_and_settled(self):
        with tempfile.TemporaryDirectory() as td:
            self._prepare(td, {"2026-11-08": [_record("m1")]}, [{
                "date": "2026-11-08", "match_id": "m1", "batch": "B",
                "ft_home": "2", "ft_away": "1", "ht_home": "1", "ht_away": "0",
                "m1_spf": "主胜", "m1_spf_first": "1", "m1_spf_any": "1",
            }])
            rows, crs, dates = ex.build_rows(td)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["ft_home"], r["ft_away"], r["ht_home"], r["ht_away"]),
                         ("2", "1", "1", "0"))
        self.assertEqual(r["settled"], 1)
        self.assertEqual(r["batch"], "B")
        self.assertEqual(r["m1_spf"], "主胜")
        self.assertEqual(r["m1_spf_first"], "1")
        self.assertEqual(r["rq_line"], "-1")
        self.assertEqual(r["spf_home"], 2.0)
        self.assertEqual(r["hafu_hd"], 13.0)
        self.assertEqual(r["ttg_7plus"], 10.0)
        self.assertEqual(dates, {"2026-11-08"})
        self.assertEqual(len(crs), 1)
        self.assertEqual(len(crs[0]), len(ex.CRS_COLUMNS))
        self.assertEqual(crs[0]["crs_1_0"], 12.0)
        self.assertEqual(crs[0]["crs_0_0"], "")  # 无报价留空

    def test_missing_sample_row_is_unsettled(self):
        with tempfile.TemporaryDirectory() as td:
            self._prepare(td, {"2026-11-08": [_record("m2")]})
            rows, _, _ = ex.build_rows(td)
        self.assertEqual(rows[0]["settled"], 0)
        self.assertEqual(rows[0]["ft_home"], "")
        self.assertEqual(rows[0]["batch"], "")

    def test_date_filter(self):
        with tempfile.TemporaryDirectory() as td:
            self._prepare(td, {"2026-11-07": [_record("a")],
                               "2026-11-08": [_record("b")]})
            rows, _, dates = ex.build_rows(td, start="2026-11-08")
        self.assertEqual([r["match_id"] for r in rows], ["b"])
        self.assertEqual(dates, {"2026-11-08"})


if __name__ == "__main__":
    unittest.main()
