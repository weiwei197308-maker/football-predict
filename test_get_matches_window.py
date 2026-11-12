import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

# 其他测试可能为便携运行注入 football_history 桩；这里恢复真实模块后再导入赛程模块。
import football_history
if not hasattr(football_history, "summarize_h2h"):
    sys.modules.pop("football_history", None)
    football_history = importlib.import_module("football_history")

from get_matches_flashscore import resolve_window

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


if __name__ == "__main__":
    unittest.main()
