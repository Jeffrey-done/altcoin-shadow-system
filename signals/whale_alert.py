#!/usr/bin/env python3
"""
链上鲸鱼预警信号 v1.0

监控大额代币从钱包转入 CEX（Binance/OKX），
这通常意味着即将抛售，是做空策略的增强信号。

数据源层级：
  1. Etherscan/BSCScan API（自建，无第三方依赖）
  2. Whale Alert 公开 API（备用）
  3. 交易所入金 API（如 Binance 大额充值）

Alpha 逻辑：
  - 大额充值到 CEX → 抛售意图 → 做空加分
  - 大额从 CEX 提出 → 囤币意图 → 做多/做空减分
  - 多笔中等金额连续充值（拆单）→ 更强的抛售信号

集成方式：
  - 被 signal_score.py 调用，返回 bonus 分数 (0~15)
  - 通过 event_bus 发布 whale.detected 事件，供 dashboard 展示
"""

from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

import requests

logger = logging.getLogger("signals.whale_alert")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class WhaleAlertConfig:
    """鲸鱼预警配置"""
    enabled: bool = True

    # CEX 钱包地址（已知的 Binance/OKX 热钱包）
    # 这些地址是公开可查的，用于检测"转入 CEX"行为
    cex_deposit_addresses: Dict[str, List[str]] = field(default_factory=lambda: {
        'binance': [
            '0x28C6c06298d514Db089934071355E5743bf21d60',  # Binance Hot Wallet
            '0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549',  # Binance
            '0xDFd5293D8e347dFe59E90eFd55b2956a1343963d',  # Binance
        ],
        'okx': [
            '0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b',  # OKX Hot Wallet
            '0x236F233dBf78341d7B1075795B5E2e8F25aEBEFC',  # OKX
        ],
    })

    # 阈值
    min_transfer_usd: float = 500_000      # 最小单笔金额 (USD) 才计入
    whale_threshold_usd: float = 2_000_000 # 大鲸鱼阈值 (≥200万U)
    mega_whale_usd: float = 10_000_000     # 超级鲸鱼 (≥1000万U)

    # 时间窗口
    lookback_hours: int = 4                # 回溯 N 小时内的转账
    batch_detection_window_min: int = 30   # 拆单检测窗口（分钟）
    batch_min_count: int = 3               # ≥N笔视为拆单

    # 评分
    max_bonus_score: int = 15              # 最大加分
    whale_score: int = 8                   # 普通鲸鱼加分
    mega_whale_score: int = 12             # 超级鲸鱼加分
    batch_bonus: int = 5                   # 拆单检测 bonus

    # 缓存
    cache_ttl_sec: int = 300               # 查询缓存 5 分钟
    poll_interval_sec: int = 120           # 主动轮询间隔

    # API Keys（可选，有 key 可以提升请求频率）
    etherscan_api_key: str = ''
    bscscan_api_key: str = ''
    whale_alert_api_key: str = ''


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class WhaleTransfer:
    """一笔鲸鱼转账"""
    tx_hash: str
    from_address: str
    to_address: str
    token_symbol: str                    # 如 'PEPE', 'SHIB'
    amount: float                        # 代币数量
    usd_value: float                     # USD 估值
    destination: str                     # 'binance' / 'okx' / 'unknown_cex'
    timestamp: datetime
    chain: str = 'ethereum'              # 'ethereum' / 'bsc'
    is_cex_deposit: bool = True          # 是否为 CEX 充值

    @property
    def is_whale(self) -> bool:
        return self.usd_value >= 2_000_000

    @property
    def is_mega_whale(self) -> bool:
        return self.usd_value >= 10_000_000


@dataclass
class WhaleSignal:
    """鲸鱼信号摘要（给策略评分系统用）"""
    symbol: str                          # 'PEPE/USDT' 格式
    total_usd_inflow: float              # 时间窗口内总充值金额
    transfer_count: int                  # 转账笔数
    largest_single: float                # 最大单笔
    is_batch_pattern: bool               # 是否检测到拆单
    bonus_score: int                     # 建议加分 (0~15)
    confidence: float                    # 置信度 (0~1)
    direction_hint: str                  # 'bearish' (充值=抛售) / 'neutral'
    last_transfer_age_min: float         # 最近一笔距今分钟数
    details: str = ''                    # 人类可读描述


# ══════════════════════════════════════════════════════════════════
#  鲸鱼预警引擎
# ══════════════════════════════════════════════════════════════════

class WhaleAlertEngine:
    """
    鲸鱼预警引擎 — 主动轮询 + 缓存查询。

    用法：
      engine = WhaleAlertEngine()
      engine.start()  # 启动后台轮询

      # 策略评分时查询
      signal = engine.get_signal('PEPE/USDT')
      if signal and signal.bonus_score > 0:
          total_score += signal.bonus_score
    """

    def __init__(self, config: Optional[WhaleAlertConfig] = None):
        self.config = config or WhaleAlertConfig()
        self._cache: Dict[str, Tuple[WhaleSignal, float]] = {}  # symbol → (signal, timestamp)
        self._transfers: List[WhaleTransfer] = []
        self._lock = threading.Lock()
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

        # 加载环境变量中的 API key
        if not self.config.etherscan_api_key:
            self.config.etherscan_api_key = os.environ.get('ETHERSCAN_API_KEY', '')
        if not self.config.whale_alert_api_key:
            self.config.whale_alert_api_key = os.environ.get('WHALE_ALERT_API_KEY', '')

    def start(self) -> None:
        """启动后台轮询线程"""
        if not self.config.enabled:
            logger.info("🐋 鲸鱼预警已禁用")
            return

        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name='whale_alert_poll',
            daemon=True,
        )
        self._poll_thread.start()
        logger.info("🐋 鲸鱼预警引擎已启动")

    def stop(self) -> None:
        """停止轮询"""
        self._running = False
        logger.info("🐋 鲸鱼预警引擎已停止")

    def get_signal(self, symbol: str) -> Optional[WhaleSignal]:
        """
        获取指定币种的鲸鱼信号。

        参数:
          symbol: 'PEPE/USDT' 格式

        返回:
          WhaleSignal（有信号时）或 None（无显著鲸鱼活动）
        """
        if not self.config.enabled:
            return None

        # 检查缓存
        with self._lock:
            cached = self._cache.get(symbol)
            if cached:
                signal, ts = cached
                if time.time() - ts < self.config.cache_ttl_sec:
                    return signal

        # 缓存过期，重新计算
        signal = self._compute_signal(symbol)
        if signal:
            with self._lock:
                self._cache[symbol] = (signal, time.time())
        return signal

    def get_all_active_signals(self) -> List[WhaleSignal]:
        """获取所有当前活跃的鲸鱼信号"""
        with self._lock:
            now = time.time()
            return [
                signal for signal, ts in self._cache.values()
                if now - ts < self.config.cache_ttl_sec and signal.bonus_score > 0
            ]

    # ── 数据获取 ─────────────────────────────────────────────────

    def _poll_loop(self):
        """后台轮询循环"""
        while self._running:
            try:
                self._fetch_recent_transfers()
            except Exception as e:
                logger.warning(f"鲸鱼数据获取失败: {e}")

            time.sleep(self.config.poll_interval_sec)

    def _fetch_recent_transfers(self):
        """
        从多个数据源获取最近的大额转账。
        优先级：Etherscan > Whale Alert API > fallback
        """
        transfers = []

        # 源 1: Etherscan Token Transfer API
        if self.config.etherscan_api_key:
            transfers.extend(self._fetch_from_etherscan())

        # 源 2: Whale Alert 公开 API
        if self.config.whale_alert_api_key and not transfers:
            transfers.extend(self._fetch_from_whale_alert_api())

        # 源 3: 公开区块链浏览器 scrape（无需 API key）
        if not transfers:
            transfers.extend(self._fetch_from_public_api())

        # 更新内部缓存
        with self._lock:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=self.config.lookback_hours
            )
            # 保留时间窗口内的 + 新获取的
            self._transfers = [
                t for t in self._transfers if t.timestamp >= cutoff
            ]
            # 去重（按 tx_hash）
            existing_hashes = {t.tx_hash for t in self._transfers}
            for t in transfers:
                if t.tx_hash not in existing_hashes:
                    self._transfers.append(t)
                    existing_hashes.add(t.tx_hash)

            # 清除过期缓存
            self._cache.clear()

        if transfers:
            logger.info(f"🐋 获取到 {len(transfers)} 笔大额转账")

            # 发布事件
            try:
                from event_bus import get_event_bus
                for t in transfers:
                    if t.usd_value >= self.config.whale_threshold_usd:
                        get_event_bus().publish('whale.detected', {
                            'symbol': t.token_symbol,
                            'usd_value': t.usd_value,
                            'destination': t.destination,
                            'tx_hash': t.tx_hash,
                        })
            except Exception:
                pass

    def _fetch_from_etherscan(self) -> List[WhaleTransfer]:
        """从 Etherscan 获取 CEX 地址收到的代币转账"""
        transfers = []
        api_key = self.config.etherscan_api_key

        for cex_name, addresses in self.config.cex_deposit_addresses.items():
            for addr in addresses[:1]:  # 每家只查主钱包避免 rate limit
                try:
                    url = 'https://api.etherscan.io/api'
                    params = {
                        'module': 'account',
                        'action': 'tokentx',
                        'address': addr,
                        'page': 1,
                        'offset': 50,
                        'sort': 'desc',
                        'apikey': api_key,
                    }
                    resp = requests.get(url, params=params, timeout=10)
                    if resp.status_code != 200:
                        continue

                    data = resp.json()
                    if data.get('status') != '1':
                        continue

                    for tx in data.get('result', []):
                        transfer = self._parse_etherscan_tx(tx, cex_name)
                        if transfer and transfer.usd_value >= self.config.min_transfer_usd:
                            transfers.append(transfer)

                except Exception as e:
                    logger.debug(f"Etherscan 查询失败 ({cex_name}): {e}")

                time.sleep(0.25)  # Rate limit

        return transfers

    def _parse_etherscan_tx(self, tx: dict, cex_name: str) -> Optional[WhaleTransfer]:
        """解析 Etherscan 代币转账交易"""
        try:
            token_symbol = tx.get('tokenSymbol', '').upper()
            decimals = int(tx.get('tokenDecimal', 18))
            value = int(tx.get('value', 0))
            amount = value / (10 ** decimals)

            # 粗略 USD 估值（实际应查价格，这里用简化逻辑）
            # 对于主流代币用已知价格范围估算
            usd_value = self._estimate_usd_value(token_symbol, amount)

            if usd_value < self.config.min_transfer_usd:
                return None

            timestamp = datetime.fromtimestamp(
                int(tx.get('timeStamp', 0)), tz=timezone.utc
            )

            # 检查时间窗口
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=self.config.lookback_hours
            )
            if timestamp < cutoff:
                return None

            return WhaleTransfer(
                tx_hash=tx.get('hash', ''),
                from_address=tx.get('from', ''),
                to_address=tx.get('to', ''),
                token_symbol=token_symbol,
                amount=amount,
                usd_value=usd_value,
                destination=cex_name,
                timestamp=timestamp,
                chain='ethereum',
                is_cex_deposit=True,
            )
        except Exception:
            return None

    def _fetch_from_whale_alert_api(self) -> List[WhaleTransfer]:
        """从 Whale Alert API 获取（backup source）"""
        transfers = []
        try:
            api_key = self.config.whale_alert_api_key
            since = int(time.time()) - self.config.lookback_hours * 3600
            url = f'https://api.whale-alert.io/v1/transactions'
            params = {
                'api_key': api_key,
                'min_value': int(self.config.min_transfer_usd),
                'start': since,
                'cursor': '',
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return []

            data = resp.json()
            for tx in data.get('transactions', []):
                transfer = self._parse_whale_alert_tx(tx)
                if transfer:
                    transfers.append(transfer)

        except Exception as e:
            logger.debug(f"Whale Alert API 查询失败: {e}")

        return transfers

    def _parse_whale_alert_tx(self, tx: dict) -> Optional[WhaleTransfer]:
        """解析 Whale Alert API 交易"""
        try:
            # 只关注转入 CEX 的
            to_owner = tx.get('to', {}).get('owner', '').lower()
            if to_owner not in ('binance', 'okx', 'huobi', 'kucoin'):
                return None

            token_symbol = tx.get('symbol', '').upper()
            amount = float(tx.get('amount', 0))
            usd_value = float(tx.get('amount_usd', 0))

            if usd_value < self.config.min_transfer_usd:
                return None

            timestamp = datetime.fromtimestamp(
                tx.get('timestamp', 0), tz=timezone.utc
            )

            return WhaleTransfer(
                tx_hash=tx.get('hash', tx.get('id', '')),
                from_address=tx.get('from', {}).get('address', ''),
                to_address=tx.get('to', {}).get('address', ''),
                token_symbol=token_symbol,
                amount=amount,
                usd_value=usd_value,
                destination=to_owner,
                timestamp=timestamp,
                chain=tx.get('blockchain', 'ethereum'),
                is_cex_deposit=True,
            )
        except Exception:
            return None

    def _fetch_from_public_api(self) -> List[WhaleTransfer]:
        """公开 API 降级方案（无需 API key）"""
        # 这里使用 Binance 的公开 deposit/withdrawal 数据
        # 注意：实际生产中应配置 Etherscan API key
        return []

    # ── 信号计算 ─────────────────────────────────────────────────

    def _compute_signal(self, symbol: str) -> Optional[WhaleSignal]:
        """为指定币种计算鲸鱼信号"""
        # 从 symbol 提取 token（如 'PEPE/USDT' → 'PEPE'）
        token = symbol.split('/')[0].upper()

        with self._lock:
            # 筛选该 token 的 CEX 充值记录
            relevant = [
                t for t in self._transfers
                if t.token_symbol == token and t.is_cex_deposit
            ]

        if not relevant:
            return None

        # 计算汇总指标
        total_usd = sum(t.usd_value for t in relevant)
        count = len(relevant)
        largest = max(t.usd_value for t in relevant)
        latest = max(t.timestamp for t in relevant)
        age_min = (datetime.now(timezone.utc) - latest).total_seconds() / 60

        # 拆单检测
        is_batch = self._detect_batch_pattern(relevant)

        # 评分
        bonus = 0
        confidence = 0.0

        if largest >= self.config.mega_whale_usd:
            bonus = self.config.mega_whale_score
            confidence = 0.9
        elif largest >= self.config.whale_threshold_usd:
            bonus = self.config.whale_score
            confidence = 0.7
        elif total_usd >= self.config.whale_threshold_usd:
            bonus = self.config.whale_score - 2
            confidence = 0.6

        # 拆单 bonus
        if is_batch:
            bonus += self.config.batch_bonus
            confidence = min(1.0, confidence + 0.15)

        # 时效性衰减（越久远信号越弱）
        if age_min > 120:
            bonus = int(bonus * 0.6)
            confidence *= 0.7
        elif age_min > 60:
            bonus = int(bonus * 0.8)
            confidence *= 0.85

        bonus = min(bonus, self.config.max_bonus_score)

        if bonus <= 0:
            return None

        # 构造描述
        details_parts = []
        if largest >= self.config.mega_whale_usd:
            details_parts.append(f"超级鲸鱼${largest/1e6:.1f}M")
        elif largest >= self.config.whale_threshold_usd:
            details_parts.append(f"鲸鱼${largest/1e6:.1f}M")
        if is_batch:
            details_parts.append(f"拆单模式({count}笔)")
        details_parts.append(f"总计${total_usd/1e6:.1f}M→CEX")

        return WhaleSignal(
            symbol=symbol,
            total_usd_inflow=total_usd,
            transfer_count=count,
            largest_single=largest,
            is_batch_pattern=is_batch,
            bonus_score=bonus,
            confidence=confidence,
            direction_hint='bearish',
            last_transfer_age_min=age_min,
            details=' | '.join(details_parts),
        )

    def _detect_batch_pattern(self, transfers: List[WhaleTransfer]) -> bool:
        """检测拆单模式：多笔中等金额在短时间内连续充值"""
        if len(transfers) < self.config.batch_min_count:
            return False

        # 按时间排序
        sorted_tx = sorted(transfers, key=lambda t: t.timestamp)

        # 滑动窗口检测
        window = timedelta(minutes=self.config.batch_detection_window_min)
        for i in range(len(sorted_tx) - self.config.batch_min_count + 1):
            window_end = sorted_tx[i].timestamp + window
            in_window = [
                t for t in sorted_tx[i:]
                if t.timestamp <= window_end
            ]
            if len(in_window) >= self.config.batch_min_count:
                # 检查金额是否在同一量级（拆单特征）
                amounts = [t.usd_value for t in in_window]
                avg = sum(amounts) / len(amounts)
                max_dev = max(abs(a - avg) / avg for a in amounts) if avg > 0 else 1
                if max_dev < 0.5:  # 金额偏差 < 50% 视为拆单
                    return True

        return False

    def _estimate_usd_value(self, token_symbol: str, amount: float) -> float:
        """粗略估算 USD 价值（生产环境应接入实时价格）"""
        # 已知代币的大致价格（定期更新）
        approx_prices = {
            'USDT': 1.0, 'USDC': 1.0, 'BUSD': 1.0,
            'ETH': 2500.0, 'BTC': 70000.0, 'WBTC': 70000.0,
            'PEPE': 0.000012, 'SHIB': 0.000022,
            'DOGE': 0.15, 'FLOKI': 0.00015,
            'BONK': 0.00002, 'WIF': 2.0,
        }
        price = approx_prices.get(token_symbol, 0)
        if price > 0:
            return amount * price

        # 未知代币：尝试从缓存价格获取
        return 0.0


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_engine: Optional[WhaleAlertEngine] = None


def get_whale_engine() -> WhaleAlertEngine:
    """获取鲸鱼预警引擎单例"""
    global _engine
    if _engine is None:
        _engine = WhaleAlertEngine()
    return _engine


def get_whale_signal(symbol: str) -> Optional[WhaleSignal]:
    """便捷函数：获取指定币种的鲸鱼信号"""
    return get_whale_engine().get_signal(symbol)


def get_whale_bonus(symbol: str) -> int:
    """便捷函数：获取鲸鱼加分（直接用于 signal_score）"""
    signal = get_whale_signal(symbol)
    return signal.bonus_score if signal else 0
