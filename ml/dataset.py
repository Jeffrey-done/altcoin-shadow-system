#!/usr/bin/env python3
"""
训练数据集构建 — 从历史交易生成 ML 训练数据

流程：
  1. 加载所有已平仓交易
  2. 对每笔交易回溯其开仓时的市场状态（特征）
  3. 标注 label: 1=盈利（tp1_locked_pnl + pnl > 0），0=亏损
  4. 按时间排序，确保无 look-ahead bias
  5. 输出 (X, y) 用于 model.train()

用法：
  from ml.dataset import build_training_set
  X, y, metadata = build_training_set()
  # X: np.ndarray shape (N, 12)
  # y: np.ndarray shape (N,) values 0/1
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple, Any

import numpy as np

logger = logging.getLogger("ml.dataset")


def build_training_set(
    trades: Optional[List[Dict]] = None,
    min_samples: int = 20,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    """
    从历史交易构建训练数据集。

    参数:
      trades: 已平仓交易列表（如果为 None，从文件加载）
      min_samples: 最少样本数

    返回:
      (X, y, metadata)
      - X: shape (N, 12) 特征矩阵
      - y: shape (N,) 标签向量
      - metadata: {'n_samples', 'n_positive', 'n_negative', 'pos_ratio'}
    """
    if trades is None:
        trades = _load_closed_trades()

    if len(trades) < min_samples:
        logger.warning(f"样本不足: {len(trades)} < {min_samples}")
        return np.array([]), np.array([]), {'error': 'insufficient_samples'}

    X_list = []
    y_list = []

    for trade in trades:
        try:
            features = _extract_trade_features(trade)
            if features is None:
                continue

            # Label: 盈利=1, 亏损=0
            pnl = trade.get('tp1_locked_pnl', 0) + trade.get('pnl', 0)
            label = 1 if pnl > 0 else 0

            X_list.append(features)
            y_list.append(label)
        except Exception as e:
            logger.debug(f"交易特征提取失败: {e}")
            continue

    if len(X_list) < min_samples:
        logger.warning(f"有效样本不足: {len(X_list)}")
        return np.array([]), np.array([]), {'error': 'insufficient_valid_samples'}

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)

    n_pos = int(np.sum(y == 1))
    n_neg = int(np.sum(y == 0))

    metadata = {
        'n_samples': len(y),
        'n_positive': n_pos,
        'n_negative': n_neg,
        'pos_ratio': round(n_pos / len(y), 3) if len(y) > 0 else 0,
    }

    logger.info(
        f"📊 训练集构建完成: {len(y)} 样本 | "
        f"正例={n_pos} ({metadata['pos_ratio']:.0%}) | 负例={n_neg}"
    )
    return X, y, metadata


def _load_closed_trades() -> List[Dict]:
    """从文件加载已平仓交易"""
    try:
        from common import TRADES_FILE, load_json
        trades = load_json(TRADES_FILE, [])
        closed = [t for t in trades if t.get('status') == 'closed']
        # 按开仓时间排序
        closed.sort(key=lambda t: t.get('opened_at', ''))
        return closed
    except Exception as e:
        logger.error(f"加载交易文件失败: {e}")
        return []


def _extract_trade_features(trade: Dict) -> Optional[np.ndarray]:
    """
    从单笔交易提取开仓时的特征向量。
    注意：这里用交易记录中保存的信息重建特征，不是实时获取。
    """
    from ml.features import extract_features

    # 从交易记录中提取可用的信息
    # 部分字段可能缺失（旧版交易没有保存），用默认值填充
    rsi_1d = trade.get('rsi_1d', trade.get('score_details', {}).get('rsi', 50))
    if rsi_1d is None:
        rsi_1d = 50.0

    # 从 reason / score_details 推断其他特征
    score_details = trade.get('score_details', {})
    pct_24h = trade.get('pct_24h', 15.0)  # 默认 15%（满足入场条件）
    oi_change = trade.get('oi_change', 0.2)
    funding_rate = trade.get('funding_rate', 0.02)
    yao_score = trade.get('yao_score', 1)

    # 推断 4h RSI 信息
    rsi_4h = trade.get('rsi_4h', 60.0)
    rsi_4h_peak = trade.get('rsi_4h_peak', rsi_4h + 10)

    # BTC 信息（开仓时可能保存了）
    btc_24h = trade.get('btc_24h_pct', 0.0)

    # 如果太多字段缺失，跳过这笔交易
    if rsi_1d == 50.0 and pct_24h == 15.0 and oi_change == 0.2:
        # 可能是旧版交易，信息不够充分
        # 仍然用默认值生成特征（有噪声但比跳过好）
        pass

    features = extract_features(
        rsi_1d=float(rsi_1d),
        rsi_4h=float(rsi_4h),
        rsi_4h_peak=float(rsi_4h_peak),
        pct_24h=float(pct_24h),
        vol_24h=trade.get('vol_24h', 500000),
        vol_7d_avg=500000,  # 无历史均值，用默认
        oi_change=float(oi_change),
        funding_rate=float(funding_rate),
        yao_score=int(yao_score),
        btc_24h_pct=float(btc_24h),
        orderbook_imbalance=0.0,  # 历史交易无此数据
        whale_inflow_score=0.0,   # 历史交易无此数据
        sentiment_score=0.0,       # 历史交易无此数据
        regime_state=2,            # 默认 ranging
    )

    return features.to_array()


def time_split(X: np.ndarray, y: np.ndarray,
               val_ratio: float = 0.2) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    按时间顺序切分训练/验证集（不 shuffle，保证无 look-ahead）。
    """
    n = len(y)
    split_idx = int(n * (1 - val_ratio))
    return X[:split_idx], y[:split_idx], X[split_idx:], y[split_idx:]
