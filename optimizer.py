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

import random
import math

import yaml

BASE_DIR     = Path(__file__).parent
RNG = random.Random()
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
    "exit_len":     {"type": int,   "min": 10,   "max": 50},
    "reversal_score": {"type": int, "min": 2,    "max": 5},
    "rsi_mr_ob":    {"type": float, "min": 55.0, "max": 75.0},
    "rsi_mr_os":    {"type": float, "min": 25.0, "max": 45.0},
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


def suggest_params(results: dict, current_config: dict,
                   round_num: int, history: list) -> dict:
    """
    本地启发式优化器：根据回测结果诊断问题，给出参数建议。
    策略：
      1. 交易太少 → 放宽入场条件
      2. PF < 1 且交易足够 → 随机扰动参数（模拟退火）
      3. PF >= 1 → 在当前基础上微调提升 Sharpe
    """
    # 根据轮数决定探索幅度（早期大步，后期精细）
    temperature = max(0.05, 0.5 * math.exp(-round_num / 15))
    suggestions = {}

    for tf, r in results.items():
        if "error" in r:
            continue
        tf_cfg = current_config.get("timeframes", {}).get(tf, {})
        tf_sugg = {}

        pf     = r.get("Profit Factor") or 0
        trades = r.get("# Trades")      or 0
        sharpe = r.get("Sharpe Ratio")  or -99.0
        wr     = r.get("Win Rate [%]")  or 0

        # ── 诊断 1：交易太少 → 放宽过滤条件 ─────────────────────────
        if trades < 25:
            curr = tf_cfg.get("min_score", 4)
            tf_sugg["min_score"] = max(3, curr - 1)
            curr = tf_cfg.get("adx_threshold", 20.0)
            tf_sugg["adx_threshold"] = max(15.0, round(curr - 2.0, 1))
            curr = tf_cfg.get("vol_mult", 1.2)
            tf_sugg["vol_mult"] = max(1.0, round(curr - 0.2, 2))
            if tf_cfg.get("use_ci", True):
                tf_sugg["use_ci"] = False

        # ── 诊断 2：PF < 1 且交易足够 → 随机扰动寻找更好配置 ────────
        elif pf < 1.0:
            # 每轮选 3-4 个参数扰动
            params_pool = [
                "ut_key", "ut_atr", "ssl_len", "ssl_mult",
                "adx_threshold", "min_score", "sqz_bbm", "sqz_kcm",
                "ci_threshold", "vol_mult", "exit_len", "reversal_score",
                "rsi_mr_ob", "rsi_mr_os",
            ]
            n_params = RNG.randint(3, 5)
            chosen = RNG.sample(params_pool, min(n_params, len(params_pool)))
            for p in chosen:
                c = PARAM_CONSTRAINTS.get(p)
                if not c or p not in tf_cfg:
                    continue
                curr = tf_cfg[p]
                rng_width = (c["max"] - c["min"]) * temperature
                delta = RNG.uniform(-rng_width, rng_width)
                nv = max(c["min"], min(c["max"], curr + delta))
                if c["type"] is int:
                    nv = int(round(nv))
                    if p == "ssl_len" and nv % 2 != 0:
                        nv += 1
                    tf_sugg[p] = nv
                else:
                    tf_sugg[p] = round(nv, 4)

        # ── 诊断 3：PF >= 1 但 Sharpe 还可以更高 → 微调 ─────────────
        else:
            params_pool = [
                "ut_key", "adx_threshold", "ssl_len", "ssl2_len",
                "rsi_len", "macd_signal", "sqz_kcm", "exit_len", "reversal_score",
                "rsi_mr_ob", "rsi_mr_os",
            ]
            n_params = RNG.randint(2, 3)
            chosen = RNG.sample(params_pool, min(n_params, len(params_pool)))
            for p in chosen:
                c = PARAM_CONSTRAINTS.get(p)
                if not c or p not in tf_cfg:
                    continue
                curr = tf_cfg[p]
                rng_width = (c["max"] - c["min"]) * temperature * 0.5
                delta = RNG.uniform(-rng_width, rng_width)
                nv = max(c["min"], min(c["max"], curr + delta))
                if c["type"] is int:
                    nv = int(round(nv))
                    if p == "ssl_len" and nv % 2 != 0:
                        nv += 1
                    tf_sugg[p] = nv
                else:
                    tf_sugg[p] = round(nv, 4)

        # macd 约束
        mf = tf_sugg.get("macd_fast",  tf_cfg.get("macd_fast",  12))
        ms = tf_sugg.get("macd_slow",  tf_cfg.get("macd_slow",  26))
        if mf >= ms:
            tf_sugg.pop("macd_fast",  None)
            tf_sugg.pop("macd_slow",  None)

        if tf_sugg:
            suggestions[tf] = clamp_params(tf_sugg)

    return suggestions


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

        print("  本地优化器分析中...")
        try:
            suggestions = suggest_params(
                results, current_config, round_num, history
            )
        except Exception as e:
            print(f"  [优化器异常] {e}")
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
