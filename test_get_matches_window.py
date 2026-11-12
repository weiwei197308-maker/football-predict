import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

# 其他测试可能为便携运行注入 football_history 桩；这里恢复真实模块后再导入赛程模块。
import football_history
if not hasattr(football_history, "summarize_h2h"):
    sys.modules.pop("football_history", None)
    football_history = importlib.import_module("football_history")

from get_matches_flashscore import resolve_days, resolve_window

CST = timezone(timedelta(hours=8))


class TestResolveWindow(unittest.TestCase):
    """赛程抓取窗口：今天用滚动 24h，历史日期用日历销售日窗口。"""

    def test_today_rolling_from_now(self):
        now = datetime(2026, 11, 12, 9, 43, tzinfo=CST)
        lower, upper, mode = resolve_window("2026-11-12", now)
        self.assertEqual(mode, "rolling")
        self.assertEqual(lower, now)
        self.assertEqual(upper, now + timedelta(hours=24))

    def test_today_rolling_covers_next_morning(self):
        now = datetime(2026, 11, 12, 6, 0, tzinfo=CST)
        lower, upper, _ = resolve_window("2026-11-12", now)
        self.assertTrue(lower <= datetime(2026, 11, 13, 5, 0, tzinfo=CST) < upper)

    def test_past_date_keeps_calendar_window(self):
        now = datetime(2026, 11, 12, 9, 43, tzinfo=CST)
        lower, upper, mode = resolve_window("2026-11-08", now)
        self.assertEqual(mode, "calendar")
        self.assertEqual(lower, datetime(2026, 11, 8, 12, 0, tzinfo=CST))
        self.assertEqual(upper, datetime(2026, 11, 9, 12, 0, tzinfo=CST))

    def test_future_date_keeps_calendar_window(self):
        now = datetime(2026, 11, 12, 9, 43, tzinfo=CST)
        lower, upper, mode = resolve_window("2026-11-15", now)
        self.assertEqual(mode, "calendar")
        self.assertEqual(lower, datetime(2026, 11, 15, 12, 0, tzinfo=CST))


class TestResolveDays(unittest.TestCase):
    """Actor days 参数：默认只请求目标日，傍晚运行才追加次日（省额度）。"""

    def test_morning_run_only_target_day(self):
        now = datetime(2026, 11, 12, 8, 33, tzinfo=CST)
        self.assertEqual(resolve_days("2026-11-12", now), ["2026-11-12"])

    def test_evening_run_adds_next_day(self):
        now = datetime(2026, 11, 12, 16, 5, tzinfo=CST)
        self.assertEqual(resolve_days("2026-11-12", now), ["2026-11-12", "2026-11-13"])

    def test_backfill_never_adds_next_day(self):
        now = datetime(2026, 11, 12, 20, 0, tzinfo=CST)
        self.assertEqual(resolve_days("2026-11-08", now), ["2026-11-08"])


if __name__ == "__main__":
    unittest.main()
