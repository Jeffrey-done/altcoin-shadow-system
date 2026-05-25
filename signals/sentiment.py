#!/usr/bin/env python3
"""
社交情绪信号 v1.0

通过 Twitter/X API v2 搜索 Crypto cashtag（$PEPE, $DOGE 等），
用关键词权重法计算情绪分数，作为策略评分的 bonus/penalty。

Alpha 逻辑：
  - 极度 FOMO 情绪（全网吹爆）→ 做空加分（群体极端看多 = 顶部信号）
  - 极度恐慌情绪（全网喊崩）→ 做多加分（恐慌抛售 = 底部信号）
  - 中性情绪 → 不加不减

数据源（按优先级）：
  1. Twitter/X API v2 Recent Search（需 Bearer Token）
  2. CoinGlass Fear & Greed Index（公开免费）
  3. 本地 fallback：用 funding rate 离散度作为情绪代理

集成方式：
  - 被 signal_score.py 调用，返回 bonus 分数 (-5 ~ +10)
  - 通过 event_bus 发布 'sentiment.update' 事件
"""

from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("signals.sentiment")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class SentimentConfig:
    """情绪信号配置"""
    enabled: bool = True

    # Twitter API
    twitter_bearer_token: str = ''     # X/Twitter API v2 Bearer Token
    search_window_hours: int = 4       # 搜索最近 N 小时的推文
    max_results_per_query: int = 100   # 每次搜索最多返回条数

    # 关键词权重（看多情绪关键词）
    bullish_keywords: Dict[str, float] = field(default_factory=lambda: {
        'moon': 2.0, 'pump': 1.5, 'bullish': 1.5, 'buy': 1.0,
        'rocket': 2.0, 'to the moon': 2.5, '100x': 3.0, 'gem': 1.5,
        'lfg': 1.5, 'send it': 1.5, 'all in': 2.0,
        '涨': 1.0, '冲': 1.5, '梭哈': 2.0, '起飞': 2.0,
    })

    # 看空情绪关键词
    bearish_keywords: Dict[str, float] = field(default_factory=lambda: {
        'dump': 1.5, 'crash': 2.0, 'bearish': 1.5, 'sell': 1.0,
        'scam': 2.0, 'rug': 2.5, 'dead': 1.5, 'rip': 1.5,
        'short': 1.0, 'overvalued': 1.5,
        '跌': 1.0, '崩': 2.0, '割': 1.5, '归零': 2.5,
    })

    # 评分阈值
    fomo_threshold: float = 3.0        # 情绪分 > 此值 = FOMO（做空加分）
    panic_threshold: float = -2.0      # 情绪分 < 此值 = 恐慌（做多加分）

    # 加分范围
    max_short_bonus: int = 10          # FOMO 时做空最大加分
    max_long_bonus: int = 8            # 恐慌时做多最大加分
    max_penalty: int = -5              # 情绪反向时的最大扣分

    # 缓存
    cache_ttl_sec: int = 600           # 10 分钟缓存
    poll_interval_sec: int = 300       # 5 分钟轮询

    # Fear & Greed Index（备用数据源）
    fear_greed_enabled: bool = True
    fear_greed_url: str = 'https://api.alternative.me/fng/?limit=1'


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class SentimentResult:
    """单币种情绪分析结果"""
    symbol: str
    raw_score: float = 0.0             # 原始情绪分（正=看多，负=看空）
    normalized_score: float = 0.0      # 归一化 (-10 ~ +10)
    tweet_count: int = 0               # 样本量
    bullish_count: int = 0             # 看多推文数
    bearish_count: int = 0             # 看空推文数
    short_bonus: int = 0              # 给做空策略的加分
    long_bonus: int = 0               # 给做多策略的加分
    confidence: float = 0.0            # 置信度 (0~1)
    source: str = 'none'               # 数据来源
    updated_at: float = 0.0


@dataclass
class MarketSentiment:
    """全市场情绪"""
    fear_greed_index: int = 50         # 0=极度恐慌, 100=极度贪婪
    fear_greed_label: str = 'Neutral'
    market_sentiment_score: float = 0.0  # -10 ~ +10
    updated_at: float = 0.0


# ══════════════════════════════════════════════════════════════════
#  情绪分析引擎
# ══════════════════════════════════════════════════════════════════

class SentimentEngine:
    """
    社交情绪分析引擎。

    用法:
      engine = SentimentEngine()
      engine.start()

      # 获取单币情绪
      result = engine.get_sentiment('PEPE/USDT')
      if result.short_bonus > 0:
          score += result.short_bonus  # FOMO → 做空加分

      # 获取市场整体情绪
      market = engine.get_market_sentiment()
    """

    def __init__(self, config: Optional[SentimentConfig] = None):
        self.config = config or SentimentConfig()
        self._cache: Dict[str, Tuple[SentimentResult, float]] = {}
        self._market_cache: Optional[Tuple[MarketSentiment, float]] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 加载环境变量
        if not self.config.twitter_bearer_token:
            self.config.twitter_bearer_token = os.environ.get('TWITTER_BEARER_TOKEN', '')

    def start(self) -> None:
        """启动后台轮询"""
        if not self.config.enabled:
            logger.info("💬 情绪分析已禁用")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, name='sentiment_poll', daemon=True
        )
        self._thread.start()
        logger.info("💬 情绪分析引擎已启动")

    def stop(self) -> None:
        self._running = False

    def get_sentiment(self, symbol: str) -> SentimentResult:
        """获取单币种情绪"""
        with self._lock:
            cached = self._cache.get(symbol)
            if cached:
                result, ts = cached
                if time.time() - ts < self.config.cache_ttl_sec:
                    return result

        # 缓存未命中，同步计算
        result = self._analyze_symbol(symbol)
        with self._lock:
            self._cache[symbol] = (result, time.time())
        return result

    def get_market_sentiment(self) -> MarketSentiment:
        """获取全市场情绪"""
        with self._lock:
            if self._market_cache:
                ms, ts = self._market_cache
                if time.time() - ts < self.config.cache_ttl_sec:
                    return ms

        ms = self._fetch_market_sentiment()
        with self._lock:
            self._market_cache = (ms, time.time())
        return ms

    def get_short_bonus(self, symbol: str) -> int:
        """便捷：获取做空加分"""
        return self.get_sentiment(symbol).short_bonus

    def get_long_bonus(self, symbol: str) -> int:
        """便捷：获取做多加分"""
        return self.get_sentiment(symbol).long_bonus

    # ── 分析逻辑 ─────────────────────────────────────────────────

    def _analyze_symbol(self, symbol: str) -> SentimentResult:
        """分析单币种情绪"""
        token = symbol.split('/')[0].upper()
        result = SentimentResult(symbol=symbol, updated_at=time.time())

        # 尝试 Twitter API
        if self.config.twitter_bearer_token:
            tweets = self._fetch_tweets(token)
            if tweets:
                result = self._score_tweets(symbol, tweets)
                result.source = 'twitter'
                self._apply_bonuses(result)
                return result

        # Fallback: Fear & Greed + funding rate 代理
        market = self.get_market_sentiment()
        result.source = 'fear_greed_proxy'
        # 转换 F&G index 到情绪分：50=中性, >75=FOMO, <25=恐慌
        fg = market.fear_greed_index
        if fg >= 75:
            result.raw_score = (fg - 50) / 10  # 2.5~5.0
        elif fg <= 25:
            result.raw_score = (fg - 50) / 10  # -5.0~-2.5
        else:
            result.raw_score = (fg - 50) / 20  # mild

        result.normalized_score = max(-10, min(10, result.raw_score))
        result.confidence = 0.4  # 低置信度（非币种特定）
        self._apply_bonuses(result)
        return result

    def _fetch_tweets(self, token: str) -> List[str]:
        """从 Twitter API v2 搜索推文"""
        try:
            url = 'https://api.twitter.com/2/tweets/search/recent'
            headers = {
                'Authorization': f'Bearer {self.config.twitter_bearer_token}',
            }
            query = f'${token} OR #{token} -is:retweet lang:en'
            params = {
                'query': query[:512],
                'max_results': min(self.config.max_results_per_query, 100),
                'tweet.fields': 'created_at,public_metrics',
            }
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                tweets = data.get('data', [])
                return [t.get('text', '') for t in tweets]
            elif resp.status_code == 429:
                logger.debug("Twitter API rate limited")
            else:
                logger.debug(f"Twitter API error: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Twitter fetch 失败: {e}")
        return []

    def _score_tweets(self, symbol: str, tweets: List[str]) -> SentimentResult:
        """对推文列表打分"""
        cfg = self.config
        result = SentimentResult(symbol=symbol, updated_at=time.time())
        result.tweet_count = len(tweets)

        total_score = 0.0
        for text in tweets:
            text_lower = text.lower()
            tweet_score = 0.0

            for keyword, weight in cfg.bullish_keywords.items():
                if keyword in text_lower:
                    tweet_score += weight

            for keyword, weight in cfg.bearish_keywords.items():
                if keyword in text_lower:
                    tweet_score -= weight

            if tweet_score > 0:
                result.bullish_count += 1
            elif tweet_score < 0:
                result.bearish_count += 1

            total_score += tweet_score

        # 归一化
        if result.tweet_count > 0:
            result.raw_score = total_score / result.tweet_count
        result.normalized_score = max(-10, min(10, result.raw_score))

        # 置信度基于样本量
        if result.tweet_count >= 50:
            result.confidence = 0.8
        elif result.tweet_count >= 20:
            result.confidence = 0.6
        elif result.tweet_count >= 5:
            result.confidence = 0.4
        else:
            result.confidence = 0.2

        return result

    def _apply_bonuses(self, result: SentimentResult):
        """根据情绪分计算 bonus"""
        cfg = self.config
        score = result.normalized_score
        conf = result.confidence

        if score >= cfg.fomo_threshold:
            # FOMO → 做空加分
            intensity = min(1.0, (score - cfg.fomo_threshold) / 3.0)
            result.short_bonus = min(cfg.max_short_bonus, round(intensity * cfg.max_short_bonus * conf))
            result.long_bonus = max(cfg.max_penalty, -round(intensity * 3 * conf))
        elif score <= cfg.panic_threshold:
            # 恐慌 → 做多加分
            intensity = min(1.0, (cfg.panic_threshold - score) / 3.0)
            result.long_bonus = min(cfg.max_long_bonus, round(intensity * cfg.max_long_bonus * conf))
            result.short_bonus = max(cfg.max_penalty, -round(intensity * 3 * conf))
        else:
            result.short_bonus = 0
            result.long_bonus = 0

    def _fetch_market_sentiment(self) -> MarketSentiment:
        """获取全市场 Fear & Greed Index"""
        ms = MarketSentiment(updated_at=time.time())

        if not self.config.fear_greed_enabled:
            return ms

        try:
            resp = requests.get(self.config.fear_greed_url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                fg_data = data.get('data', [{}])[0]
                ms.fear_greed_index = int(fg_data.get('value', 50))
                ms.fear_greed_label = fg_data.get('value_classification', 'Neutral')
                # 转换为 -10~+10 分
                ms.market_sentiment_score = (ms.fear_greed_index - 50) / 5
        except Exception as e:
            logger.debug(f"Fear & Greed 获取失败: {e}")

        return ms

    # ── 后台轮询 ─────────────────────────────────────────────────

    def _poll_loop(self):
        """后台轮询：定期更新 market sentiment"""
        while self._running:
            try:
                ms = self._fetch_market_sentiment()
                with self._lock:
                    self._market_cache = (ms, time.time())

                # 发布事件
                try:
                    from event_bus import get_event_bus
                    get_event_bus().publish('sentiment.update', {
                        'fear_greed_index': ms.fear_greed_index,
                        'label': ms.fear_greed_label,
                        'score': ms.market_sentiment_score,
                    })
                except Exception:
                    pass

            except Exception as e:
                logger.debug(f"情绪轮询异常: {e}")

            time.sleep(self.config.poll_interval_sec)


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_engine: Optional[SentimentEngine] = None


def get_sentiment_engine() -> SentimentEngine:
    """获取情绪引擎单例"""
    global _engine
    if _engine is None:
        _engine = SentimentEngine()
    return _engine


def get_short_sentiment_bonus(symbol: str) -> int:
    """便捷：获取做空情绪加分（直接用于 signal_score）"""
    return get_sentiment_engine().get_short_bonus(symbol)


def get_long_sentiment_bonus(symbol: str) -> int:
    """便捷：获取做多情绪加分"""
    return get_sentiment_engine().get_long_bonus(symbol)
