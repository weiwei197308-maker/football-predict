# README · 阶段二（两阶段核心剧本锁定 + 规则引擎修正）

> 版本：2026-10-14。本文记录阶段二的**技术方案**（含方案 A 快照口径改造、赛果抓取日期边界、API 编码防线）；开发进度、实测结果与接手指南见 `项目交接备忘.md` 第八章（2026-10-12）、第九章（2026-10-13）与**第十章（2026-10-13 ~ 2026-10-14）**。

## 1. 技术目标

在“中国体育彩票足球分析 A 思路”下，把规则一致性从 LLM 自觉改为 Python 程序强制：

- 先锁定**比赛剧本与净胜球分布**，再由同一剧本推导体彩五大玩法（胜平负/让球/总进球/半全场/比分）；
- 剧本未通过程序校验时，**硬阻止**玩法分析，不产出虚假预测；
- 玩法层的规则错误由程序确定性修正，修正痕迹全部留档，**不覆盖原始预测**；
- **规则一致性修正 ≠ 预测准确率提高**——没有真实赛果时不得声称准确率提升。

## 2. 核心模块关系

```
analyze.py（编排 + LLM 交互）
  ├─ call_deepseek_core_script ──→ 阶段一：核心剧本 JSON
  │     └─ 入口调用 ensure_api_key_for_request：ASCII 校验，构建 HTTP 头前拦非法 Key（防 UnicodeEncodeError）
  ├─ analyze_core_script ──→ validate_core_script（lottery_rules）
  │     ├─ 通过 → 锁定剧本（CoreScriptResult：dict 子类，可三元组解包）
  │     ├─ 失败 → build_core_feedback_text 反馈重生成（最多 1 次）
  │     └─ 再失败 → 硬阻止：build_failed_result_row 写阻止行，本场不产生任何玩法预测
  ├─ apply_locked_core_script ──→ 锁定字段覆盖阶段二输出的对应字段
  ├─ post_process_prediction ──→ 玩法层确定性修正（入口 deepcopy）
  └─ main() ──→ validate_api_key fail-fast；写 CSV + 原始预测快照 JSON

lottery_rules.py（纯函数规则引擎，无网络依赖，判定逻辑唯一权威）
  ├─ 解析：parse_handicap / parse_score / parse_hafu_codes（中文转码）/ parse_total_goals_list
  ├─ 计算：compute_handicap_result / derive_handicap_recommendation
  ├─ 检查：run_consistency_checks → (hard_errors, soft_warnings, unjudgeable) / extract_handicap_direction
  └─ 剧本校验：validate_core_script / format_core_validation_report

compare_report.py（离线对照报告，只读原始文件；2026-10-13 方案 A）
  ├─ load_snapshot(date) = 读 output/原始预测快照_<date>.json → (按编号索引, 异常清单)
  ├─ build_snapshot_comparison = 快照口径：修正前=快照“原始玩法输出”，修正后=CSV 行（三视角 O/L/C）
  └─ build_match_comparison = 旧口径回退：无快照时以 CSV 行 + post_process_prediction 前后对照

get_results.py（体彩赛果抓取，被 review.py 复用；2026-10-14 修复跨零点漏抓）
  └─ fetch_tecai_results(date, allowed_nums) 双重边界：matchDate ∈ {销售日, 次日} 且 编号 ∈ 当日预测编号白名单
       （深夜场次跨零点开球，官方 matchDate 记为次日；旧 matchDate==date 严格过滤会把这些场次全部误杀）
```

review.py、generate_report.py 复用同一规则口径；`test_lottery_rules.py`（145 例）、`test_review.py`（29 例）与 `test_a_thesis_logic.py`（23 例，A 思路让球临界线/赔率辅助）是规则引擎、analyze.py 与 review.py 判定行为的权威规格。

> **2026-10-14 续修**：`review.py` 的判定逻辑已迁移 `lottery_rules.py`：删除本地重复实现（`get_result_label`/`parse_total_goals_rec`/`parse_correct_score_rec`/`parse_half_full_rec`），`check_spf/check_rq/check_ttg/check_crs/check_hafu` 与让球实际结果统一委托 `lottery_rules`（`parse_hafu_codes`/`parse_total_goals_list`/`parse_score`/`compute_handicap_result`/`extract_handicap_direction`/`get_result_label`+`cn_to_code`）。半全场中文描述口径分叉由此闭合（详见交接备忘 §10.9）。

> **2026-10-14 A 思路落库**：`lottery_rules.py` 新增 `analyze_handicap_line` 等纯函数（**让球临界净胜球 = -让球数**、净胜球区间 → 让球后可能方向、走盘风险、胜平负/让球分离判定、赔率隐含概率排序），`analyze.py` 将分析顺序按 A 思路重排（基本面与赔率 → 剧本 → 净胜球 → 让球(含临界线) → 半全场 → 总进球 → 比分）并接入 `post_process_prediction`（临界线说明进入 CSV「一致性检查」、临界净胜球进入被修正的让球理由）。赔率仅作市场倾向辅助，让球结论仍由程序推导。详见交接备忘 §10.10。

### 2.1 对照报告口径（方案 A 关键设计）

- **修正前数据来源**：优先 `原始预测快照_<date>.json` 的“原始玩法输出”（LLM 原始值）；**不再**对已修正 CSV 重跑 `post_process_prediction`（旧做法会使修正场次恒为 0）；无快照则回退旧口径并在报告头部标注“旧口径：无法恢复原始模型输出”。
- **三视角**：O=快照原始玩法输出、L=快照锁定核心剧本、C=CSV 最终输出；另算 `post_process(O 的深拷贝)` 作“仅规则后处理”参考值。
- **归因四类**（互斥归类，每场每玩法最多计 1 类）：
  - **核心剧本锁定覆盖**：比分最终 ⊆ 锁定允许集合（`_locked_covers_scores`：首选=锁定主比分，其余 ⊆ 锁定集合）；或胜平负最终=锁定值。
  - **玩法规则后处理**：`post_process(O) == C`（规则单独即可解释）。
  - **共同作用**：让球/总进球的证据是 **`primary_lock_effective`（主推被锁定改变且最终采纳）**；半全场的证据是 **`spf_lock_effective`（胜平负被锁定改变且最终采纳）**。**禁止**以“备选比分列表变化”作为跨玩法归因依据。
  - **无法归因**：以上均不成立（证据不足，保守归类）。
- **两维统计正交**：按玩法字段 与 按变化原因 两套计数**独立**，报告注明“两组数字不得相加；修正率 ≠ 命中率”。
- **类型异常防护**：`_safe_str_list`（仅 list/tuple，其余返回 `[]`）+ `primary_score` 仅接受 str；锁定字段类型异常时，向该场“快照问题”写**场次编号与字段名**并归“无法归因”，经 `[快照异常]` 通道显式报告（不静默忽略）。
- **拒覆盖 + 另存**：目标报告已存在则抛 `FileExistsError`，退出码 2（保护历史报告）；需在不覆盖前提下另存时用 `generate_compare_report(date, suffix="_vN")` 或 CLI 第二参数，文件名变为 `修正对照报告_<date><suffix>.txt`，suffix 仅允许字母/数字/下划线/连字符。main 退出码：1=无 CSV，2=拒覆盖，3=快照异常。

## 3. 规则引擎设计要点

- **让球**：`adjusted = 主队进球 + handicap`（主让为负、客让为正）后与客队进球比较；让球推荐由首选比分严格推导，禁止模型自由生成。
- **一致性三元组**：硬性错误 / 软性冲突 / 无法判定。数据缺失、无法识别一律进“无法判定”，**严禁默认通过**。
- **净胜球三态**：通过 / 冲突 / 无法判定；空区间、未知区间、比分非法 → 无法判定。
- **备选比分**：格式异常 → 硬性；方向不同 → 软性“替代剧本”；同方向区间外 → 软性提示。不得静默忽略，也不得伪装成同剧本备选。
- **总进球修正**：首选比分对应进球数缺失时**追加在列表末尾**，理由中声明“仅为一致性修正，不代表概率更高”。
- **半全场**：中文描述转 HDA 码 → 过滤与全场不兼容项 → 全不兼容/缺失时按全场结果规则兜底并标注“程序兜底生成，待模型确认”；`get_hafu_full_result` 双字符均须合法 HDA。
- **post_process 四态报告**：通过 / 已修正，复查通过 / 仍有冲突 / 无法判定；报告基于**修正后**状态重新检查生成。

## 4. 输入输出格式

### 阶段一核心剧本 JSON（LLM 输出，受控枚举）
```json
{
  "home_team": "...", "away_team": "...",
  "full_time_result": "主胜/平局/客胜",
  "net_goal_interval": "主胜2+/主胜1/平/客胜1/客胜2+",
  "primary_score": "2:1",
  "alternative_scores": ["1:0"],
  "match_script": "≤100字", "net_goal_reasoning": "...",
  "uncertainties": ["..."], "confidence": "低/中/高"
}
```

### 竞彩分析 CSV（22 字段，保持不变）
编号, 联赛, 对阵, 开赛, 胜平负推荐/理由, 让球推荐/理由, 总进球推荐/理由, 半全场推荐/理由, 比分推荐/理由, 信心指数, 综合分析, 历史数据, 核心剧本, 净胜球分布, 体彩让球数, 数据缺失说明, 一致性检查
- 被阻止行：`综合分析` 以“核心剧本校验未通过/分析失败：”开头，玩法字段为空（不伪造）。
- `_原始快照` 不进 CSV，另存 `output/原始预测快照_<date>.json`。

### 修正对照报告（compare_report.py）
`output/修正对照报告_<date>.txt`，两种口径：
- **快照口径**（有 `原始预测快照_<date>.json`）：头部标注“修正前=快照中的 LLM 原始玩法输出，修正后=竞彩分析 CSV 最终输出”；逐场含 13 项 + **变化归因行**（每变化玩法一类：锁定覆盖/规则后处理/共同作用/无法归因）；末尾含“按玩法字段统计”“按变化原因统计”两维块、CSV-only / 快照-only 场次清单、快照异常清单。
- **旧口径**（无快照）：头部标注“旧口径：无法恢复原始模型输出”，逐场 13 项（编号/主客队、原始剧本、原始主比分、原始净胜球分布、体彩让球数、修正前让球推荐、规则计算正确结果、半全场前后+删除项、总进球前后+补入项、备选比分冲突、一致性检查原因、数据缺失、实际赛果+前后命中），统计：总场数、三项修正场数及比例、备选冲突场数、数据缺失场数、修正前硬性错误场数、规则检查失败场数、有赛果时的前后命中率（附免责声明）。

## 5. 测试命令与验收标准

```bash
# 编译检查（12 个源文件）
python3 -m py_compile analyze.py lottery_rules.py review.py compare_report.py get_results.py generate_report.py test_core_script.py test_lottery_rules.py test_compare_report.py test_get_results.py test_review.py test_a_thesis_logic.py

# 单元测试（304 例：145 规则 + 26 核心剧本 + 75 对照报告 + 6 赛果抓取 + 29 review + 23 A 思路）
python3 -m unittest test_a_thesis_logic test_review test_lottery_rules test_core_script test_compare_report test_get_results

# 生成对照报告（拒覆盖：目标已存在则退出码 2；需另存时加 suffix）
python3 compare_report.py 2026-10-11         # 10-11 无快照 → 旧口径
python3 compare_report.py 2026-10-12         # 有快照 → 快照口径
python3 compare_report.py 2026-10-11 _v3     # 另存为 修正对照报告_2026-10-11_v3.txt（不覆盖）
python3 compare_report.py 2026-10-12 _v2     # 另存为 修正对照报告_2026-10-12_v2.txt（2026-10-14 重出，不覆盖旧报告）
# 注：10-11 已于 2026-10-14 再出 _v4（报告头换统一口径新说明，历史报告字节不变）
```

验收标准：
- **304 个单元测试**（145 规则 + 26 核心剧本 + 75 对照报告 + 6 赛果抓取 + 29 review + 23 A 思路），不得删测试或弱化断言。**当前实测：Ran 304，0 失败**（`test_caliber_unchanged` 已于 2026-10-14 按真实 10 场重新冻结；`test_a_thesis_logic.py` 为本轮 A 思路落库新增，见交接备忘 §10.10）。
- 对照报告统计与逐场明细一致；两维统计（玩法字段 / 变化原因）分别自洽；无赛果时不出现命中率结论。
- 归因证据充分：跨玩法“共同作用”不得仅凭“备选比分列表变化”判定；证据不足一律“无法归因”。
- 锁定核心剧本字段类型异常必须写入该场“快照问题”（含编号与字段名），不静默忽略。
- 原始 CSV / JSON 的 mtime 与内容不因报告生成而改变；目标报告已存在时拒绝覆盖（退出码 2）；用 suffix 另存时原报告字节不变。
- 赛果抓取：跨零点场次（编号属销售日、`matchDate` 为次日）必须被纳入，相邻销售日同 `matchDate` 场次必须被排除（白名单精确圈定，不跨日期回填）。
- Key 缺失/格式错时 `python3 analyze.py` 在发起任何网络请求前退出（exit 1）并给出设置指引，不回显完整密钥；`ensure_api_key_for_request` 在两个 LLM 调用函数入口再校验一次（防 latin-1 编码的 `UnicodeEncodeError`）。
- **真实 API 端到端**：`source ~/.zshrc && python3 analyze.py` 全场次跑通、阻止行/快照正确。**当前状态：review.py 归因链路已由真实 HTTP 调用验证（10-11 共 17 条、10-12 共 8 条、10-13 共 8 条，0 失败）；analyze.py 两阶段已由 10-14 12:26 完整运行日志（`output/run_logs/run_all_20261014_122636.log`，194 KB，含逐场输出与退出码）逐行核实，不再依赖“产物时间戳推断”。10-13 赛后复盘已完成：9 场跨零点场次全部被抓取、赛果完整，跨零点白名单修复通过首次真实检验；命中 胜平负 7/9、让球 4/9、总进球 4/9、半全场 6/9、比分 3/9（详见交接备忘 §10.9 第 8 条）。**

---

## 十二、复盘深度归因（2026-10-18 接入，详见交接备忘 §11）

复盘“错误归因”原有两条来源：规则引擎的确定性标记 + DeepSeek 的结果导向归因（只会“高估/低估”）。本轮新增第三条：**联网深度归因**——对错题场搜索真实赛后战报，找红牌/伤病/轮换/门将失误/裁判漏判/盘口异动等具体因素。独立脚本实现，review.py 主流程零改动。

### 命令

```bash
# run_all.sh 已六步化：取赛程 → 分析 → 报告 → 复盘 → 深度归因(软失败) → 推送
bash run_all.sh                        # 日常全链路，深度归因失败只警告不阻断

# 手动/补跑深度归因（读 每日记录_<日期>.json，挑命中=False 的场次）
python3 enrich_attribution.py 2026-10-17        # 输出 深度归因_<日期>.json/.txt，已存在则拒覆盖(退出码2)
python3 enrich_attribution.py 2026-09-27 _v2    # 另存，不覆盖
```

### 机制要点

- **API**：智谱 GLM + `web_search` 工具（`ZHIPU_API_KEY` 环境变量，非 ASCII/缺失在任何网络请求前拦截）；DeepSeek 官方 API 无联网工具。
- **两阶段调用**：①裸查询强制联网取材料 → ②无联网基于材料生成归因（事实只能来自材料，缺证据标“推测”）。单阶段长指令会抑制联网。
- **推送**：`push_feishu.py` 主推送后自动向**深度归因独立群**（`FEISHU_ATTR_WEBHOOK`，机器人关键词须含“深度归因”）推汇总卡+明细卡，成功写 `深度归因_<日期>.pushed` 标记防重发。
- **日报卡保护**：`MAX_CARD_ELEMENTS=120`，复盘明细超限整体折叠（全显示或全不显示）+ note 指向本地文件。

### 验收标准（新增部分）

- 深度归因为**可选增强**：智谱无额度/网络故障/产物已存在时，run_all.sh 打印“⚠️ 深度归因失败，跳过”后继续推送，`FAILED_STEPS` 不增加。
- 产物为全新文件，拒覆盖（退出码 2）+ `--suffix` 另存；推送有 `.pushed` 标记防重发；每日记录 / lessons.txt / review.py 不被触碰。
- ⚠️ **搜索摘要可能含错误事实**（实测 9-27 周日006 主客场被弄反）：深度归因结论**使用/传播前必须人工抽查**，JSON 内每场留有“战报材料”原文供核对；材料 <120 字视为过薄。输出头部带免责声明，不构成投注建议。
- **模拟盘接入（2026-10-25）**：`review_data_loader.load_review_data` 在读完每日记录后，自动叠加 `深度归因_<date>.json`——`模拟30.csv`“错题标签(归因)”列优先显示深度归因（带 `【深度归因】` 前缀留痕、折叠单行）；深度文件缺失/损坏/调用失败占位时安全降级沿用旧归因。详见交接备忘 §12。
