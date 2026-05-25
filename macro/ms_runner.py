#!/usr/bin/env python3
"""
multi-signal 数据采集运行器
在 altcoin-shadow-system 的 scheduler 中定时触发 multi-signal 的数据采集。

职责：
  1. 自动定位 multi-signal 项目目录
  2. 执行 scripts/run.sh（采集6维数据 + 聚合评分）
  3. 产出 output/signal_latest.json 供 macro/collector.py 消费
  4. 如果 multi-signal 未部署，静默跳过（不影响主流程）

调用方式：
  - scheduler.py 每 2~4 小时调用一次 run_macro_collection()
  - 或手动执行: python3 -m macro.ms_runner
"""

import logging
import os
import subprocess
import time
import json
from typing import Optional

logger = logging.getLogger("macro.ms_runner")

_MS_SEARCH_PATHS = [
    '/projects/sandbox/multi-signal',
    os.path.expanduser('~/.openclaw/skills/multi-signal'),
    os.path.expanduser('~/multi-signal'),
    os.environ.get('MS_PROJECT_PATH', ''),
]


def find_ms_project() -> Optional[str]:
    for path in _MS_SEARCH_PATHS:
        if not path:
            continue
        run_sh = os.path.join(path, 'scripts', 'run.sh')
        if os.path.exists(run_sh):
            return path
    return None


def run_ms_collection(timeout_sec: int = 180) -> dict:
    """执行 multi-signal 数据采集 (bash scripts/run.sh)"""
    t0 = time.monotonic()
    result = {'success': False, 'ms_dir': '', 'output_file': '', 'elapsed_sec': 0.0, 'error': ''}

    ms_dir = find_ms_project()
    if not ms_dir:
        result['error'] = 'multi-signal 项目未找到'
        logger.info("multi-signal 未部署，跳过采集")
        return result

    result['ms_dir'] = ms_dir
    run_sh = os.path.join(ms_dir, 'scripts', 'run.sh')
    output_file = os.path.join(ms_dir, 'output', 'signal_latest.json')
    old_mtime = os.path.getmtime(output_file) if os.path.exists(output_file) else 0

    logger.info(f"[MS Runner] 开始采集: {run_sh}")
    try:
        proc = subprocess.run(
            ['bash', run_sh], cwd=ms_dir,
            capture_output=True, text=True, timeout=timeout_sec,
            env={**os.environ, 'PATH': f"{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH', '')}"},
        )
        elapsed = time.monotonic() - t0
        result['elapsed_sec'] = round(elapsed, 2)
        if proc.returncode != 0:
            result['error'] = f"exit code {proc.returncode}"
            return result
    except subprocess.TimeoutExpired:
        result['error'] = f"超时({timeout_sec}s)"
        return result
    except Exception as e:
        result['error'] = str(e)
        return result

    if os.path.exists(output_file) and os.path.getmtime(output_file) > old_mtime:
        result['success'] = True
        result['output_file'] = output_file
        logger.info(f"[MS Runner] ✅ 完成 ({result['elapsed_sec']:.1f}s)")
    else:
        result['error'] = 'output 未更新'
    return result


def run_cmm_collection() -> dict:
    """执行 CMM 轻量决策采集"""
    import sys
    result = {'success': False, 'error': '', 'output_file': ''}
    cmm_paths = ['/projects/sandbox/Crypto-Market-Monitor', os.path.expanduser('~/Crypto-Market-Monitor')]
    cmm_path = None
    for p in cmm_paths:
        if os.path.exists(os.path.join(p, 'decision', 'engine.py')):
            cmm_path = p
            break
    if not cmm_path:
        result['error'] = 'CMM 未找到'
        return result

    if cmm_path not in sys.path:
        sys.path.insert(0, cmm_path)
    try:
        from decision.engine import DecisionEngine
        from data_collector.sentiment import SentimentCollector
        import asyncio

        async def _collect():
            sc = SentimentCollector()
            try:
                fg_data = await sc.get_fear_greed_index()
                return fg_data.get('value', 50) if fg_data else 50
            finally:
                await sc.close()

        loop = asyncio.new_event_loop()
        fg = loop.run_until_complete(_collect())
        loop.close()

        engine = DecisionEngine()
        decision = engine.decide(fear_greed=fg)

        output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'macro_output')
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, 'cmm_latest.json')
        with open(output_file, 'w') as f:
            json.dump({
                'timestamp': int(time.time()),
                'action': decision.action.value,
                'confidence': decision.confidence,
                'risk_level': decision.risk_level,
                'position_pct': decision.position_pct,
                'phase': decision.phase.value,
                'macro_signal': decision.macro_signal,
                'fear_greed': fg,
            }, f, indent=2)
        result['success'] = True
        result['output_file'] = output_file
        logger.info(f"[CMM] ✅ {decision.action.value} (FG={fg})")
    except Exception as e:
        result['error'] = str(e)
    finally:
        if cmm_path in sys.path:
            sys.path.remove(cmm_path)
    return result


def run_macro_collection():
    """scheduler 调用的顶层接口：采集 MS + CMM 数据"""
    logger.info("=== 宏观数据采集 ===")
    ms = run_ms_collection()
    cmm = run_cmm_collection()
    try:
        from macro.filter import _get_collector
        _get_collector().invalidate_cache()
    except Exception:
        pass
    logger.info(f"  MS={'✅' if ms['success'] else '⏭️ '+ms['error']} | CMM={'✅' if cmm['success'] else '⏭️ '+cmm['error']}")
    return {'ms': ms, 'cmm': cmm}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_macro_collection()
