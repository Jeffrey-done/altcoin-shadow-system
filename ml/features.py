#!/usr/bin/env python3
"""
特征工程 — 从原始市场数据提取标准化因子向量

12 维因子空间：
  1. rsi_1d_normalized      日线 RSI (0~1 归一化)
  2. rsi_4h_drop            4h RSI 从峰值回落幅度 (归一化)
  3. pct_24h                24h 涨跌幅 (winsorize ±50%)
  4. volume_zscore          24h 成交量 z-score (相对 7d 均值)
  5. oi_change_pct          OI 24h 变化率
  6. funding_rate           资金费率 (%/8h)
  7. yao_score_normalized   妖币评分 (0~1)
  8. btc_24h_pct            BTC 24h 涨跌幅
  9. orderbook_imbalance    Order Book 买卖不平衡度 (-1~1)
  10. whale_inflow_score    鲸鱼 CEX 充值信号 (0~1)
  11. sentiment_score       社交情绪分 (-1~1)
  12. regime_encoded        市场状态编码 (0~4)

设计原则：
  - 所有特征 winsorize 到合理范围，防止极端值干扰模型
  - 归一化到 [0,1] 或 [-1,1]，XGBoost 虽不严格要求但有助于特征重要性解释
  - 特征提取是纯函数，不依赖全局状态（方便回测时逐 bar 调用）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import numpy as np

logger = logging.getLogger("ml.features")


@dataclass
class FeatureVector:
    """标准化特征向量"""
    rsi_1d_normalized: float = 0.5
    rsi_4h_drop: float = 0.0
    pct_24h: float = 0.0
    volume_zscore: float = 0.0
    oi_change_pct: float = 0.0
    funding_rate: float = 0.0
    yao_score_normalized: float = 0.0
    btc_24h_pct: float = 0.0
    orderbook_imbalance: float = 0.0
    whale_inflow_score: float = 0.0
    sentiment_score: float = 0.0
    regime_encoded: float = 0.0

    def to_array(self) -> np.ndarray:
        """转为 numpy 数组（模型输入）"""
        return np.array([
            self.rsi_1d_normalized,
            self.rsi_4h_drop,
            self.pct_24h,
            self.volume_zscore,
            self.oi_change_pct,
            self.funding_rate,
            self.yao_score_normalized,
            self.btc_24h_pct,
            self.orderbook_imbalance,
            self.whale_inflow_score,
            self.sentiment_score,
            self.regime_encoded,
        ], dtype=np.float32)

    def to_dict(self) -> Dict[str, float]:
        return {
            'rsi_1d_normalized': self.rsi_1d_normalized,
            'rsi_4h_drop': self.rsi_4h_drop,
            'pct_24h': self.pct_24h,
            'volume_zscore': self.volume_zscore,
            'oi_change_pct': self.oi_change_pct,
            'funding_rate': self.funding_rate,
            'yao_score_normalized': self.yao_score_normalized,
            'btc_24h_pct': self.btc_24h_pct,
            'orderbook_imbalance': self.orderbook_imbalance,
            'whale_inflow_score': self.whale_inflow_score,
            'sentiment_score': self.sentiment_score,
            'regime_encoded': self.regime_encoded,
        }

    @classmethod
    def feature_names(cls) -> List[str]:
        return [
            'rsi_1d_normalized', 'rsi_4h_drop', 'pct_24h', 'volume_zscore',
            'oi_change_pct', 'funding_rate', 'yao_score_normalized',
            'btc_24h_pct', 'orderbook_imbalance', 'whale_inflow_score',
            'sentiment_score', 'regime_encoded',
        ]


# ══════════════════════════════════════════════════════════════════
#  特征提取函数
# ══════════════════════════════════════════════════════════════════

def _winsorize(value: float, low: float, high: float) -> float:
    """截断极端值"""
    return max(low, min(high, value))


def _normalize_rsi(rsi: float) -> float:
    """RSI 归一化到 [0, 1]"""
    return _winsorize(rsi / 100.0, 0.0, 1.0)


def extract_features(
    rsi_1d: float = 50.0,
    rsi_4h: float = 50.0,
    rsi_4h_peak: float = 70.0,
    pct_24h: float = 0.0,
    vol_24h: float = 0.0,
    vol_7d_avg: float = 1.0,
    oi_change: float = 0.0,
    funding_rate: float = 0.0,
    yao_score: int = 0,
    btc_24h_pct: float = 0.0,
    orderbook_imbalance: float = 0.0,
    whale_inflow_score: float = 0.0,
    sentiment_score: float = 0.0,
    regime_state: int = 2,  # 0=trending_up, 1=trending_down, 2=ranging, 3=high_vol, 4=crash
) -> FeatureVector:
    """
    从原始市场数据提取标准化特征向量。

    参数说明：
      rsi_1d: 日线 RSI (0~100)
      rsi_4h: 当前 4h RSI
      rsi_4h_peak: 4h RSI 近期峰值
      pct_24h: 24h 涨跌幅 (%)
      vol_24h: 24h 成交量 (USDT)
      vol_7d_avg: 7 日平均成交量 (USDT)
      oi_change: OI 24h 变化率 (0~1, 如 0.3 = 30%)
      funding_rate: 资金费率 (%/8h)
      yao_score: 妖币评分 (0~3)
      btc_24h_pct: BTC 24h 涨跌幅 (%)
      orderbook_imbalance: 买卖不平衡度 (-1~1)
      whale_inflow_score: 鲸鱼充值信号 (0~1)
      sentiment_score: 社交情绪分 (-1~1)
      regime_state: 市场状态编码 (0~4)

    返回：
      FeatureVector 标准化特征
    """
    # 1. RSI 归一化
    rsi_norm = _normalize_rsi(rsi_1d)

    # 2. 4h RSI 回落幅度 (0~1, 越大越好做空)
    rsi_drop = _winsorize((rsi_4h_peak - rsi_4h) / 50.0, 0.0, 1.0)

    # 3. 24h 涨跌幅 (winsorize ±50%, 归一化到 [-1, 1])
    pct_norm = _winsorize(pct_24h / 50.0, -1.0, 1.0)

    # 4. 成交量 z-score
    if vol_7d_avg > 0:
        vol_z = _winsorize((vol_24h - vol_7d_avg) / max(vol_7d_avg, 1), -3.0, 5.0) / 5.0
    else:
        vol_z = 0.0

    # 5. OI 变化率 (winsorize 0~1)
    oi_norm = _winsorize(oi_change, 0.0, 1.0)

    # 6. Funding rate (winsorize ±0.3%, 归一化到 [-1, 1])
    funding_norm = _winsorize(funding_rate / 0.3, -1.0, 1.0)

    # 7. 妖币评分归一化 (0~3 → 0~1)
    yao_norm = _winsorize(yao_score / 3.0, 0.0, 1.0)

    # 8. BTC 24h (winsorize ±15%, 归一化到 [-1, 1])
    btc_norm = _winsorize(btc_24h_pct / 15.0, -1.0, 1.0)

    # 9. Order Book 不平衡 (已经是 -1~1)
    ob_norm = _winsorize(orderbook_imbalance, -1.0, 1.0)

    # 10. 鲸鱼充值 (0~1)
    whale_norm = _winsorize(whale_inflow_score, 0.0, 1.0)

    # 11. 情绪分 (归一化到 -1~1)
    sent_norm = _winsorize(sentiment_score, -1.0, 1.0)

    # 12. Regime 编码 (0~4 → 0~1)
    regime_norm = _winsorize(regime_state / 4.0, 0.0, 1.0)

    return FeatureVector(
        rsi_1d_normalized=round(rsi_norm, 4),
        rsi_4h_drop=round(rsi_drop, 4),
        pct_24h=round(pct_norm, 4),
        volume_zscore=round(vol_z, 4),
        oi_change_pct=round(oi_norm, 4),
        funding_rate=round(funding_norm, 4),
        yao_score_normalized=round(yao_norm, 4),
        btc_24h_pct=round(btc_norm, 4),
        orderbook_imbalance=round(ob_norm, 4),
        whale_inflow_score=round(whale_norm, 4),
        sentiment_score=round(sent_norm, 4),
        regime_encoded=round(regime_norm, 4),
    )
