"""
optimizer.py — 使用 Claude API 自动迭代优化策略参数。

流程：
  1. 运行回测 → 读取 JSON 报告
  2. 发给 Claude → 获取参数调整建议
  3. 更新 config.yaml → 下一轮
  4. N 轮后：还原最优参数并输出汇总

用法：
  python optimizer.py              # 默认 10 轮
  python optimizer.py --rounds 20  # 指定轮数
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

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# 允许被优化的参数及其约束
PARAM_CONSTRAINTS = {
    "ut_key":        {"type": float, "min": 0.5,  "max": 5.0},
    "ut_atr":        {"type": int,   "min": 5,    "max": 20},
    "ssl_len":       {"type": int,   "min": 20,   "max": 120, "step": 2},  # 必须为偶数
    "ssl2_len":      {"type": int,   "min": 3,    "max": 20},
    "ssl_mult":      {"type": float, "min": 0.1,  "max": 0.5},
    "rsi_len":       {"type": int,   "min": 7,    "max": 21},
    "macd_fast":     {"type": int,   "min": 5,    "max": 20},
    "macd_slow":     {"type": int,   "min": 20,   "max": 50},
    "macd_signal":   {"type": int,   "min": 5,    "max": 15},
    "sqz_bbl":       {"type": int,   "min": 10,   "max": 30},
    "sqz_bbm":       {"type": float, "min": 1.5,  "max": 3.0},
    "sqz_kcl":       {"type": int,   "min": 10,   "max": 30},
    "sqz_kcm":       {"type": float, "min": 1.0,  "max": 2.5},
    "min_score":     {"type": int,   "min": 3,    "max": 6},
    "ci_len":        {"type": int,   "min": 10,   "max": 30},
    "ci_threshold":  {"type": float, "min": 50.0, "max": 75.0},
}


def clamp_params(params: dict) -> dict:
    """将 Claude 建议的参数约束到合法范围并保证类型正确。"""
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
        # ssl_len 必须为偶数
        if k == "ssl_len" and int(v) % 2 != 0:
            v = int(v) + 1
        # 整数参数取整
        if c["type"] is int:
            v = int(v)
        # macd_fast < macd_slow
        result[k] = round(v, 4) if c["type"] is float else v
    return result


def _python_exe() -> str:
    """找到正确的 Python 可执行文件路径。"""
    # 优先用运行 optimizer.py 的同一个解释器
    exe = sys.executable
    if exe and os.path.isfile(exe):
        return exe
    # 回退到 pyenv 3.12.7
    fallback = os.path.expanduser("~/.pyenv/versions/3.12.7/bin/python")
    if os.path.isfile(fallback):
        return fallback
    return "python3"


def run_backtest_subprocess() -> dict:
    """子进程运行回测，返回 JSON 报告。"""
    cmd = [_python_exe(), str(BASE_DIR / "backtest_runner.py")]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR))
    if proc.returncode != 0:
        raise RuntimeError(f"回测失败:\n{proc.stderr[-2000:]}")
    with open(RESULTS_DIR / "latest_report.json") as f:
        return json.load(f)


def ask_claude(report: dict, current_params: dict,
               round_num: int, history: list, optimize_for: str) -> dict:
    """
    调用 Claude claude-opus-4-6（adaptive thinking + streaming），
    返回建议的参数修改 dict。
    """
    client = anthropic.Anthropic()

    constraints_text = "\n".join(
        f"  {k}: {c['type'].__name__}, [{c['min']}, {c['max']}]"
        + (f", 步长={c.get('step',1)}" if "step" in c else "")
        for k, c in PARAM_CONSTRAINTS.items()
    )

    # 只传最近 5 轮历史，控制 token
    recent_history = history[-5:] if len(history) > 5 else history
    history_text = json.dumps(
        [{"round": h["round"], "sharpe": round(h["sharpe"], 3),
          "return_pct": round(h["return_pct"], 2),
          "max_dd": round(h["max_dd"], 2),
          "trades": h["trades"],
          "changed": h.get("changed_params", {})}
         for h in recent_history],
        indent=2
    )

    current_opt_params = {
        k: current_params[k]
        for k in PARAM_CONSTRAINTS
        if k in current_params
    }

    prompt = f"""你是一位量化策略优化专家。请分析以下回测结果，给出参数调整建议。

## 第 {round_num} 轮回测结果
```json
{json.dumps({k: v for k, v in report.items() if k not in ("yearly", "params", "timestamp")}, indent=2)}
```

## 当前参数
```json
{json.dumps(current_opt_params, indent=2)}
```

## 近期优化历史（最近 5 轮）
```json
{history_text}
```

## 参数约束
```
{constraints_text}
```
注意：macd_fast 必须小于 macd_slow；ssl_len 必须为偶数。

## 优化目标
最大化 **{optimize_for}**，同时保持交易次数 > 10，最大回撤 < 50%。

请返回一个 JSON 对象，仅包含你建议修改的参数及其新值。
不要解释，直接输出 JSON。示例格式：
{{"min_score": 3, "ssl_mult": 0.15}}"""

    # 使用流式 + adaptive thinking
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=512,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        response = stream.get_final_message()

    # 从响应中提取 JSON（text block 可能夹杂思考内容）
    text = ""
    for block in response.content:
        if hasattr(block, "text") and block.type == "text":
            text = block.text
            break

    # 找到第一个 {...} JSON 对象
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not match:
        print(f"  [警告] Claude 未返回有效 JSON，跳过本轮更新。原始回复:\n  {text[:300]}")
        return {}

    try:
        raw = json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"  [警告] JSON 解析失败: {e}")
        return {}

    return clamp_params(raw)


def optimize(n_rounds: int = 10):
    """主优化循环。"""
    with open(CONFIG_PATH) as f:
        init_params = yaml.safe_load(f)

    optimize_for = init_params.get("optimize_for", "Sharpe Ratio")
    min_trades   = init_params.get("min_trades", 10)

    history: list = []
    best_params   = copy.deepcopy(init_params)
    best_report   = None
    best_score    = float("-inf")

    print(f"\n{'═'*52}")
    print(f"  开始优化: {n_rounds} 轮  目标: {optimize_for}")
    print(f"  标的: {init_params['symbol']} {init_params['interval']}")
    print(f"{'═'*52}")

    for round_num in range(1, n_rounds + 1):
        print(f"\n── Round {round_num}/{n_rounds} ─────────────────────────────")

        # 运行回测
        try:
            report = run_backtest_subprocess()
        except RuntimeError as e:
            print(f"  [ERROR] {e}")
            continue

        sharpe   = report.get("Sharpe Ratio") or float("-inf")
        n_trades = report.get("# Trades") or 0
        ret_pct  = report.get("Return [%]") or 0.0
        max_dd   = report.get("Max. Drawdown [%]") or 0.0

        print(f"  Sharpe={sharpe:.3f}  Return={ret_pct:.1f}%  "
              f"MaxDD={max_dd:.1f}%  Trades={n_trades}")

        with open(CONFIG_PATH) as f:
            current_params = yaml.safe_load(f)

        # 记录历史
        record = {
            "round":      round_num,
            "sharpe":     sharpe if sharpe != float("-inf") else -99,
            "return_pct": ret_pct,
            "max_dd":     max_dd,
            "trades":     n_trades,
            "params":     {k: current_params[k]
                           for k in PARAM_CONSTRAINTS if k in current_params},
            "changed_params": {},
        }

        # 判断是否为最优
        score = sharpe if optimize_for == "Sharpe Ratio" else ret_pct
        if score > best_score and n_trades >= min_trades:
            best_score  = score
            best_report = report
            best_params = copy.deepcopy(current_params)
            print(f"  *** 新最优! {optimize_for} = {score:.4f} ***")

        history.append(record)

        # 最后一轮不需要再优化
        if round_num == n_rounds:
            break

        # 询问 Claude
        print("  Claude 分析中...")
        try:
            suggestions = ask_claude(
                report, current_params, round_num, history, optimize_for
            )
        except Exception as e:
            print(f"  [Claude 调用失败] {e}")
            suggestions = {}

        if not suggestions:
            print("  无参数变更。")
        else:
            # 验证 macd 约束
            mf = suggestions.get("macd_fast", current_params.get("macd_fast", 12))
            ms = suggestions.get("macd_slow", current_params.get("macd_slow", 26))
            if mf >= ms:
                suggestions.pop("macd_slow", None)
                suggestions.pop("macd_fast", None)

            current_params.update(suggestions)
            with open(CONFIG_PATH, "w") as f:
                yaml.dump(current_params, f, default_flow_style=False,
                          allow_unicode=True)
            history[-1]["changed_params"] = suggestions
            print(f"  更新参数: {suggestions}")

    # ── 收尾：还原最优参数 ─────────────────────────────────────────
    print(f"\n{'═'*52}")
    print(f"  优化完成! 最优 {optimize_for} = {best_score:.4f}")
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(best_params, f, default_flow_style=False, allow_unicode=True)
    print("  最优参数已还原到 config.yaml")

    # 保存优化历史
    summary = {
        "best_score":      best_score,
        "optimize_for":    optimize_for,
        "best_report":     best_report,
        "best_params":     {k: best_params[k]
                            for k in PARAM_CONSTRAINTS if k in best_params},
        "rounds":          history,
    }
    history_path = RESULTS_DIR / "optimization_history.json"
    with open(history_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"  历史保存至: {history_path}")
    print(f"{'═'*52}\n")

    return best_params, best_report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10, help="优化轮数（默认 10）")
    args = parser.parse_args()
    optimize(n_rounds=args.rounds)
