"""影子实验统计 — 执行入口脚本。

用法: python3 shadow_experiment/run_manifest.py --date 2026-10-29

执行规范:
  §一 执行前检查（Git 状态 + SHA256 + 输入文件）
  §二 调用 validate_shadow + evaluate_shadow
  §三 写 O1-O5 报告
  §六 执行后检查（保护文件 SHA256 不变 + master 可重建）
  §六 写 O6 run_manifest.json
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from shadow_experiment import config
from shadow_experiment.utils import sha256_file, save_json
from shadow_experiment.validate_shadow import load_all_shadow_files
from shadow_experiment.evaluate_shadow import run_evaluate
from shadow_experiment.report_writer import write_all_reports


def _git_status():
    """返回 (status_text, HEAD)。"""
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "-uno"],
            cwd=_PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL)
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=_PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        return status, head
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "", ""


def _collect_protected_hashes():
    """收集保护文件列表（glob 展开）的 SHA256。"""
    hashes = {}
    for pattern in config.PROTECTED_FILE_PATTERNS:
        path = os.path.join(_PROJECT_ROOT, pattern)
        if os.path.isfile(path):
            hashes[pattern] = sha256_file(path)
    # 也收集当天所有正式 CSV / 快照 / 复盘 / 三玩法影子文件
    for sub in ("output",):
        dir_path = os.path.join(_PROJECT_ROOT, sub)
        if not os.path.isdir(dir_path):
            continue
        for fname in os.listdir(dir_path):
            fpath = os.path.join(dir_path, fname)
            if os.path.isfile(fpath) and (
                    fname.startswith("竞彩分析_") or
                    fname.startswith("原始预测快照_") or
                    fname.startswith("竞彩复盘_") or
                    fname.startswith("影子实验_总进球_") or
                    fname.startswith("影子实验_让球_") or
                    fname.startswith("影子实验_半全场_")):
                hashes[f"{sub}/{fname}"] = sha256_file(fpath)
    return hashes


def _hashes_match(h1, h2):
    """两份 hash dict 是否完全相同。"""
    if set(h1.keys()) != set(h2.keys()):
        return False, f"key 集合不同: 仅在 pre={set(h1)-set(h2)}, 仅在 post={set(h2)-set(h1)}"
    diffs = []
    for k in h1:
        if h1[k] != h2[k]:
            diffs.append(k)
    if diffs:
        return False, f"内容不同: {diffs}"
    return True, None


def _load_previous_manifest(date_str):
    """读取最近一次其他日期的 run_manifest，返回 (path, obj)；无可用则 (None, None)。"""
    paths = sorted(glob.glob(os.path.join(config.RUN_LOGS_DIR, "run_manifest_*.json")))
    paths = [p for p in paths
             if os.path.basename(p) != f"run_manifest_{date_str}.json"]
    if not paths:
        return None, None
    path = paths[-1]
    try:
        with open(path, encoding="utf-8") as f:
            return path, json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None


def _detect_missing_inputs(pre_hashes, prev_manifest):
    """对比上次运行前的输入文件清单，返回“上次存在、本次缺失”的文件列表。

    备份文件（.backup.）与锁文件（.lock）不计入，避免清理备份时误报。
    """
    if not prev_manifest:
        return []
    prev_hashes = (prev_manifest.get("pre_run") or {}).get("pre_run_hash") or {}
    missing = []
    for key in set(prev_hashes) - set(pre_hashes):
        if ".backup." in key or key.endswith(".lock"):
            continue
        missing.append(key)
    return sorted(missing)


def _play_file_path(play, date_str):
    """当日指定玩法的影子实验文件路径。"""
    pcfg = config.PLAY_CONFIG[play]
    return os.path.join(_PROJECT_ROOT, "output", f"{pcfg['文件前缀']}{date_str}.json")


def pre_run_checks(date_str):
    """§一 执行前检查。返回 (ok, report_dict, pre_hashes, pre_git_status)。"""
    report = {
        "check_time": datetime.now().isoformat(timespec="seconds"),
        "date": date_str,
    }

    # 1. Git 状态
    git_status, head = _git_status()
    report["git_HEAD"] = head
    report["git_status_porcelain"] = git_status
    if git_status.strip():
        report["git_warning"] = f"工作区非 clean: {git_status.strip()[:200]}"

    # 2-3. 输入文件存在性（三玩法任一存在即可继续；缺失玩法单独跳过）
    shadow_files = {play: _play_file_path(play, date_str)
                    for play in config.PLAY_ORDER}
    daily_path = os.path.join(_PROJECT_ROOT, "output", f"每日记录_{date_str}.json")
    report["shadow_files"] = {
        play: {"path": path, "exists": os.path.exists(path)}
        for play, path in shadow_files.items()
    }
    report["daily_file"] = {"path": daily_path, "exists": os.path.exists(daily_path)}

    # 4. 模块版本
    report["module_version"] = config.MODULE_VERSION
    report["schema_version"] = config.SCHEMA_VERSION

    # 5. pre_run SHA256
    pre_hashes = _collect_protected_hashes()
    report["pre_run_hash"] = pre_hashes

    # 5.1 与上次运行对比：检测输入文件静默消失（不阻断，仅告警）
    prev_path, prev_manifest = _load_previous_manifest(date_str)
    missing_inputs = _detect_missing_inputs(pre_hashes, prev_manifest)
    report["previous_manifest"] = prev_path
    report["missing_inputs"] = missing_inputs
    if missing_inputs:
        report["missing_inputs_warning"] = (
            f"与上次运行（{os.path.basename(prev_path)}）相比缺失 {len(missing_inputs)} 个输入文件，"
            "累计统计可能静默减少，请核对: " + "、".join(missing_inputs))

    # 6. 隔离确认（保护文件存在即可）
    report["isolation_ok"] = all(
        os.path.exists(os.path.join(_PROJECT_ROOT, p))
        for p in ("analyze.py", "lottery_rules.py", "settle_shadow.py")
    )

    # 输入文件：三玩法至少一个存在才继续；全部缺失才中止
    missing_plays = [play for play, path in shadow_files.items()
                     if not os.path.exists(path)]
    ok = len(missing_plays) < len(shadow_files)
    if not ok:
        report["abort_reason"] = (
            f"三种玩法的影子实验 JSON 均不存在（{date_str}）："
            + "、".join(os.path.basename(p) for p in shadow_files.values()))
    elif missing_plays:
        report["skipped_plays"] = missing_plays
    return ok, report, pre_hashes, git_status


def post_run_checks(pre_hashes, pre_git_status, outputs):
    """§六 执行后检查。返回 (ok, report_dict)。"""
    report = {
        "check_time": datetime.now().isoformat(timespec="seconds"),
    }

    # 6.1 保护文件 SHA256 不变（硬门禁）
    post_hashes = _collect_protected_hashes()
    hash_ok, hash_err = _hashes_match(pre_hashes, post_hashes)
    report["post_run_hash"] = post_hashes
    report["hash_invariant"] = hash_ok
    report["hash_invariant_detail"] = hash_err

    # 6.2 git tracked 文件无**新增**改动（对比 pre_run 的 git_status）
    post_git_status, _ = _git_status()
    pre_set = set(line for line in pre_git_status.splitlines() if line.strip())
    post_set = set(line for line in post_git_status.splitlines() if line.strip())
    new_tracked = [l for l in (post_set - pre_set) if not l.strip().startswith("??")]
    report["pre_git_tracked_count"] = len(pre_set - {l for l in pre_set if l.strip().startswith("??")})
    report["new_tracked_changes"] = new_tracked
    report["git_no_new_tracked"] = len(new_tracked) == 0

    ok = hash_ok and report["git_no_new_tracked"]
    return ok, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="销售日期 YYYY-MM-DD")
    args = parser.parse_args()

    date_str = args.date
    start = time.time()

    print(f"[run_manifest] 开始执行 {date_str} 的影子实验统计")

    # ========== §一 执行前检查 ==========
    print("  §一 执行前检查...")
    ok, pre_report, pre_hashes, pre_git_status = pre_run_checks(date_str)
    manifest = {"pre_run": pre_report}

    if not ok:
        print(f"  ❌ 执行前检查未通过: {pre_report.get('abort_reason', '未知')}")
        manifest["status"] = "ABORTED_PRE_CHECK"
        manifest["error"] = pre_report.get("abort_reason")
        os.makedirs(config.RUN_LOGS_DIR, exist_ok=True)
        save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
        sys.exit(1)

    print(f"  ✅ Git HEAD={pre_report['git_HEAD'][:8]}  输入文件就绪")

    if pre_report.get("missing_inputs"):
        print(f"  ⚠️ 与上次运行相比缺失 {len(pre_report['missing_inputs'])} 个输入文件"
              f"（累计统计可能静默减少）:")
        for item in pre_report["missing_inputs"]:
            print(f"     - {item}")

    play_results = {}
    outputs = {}
    result = None

    # ========== §二~§五 总进球（缺失当日文件则跳过该玩法） ==========
    tg_file = _play_file_path(config.PLAY_TTG, date_str)
    if not os.path.exists(tg_file):
        print(f"  [总进球] 当日影子文件不存在，跳过该玩法（{tg_file}）")
        play_results[config.PLAY_TTG] = {"status": "SKIPPED_NO_FILE"}
    else:
        # §二 数据处理 + §三 统计计算
        print("  §二 数据处理 + §三 统计计算...")
        try:
            result = run_evaluate(output_dir=os.path.join(_PROJECT_ROOT, "output"))
        except Exception as e:
            import traceback
            print(f"  ❌ 统计计算失败: {e}")
            traceback.print_exc()
            manifest["status"] = "ABORTED_EVALUATE"
            manifest["error"] = str(e)
            save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
            sys.exit(1)

        print(f"  ✅ 统计完成: N_settled={result['settled_records']}  阶段={result['phase_num']}")
        node = result.get("node_metrics") or {}
        if node.get("已到达"):
            print(f"  ✅ 300 场观察节点已生成（{node['玩法']} n={node['配对数']}）")

        # ========== §四 数据质量检查（阻断级） ==========
        qr = result["quality_report"]
        critical_failures = []
        if not qr.get("invariant_pass", True):
            critical_failures.append("分母不变式不成立")
        if qr.get("files_errors"):
            critical_failures.append(f"影子文件解析错误: {qr['files_errors']}")

        if critical_failures:
            print(f"  ❌ 质量检查阻断: {critical_failures}")
            manifest["status"] = "ABORTED_QUALITY"
            manifest["quality_failures"] = critical_failures
            save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
            sys.exit(1)
        print(f"  ✅ 数据质量检查通过")

        # ========== §五 输出报告 ==========
        print("  §五 生成报告...")
        try:
            outputs = write_all_reports(result, date_str)
        except Exception as e:
            import traceback
            print(f"  ❌ 报告生成失败: {e}")
            traceback.print_exc()
            manifest["status"] = "ABORTED_REPORT"
            manifest["error"] = str(e)
            save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
            sys.exit(1)
        print(f"  ✅ 报告生成完成: {len(outputs)} 个文件")
        for key, path in outputs.items():
            print(f"    {key}: {path}")
        play_results[config.PLAY_TTG] = {
            "status": "SUCCESS",
            "settled_records": result["settled_records"],
            "phase_num": result["phase_num"],
            "outputs": outputs,
        }

    # ========== §五-扩展 让球/半全场统计（独立文件、独立统计；缺失当日文件则跳过） ==========
    from shadow_experiment.evaluate_shadow import run_evaluate_play
    from shadow_experiment.report_writer import write_play_reports

    for play in (config.PLAY_RQ, config.PLAY_HAFU):
        play_file = _play_file_path(play, date_str)
        if not os.path.exists(play_file):
            print(f"  [{play}] 当日影子文件不存在，跳过该玩法（{play_file}）")
            play_results[play] = {"status": "SKIPPED_NO_FILE"}
            continue
        print(f"  [{play}] 统计计算...")
        try:
            p_result = run_evaluate_play(play, output_dir=os.path.join(_PROJECT_ROOT, "output"))
        except Exception as e:
            import traceback
            print(f"  ❌ [{play}] 统计计算失败: {e}")
            traceback.print_exc()
            manifest["status"] = "ABORTED_EVALUATE"
            manifest["error"] = f"[{play}] {e}"
            save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
            sys.exit(1)

        p_qr = p_result["quality_report"]
        p_node = p_result.get("node_metrics") or {}
        if p_node.get("已到达"):
            print(f"  ✅ [{play}] 300 场观察节点已生成（n={p_node['配对数']}）")
        p_failures = []
        if not p_qr.get("invariant_pass", True):
            p_failures.append(f"[{play}] 分母不变式不成立")
        if p_qr.get("files_errors"):
            p_failures.append(f"[{play}] 影子文件解析错误: {p_qr['files_errors']}")
        if p_failures:
            print(f"  ❌ [{play}] 质量检查阻断: {p_failures}")
            manifest["status"] = "ABORTED_QUALITY"
            manifest["quality_failures"] = p_failures
            save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
            sys.exit(1)

        try:
            p_outputs = write_play_reports(p_result, date_str)
        except Exception as e:
            import traceback
            print(f"  ❌ [{play}] 报告生成失败: {e}")
            traceback.print_exc()
            manifest["status"] = "ABORTED_REPORT"
            manifest["error"] = f"[{play}] {e}"
            save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
            sys.exit(1)
        print(f"  ✅ [{play}] N_settled={p_result['settled_records']}  "
              f"阶段={p_result['phase_num']}  报告 {len(p_outputs)} 个文件")
        play_results[play] = {
            "status": "SUCCESS",
            "settled_records": p_result["settled_records"],
            "phase_num": p_result["phase_num"],
            "outputs": p_outputs,
        }

    # ========== §六 执行后检查 ==========
    print("  §六 执行后检查...")
    post_ok, post_report = post_run_checks(pre_hashes, pre_git_status, outputs)
    manifest["post_run"] = post_report
    manifest["outputs"] = outputs

    if not post_ok:
        print(f"  ❌ 执行后检查未通过")
        print(f"    hash_invariant: {post_report.get('hash_invariant')} "
              f"({post_report.get('hash_invariant_detail')})")
        print(f"    git_tracked_clean: {post_report.get('git_tracked_clean')}")
        manifest["status"] = "ABORTED_POST_CHECK"
        save_json(f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json", manifest)
        sys.exit(1)
    print(f"  ✅ 执行后检查通过（SHA256 不变、git tracked clean）")

    # ========== 写 manifest ==========
    elapsed = round(time.time() - start, 1)
    manifest["status"] = "SUCCESS"
    manifest["elapsed_seconds"] = elapsed
    manifest["date"] = date_str
    manifest["phase_num"] = result["phase_num"] if result else None
    manifest["phase_desc"] = result["phase_desc"] if result else None
    manifest["settled_records"] = result["settled_records"] if result else 0
    manifest["total_records"] = result["total_records"] if result else 0
    manifest["plays"] = play_results

    os.makedirs(config.RUN_LOGS_DIR, exist_ok=True)
    manifest_path = f"{config.RUN_LOGS_DIR}/run_manifest_{date_str}.json"
    save_json(manifest_path, manifest)
    print(f"\n✅ 执行完成，耗时 {elapsed}s")
    print(f"   run_manifest: {manifest_path}")
    print(f"   报告目录: {config.REPORTS_DIR}/")


if __name__ == "__main__":
    main()
