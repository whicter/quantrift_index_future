"""
optimizer.py — 使用 Claude API 自动迭代优化三个周期的策略参数。

评分规则：
  - 每个周期必须满足 PF >= 1.0 且 trades >= min_trades
  - score = 加权平均 Sharpe（1H 40% / 4H 35% / 1D 25%）
  - 任何周期不达标 → score 扣 1.0

用法：
  python optimizer.py              # 默认 10 轮
  python optimizer.py --rounds 20
"""

import os
import re
import sys
import json
import copy
import argparse
import subprocess
from pathlib import Path

import yaml
import anthropic

BASE_DIR     = Path(__file__).parent
CONFIG_PATH  = BASE_DIR / "config.yaml"
RESULTS_DIR  = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# 每个周期可优化的参数及约束（共用同一套范围，各周期独立取值）
PARAM_CONSTRAINTS = {
    "ut_key":       {"type": float, "min": 0.5,  "max": 5.0},
    "ut_atr":       {"type": int,   "min": 5,    "max": 20},
    "ssl_len":      {"type": int,   "min": 10,   "max": 120, "step": 2},
    "ssl2_len":     {"type": int,   "min": 3,    "max": 20},
    "ssl_mult":     {"type": float, "min": 0.1,  "max": 0.5},
    "rsi_len":      {"type": int,   "min": 7,    "max": 21},
    "macd_fast":    {"type": int,   "min": 5,    "max": 20},
    "macd_slow":    {"type": int,   "min": 20,   "max": 50},
    "macd_signal":  {"type": int,   "min": 5,    "max": 15},
    "sqz_bbl":      {"type": int,   "min": 10,   "max": 30},
    "sqz_bbm":      {"type": float, "min": 1.5,  "max": 3.0},
    "sqz_kcl":      {"type": int,   "min": 10,   "max": 30},
    "sqz_kcm":      {"type": float, "min": 1.0,  "max": 2.5},
    "min_score":    {"type": int,   "min": 3,    "max": 6},
    "ci_len":       {"type": int,   "min": 10,   "max": 30},
    "ci_threshold": {"type": float, "min": 50.0, "max": 75.0},
    "adx_len":      {"type": int,   "min": 7,    "max": 28},
    "adx_threshold":{"type": float, "min": 15.0, "max": 40.0},
    "vol_len":      {"type": int,   "min": 10,   "max": 50},
    "vol_mult":     {"type": float, "min": 1.0,  "max": 3.0},
}

TF_WEIGHTS = {"1h": 0.40, "4h": 0.35, "1d": 0.25}


def clamp_params(params: dict) -> dict:
    result = {}
    for k, v in params.items():
        if k not in PARAM_CONSTRAINTS:
            continue
        c = PARAM_CONSTRAINTS[k]
        try:
            v = c["type"](v)
        except (TypeError, ValueError):
            continue
        v = max(c["min"], min(c["max"], v))
        if k == "ssl_len" and int(v) % 2 != 0:
            v = int(v) + 1
        if c["type"] is int:
            v = int(v)
        result[k] = round(v, 4) if c["type"] is float else v
    return result


def _python_exe() -> str:
    exe = sys.executable
    if exe and os.path.isfile(exe):
        return exe
    fallback = os.path.expanduser("~/.pyenv/versions/3.12.7/bin/python")
    if os.path.isfile(fallback):
        return fallback
    return "python3"


def run_backtest_subprocess() -> dict:
    """子进程运行回测，返回 {tf_name: report} 字典。"""
    cmd  = [_python_exe(), str(BASE_DIR / "backtest_runner.py")]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR))
    if proc.returncode != 0:
        raise RuntimeError(f"回测失败:\n{proc.stderr[-2000:]}")
    with open(RESULTS_DIR / "latest_report.json") as f:
        return json.load(f)


def score_results(results: dict, min_trades: int) -> float:
    """
    加权平均 Sharpe，不达标周期扣分。
    results: {tf_name: report_dict}
    """
    total  = 0.0
    weight = 0.0
    penalty = 0.0

    for tf, r in results.items():
        if "error" in r:
            penalty += 1.0
            continue
        pf     = r.get("Profit Factor") or 0
        trades = r.get("# Trades") or 0
        sharpe = r.get("Sharpe Ratio") or float("-inf")
        if sharpe == float("-inf") or sharpe is None:
            sharpe = -99

        w = TF_WEIGHTS.get(tf, 0.33)
        total  += w * sharpe
        weight += w

        if pf < 1.0 or trades < min_trades:
            penalty += 0.5

    if weight == 0:
        return float("-inf")
    return (total / weight) - penalty


def ask_claude(results: dict, current_config: dict,
               round_num: int, history: list, optimize_for: str) -> dict:
    """
    调用 Claude claude-opus-4-6（adaptive thinking + streaming）。
    返回 {tf_name: {param: value, ...}} 格式的建议。
    """
    client = anthropic.Anthropic()

    constraints_text = "\n".join(
        f"  {k}: {c['type'].__name__}, [{c['min']}, {c['max']}]"
        + (f", 步长={c.get('step',1)}" if "step" in c else "")
        for k, c in PARAM_CONSTRAINTS.items()
    )

    recent = history[-5:] if len(history) > 5 else history
    history_text = json.dumps(
        [{"round": h["round"], "score": round(h["score"], 3),
          "tf_results": {tf: {"sharpe": round(r.get("Sharpe Ratio") or -99, 3),
                              "pf":     round(r.get("Profit Factor") or 0, 3),
                              "trades": r.get("# Trades") or 0}
                         for tf, r in h["tf_results"].items()},
          "changed": h.get("changed_params", {})}
         for h in recent],
        indent=2
    )

    # 当前各周期可优化参数快照
    tf_params_text = {}
    for tf, tf_cfg in current_config.get("timeframes", {}).items():
        tf_params_text[tf] = {k: tf_cfg[k] for k in PARAM_CONSTRAINTS if k in tf_cfg}

    # 当前回测结果摘要
    results_summary = {}
    for tf, r in results.items():
        if "error" not in r:
            results_summary[tf] = {
                "Sharpe Ratio":     r.get("Sharpe Ratio"),
                "Profit Factor":    r.get("Profit Factor"),
                "Win Rate [%]":     r.get("Win Rate [%]"),
                "# Trades":         r.get("# Trades"),
                "Return [%]":       r.get("Return [%]"),
                "Max. Drawdown [%]":r.get("Max. Drawdown [%]"),
            }

    prompt = f"""你是量化策略优化专家。以下是三个周期的回测结果，请给出参数调整建议。

## 第 {round_num} 轮回测结果
```json
{json.dumps(results_summary, indent=2)}
```

## 当前各周期参数
```json
{json.dumps(tf_params_text, indent=2)}
```

## 近期优化历史（最近5轮）
```json
{history_text}
```

## 参数约束（各周期共用同一范围）
```
{constraints_text}
```
注意：macd_fast < macd_slow；ssl_len 必须为偶数。

## 优化目标
最大化加权平均 Sharpe（1H×40% + 4H×35% + 1D×25%）。
**每个周期必须独立盈利**（PF >= 1.0，交易次数 >= 20）。
当前评分权重：1H最重要，因为小仓位但交易最频繁。

请返回一个 JSON，格式为各周期需要修改的参数：
{{"1h": {{"min_score": 5}}, "4h": {{"adx_threshold": 25}}, "1d": {{}}}}

不要解释，直接输出 JSON。"""

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        response = stream.get_final_message()

    text = ""
    for block in response.content:
        if hasattr(block, "text") and block.type == "text":
            text = block.text
            break

    # 提取最外层的 {...} JSON
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        print(f"  [警告] Claude 未返回有效 JSON:\n  {text[:300]}")
        return {}

    try:
        raw = json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"  [警告] JSON 解析失败: {e}")
        return {}

    # 对每个 tf 的建议做 clamp
    result = {}
    for tf, suggestions in raw.items():
        if isinstance(suggestions, dict) and suggestions:
            result[tf] = clamp_params(suggestions)
    return result


def apply_suggestions(config: dict, suggestions: dict) -> tuple[dict, dict]:
    """将 Claude 建议写入 config，返回 (new_config, actually_changed)。"""
    new_config = copy.deepcopy(config)
    changed    = {}

    for tf, params in suggestions.items():
        if tf not in new_config.get("timeframes", {}):
            continue
        tf_changed = {}
        current_tf = new_config["timeframes"][tf]

        for k, v in params.items():
            # macd_fast < macd_slow
            if k == "macd_fast":
                slow = params.get("macd_slow", current_tf.get("macd_slow", 26))
                if v >= slow:
                    continue
            if k == "macd_slow":
                fast = params.get("macd_fast", current_tf.get("macd_fast", 12))
                if v <= fast:
                    continue

            if current_tf.get(k) != v:
                current_tf[k] = v
                tf_changed[k] = v

        if tf_changed:
            changed[tf] = tf_changed

    return new_config, changed


def optimize(n_rounds: int = 10):
    with open(CONFIG_PATH) as f:
        init_config = yaml.safe_load(f)

    optimize_for = init_config.get("optimize_for", "Sharpe Ratio")
    min_trades   = init_config.get("min_trades",   20)

    history: list = []
    best_config   = copy.deepcopy(init_config)
    best_results  = None
    best_score    = float("-inf")

    print(f"\n{'═'*56}")
    print(f"  开始优化: {n_rounds} 轮  目标: {optimize_for}")
    print(f"  标的: {init_config['symbol']}")
    print(f"  周期: {list(init_config.get('timeframes', {}).keys())}")
    print(f"{'═'*56}")

    for round_num in range(1, n_rounds + 1):
        print(f"\n── Round {round_num}/{n_rounds} ──────────────────────────────")

        try:
            results = run_backtest_subprocess()
        except RuntimeError as e:
            print(f"  [ERROR] {e}")
            continue

        score = score_results(results, min_trades)

        # 打印当前轮结果
        for tf, r in results.items():
            if "error" not in r:
                print(f"  {tf}: Sharpe={r.get('Sharpe Ratio') or 0:.3f}  "
                      f"PF={r.get('Profit Factor') or 0:.3f}  "
                      f"Trades={r.get('# Trades') or 0}")
        print(f"  综合评分: {score:.4f}")

        with open(CONFIG_PATH) as f:
            current_config = yaml.safe_load(f)

        record = {
            "round":      round_num,
            "score":      score if score != float("-inf") else -99,
            "tf_results": results,
            "changed_params": {},
        }

        if score > best_score:
            best_score   = score
            best_results = results
            best_config  = copy.deepcopy(current_config)
            print(f"  *** 新最优! score = {score:.4f} ***")

        history.append(record)

        if round_num == n_rounds:
            break

        print("  Claude 分析中...")
        try:
            suggestions = ask_claude(
                results, current_config, round_num, history, optimize_for
            )
        except Exception as e:
            print(f"  [Claude 调用失败] {e}")
            suggestions = {}

        if not suggestions:
            print("  无参数变更。")
        else:
            new_config, changed = apply_suggestions(current_config, suggestions)
            if changed:
                with open(CONFIG_PATH, "w") as f:
                    yaml.dump(new_config, f, default_flow_style=False,
                              allow_unicode=True)
                history[-1]["changed_params"] = changed
                print(f"  更新参数: {changed}")
            else:
                print("  建议无实质变化，跳过。")

    # ── 收尾 ───────────────────────────────────────────────
    print(f"\n{'═'*56}")
    print(f"  优化完成! 最优综合评分 = {best_score:.4f}")
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(best_config, f, default_flow_style=False, allow_unicode=True)
    print("  最优参数已还原到 config.yaml")

    summary = {
        "best_score":   best_score,
        "optimize_for": optimize_for,
        "best_results": best_results,
        "best_config":  {tf: {k: v for k, v in cfg.items()
                              if k in PARAM_CONSTRAINTS}
                         for tf, cfg in best_config.get("timeframes", {}).items()},
        "rounds":       history,
    }
    hist_path = RESULTS_DIR / "optimization_history.json"
    with open(hist_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"  历史保存至: {hist_path}")
    print(f"{'═'*56}\n")

    return best_config, best_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()
    optimize(n_rounds=args.rounds)
