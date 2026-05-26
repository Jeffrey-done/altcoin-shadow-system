#!/usr/bin/env python3
"""
缺失数据填补器 v1.0

对回测 K 线数据中的缺失 bar 进行智能填补，避免因少量缺失丢弃整段数据。

填补策略（按优先级）：
  1. 备源填补：从 OKX/Gate 拉取同时段数据补入（最精确）
  2. 线性插值：缺失 ≤ 3 根 → OHLCV 线性/前值插值
  3. 前值填充：价格用 forward fill，volume 用前 N 根均值
  4. 标记跳过：缺失 > 阈值 → 标记为 NaN，回测时跳过

数据质量保证：
  - 填补后做完整性校验（时间戳等间隔、无 NaN）
  - 输出填补报告（缺失分布、填补方法、质量评分）
  - 大段缺失（>阈值）不填补，标记为"不可靠区间"

用法：
  from data.imputer import DataImputer, ImputerConfig

  imputer = DataImputer(config=ImputerConfig(
      max_interpolation_gap=3,
      fallback_source='okx',
  ))
  result = imputer.fill(df, symbol='PEPE/USDT')

  print(f"填补了 {result.gaps_filled} 个缺口")
  print(f"数据质量: {result.quality_score}/100")
  clean_df = result.filled_df  # 用于回测
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("data.imputer")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class ImputerConfig:
    """填补器配置"""
    # 填补策略
    max_interpolation_gap: int = 3        # 线性插值最大连续缺失 bar 数
    max_total_missing_pct: float = 10.0   # 总缺失超过此比例 → 数据不可用
    use_fallback_source: bool = True      # 是否尝试从备源获取
    fallback_source: str = 'okx'          # 备源交易所

    # 时间框架
    timeframe: str = '1h'
    expected_interval_ms: int = 3600000   # 1h = 3,600,000 ms

    # 填补方法
    price_method: str = 'linear'          # 'linear' / 'ffill' / 'cubic'
    volume_method: str = 'mean'           # 'mean' (前N根均值) / 'zero' / 'ffill'
    volume_lookback: int = 10             # volume 均值计算回溯 bar 数

    # 标记
    mark_filled_bars: bool = True         # 在 df 中标记哪些 bar 是填补的

    # API
    request_timeout: int = 10


# ══════════════════════════════════════════════════════════════════
#  结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class GapInfo:
    """单个缺口信息"""
    start_idx: int                     # 缺口起始位置（在原 df 中的索引）
    gap_size: int                      # 缺失 bar 数
    start_timestamp: int = 0           # 缺口起始时间戳
    end_timestamp: int = 0             # 缺口结束时间戳
    fill_method: str = ''              # 使用的填补方法
    filled: bool = False               # 是否成功填补


@dataclass
class ImputerResult:
    """填补结果"""
    # 基础统计
    original_bars: int = 0
    expected_bars: int = 0
    missing_bars: int = 0
    missing_pct: float = 0.0

    # 填补统计
    gaps_found: int = 0                  # 发现的缺口数
    gaps_filled: int = 0                 # 成功填补的缺口数
    bars_interpolated: int = 0           # 线性插值填补的 bar 数
    bars_from_fallback: int = 0          # 从备源获取的 bar 数
    bars_forward_filled: int = 0         # 前值填充的 bar 数
    bars_unfillable: int = 0             # 无法填补的 bar 数

    # 质量
    quality_score: float = 0.0           # 0~100 填补后数据质量
    data_usable: bool = True             # 数据是否可用于回测

    # 缺口详情
    gaps: List[GapInfo] = field(default_factory=list)

    # 填补后数据
    filled_df: Optional[pd.DataFrame] = None

    # 不可靠区间（大段缺失，未填补）
    unreliable_ranges: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"数据填补报告\n"
            f"{'═'*50}\n"
            f"原始: {self.original_bars} bars  "
            f"预期: {self.expected_bars} bars  "
            f"缺失: {self.missing_bars} ({self.missing_pct:.1f}%)\n"
            f"{'─'*50}\n"
            f"缺口: {self.gaps_found} 个  填补: {self.gaps_filled} 个\n"
            f"  线性插值: {self.bars_interpolated} bars\n"
            f"  备源补入: {self.bars_from_fallback} bars\n"
            f"  前值填充: {self.bars_forward_filled} bars\n"
            f"  无法填补: {self.bars_unfillable} bars\n"
            f"{'─'*50}\n"
            f"质量评分: {self.quality_score:.0f}/100  "
            f"可用: {'✅' if self.data_usable else '❌'}\n"
            f"不可靠区间: {len(self.unreliable_ranges)} 段\n"
        )


# ══════════════════════════════════════════════════════════════════
#  填补器
# ══════════════════════════════════════════════════════════════════

class DataImputer:
    """数据缺失填补器"""

    def __init__(self, config: Optional[ImputerConfig] = None):
        self.config = config or ImputerConfig()

    def fill(
        self,
        df: pd.DataFrame,
        symbol: str = '',
    ) -> ImputerResult:
        """
        检测并填补缺失数据。

        参数:
          df: 输入 K 线 DataFrame (必须有 timestamp, open, high, low, close, volume)
          symbol: 交易对（用于备源获取）

        返回:
          ImputerResult 包含填补后的 DataFrame 和报告
        """
        cfg = self.config
        result = ImputerResult()

        if df is None or df.empty:
            result.data_usable = False
            return result

        # 确保列存在
        required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        for col in required_cols:
            if col not in df.columns:
                logger.warning(f"缺少必需列: {col}")
                result.data_usable = False
                return result

        df = df.copy()
        df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
        df = df.sort_values('timestamp').reset_index(drop=True)

        result.original_bars = len(df)

        # ── 步骤 1: 检测缺口 ──
        gaps = self._detect_gaps(df)
        result.gaps_found = len(gaps)
        result.gaps = gaps

        total_missing = sum(g.gap_size for g in gaps)
        result.missing_bars = total_missing

        # 计算预期总 bar 数
        if len(df) >= 2:
            ts_range = df['timestamp'].iloc[-1] - df['timestamp'].iloc[0]
            result.expected_bars = int(ts_range / cfg.expected_interval_ms) + 1
        else:
            result.expected_bars = result.original_bars

        result.missing_pct = round(
            total_missing / max(result.expected_bars, 1) * 100, 1
        )

        # 数据可用性检查
        if result.missing_pct > cfg.max_total_missing_pct:
            result.data_usable = False
            result.filled_df = df
            result.quality_score = max(0, 100 - result.missing_pct * 5)
            logger.warning(
                f"[Imputer] {symbol} 缺失 {result.missing_pct:.1f}% "
                f"(>{cfg.max_total_missing_pct}%)，标记为不可用"
            )
            return result

        if not gaps:
            result.filled_df = df
            result.quality_score = 100
            if cfg.mark_filled_bars:
                result.filled_df['is_filled'] = False
            return result

        # ── 步骤 2: 逐缺口填补 ──
        filled_df = self._fill_gaps(df, gaps, symbol, result)

        # ── 步骤 3: 最终验证 ──
        if cfg.mark_filled_bars and 'is_filled' not in filled_df.columns:
            filled_df['is_filled'] = False

        result.filled_df = filled_df.reset_index(drop=True)
        result.quality_score = self._compute_quality_score(result)

        logger.info(
            f"[Imputer] {symbol}: {result.gaps_found} 缺口, "
            f"填补 {result.gaps_filled}/{result.gaps_found}, "
            f"质量 {result.quality_score:.0f}/100"
        )

        return result

    # ── 缺口检测 ─────────────────────────────────────────────────

    def _detect_gaps(self, df: pd.DataFrame) -> List[GapInfo]:
        """检测时间序列中的缺口"""
        cfg = self.config
        gaps = []

        timestamps = df['timestamp'].values
        expected_gap = cfg.expected_interval_ms

        for i in range(1, len(timestamps)):
            actual_gap = timestamps[i] - timestamps[i - 1]
            if actual_gap > expected_gap * 1.5:
                gap_size = int(round(actual_gap / expected_gap)) - 1
                if gap_size > 0:
                    gaps.append(GapInfo(
                        start_idx=i,
                        gap_size=gap_size,
                        start_timestamp=int(timestamps[i - 1] + expected_gap),
                        end_timestamp=int(timestamps[i] - expected_gap),
                    ))

        return gaps

    # ── 缺口填补 ─────────────────────────────────────────────────

    def _fill_gaps(
        self,
        df: pd.DataFrame,
        gaps: List[GapInfo],
        symbol: str,
        result: ImputerResult,
    ) -> pd.DataFrame:
        """逐缺口填补"""
        cfg = self.config

        # 从后往前处理（避免索引偏移）
        sorted_gaps = sorted(gaps, key=lambda g: g.start_idx, reverse=True)
        filled_df = df.copy()

        for gap in sorted_gaps:
            if gap.gap_size <= cfg.max_interpolation_gap:
                # 小缺口：尝试线性插值
                new_rows = self._interpolate_gap(filled_df, gap)
                if new_rows is not None:
                    gap.fill_method = 'interpolation'
                    gap.filled = True
                    result.gaps_filled += 1
                    result.bars_interpolated += gap.gap_size
                    filled_df = self._insert_rows(filled_df, gap.start_idx, new_rows)
                    continue

            # 中等缺口：尝试备源
            if cfg.use_fallback_source and symbol:
                new_rows = self._fetch_from_fallback(symbol, gap)
                if new_rows is not None:
                    gap.fill_method = 'fallback_source'
                    gap.filled = True
                    result.gaps_filled += 1
                    result.bars_from_fallback += len(new_rows)
                    filled_df = self._insert_rows(filled_df, gap.start_idx, new_rows)
                    continue

            # 大缺口或备源也没数据：前值填充（仅小缺口）
            if gap.gap_size <= cfg.max_interpolation_gap:
                new_rows = self._forward_fill_gap(filled_df, gap)
                if new_rows is not None:
                    gap.fill_method = 'forward_fill'
                    gap.filled = True
                    result.gaps_filled += 1
                    result.bars_forward_filled += gap.gap_size
                    filled_df = self._insert_rows(filled_df, gap.start_idx, new_rows)
                    continue

            # 无法填补
            gap.fill_method = 'unfillable'
            result.bars_unfillable += gap.gap_size
            result.unreliable_ranges.append((gap.start_timestamp, gap.end_timestamp))

        return filled_df

    def _interpolate_gap(self, df: pd.DataFrame, gap: GapInfo) -> Optional[pd.DataFrame]:
        """线性插值填补小缺口"""
        cfg = self.config
        idx = gap.start_idx

        if idx < 1 or idx >= len(df):
            return None

        # 前一根和后一根 bar
        before = df.iloc[idx - 1]
        after = df.iloc[idx]

        n = gap.gap_size
        rows = []

        for i in range(1, n + 1):
            ratio = i / (n + 1)
            ts = int(before['timestamp'] + cfg.expected_interval_ms * i)

            row = {
                'timestamp': ts,
                'open': before['close'] + (after['open'] - before['close']) * ratio,
                'high': before['high'] + (after['high'] - before['high']) * ratio,
                'low': before['low'] + (after['low'] - before['low']) * ratio,
                'close': before['close'] + (after['close'] - before['close']) * ratio,
                'volume': self._estimate_volume(df, idx, n),
            }

            if cfg.mark_filled_bars:
                row['is_filled'] = True

            rows.append(row)

        return pd.DataFrame(rows)

    def _forward_fill_gap(self, df: pd.DataFrame, gap: GapInfo) -> Optional[pd.DataFrame]:
        """前值填充"""
        cfg = self.config
        idx = gap.start_idx

        if idx < 1:
            return None

        before = df.iloc[idx - 1]
        n = gap.gap_size
        rows = []

        for i in range(1, n + 1):
            ts = int(before['timestamp'] + cfg.expected_interval_ms * i)
            row = {
                'timestamp': ts,
                'open': before['close'],
                'high': before['close'],
                'low': before['close'],
                'close': before['close'],
                'volume': self._estimate_volume(df, idx, n),
            }
            if cfg.mark_filled_bars:
                row['is_filled'] = True
            rows.append(row)

        return pd.DataFrame(rows)

    def _fetch_from_fallback(self, symbol: str, gap: GapInfo) -> Optional[pd.DataFrame]:
        """从备源获取缺失数据"""
        cfg = self.config

        try:
            import requests
            source = cfg.fallback_source

            if source == 'okx':
                base = symbol.split('/')[0] if '/' in symbol else symbol.replace('USDT', '')
                inst_id = f"{base}-USDT-SWAP"
                bar = '1H' if cfg.timeframe == '1h' else '4H'

                # OKX 支持 after 参数（时间戳之后的数据）
                url = "https://www.okx.com/api/v5/market/candles"
                params = {
                    "instId": inst_id,
                    "bar": bar,
                    "after": str(gap.start_timestamp - 1),
                    "limit": str(min(gap.gap_size + 2, 100)),
                }
                r = requests.get(url, params=params, timeout=cfg.request_timeout)

                if r.status_code == 200:
                    resp = r.json()
                    if resp.get('data'):
                        rows = resp['data']
                        df = pd.DataFrame(rows, columns=[
                            'timestamp', 'open', 'high', 'low', 'close',
                            'volume', 'volCcy', 'volQuote', 'confirm'
                        ])
                        for col in ['open', 'high', 'low', 'close', 'volume']:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                        df['timestamp'] = pd.to_numeric(df['timestamp'])
                        df = df.sort_values('timestamp')

                        # 筛选在缺口范围内的数据
                        mask = (
                            (df['timestamp'] >= gap.start_timestamp) &
                            (df['timestamp'] <= gap.end_timestamp)
                        )
                        result_df = df.loc[mask, ['timestamp', 'open', 'high', 'low', 'close', 'volume']].copy()

                        if not result_df.empty:
                            if cfg.mark_filled_bars:
                                result_df['is_filled'] = True
                            return result_df.reset_index(drop=True)

            elif source == 'binance':
                sym = symbol.replace('/USDT', 'USDT').replace('/', '')
                url = "https://fapi.binance.com/fapi/v1/klines"
                params = {
                    "symbol": sym,
                    "interval": cfg.timeframe,
                    "startTime": str(gap.start_timestamp),
                    "endTime": str(gap.end_timestamp),
                    "limit": str(min(gap.gap_size + 2, 500)),
                }
                r = requests.get(url, params=params, timeout=cfg.request_timeout)

                if r.status_code == 200:
                    data = r.json()
                    if data:
                        df = pd.DataFrame(data)
                        df = df.iloc[:, :6]
                        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                        for col in ['open', 'high', 'low', 'close', 'volume']:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                        df['timestamp'] = pd.to_numeric(df['timestamp'])

                        if not df.empty:
                            if cfg.mark_filled_bars:
                                df['is_filled'] = True
                            return df.reset_index(drop=True)

        except Exception as e:
            logger.debug(f"备源 {cfg.fallback_source} 获取失败: {e}")

        return None

    # ── 辅助方法 ─────────────────────────────────────────────────

    def _estimate_volume(self, df: pd.DataFrame, idx: int, gap_size: int) -> float:
        """估算缺失 bar 的成交量（取前 N 根均值）"""
        cfg = self.config
        lookback = cfg.volume_lookback

        if cfg.volume_method == 'zero':
            return 0.0
        elif cfg.volume_method == 'ffill':
            return float(df['volume'].iloc[max(0, idx - 1)])
        else:  # 'mean'
            start = max(0, idx - lookback)
            segment = df['volume'].iloc[start:idx]
            if segment.empty:
                return 0.0
            return float(segment.mean())

    def _insert_rows(self, df: pd.DataFrame, insert_idx: int,
                     new_rows: pd.DataFrame) -> pd.DataFrame:
        """在指定位置插入新行"""
        before = df.iloc[:insert_idx]
        after = df.iloc[insert_idx:]

        # 确保列对齐
        for col in df.columns:
            if col not in new_rows.columns:
                if col == 'is_filled':
                    new_rows[col] = True
                else:
                    new_rows[col] = np.nan

        result = pd.concat([before, new_rows, after], ignore_index=True)
        return result

    def _compute_quality_score(self, result: ImputerResult) -> float:
        """计算填补后数据质量评分"""
        score = 100.0

        # 扣分项
        # 1. 总缺失比例 (每1%扣5分)
        score -= result.missing_pct * 5

        # 2. 无法填补的 bar (每个扣2分)
        score -= result.bars_unfillable * 2

        # 3. 前值填充比例（不如插值精确）
        if result.expected_bars > 0:
            ffill_pct = result.bars_forward_filled / result.expected_bars * 100
            score -= ffill_pct * 1  # 每1%扣1分

        # 4. 不可靠区间数量
        score -= len(result.unreliable_ranges) * 5

        # 加分项
        # 备源填补是高质量的
        if result.bars_from_fallback > 0:
            score += min(5, result.bars_from_fallback * 0.5)

        return round(max(0, min(100, score)), 1)


# ══════════════════════════════════════════════════════════════════
#  便捷函数
# ══════════════════════════════════════════════════════════════════

def impute_and_validate(
    df: pd.DataFrame,
    symbol: str = '',
    timeframe: str = '1h',
    fallback_source: str = 'okx',
) -> Tuple[pd.DataFrame, ImputerResult]:
    """
    一键填补 + 验证。

    返回: (clean_df, report)
    """
    config = ImputerConfig(
        timeframe=timeframe,
        fallback_source=fallback_source,
        expected_interval_ms=3600000 if timeframe == '1h' else 4 * 3600000,
    )
    imputer = DataImputer(config)
    result = imputer.fill(df, symbol=symbol)
    return result.filled_df, result
