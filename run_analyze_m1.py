"""M1 组 wrapper：带赔率分析（analyze.py 原样运行，文件零改动）。

仅在运行前用 history_cache.load_history_lookup 的查询函数替换 analyze
模块命名空间中的 build_history_for_match，使历史数据改由本地
output/history_cache_{date}.json 提供，不再消耗 FOOTBALL_API_KEY 额度。
日期保护（只处理文件名日期 == 今天的赛程 CSV）由 analyze.main() 自身负责。
"""
import analyze
from history_cache import load_history_lookup


def main():
    # 与 analyze.main() 相同的赛程定位逻辑，用于确定历史缓存日期
    csv_path = analyze.find_latest_csv()
    if not csv_path:
        raise SystemExit(1)
    analyze.build_history_for_match = load_history_lookup(csv_path)
    analyze.main()


if __name__ == "__main__":
    main()
