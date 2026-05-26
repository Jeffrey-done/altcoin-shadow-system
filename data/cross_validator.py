#!/usr/bin/env python3
"""
多源数据交叉验证器 v1.0

从 Binance / OKX / Gate 三个交易所拉取同一币种的历史 K 线，
对比检测数据异常、价格偏差、成交量不一致，生成数据质量报告。

验证维度：
  1. 价格一致性：同一时刻 close 偏差 > 0.1% → 标记异常
  2. 成交量相关性：两所 volume 的 Pearson 相关系数
  3. 时间戳完整性：各源的缺失 bar 统计
  4. 资金费率验证：实际历史 funding vs 回测假设 (0.01%)
  5. 异常检测：价格突变（单 bar 涨跌 >10%）、量能突变（>5x 均值）

输出：
  - data_quality_score: 0~100 综合评分
  - anomalies: 异常事件列表
  - validated_df: 经验证的"黄金"数据集（取多源中位数）

用法：
  from data.cross_validator import CrossValidator, CrossValidatorConfig

  cv = CrossValidator(config=CrossValidatorConfig(
      primary_source='binance',
      validation_sources=['okx', 'gate'],
  ))
  report = cv.validate('PEPE/USDT', days=90)
  print(f"数据质量: {report.quality_score}/100")
  print(report.summary)

  # 获取验证后的数据用于回测
  clean_df = report.validated_df
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("data.cross_validator")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class CrossValidatorConfig:
    """交叉验证配置"""
    primary_source: str = 'binance'           # 主数据源
    validation_sources: List[str] = field(
        default_factory=lambda: ['okx']
    )

    # 阈值
    price_divergence_threshold_pct: float = 0.1   # 价格偏差告警阈值 %
    volume_correlation_min: float = 0.7           # 最低成交量相关性
    price_spike_threshold_pct: float = 10.0       # 单 bar 价格突变阈值 %
    volume_spike_multiplier: float = 5.0          # 量能突变倍数

    # 数据
    timeframe: str = '1h'
    default_days: int = 90
    request_timeout: int = 15

    # 资金费率
    expected_funding_rate: float = 0.01   # 预期基准费率 %/8h
    funding_deviation_alert: float = 0.05  # 费率偏差告警阈值 %


# ══════════════════════════════════════════════════════════════════
#  结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class DataAnomaly:
    """数据异常事件"""
    timestamp: str = ''
    anomaly_type: str = ''       # 'price_divergence' / 'price_spike' / 'volume_spike' / 'gap'
    severity: str = 'low'        # 'low' / 'medium' / 'high'
    description: str = ''
    source: str = ''
    value: float = 0.0


@dataclass
class CrossValidationReport:
    """交叉验证报告"""
    symbol: str = ''
    primary_source: str = ''
    validation_sources: List[str] = field(default_factory=list)
    timeframe: str = '1h'
    total_bars: int = 0
    date_range: str = ''

    # 综合评分
    quality_score: float = 0.0          # 0~100

    # 分项评分
    price_consistency_score: float = 0.0    # 价格一致性 (0~100)
    volume_correlation_score: float = 0.0   # 量能相关性 (0~100)
    completeness_score: float = 0.0         # 数据完整性 (0~100)
    stability_score: float = 0.0            # 价格稳定性 (0~100)

    # 统计
    price_divergence_count: int = 0      # 价格偏差次数
    price_divergence_mean_pct: float = 0.0
    price_divergence_max_pct: float = 0.0
    volume_correlation: float = 0.0      # Pearson 相关系数
    missing_bars_primary: int = 0
    missing_bars_validation: int = 0
    price_spikes: int = 0
    volume_spikes: int = 0

    # 资金费率
    funding_rate_mean: float = 0.0
    funding_rate_std: float = 0.0
    funding_deviation_from_expected: float = 0.0

    # 异常列表
    anomalies: List[DataAnomaly] = field(default_factory=list)

    # 验证后的干净数据
    validated_df: Optional[pd.DataFrame] = None

    @property
    def summary(self) -> str:
        grade = 'A' if self.quality_score >= 85 else \
                'B' if self.quality_score >= 70 else \
                'C' if self.quality_score >= 50 else 'D'
        return (
            f"数据交叉验证报告: {self.symbol}\n"
            f"{'═'*55}\n"
            f"质量评分: {self.quality_score:.0f}/100 (Grade {grade})\n"
            f"数据范围: {self.date_range} ({self.total_bars} bars)\n"
            f"{'─'*55}\n"
            f"  价格一致性: {self.price_consistency_score:.0f}/100  "
            f"(偏差{self.price_divergence_count}次, 均值{self.price_divergence_mean_pct:.3f}%)\n"
            f"  量能相关性: {self.volume_correlation_score:.0f}/100  "
            f"(r={self.volume_correlation:.3f})\n"
            f"  数据完整性: {self.completeness_score:.0f}/100  "
            f"(缺失: 主源{self.missing_bars_primary}, 验证源{self.missing_bars_validation})\n"
            f"  价格稳定性: {self.stability_score:.0f}/100  "
            f"(突变{self.price_spikes}次, 量变{self.volume_spikes}次)\n"
            f"{'─'*55}\n"
            f"异常事件: {len(self.anomalies)} 条\n"
            f"资金费率: {self.funding_rate_mean:.4f}% ± {self.funding_rate_std:.4f}%\n"
        )


# ══════════════════════════════════════════════════════════════════
#  交叉验证器
# ══════════════════════════════════════════════════════════════════

class CrossValidator:
    """多源数据交叉验证器"""

    def __init__(self, config: Optional[CrossValidatorConfig] = None):
        self.config = config or CrossValidatorConfig()

    def validate(
        self,
        symbol: str,
        days: Optional[int] = None,
        primary_df: Optional[pd.DataFrame] = None,
    ) -> CrossValidationReport:
        """
        执行交叉验证。

        参数:
          symbol: 交易对 (如 'PEPE/USDT')
          days: 验证天数
          primary_df: 可选，直接提供主源数据（跳过拉取）

        返回:
          CrossValidationReport
        """
        cfg = self.config
        days = days or cfg.default_days

        report = CrossValidationReport(
            symbol=symbol,
            primary_source=cfg.primary_source,
            validation_sources=list(cfg.validation_sources),
            timeframe=cfg.timeframe,
        )

        # 获取主源数据
        if primary_df is not None:
            df_primary = primary_df
        else:
            df_primary = self._fetch_klines(symbol, cfg.primary_source, days)

        if df_primary is None or df_primary.empty:
            logger.warning(f"主源 {cfg.primary_source} 数据为空")
            return report

        report.total_bars = len(df_primary)

        # 设置日期范围
        if 'timestamp' in df_primary.columns:
            report.date_range = (
                f"{df_primary['timestamp'].iloc[0]} ~ "
                f"{df_primary['timestamp'].iloc[-1]}"
            )

        # 获取验证源数据
        validation_dfs = {}
        for source in cfg.validation_sources:
            df_val = self._fetch_klines(symbol, source, days)
            if df_val is not None and not df_val.empty:
                validation_dfs[source] = df_val

        # ── 验证 1: 数据完整性 ──
        self._check_completeness(df_primary, validation_dfs, report)

        # ── 验证 2: 价格一致性 ──
        if validation_dfs:
            self._check_price_consistency(df_primary, validation_dfs, report)

        # ── 验证 3: 成交量相关性 ──
        if validation_dfs:
            self._check_volume_correlation(df_primary, validation_dfs, report)

        # ── 验证 4: 价格/量能稳定性 ──
        self._check_stability(df_primary, report)

        # ── 验证 5: 资金费率 ──
        self._check_funding_rates(symbol, report)

        # ── 生成验证后数据 ──
        report.validated_df = self._generate_validated_df(df_primary, validation_dfs)

        # ── 计算综合评分 ──
        report.quality_score = round(
            report.price_consistency_score * 0.3 +
            report.volume_correlation_score * 0.2 +
            report.completeness_score * 0.3 +
            report.stability_score * 0.2,
            1
        )

        return report

    # ── 验证方法 ─────────────────────────────────────────────────

    def _check_completeness(self, df_primary: pd.DataFrame,
                            validation_dfs: Dict[str, pd.DataFrame],
                            report: CrossValidationReport):
        """检查数据完整性"""
        cfg = self.config

        # 主源缺失检测
        if 'timestamp' in df_primary.columns:
            ts = pd.to_numeric(df_primary['timestamp'], errors='coerce')
            if cfg.timeframe == '1h':
                expected_gap = 3600 * 1000  # 1h in ms
            else:
                expected_gap = 4 * 3600 * 1000

            diffs = ts.diff().dropna()
            gaps = diffs[diffs > expected_gap * 1.5]
            report.missing_bars_primary = len(gaps)

            for idx in gaps.index:
                gap_size = int(diffs[idx] / expected_gap) - 1
                report.anomalies.append(DataAnomaly(
                    timestamp=str(df_primary['timestamp'].iloc[idx]) if idx < len(df_primary) else '',
                    anomaly_type='gap',
                    severity='high' if gap_size >= 4 else 'medium',
                    description=f"主源缺失 {gap_size} 根 K 线",
                    source=cfg.primary_source,
                    value=gap_size,
                ))

        # 验证源缺失
        for source, df_val in validation_dfs.items():
            if 'timestamp' in df_val.columns:
                ts = pd.to_numeric(df_val['timestamp'], errors='coerce')
                diffs = ts.diff().dropna()
                expected_gap = 3600 * 1000 if cfg.timeframe == '1h' else 4 * 3600 * 1000
                gaps = diffs[diffs > expected_gap * 1.5]
                report.missing_bars_validation += len(gaps)

        # 评分
        total_expected = report.total_bars
        missing = report.missing_bars_primary
        if total_expected > 0:
            completeness = (total_expected - missing) / total_expected * 100
            report.completeness_score = round(min(100, completeness), 1)
        else:
            report.completeness_score = 0

    def _check_price_consistency(self, df_primary: pd.DataFrame,
                                 validation_dfs: Dict[str, pd.DataFrame],
                                 report: CrossValidationReport):
        """检查价格一致性"""
        cfg = self.config
        divergences = []

        for source, df_val in validation_dfs.items():
            # 按时间戳对齐
            if 'timestamp' not in df_primary.columns or 'timestamp' not in df_val.columns:
                continue

            merged = pd.merge(
                df_primary[['timestamp', 'close']].rename(columns={'close': 'close_primary'}),
                df_val[['timestamp', 'close']].rename(columns={'close': 'close_val'}),
                on='timestamp',
                how='inner',
            )

            if merged.empty:
                continue

            # 计算偏差
            merged['divergence_pct'] = (
                (merged['close_primary'] - merged['close_val']).abs() /
                merged['close_primary'] * 100
            )

            # 标记异常
            threshold = cfg.price_divergence_threshold_pct
            anomalous = merged[merged['divergence_pct'] > threshold]

            for _, row in anomalous.iterrows():
                report.anomalies.append(DataAnomaly(
                    timestamp=str(row['timestamp']),
                    anomaly_type='price_divergence',
                    severity='high' if row['divergence_pct'] > 1.0 else 'medium',
                    description=(
                        f"{cfg.primary_source}={row['close_primary']:.6f} vs "
                        f"{source}={row['close_val']:.6f} "
                        f"(差异 {row['divergence_pct']:.3f}%)"
                    ),
                    source=source,
                    value=row['divergence_pct'],
                ))

            divergences.extend(merged['divergence_pct'].tolist())
            report.price_divergence_count += len(anomalous)

        if divergences:
            report.price_divergence_mean_pct = round(float(np.mean(divergences)), 4)
            report.price_divergence_max_pct = round(float(np.max(divergences)), 4)

            # 评分：偏差越小越好
            mean_div = report.price_divergence_mean_pct
            if mean_div <= 0.01:
                report.price_consistency_score = 100
            elif mean_div <= 0.05:
                report.price_consistency_score = 90
            elif mean_div <= 0.1:
                report.price_consistency_score = 75
            elif mean_div <= 0.5:
                report.price_consistency_score = 50
            else:
                report.price_consistency_score = 25
        else:
            report.price_consistency_score = 70  # 无验证源时给中性分

    def _check_volume_correlation(self, df_primary: pd.DataFrame,
                                  validation_dfs: Dict[str, pd.DataFrame],
                                  report: CrossValidationReport):
        """检查成交量相关性"""
        correlations = []

        for source, df_val in validation_dfs.items():
            if 'timestamp' not in df_primary.columns or 'volume' not in df_val.columns:
                continue

            merged = pd.merge(
                df_primary[['timestamp', 'volume']].rename(columns={'volume': 'vol_primary'}),
                df_val[['timestamp', 'volume']].rename(columns={'volume': 'vol_val'}),
                on='timestamp',
                how='inner',
            )

            if len(merged) < 10:
                continue

            corr = merged['vol_primary'].corr(merged['vol_val'])
            if not np.isnan(corr):
                correlations.append(corr)

        if correlations:
            report.volume_correlation = round(float(np.mean(correlations)), 3)
            # 评分
            if report.volume_correlation >= 0.9:
                report.volume_correlation_score = 100
            elif report.volume_correlation >= 0.8:
                report.volume_correlation_score = 80
            elif report.volume_correlation >= 0.7:
                report.volume_correlation_score = 60
            elif report.volume_correlation >= 0.5:
                report.volume_correlation_score = 40
            else:
                report.volume_correlation_score = 20
        else:
            report.volume_correlation_score = 50  # 无数据时中性

    def _check_stability(self, df: pd.DataFrame, report: CrossValidationReport):
        """检查价格/量能稳定性"""
        cfg = self.config

        if 'close' not in df.columns:
            report.stability_score = 50
            return

        closes = df['close'].values
        volumes = df['volume'].values if 'volume' in df.columns else np.array([])

        # 价格突变检测
        if len(closes) > 1:
            returns = np.diff(closes) / closes[:-1] * 100
            spikes = np.abs(returns) > cfg.price_spike_threshold_pct
            report.price_spikes = int(spikes.sum())

            for idx in np.where(spikes)[0]:
                ts = str(df['timestamp'].iloc[idx + 1]) if 'timestamp' in df.columns else f"bar_{idx+1}"
                report.anomalies.append(DataAnomaly(
                    timestamp=ts,
                    anomaly_type='price_spike',
                    severity='high',
                    description=f"价格突变 {returns[idx]:+.1f}%",
                    source=self.config.primary_source,
                    value=abs(returns[idx]),
                ))

        # 量能突变检测
        if len(volumes) > 20:
            vol_ma = pd.Series(volumes).rolling(20).mean().values
            valid = vol_ma > 0
            vol_ratio = np.zeros_like(volumes, dtype=float)
            vol_ratio[valid] = volumes[valid] / vol_ma[valid]
            spikes = vol_ratio > cfg.volume_spike_multiplier
            report.volume_spikes = int(spikes.sum())

        # 评分
        total_bars = len(df)
        spike_rate = (report.price_spikes + report.volume_spikes) / max(total_bars, 1)
        if spike_rate <= 0.005:
            report.stability_score = 100
        elif spike_rate <= 0.01:
            report.stability_score = 80
        elif spike_rate <= 0.03:
            report.stability_score = 60
        elif spike_rate <= 0.05:
            report.stability_score = 40
        else:
            report.stability_score = 20

    def _check_funding_rates(self, symbol: str, report: CrossValidationReport):
        """检查资金费率"""
        try:
            import requests
            sym = symbol.replace('/USDT', 'USDT').replace('/', '')
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": sym, "limit": 100},
                timeout=self.config.request_timeout,
            )
            if r.status_code == 200 and r.json():
                rates = [float(item['fundingRate']) * 100 for item in r.json()]
                if rates:
                    report.funding_rate_mean = round(float(np.mean(rates)), 4)
                    report.funding_rate_std = round(float(np.std(rates)), 4)
                    report.funding_deviation_from_expected = round(
                        abs(report.funding_rate_mean - self.config.expected_funding_rate), 4
                    )
        except Exception as e:
            logger.debug(f"获取资金费率历史失败: {e}")

    # ── 数据获取 ─────────────────────────────────────────────────

    def _fetch_klines(self, symbol: str, source: str, days: int) -> Optional[pd.DataFrame]:
        """从指定交易所获取历史 K 线"""
        try:
            import requests

            limit = min(days * 24, 1000) if self.config.timeframe == '1h' else min(days * 6, 1000)
            sym = symbol.replace('/USDT', 'USDT').replace('/', '')

            if source == 'binance':
                url = "https://fapi.binance.com/fapi/v1/klines"
                params = {"symbol": sym, "interval": self.config.timeframe, "limit": limit}
                r = requests.get(url, params=params, timeout=self.config.request_timeout)
                if r.status_code == 200:
                    data = r.json()
                    df = pd.DataFrame(data, columns=[
                        'timestamp', 'open', 'high', 'low', 'close', 'volume',
                        'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                        'taker_buy_quote', 'ignore'
                    ])
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                    df['timestamp'] = pd.to_numeric(df['timestamp'])
                    return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

            elif source == 'okx':
                # OKX API: instId format = BTC-USDT-SWAP
                base = symbol.split('/')[0] if '/' in symbol else sym.replace('USDT', '')
                inst_id = f"{base}-USDT-SWAP"
                bar = '1H' if self.config.timeframe == '1h' else '4H'
                url = "https://www.okx.com/api/v5/market/candles"
                params = {"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))}
                r = requests.get(url, params=params, timeout=self.config.request_timeout)
                if r.status_code == 200:
                    resp = r.json()
                    if resp.get('data'):
                        # OKX returns [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                        rows = resp['data']
                        df = pd.DataFrame(rows, columns=[
                            'timestamp', 'open', 'high', 'low', 'close',
                            'volume', 'volCcy', 'volQuote', 'confirm'
                        ])
                        for col in ['open', 'high', 'low', 'close', 'volume']:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                        df['timestamp'] = pd.to_numeric(df['timestamp'])
                        df = df.sort_values('timestamp').reset_index(drop=True)
                        return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

            elif source == 'gate':
                # Gate.io futures
                contract = f"{sym.replace('USDT', '')}_USDT"
                interval = '1h' if self.config.timeframe == '1h' else '4h'
                url = f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
                params = {"contract": contract, "interval": interval, "limit": min(limit, 200)}
                r = requests.get(url, params=params, timeout=self.config.request_timeout)
                if r.status_code == 200 and r.json():
                    data = r.json()
                    df = pd.DataFrame(data)
                    if 't' in df.columns:
                        df = df.rename(columns={
                            't': 'timestamp', 'o': 'open', 'h': 'high',
                            'l': 'low', 'c': 'close', 'v': 'volume'
                        })
                        for col in ['open', 'high', 'low', 'close', 'volume']:
                            if col in df.columns:
                                df[col] = pd.to_numeric(df[col], errors='coerce')
                        df['timestamp'] = pd.to_numeric(df['timestamp']) * 1000  # Gate returns seconds
                        return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

        except Exception as e:
            logger.debug(f"获取 {source} {symbol} K线失败: {e}")

        return None

    # ── 数据生成 ─────────────────────────────────────────────────

    def _generate_validated_df(
        self,
        df_primary: pd.DataFrame,
        validation_dfs: Dict[str, pd.DataFrame],
    ) -> pd.DataFrame:
        """
        生成经验证的数据集。

        策略：
          - 只有主源有数据 → 直接使用主源
          - 多源可用 → 取 close 的中位数（减少单源异常影响）
          - 标记验证状态列
        """
        result = df_primary.copy()
        result['validated'] = True
        result['n_sources'] = 1

        if not validation_dfs:
            return result

        # 对每个验证源做 merge，取中位数
        for source, df_val in validation_dfs.items():
            if 'timestamp' not in df_val.columns:
                continue
            suffix = f"_{source}"
            merged = pd.merge(
                result,
                df_val[['timestamp', 'close']].rename(columns={'close': f'close{suffix}'}),
                on='timestamp',
                how='left',
            )
            result = merged
            # 有验证数据的行 n_sources +1
            mask = result[f'close{suffix}'].notna()
            result.loc[mask, 'n_sources'] += 1

        # 取多源 close 中位数
        close_cols = ['close'] + [c for c in result.columns if c.startswith('close_')]
        if len(close_cols) > 1:
            result['close_validated'] = result[close_cols].median(axis=1)
        else:
            result['close_validated'] = result['close']

        # 清理临时列
        drop_cols = [c for c in result.columns if c.startswith('close_') and c != 'close_validated']
        result = result.drop(columns=drop_cols, errors='ignore')

        return result
