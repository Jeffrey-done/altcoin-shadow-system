#!/usr/bin/env python3
"""
系统自检 (Smoke Test) — 服务器部署后的端到端健康验证

设计目标：
  ✓ 一键跑、人能看懂结果
  ✓ 既能 CLI（cron / 部署后立刻验证），又能从 admin panel 调用
  ✓ 自我隔离：每个 check 独立 try/except，单点失败不影响其他
  ✓ 不会下任何真单 / 不修改任何状态（只读 + 公共 API）

四个阶段：
  Phase A · 配置 (静态)    无需网络/API key —— 依赖装齐、.env 字段、config 一致性、数据文件完整
  Phase B · 网络 (公共)    无需 API key —— Binance/OKX/Telegram 公共端点、行情可读
  Phase C · 实盘 (需 key)  调用现有交易所凭证，仅 fetch_balance 不下单
  Phase D · 运行时         检查候选池/交易/风控数据是否在流动

CLI 用法：
    python3 smoke_test.py                           # 跑 A B D（默认跳过 C）
    python3 smoke_test.py --phases A,B,C,D          # 全部跑（含实盘凭证认证）
    python3 smoke_test.py --phases A               # 只跑某阶段
    python3 smoke_test.py --json                    # 输出机器可解析的 JSON（cron 用）
    python3 smoke_test.py --no-color                # 非彩色输出（日志收集用）

接入 admin panel：
    POST /<ADMIN_URL_SECRET>/api/smoke-test
    Body: {"phases": ["A","B","D"]}
    Response: {"results":[...], "summary":{...}, "all_ok": bool}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Dict, Optional

# 让脚本能被任何工作目录调起
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


# ══════════════════════════════════════════════════════════════════
#  数据模型
# ══════════════════════════════════════════════════════════════════

class Status:
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class CheckResult:
    id: str               # 例如 "A1"
    phase: str            # "A" | "B" | "C" | "D"
    name: str             # 人可读
    status: str           # Status.*
    detail: str = ""      # 详情（成功也填，方便用户看到具体数字）
    duration_ms: int = 0
    error: Optional[str] = None  # 异常时填 traceback 摘要

    def is_ok(self) -> bool:
        return self.status in (Status.PASS, Status.SKIP, Status.WARN)


# ══════════════════════════════════════════════════════════════════
#  Check 注册器（装饰器）
# ══════════════════════════════════════════════════════════════════

# 存放结构: phase -> [(id, name, fn)]
_REGISTRY: Dict[str, List] = {"A": [], "B": [], "C": [], "D": []}


def check(phase: str, cid: str, name: str):
    """注册一个 check 函数。函数应返回 (status, detail) 元组或 CheckResult。"""
    def deco(func: Callable):
        _REGISTRY[phase].append((cid, name, func))
        return func
    return deco


def _normalize(cid: str, phase: str, name: str, raw, t0: float) -> CheckResult:
    """把 check 函数的返回值归一化为 CheckResult"""
    duration_ms = int((time.monotonic() - t0) * 1000)
    if isinstance(raw, CheckResult):
        raw.id = cid
        raw.phase = phase
        raw.name = name
        raw.duration_ms = duration_ms
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        status, detail = raw
        return CheckResult(id=cid, phase=phase, name=name, status=status,
                           detail=detail, duration_ms=duration_ms)
    return CheckResult(id=cid, phase=phase, name=name, status=Status.FAIL,
                       detail=f"check 返回值格式错误: {raw!r}",
                       duration_ms=duration_ms)


# ══════════════════════════════════════════════════════════════════
#  Phase A · 配置 (静态，无网络)
# ══════════════════════════════════════════════════════════════════

@check("A", "A1", ".env 关键字段")
def _check_env_keys():
    """TG_BOT_TOKEN / TG_CHAT_ID 这些核心字段必须配置"""
    required = ["TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        return Status.FAIL, f"缺失：{', '.join(missing)}（参考 .env.example）"
    return Status.PASS, f"已配置 {len(required)} 项核心字段"


@check("A", "A2", "config 跨字段一致性")
def _check_config_consistency():
    """validate_cross_field_consistency 检查 stake/balance/leverage 等参数组合是否合理"""
    try:
        import runtime_config
        errors, warnings = runtime_config.validate_cross_field_consistency({})
    except Exception as e:
        return Status.FAIL, f"调用 validate_cross_field_consistency 失败: {e}"
    if errors:
        return Status.FAIL, f"{len(errors)} 个错误: {'; '.join(errors[:3])}"
    if warnings:
        return Status.WARN, f"{len(warnings)} 个警告: {'; '.join(warnings[:3])}"
    return Status.PASS, "配置一致性 OK，0 errors / 0 warnings"


@check("A", "A3", "数据文件 JSON 完整性")
def _check_data_files():
    """关键 JSON 文件能解析（不存在视为新部署，PASS；损坏则 FAIL）"""
    from common import (
        TRADES_FILE, CANDIDATES_FILE, RISK_FILE, WEEKLY_REPORT_FILE,
    )
    targets = {
        "trades": TRADES_FILE,
        "candidates": CANDIDATES_FILE,
        "risk_state": RISK_FILE,
        "weekly_report": WEEKLY_REPORT_FILE,
    }
    broken = []
    sizes = {}
    for label, path in targets.items():
        if not os.path.exists(path):
            sizes[label] = "—"
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            sizes[label] = f"{os.path.getsize(path):,}B"
        except Exception as e:
            broken.append(f"{label}({e.__class__.__name__})")
    if broken:
        return Status.FAIL, f"损坏: {', '.join(broken)}"
    summary = ", ".join(f"{k}={v}" for k, v in sizes.items())
    return Status.PASS, summary


@check("A", "A4", "依赖库可导入")
def _check_imports():
    """关键依赖能 import；ccxt/requests/flask 缺一不可"""
    failed = []
    for mod in ["ccxt", "requests", "flask", "dotenv", "websocket"]:
        try:
            __import__(mod)
        except Exception as e:
            failed.append(f"{mod}({e.__class__.__name__})")
    if failed:
        return Status.FAIL, f"缺失/损坏: {', '.join(failed)}"
    return Status.PASS, "ccxt / requests / flask / dotenv / websocket-client 全部可导入"


# ══════════════════════════════════════════════════════════════════
#  Phase B · 网络可达 (公共 API)
# ══════════════════════════════════════════════════════════════════

def _http_ping(url: str, timeout: float = 5.0) -> tuple:
    """通用 HTTP ping，返回 (latency_ms, status_code) 或 raise"""
    import requests as _rq
    t0 = time.monotonic()
    r = _rq.get(url, timeout=timeout)
    latency = int((time.monotonic() - t0) * 1000)
    return latency, r.status_code


@check("B", "B1", "Binance 公共 API")
def _check_binance_public():
    try:
        latency, code = _http_ping("https://api.binance.com/api/v3/ping", timeout=5)
        if code != 200:
            return Status.FAIL, f"HTTP {code}"
        return Status.PASS, f"{latency}ms"
    except Exception as e:
        return Status.FAIL, f"{e.__class__.__name__}: {e}"


@check("B", "B2", "OKX 公共 API")
def _check_okx_public():
    try:
        latency, code = _http_ping("https://www.okx.com/api/v5/public/time", timeout=5)
        if code != 200:
            return Status.FAIL, f"HTTP {code}"
        return Status.PASS, f"{latency}ms"
    except Exception as e:
        return Status.FAIL, f"{e.__class__.__name__}: {e}"


@check("B", "B3", "Telegram API")
def _check_telegram_api():
    """getMe 验证 bot token + 网络通路（需 TG_BOT_TOKEN）"""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token:
        return Status.SKIP, "TG_BOT_TOKEN 未配置（A1 已报错）"
    try:
        latency, code = _http_ping(
            f"https://api.telegram.org/bot{token}/getMe", timeout=5
        )
        if code == 200:
            return Status.PASS, f"{latency}ms"
        if code == 401:
            return Status.FAIL, f"HTTP 401 — bot token 无效"
        return Status.FAIL, f"HTTP {code}"
    except Exception as e:
        return Status.FAIL, f"{e.__class__.__name__}: {e}"


@check("B", "B4", "BTC 24h 行情可读")
def _check_btc_quote():
    """通过 exchange_manager 抓取 BTC 24h 涨跌，验证完整行情链路"""
    try:
        from exchange_manager import get_btc_24h_change_multi
        pct = get_btc_24h_change_multi()
        if pct is None:
            return Status.FAIL, "返回 None（Binance + OKX 都失败）"
        return Status.PASS, f"BTC 24h = {pct:+.2f}%"
    except Exception as e:
        return Status.FAIL, f"{e.__class__.__name__}: {e}"


# ══════════════════════════════════════════════════════════════════
#  Phase C · 实盘凭证 (需要 API key)
# ══════════════════════════════════════════════════════════════════

def _check_exchange_auth(exchange_id: str) -> tuple:
    """通用：fetch_balance 验证凭证 + 余额够 DEFAULT_STAKE"""
    try:
        import admin_secrets
        creds = admin_secrets.get_exchange_credentials(exchange_id)
    except Exception as e:
        return Status.SKIP, f"读取凭证失败 (admin_secrets 未初始化): {e}"

    api_key = creds.get("api_key", "") if creds else ""
    if not api_key:
        # 回退到环境变量
        env_key_name = "BINANCE_API_KEY" if exchange_id == "binance" else "OKX_API_KEY"
        api_key = os.environ.get(env_key_name, "")
        if not api_key:
            return Status.SKIP, f"{exchange_id} API key 未配置（在 admin panel 凭证 tab 设置）"

    try:
        from exchange_manager import make_exchange
        kwargs = dict(api_key=api_key, secret=creds.get("secret", ""))
        if exchange_id == "okx":
            kwargs["passphrase"] = creds.get("passphrase", "")
            kwargs["default_type"] = "swap"
        else:
            kwargs["default_type"] = "future"
        ex = make_exchange(exchange_id, **kwargs)
    except Exception as e:
        return Status.FAIL, f"创建交易所对象失败: {e}"

    try:
        balance = ex.fetch_balance()
        usdt = balance.get("USDT", {})
        total = float(usdt.get("total", 0) or 0)
        free = float(usdt.get("free", 0) or 0)
    except Exception as e:
        return Status.FAIL, f"fetch_balance 失败（凭证错误或权限不足）: {e}"

    try:
        import config as _cfg
        min_required = _cfg.DEFAULT_STAKE
    except Exception:
        min_required = 0

    if total < min_required:
        return Status.WARN, (
            f"余额 total={total:.2f}U / free={free:.2f}U "
            f"低于 DEFAULT_STAKE={min_required}U"
        )
    return Status.PASS, f"凭证 OK，total={total:.2f}U / free={free:.2f}U"


@check("C", "C1", "Binance 凭证认证")
def _check_binance_auth():
    return _check_exchange_auth("binance")


@check("C", "C2", "OKX 凭证认证")
def _check_okx_auth():
    return _check_exchange_auth("okx")


# ══════════════════════════════════════════════════════════════════
#  Phase D · 运行时数据流
# ══════════════════════════════════════════════════════════════════

@check("D", "D1", "候选池新鲜度")
def _check_candidates_freshness():
    """候选池存在且最近 25 小时内有更新（每天 00:30 UTC 刷新）"""
    from common import CANDIDATES_FILE
    if not os.path.exists(CANDIDATES_FILE):
        return Status.WARN, "候选池文件不存在（系统刚启动？）"
    age_sec = time.time() - os.path.getmtime(CANDIDATES_FILE)
    age_hours = age_sec / 3600
    try:
        with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        n = len(data) if isinstance(data, list) else len(data.get("candidates", []))
    except Exception as e:
        return Status.FAIL, f"读取失败: {e}"
    if age_hours > 25:
        return Status.WARN, f"候选池 {age_hours:.1f}h 未更新（>25h），N={n}"
    return Status.PASS, f"{age_hours:.1f}h 前刷新，N={n}"


@check("D", "D2", "Trades 文件健康")
def _check_trades_health():
    """trades 文件可读，open trade 数量 ≤ MAX_OPEN_TRADES"""
    from common import TRADES_FILE
    if not os.path.exists(TRADES_FILE):
        return Status.PASS, "trades 文件不存在（无历史交易，新部署？）"
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return Status.FAIL, f"读取失败: {e}"
    if not isinstance(data, list):
        return Status.FAIL, "trades.json 不是列表（数据格式异常）"
    n_total = len(data)
    n_open = sum(1 for t in data if t.get("status") == "open")
    try:
        import config as _cfg
        cap = getattr(_cfg, "MAX_OPEN_TRADES", 999)
    except Exception:
        cap = 999
    if n_open > cap:
        return Status.WARN, f"open={n_open} > MAX_OPEN_TRADES={cap}"
    return Status.PASS, f"total={n_total}, open={n_open} (cap={cap})"


@check("D", "D3", "Risk state 一致性")
def _check_risk_state():
    """risk_state.total_open_stake ≈ trades 中 open stake 之和"""
    try:
        import risk_control
        state = risk_control.load_risk_state()
        derived = risk_control._calc_actual_open_stake()
    except Exception as e:
        return Status.FAIL, f"读取风控状态失败: {e}"
    drift = state.total_open_stake - derived
    if abs(drift) > 0.5:
        return Status.WARN, (
            f"漂移: state.total_open_stake={state.total_open_stake:.2f} "
            f"vs trades 反算={derived:.2f}（差 {drift:+.2f}U）"
        )
    return Status.PASS, (
        f"total_open_stake={state.total_open_stake:.2f}U / 反算={derived:.2f}U"
    )


@check("D", "D4", "TG 通路冒烟（可选）")
def _check_tg_send():
    """有 TG token 的话，调用 sendMessage 发一条静默测试消息"""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not (token and chat_id):
        return Status.SKIP, "TG 未完整配置"
    try:
        import requests as _rq
        r = _rq.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": "🧪 smoke_test: TG 通路 OK",
                "disable_notification": "true",
            },
            timeout=8,
        )
        if r.status_code == 200:
            return Status.PASS, "已发出测试消息（已静默）"
        return Status.FAIL, f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return Status.FAIL, f"{e.__class__.__name__}: {e}"


# ══════════════════════════════════════════════════════════════════
#  Runner
# ══════════════════════════════════════════════════════════════════

VALID_PHASES = ("A", "B", "C", "D")
DEFAULT_PHASES = ("A", "B", "D")  # 默认跳过 C（实盘凭证），避免不必要的鉴权


def run_phase(phase: str) -> List[CheckResult]:
    """跑单个 phase，返回所有 CheckResult（每个 check 独立 try/except）"""
    if phase not in _REGISTRY:
        raise ValueError(f"未知阶段: {phase}")
    out: List[CheckResult] = []
    for cid, name, fn in _REGISTRY[phase]:
        t0 = time.monotonic()
        try:
            raw = fn()
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            out.append(CheckResult(
                id=cid, phase=phase, name=name,
                status=Status.FAIL,
                detail=f"未捕获异常: {e.__class__.__name__}: {e}",
                duration_ms=duration_ms,
                error=repr(e),
            ))
            continue
        out.append(_normalize(cid, phase, name, raw, t0))
    return out


def run_phases(phases=DEFAULT_PHASES) -> dict:
    """跑指定 phases，返回 {results, summary}"""
    phases = tuple(p for p in phases if p in VALID_PHASES)
    if not phases:
        phases = DEFAULT_PHASES
    all_results: List[CheckResult] = []
    for p in phases:
        all_results.extend(run_phase(p))
    summary = _summarize(all_results, phases)
    return {
        "results": [asdict(r) for r in all_results],
        "summary": summary,
    }


def _summarize(results: List[CheckResult], phases) -> dict:
    by_status = {Status.PASS: 0, Status.FAIL: 0, Status.WARN: 0, Status.SKIP: 0}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "phases": list(phases),
        "total": len(results),
        "pass": by_status[Status.PASS],
        "fail": by_status[Status.FAIL],
        "warn": by_status[Status.WARN],
        "skip": by_status[Status.SKIP],
        "all_ok": by_status[Status.FAIL] == 0,
    }


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════

# ANSI color codes — 默认开，可 --no-color 关
_C = {
    "reset": "\033[0m", "bold": "\033[1m",
    "green": "\033[92m", "red": "\033[91m",
    "yellow": "\033[93m", "cyan": "\033[96m",
    "gray": "\033[90m",
}

_STATUS_GLYPH = {
    Status.PASS: "✓",
    Status.FAIL: "✗",
    Status.WARN: "!",
    Status.SKIP: "·",
}

_STATUS_COLOR = {
    Status.PASS: "green",
    Status.FAIL: "red",
    Status.WARN: "yellow",
    Status.SKIP: "gray",
}


def _color(text: str, color: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{_C[color]}{text}{_C['reset']}"


def _render_text(payload: dict, use_color: bool) -> str:
    lines = []
    by_phase = {}
    for r in payload["results"]:
        by_phase.setdefault(r["phase"], []).append(r)

    phase_titles = {
        "A": "配置 (静态)",
        "B": "网络可达",
        "C": "实盘凭证",
        "D": "运行时数据",
    }

    for phase in payload["summary"]["phases"]:
        rows = by_phase.get(phase, [])
        title = f"Phase {phase} · {phase_titles.get(phase, phase)} ({len(rows)} 项)"
        lines.append("")
        lines.append(_color("═" * 64, "cyan", use_color))
        lines.append(_color(f"  {title}", "bold", use_color))
        lines.append(_color("═" * 64, "cyan", use_color))
        for r in rows:
            glyph = _STATUS_GLYPH.get(r["status"], "?")
            color = _STATUS_COLOR.get(r["status"], "gray")
            head = _color(f"  {glyph} {r['id']:<3} {r['name']}", color, use_color)
            tail = _color(f" — {r['detail']}", "gray", use_color)
            ms = _color(f"  ({r['duration_ms']}ms)", "gray", use_color)
            lines.append(head + tail + ms)

    s = payload["summary"]
    lines.append("")
    lines.append(_color("═" * 64, "cyan", use_color))
    overall = "全部通过" if s["all_ok"] else "存在失败项"
    overall_color = "green" if s["all_ok"] else "red"
    lines.append(_color(f"  总结: {overall}", overall_color, use_color) + (
        f"  ({s['total']} 项, "
        f"pass={s['pass']} fail={s['fail']} warn={s['warn']} skip={s['skip']})"
    ))
    lines.append(_color("═" * 64, "cyan", use_color))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_test.py",
        description="系统自检 — 服务器部署后的端到端健康验证",
    )
    parser.add_argument(
        "--phases", default=",".join(DEFAULT_PHASES),
        help=f"要跑的阶段，逗号分隔。可选: {','.join(VALID_PHASES)} "
             f"（默认 {','.join(DEFAULT_PHASES)}，跳过 C 因其需 API key）",
    )
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 而非彩色文本（监控/日志收集用）")
    parser.add_argument("--no-color", action="store_true",
                        help="禁用 ANSI 颜色（终端不支持时用）")
    args = parser.parse_args(argv)

    phases = [p.strip().upper() for p in args.phases.split(",") if p.strip()]
    invalid = [p for p in phases if p not in VALID_PHASES]
    if invalid:
        parser.error(f"非法阶段: {','.join(invalid)}（合法值 {','.join(VALID_PHASES)}）")

    payload = run_phases(phases)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        # 自动关闭彩色：非 tty / Windows cmd 老版 / --no-color
        use_color = (not args.no_color) and sys.stdout.isatty()
        print(_render_text(payload, use_color))

    return 0 if payload["summary"]["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
