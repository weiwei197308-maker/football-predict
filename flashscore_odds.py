"""flashscore_odds.py — Apify Flashscore 赔率/赛果适配层（zucai_all 全量样本环境）。

唯一外部数据源：Apify Actor `omkar-cloud~flashscore-odds-scraper`
（API-Football 在本环境已隔离，不再发起任何请求）。

职责
  1. 调用 Actor：APIFY_TOKEN 只从环境变量读取，绝不出现在日志/文件/提交里。
  2. 原始响应落盘 `data/raw/{date}/apify_{runId}.json`（不覆盖既有归档）。
  3. 标准化为与既有赛程 CSV 完全同构的 `odds_mapped`：
       spf（胜平负）/ rq（让球胜平负）/ ttg（总进球）/ hafu（半全场）/ crs（比分）
  4. `data/odds/{date}.jsonl` 每场一行（按 matchId 幂等合并）。

口径（2026-11-08 用户拍板）
  - 总进球：由 OVER_UNDER 阶梯推导 0..6 球 + 7+ 球。以半球盘（x.5）为精确边界
    （无走盘），逐盘 over/under 去水归一得到 F(k)=P(总进球≤k)；缺边界时先用
    四分之一盘按去走盘归一近似，再用泊松补尾。7+ = 1 - F(6)。
  - 让球：只取整数盘（优先 ±1，其次 0/±2）。亚盘整数盘只有主/客两侧报价，
    体彩「让球-平」由 CORRECT_SCORE 的净胜球分布在同一水位下推导；无 CRS 时留空
    并在 flags 标注 `rq_draw_missing`。
  - 书商：优先 bet365；该盘口不可用时退回该盘口最完整的一家，逐盘口记录实际来源。
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

ACTOR_ID = "omkar-cloud~flashscore-odds-scraper"
RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
TOKEN_ENV = "APIFY_TOKEN"
HTTP_TIMEOUT = 600  # Actor 全量跑批较慢；run-sync 服务端上限约 5 分钟
RETRIES = 3
RETRY_BACKOFF_SECONDS = 20
CST = timezone(timedelta(hours=8))

DEFAULT_BET_TYPES = (
    "home-draw-away", "asian-handicap", "over-under",
    "half-time-full-time", "correct-score",
)
DEFAULT_GEO = os.environ.get("APIFY_GEO", "GB")
DEFAULT_MAX_RESULTS = 70
MAX_MATCHES_PER_DAY = 60  # 每日分析上限（成本 + 模型调用量控制）

TTG_NAMES = ["0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"]
# 体彩比分 28 档（与主环境 CSV 列完全一致，不含 3:4 / 4:3 等）
CRS_SCORES = [
    "0:0", "0:1", "0:2", "0:3", "0:4", "0:5",
    "1:0", "1:1", "1:2", "1:3", "1:4", "1:5",
    "2:0", "2:1", "2:2", "2:3", "2:4", "2:5",
    "3:0", "3:1", "3:2", "3:3",
    "4:0", "4:1", "4:2",
    "5:0", "5:1", "5:2",
]
CRS_SCORE_SET = set(CRS_SCORES)
HAFU_CN = {"1": "主胜", "X": "平", "2": "客胜"}
SPF_CN = {"home": "主胜", "draw": "平", "away": "客胜"}


class ApifyError(RuntimeError):
    """Actor 调用/响应解析失败；消息中绝不含 token。"""


def get_token():
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ApifyError(
            f"未设置 {TOKEN_ENV} 环境变量：请先 source ~/.zshrc；密钥只放环境变量。")
    if re.search(r"\s", token):
        raise ApifyError(f"{TOKEN_ENV} 含空白字符，请检查环境变量。")
    return token


# ---------------- Actor 调用与原始归档 ----------------

def fetch_items(competitions, days, status="scheduled",
                max_results=DEFAULT_MAX_RESULTS, bet_types=DEFAULT_BET_TYPES,
                geo=DEFAULT_GEO, timeout=HTTP_TIMEOUT):
    """调用 Actor，返回 (items, meta)。失败重试 3 次（429/5xx/超时/网络）。"""
    payload = {
        "sports": ["football"],
        "days": list(days),
        "competitions": list(competitions),
        "status": status,
        "geo": geo,
        "timezone": "Asia/Shanghai",
        "maxResults": int(max_results),
        "betTypes": list(bet_types),
        "betScope": "full-time",
    }
    last_err = None
    for attempt in range(RETRIES):
        try:
            resp = requests.post(RUN_URL, params={"token": get_token()},
                                 json=payload, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = ApifyError(f"Actor 请求超时/网络错误: {type(e).__name__}")
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = ApifyError(f"Actor HTTP {resp.status_code}")
            if attempt < RETRIES - 1:
                wait = RETRY_BACKOFF_SECONDS * (attempt + 1)
                print(f"  ⚠️ Actor {resp.status_code}，{wait}s 后重试（第 {attempt + 1} 次）")
                time.sleep(wait)
            continue
        if resp.status_code >= 300:
            raise ApifyError(f"Actor HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ApifyError("Actor 返回非 JSON") from e
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ApifyError(f"Actor 输入/运行错误: {str(msg)[:200]}")
        if not isinstance(data, list):
            raise ApifyError(f"Actor 返回结构异常（{type(data).__name__}）")
        meta = {
            "run_id": (resp.headers.get("x-apify-run-id")
                       or resp.headers.get("x-apify-act-run-id") or ""),
            "status": status,
            "requested": len(competitions),
            "input": payload,
            "count": len(data),
        }
        return data, meta
    raise last_err or ApifyError("Actor 调用失败")


def save_raw(items, meta, target_date, data_dir="data", prefix="apify"):
    """原始响应归档到 data/raw/{date}/{prefix}_{runId}.json，同名加序号绝不覆盖。"""
    folder = os.path.join(data_dir, "raw", target_date)
    os.makedirs(folder, exist_ok=True)
    key = (meta.get("run_id") or datetime.now().strftime("%Y%m%dT%H%M%S")).strip()
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", key) or "run"
    base = os.path.join(folder, f"{prefix}_{key}.json")
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{prefix}_{key}_{n}.json")
    payload = {
        "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
        "target_date": target_date,
        "run_id": meta.get("run_id", ""),
        "input": meta.get("input"),
        "count": len(items),
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False)
    return path


# ---------------- 基础解析 ----------------

def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 and math.isfinite(f) else None


def _active(sel):
    return sel.get("is_active") is not False


def _bookmaker_name(market):
    return str((market.get("bookmaker") or {}).get("name") or "").strip()


def _is_bet365(market):
    return _bookmaker_name(market).lower() == "bet365"


def _pick_market(markets, bet_type, usable):
    """优先 bet365，其次选择项最多的书商（逐盘口独立选择）。"""
    cands = [m for m in markets if m.get("bet_type") == bet_type and usable(m)]
    if not cands:
        return None
    for m in cands:
        if _is_bet365(m):
            return m
    cands.sort(key=lambda m: sum(1 for s in (m.get("selections") or [])
                                 if _active(s) and _num(s.get("value")) is not None),
               reverse=True)
    return cands[0]


def map_spf(market):
    out = {}
    for s in (market.get("selections") or []):
        if not _active(s):
            continue
        key = SPF_CN.get(str(s.get("selection") or "").strip().lower())
        v = _num(s.get("value"))
        if key and v:
            out[key] = v
    return out


def map_hafu(market):
    out = {}
    for s in (market.get("selections") or []):
        if not _active(s):
            continue
        raw = str(s.get("selection") or "").strip().upper()
        parts = raw.split("/")
        v = _num(s.get("value"))
        if v and len(parts) == 2 and parts[0] in HAFU_CN and parts[1] in HAFU_CN:
            out[f"半{HAFU_CN[parts[0]]}-全{HAFU_CN[parts[1]]}"] = v
    return out


def map_crs(market):
    out = {}
    for s in (market.get("selections") or []):
        if not _active(s):
            continue
        sc = str(s.get("score") or s.get("selection") or "").strip()
        v = _num(s.get("value"))
        if v and sc in CRS_SCORE_SET:
            out[sc] = v
    return out


def crs_margin_probs(market):
    """CORRECT_SCORE 归一化后的净胜球分布 {主队净胜球: 概率}。"""
    odds = map_crs(market)
    if not odds:
        return {}
    inv = {}
    for sc, o in odds.items():
        h, a = (int(x) for x in sc.split(":"))
        inv[h - a] = inv.get(h - a, 0.0) + 1.0 / o
    total = sum(inv.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in inv.items()}


# ---------------- 总进球：OVER_UNDER 阶梯 -> 0..6 / 7+ ----------------

def _ou_lines(market):
    """{盘口: {over/under: 赔率}}，只保留双侧有效报价。"""
    lines = {}
    for s in (market.get("selections") or []):
        if not _active(s):
            continue
        try:
            h = float(s.get("handicap"))
        except (TypeError, ValueError):
            continue
        sel = str(s.get("selection") or "").strip().lower()
        v = _num(s.get("value"))
        if sel in ("over", "under") and v:
            lines.setdefault(h, {})[sel] = v
    return {h: p for h, p in lines.items() if "over" in p and "under" in p}


def _is_half(h):
    return abs(h * 2 - round(h * 2)) < 1e-9 and int(round(h * 2)) % 2 == 1


def _is_quarter(h):
    return abs(h * 4 - round(h * 4)) < 1e-9 and int(round(h * 4)) % 2 == 1


def _pair_prob(pair):
    """去水归一的 under 概率（over+under 互为补集）。"""
    po, pu = 1.0 / pair["over"], 1.0 / pair["under"]
    return pu / (po + pu)


def _poisson_cdf(k, lam):
    if k < 0:
        return 0.0
    term = math.exp(-lam)
    acc = term
    for i in range(1, k + 1):
        term *= lam / i
        acc += term
    return min(max(acc, 0.0), 1.0)


def _estimate_lambda(cum):
    """用 F(k)=P(T≤k) 的 0.5 交叉点估计泊松 λ（泊松中位数≈λ-0.5）。"""
    pts = sorted(cum.items())
    for (k0, c0), (k1, c1) in zip(pts, pts[1:]):
        if c0 < 0.5 <= c1 and c1 > c0:
            return max(0.6, k0 + (0.5 - c0) / (c1 - c0) + 0.5)
    if pts:
        return max(0.6, pts[-1][0] + 0.5) if pts[-1][1] >= 0.5 else 0.8
    return 2.6


def derive_total_goals(market):
    """返回 ({'0球'..'7+球': 赔率}, flags)。以半球盘为精确边界，缺边界用泊松补。"""
    lines = _ou_lines(market)
    if not lines:
        return {}, {"ttg_source": "missing"}

    cum, quarter_used = {}, False
    for h, pair in lines.items():
        if _is_half(h) and 0 <= h <= 6.5:
            cum[int(round(h - 0.5))] = _pair_prob(pair)
        elif _is_quarter(h) and 0 < h < 7:
            k = int(h + 0.5)
            if 0 <= k <= 6 and k not in cum:
                cum[k] = _pair_prob(pair)  # 四分之一盘：去走盘归一后作近似边界
                quarter_used = True

    lam = _estimate_lambda(cum)
    F = {}
    for k in range(7):
        if k in cum:
            F[k] = cum[k]
        else:
            F[k] = _poisson_cdf(k, lam)
    for k in range(1, 7):
        F[k] = max(F[k], F[k - 1])
    F[6] = min(max(F[6], 0.0), 1.0)

    probs = [max(F[0], 0.004)]
    for k in range(1, 7):
        probs.append(max(F[k] - F[k - 1], 0.004))
    probs.append(max(1.0 - F[6], 0.004))
    total = sum(probs)
    probs = [p / total for p in probs]
    odds = {name: round(max(1.01, 1.0 / p), 2) for name, p in zip(TTG_NAMES, probs)}
    flags = {
        "ttg_source": "over_under_ladder",
        "ttg_bookmaker": _bookmaker_name(market),
        "ttg_lambda": round(lam, 3),
        "ttg_buckets": {n: round(p, 4) for n, p in zip(TTG_NAMES, probs)},
    }
    if quarter_used:
        flags["ttg_quarter_fallback"] = True
    return odds, flags


# ---------------- 让球：整数亚盘 + CRS 推导「让球-平」 ----------------

def _integer_pairs(market):
    """{让球线 L: (主队赔率@L, 客队赔率@-L)}；L 为主队让球数（负数=主队让球）。"""
    rows = {}
    for s in (market.get("selections") or []):
        if not _active(s):
            continue
        try:
            h = int(float(s.get("handicap")))
        except (TypeError, ValueError):
            continue
        if abs(float(s.get("handicap")) - h) > 1e-9:
            continue
        sel = str(s.get("selection") or "").strip().lower()
        v = _num(s.get("value"))
        if sel in ("home", "away") and v:
            rows.setdefault(h, {})[sel] = v
    pairs = {}
    for L, row in rows.items():
        home = row.get("home")
        away = rows.get(-L, {}).get("away")
        if home and away:
            pairs[L] = (home, away)
    return pairs


def _handicap_priority(L):
    """体彩让球盘优先 ±1，其次 0，再 ±2/±3。"""
    if abs(L) == 1:
        return 0
    if L == 0:
        return 1
    return 2 + abs(L)


def _fmt_handicap(L):
    return f"{L:+d}" if L else "0"


def derive_handicap(market, crs_market):
    """返回 (rq_mapped, flags)；rq_mapped 含 让球数/主胜/平/客胜。"""
    pairs = _integer_pairs(market)
    if not pairs:
        return {}, {"rq_source": "missing"}

    def score(pair):
        home, away = pair
        return abs(1.0 / home - 1.0 / away)

    valid = [L for L, p in pairs.items() if 0.9 <= 1 / p[0] + 1 / p[1] <= 1.2]
    # 体彩让球盘几乎总取 ±1（让强队让一球/受让一球）：优先 ±1，其次 0，再 ±2/±3。
    plus_minus_one = [L for L in (valid or list(pairs)) if abs(L) == 1]
    if plus_minus_one:
        pool = plus_minus_one
    elif 0 in (valid or pairs):
        pool = [0]
    else:
        pool = valid or list(pairs)
    best = min(pool, key=lambda L: (score(pairs[L]), _handicap_priority(L)))
    home, away = pairs[best]
    mapped = {"让球数": _fmt_handicap(best), "主胜": home, "客胜": away}
    flags = {
        "rq_source": "asian_handicap_integer",
        "rq_bookmaker": _bookmaker_name(market),
        "rq_line": best,
        "rq_candidates": sorted(pairs),
    }

    margin = crs_margin_probs(crs_market) if crs_market else {}
    p_draw = margin.get(-best)
    if p_draw and 0.005 <= p_draw <= 0.95:
        p_total = 1.0 / home + 1.0 / away
        ratio = p_draw / (1.0 - p_draw)
        p_adj = p_total * ratio / (1.0 + ratio)
        mapped["平"] = round(max(1.01, 1.0 / p_adj), 2)
        flags["rq_draw_derived_from_crs"] = True
    else:
        mapped["平"] = ""
        flags["rq_draw_missing"] = True
    return mapped, flags


# ---------------- 单场标准化 ----------------

def _cst_dt(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CST)


def half_time_scores(item):
    """半场比分：优先 1st Half；只有 2nd Half 时用 全场 - 下半场 反推。"""
    ft_h, ft_a = item.get("home_score"), item.get("away_score")
    if ft_h is None or ft_a is None:
        return None, None
    periods = item.get("periods") or []
    by_name = {str(p.get("name") or ""): p for p in periods if isinstance(p, dict)}
    first = by_name.get("1st Half")
    if first and first.get("home") is not None and first.get("away") is not None:
        return int(first["home"]), int(first["away"])
    second = by_name.get("2nd Half")
    if second and second.get("home") is not None and second.get("away") is not None:
        return int(ft_h) - int(second["home"]), int(ft_a) - int(second["away"])
    return None, None


def normalize_item(item, league_cfg=None):
    """Actor item -> 统一结构（odds_mapped 与赛程 CSV 列一一对应）。"""
    odds = item.get("odds") or {}
    markets = [m for m in (odds.get("markets") or []) if isinstance(m, dict)]

    spf_market = _pick_market(markets, "HOME_DRAW_AWAY", lambda m: len(map_spf(m)) == 3)
    ttg_market = _pick_market(markets, "OVER_UNDER", lambda m: len(_ou_lines(m)) >= 4)
    rq_market = _pick_market(markets, "ASIAN_HANDICAP", lambda m: len(_integer_pairs(m)) >= 1)
    hafu_market = _pick_market(markets, "HALF_FULL_TIME", lambda m: len(map_hafu(m)) >= 6)
    crs_market = _pick_market(markets, "CORRECT_SCORE", lambda m: len(map_crs(m)) >= 8)

    spf = map_spf(spf_market) if spf_market else {}
    if not spf:
        raise ValueError("无有效胜平负赔率（跳过本场）")
    ttg, ttg_flags = derive_total_goals(ttg_market) if ttg_market else ({}, {"ttg_source": "missing"})
    rq, rq_flags = derive_handicap(rq_market, crs_market) if rq_market else ({}, {"rq_source": "missing"})
    hafu = map_hafu(hafu_market) if hafu_market else {}
    crs = map_crs(crs_market) if crs_market else {}

    cfgs = item.get("competition") or {}
    kickoff = _cst_dt(item.get("start_time"))
    ht_h, ht_a = half_time_scores(item)
    flags = {
        "odds_source": "apify:flashscore",
        "match_id": str(item.get("id") or ""),
        "spf_bookmaker": _bookmaker_name(spf_market) if spf_market else "",
        "hafu_bookmaker": _bookmaker_name(hafu_market) if hafu_market else "",
        "crs_bookmaker": _bookmaker_name(crs_market) if crs_market else "",
        "has_ttg": bool(ttg),
        "has_rq": bool(rq),
        "has_hafu": bool(hafu),
        "has_crs": bool(crs),
    }
    flags.update(ttg_flags)
    flags.update(rq_flags)

    return {
        "match_id": str(item.get("id") or ""),
        "league_display": str(cfgs.get("display_name") or ""),
        "league_name": (league_cfg or {}).get("name") or str(cfgs.get("name") or ""),
        "league_full": (league_cfg or {}).get("full_name") or str(cfgs.get("display_name") or ""),
        "country": str(cfgs.get("country") or ""),
        "home": str((item.get("home") or {}).get("name") or ""),
        "away": str((item.get("away") or {}).get("name") or ""),
        "kickoff_utc": str(item.get("start_time") or ""),
        "kickoff_cst": kickoff.isoformat(timespec="minutes"),
        "status": str(item.get("status") or ""),
        "home_score": item.get("home_score"),
        "away_score": item.get("away_score"),
        "ht_home": ht_h,
        "ht_away": ht_a,
        "odds_mapped": {"spf": spf, "rq": rq, "ttg": ttg, "hafu": hafu, "crs": crs},
        "odds_flags": flags,
    }


# ---------------- 赛程行（与主环境 CSV 同构） ----------------

def build_schedule_row(norm, number):
    """标准化结果 -> CSV 行；列名与主环境 竞彩足球_{date}.csv 完全一致。"""
    dt = _cst_dt(norm["kickoff_utc"])
    row = {
        "编号": number,
        "联赛": norm["league_name"],
        "联赛全称": norm["league_full"],
        "主队": norm["home"],
        "主队排名": "",
        "客队": norm["away"],
        "客队排名": "",
        "开赛日期": dt.strftime("%Y-%m-%d"),
        "开赛时间": dt.strftime("%H:%M"),
        "matchId": norm["match_id"],
        "matchStatus": "Selling",
        "胜平负-主胜": norm["odds_mapped"]["spf"].get("主胜", ""),
        "胜平负-平": norm["odds_mapped"]["spf"].get("平", ""),
        "胜平负-客胜": norm["odds_mapped"]["spf"].get("客胜", ""),
        "让球数": norm["odds_mapped"]["rq"].get("让球数", ""),
        "让球-主胜": norm["odds_mapped"]["rq"].get("主胜", ""),
        "让球-平": norm["odds_mapped"]["rq"].get("平", ""),
        "让球-客胜": norm["odds_mapped"]["rq"].get("客胜", ""),
    }
    for k in TTG_NAMES:
        row[f"总进球-{k}"] = norm["odds_mapped"]["ttg"].get(k, "")
    for half in ("主胜", "平", "客胜"):
        for full in ("主胜", "平", "客胜"):
            col = f"半全场-半{half}-全{full}"
            row[col] = norm["odds_mapped"]["hafu"].get(f"半{half}-全{full}", "")
    for sc in CRS_SCORES:
        row[f"比分-{sc}"] = norm["odds_mapped"]["crs"].get(sc, "")
    return row


# ---------------- JSONL 存储 ----------------

def write_odds_jsonl(records, target_date, data_dir="data"):
    """写 data/odds/{date}.jsonl；同 matchId 以本次为准，其余保留（幂等合并）。"""
    folder = os.path.join(data_dir, "odds")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{target_date}.jsonl")
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get("match_id"):
                        existing[obj["match_id"]] = obj
        except (OSError, ValueError):
            existing = {}
    for rec in records:
        existing[rec["match_id"]] = rec
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        for mid in sorted(existing):
            fp.write(json.dumps(existing[mid], ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


# ---------------- 队名归一（供竞彩排除与结算配对） ----------------

def norm_team_name(name):
    s = str(name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s
