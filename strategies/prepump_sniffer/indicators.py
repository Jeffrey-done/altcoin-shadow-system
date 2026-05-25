"""
Pre-Pump 异常检测指标
7维度异常评分：检测"价格平静但资金异常流入"的吸筹模式。
"""

from typing import List, Dict, Tuple
import numpy as np


def detect_volume_spike(vol_current: float, vol_24h_avg: float,
                        price_change_4h_pct: float,
                        vol_threshold: float = 2.0,
                        calm_threshold: float = 3.0) -> Tuple[bool, str]:
    """维度1: 成交量突增 + 价格平静 = 主力吸筹"""
    if vol_24h_avg <= 0:
        return False, ''
    ratio = vol_current / vol_24h_avg
    if ratio >= vol_threshold and abs(price_change_4h_pct) < calm_threshold:
        return True, f'Vol×{ratio:.1f}'
    return False, ''


def detect_oi_spike(oi_4h_change_pct: float, price_change_4h_pct: float,
                    oi_threshold: float = 2.5,
                    calm_threshold: float = 3.0) -> Tuple[bool, str]:
    """维度2: OI飙升 + 价格平静 = 合约多头建仓"""
    if oi_4h_change_pct >= oi_threshold and abs(price_change_4h_pct) < calm_threshold:
        return True, f'OI+{oi_4h_change_pct:.0f}%'
    return False, ''


def detect_funding_shift(funding_now: float, funding_4h_ago: float,
                         threshold: float = 0.015) -> Tuple[bool, str]:
    """维度3: Funding从低/负翻正 = 多头开始愿意付费"""
    if funding_now >= threshold and funding_4h_ago < threshold * 0.5:
        return True, 'FR↑'
    return False, ''


def detect_depth_imbalance(oi_4h_change: float, vol_ratio: float) -> Tuple[bool, str]:
    """维度4: 盘口失衡（OI增 + 成交量增 = 买方主导）"""
    if oi_4h_change > 1.5 and vol_ratio > 1.8:
        return True, 'Dep'
    return False, ''


def detect_cross_exchange_signal(price_change_1h_pct: float,
                                 vol_ratio: float) -> Tuple[bool, str]:
    """维度5: 价格微幅但成交量异常 = 跨所先行资金"""
    if abs(price_change_1h_pct) < 0.5 and vol_ratio > 2.0:
        return True, 'Spr'
    return False, ''


def detect_smart_money_flow(vol_series_3h: List[float], price_change_3h_pct: float,
                            vol_ratio: float) -> Tuple[bool, str]:
    """维度6: 连续放量 + 价格稳定 = Smart Money 静默吸筹"""
    if len(vol_series_3h) < 3:
        return False, ''
    vol_increasing = vol_series_3h[2] > vol_series_3h[1] > vol_series_3h[0]
    price_flat = abs(price_change_3h_pct) < 2.0
    if vol_increasing and price_flat and vol_ratio > 1.8:
        return True, 'SM'
    return False, ''


def detect_volatility_squeeze(bb_width_percentile: float,
                              threshold: float = 55.0) -> Tuple[bool, str]:
    """维度7: 波动率压缩 = 暴风雨前的宁静"""
    if bb_width_percentile < threshold:
        return True, f'Sq{bb_width_percentile:.0f}'
    return False, ''


def calculate_anomaly_score(
    vol_current: float,
    vol_24h_avg: float,
    price_change_4h_pct: float,
    price_change_1h_pct: float,
    price_change_3h_pct: float,
    oi_4h_change_pct: float,
    funding_now: float,
    funding_4h_ago: float,
    vol_series_3h: List[float],
    bb_width_percentile: float,
    params: dict,
) -> Tuple[int, List[str]]:
    """
    计算7维度异常综合评分。

    返回: (score, detail_list)
    """
    score = 0
    details = []
    vol_ratio = vol_current / vol_24h_avg if vol_24h_avg > 0 else 0

    # 1. 成交量突增 + 价格平静
    hit, d = detect_volume_spike(vol_current, vol_24h_avg, price_change_4h_pct,
                                 params.get('vol_spike_ratio', 2.0),
                                 params.get('price_calm_pct', 3.0))
    if hit:
        score += 1; details.append(d)

    # 2. OI 飙升
    hit, d = detect_oi_spike(oi_4h_change_pct, price_change_4h_pct,
                             params.get('oi_spike_pct', 2.5),
                             params.get('price_calm_pct', 3.0))
    if hit:
        score += 1; details.append(d)

    # 3. Funding 翻转
    hit, d = detect_funding_shift(funding_now, funding_4h_ago,
                                  params.get('funding_shift', 0.015))
    if hit:
        score += 1; details.append(d)

    # 4. 盘口失衡
    hit, d = detect_depth_imbalance(oi_4h_change_pct, vol_ratio)
    if hit:
        score += 1; details.append(d)

    # 5. 跨所信号
    hit, d = detect_cross_exchange_signal(price_change_1h_pct, vol_ratio)
    if hit:
        score += 1; details.append(d)

    # 6. Smart Money
    hit, d = detect_smart_money_flow(vol_series_3h, price_change_3h_pct, vol_ratio)
    if hit:
        score += 1; details.append(d)

    # 7. 波动率压缩
    hit, d = detect_volatility_squeeze(bb_width_percentile,
                                       params.get('bb_squeeze_pctile', 55.0))
    if hit:
        score += 1; details.append(d)

    return score, details
