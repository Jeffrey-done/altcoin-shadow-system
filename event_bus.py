#!/usr/bin/env python3
"""
事件总线 — 进程间通信中枢 v1.0

支持两种后端：
  - Redis Pub/Sub（生产环境推荐，跨进程/跨机器）
  - 内存 InProcess（单进程测试 / Redis 不可用时降级）

设计原则：
  - 发布-订阅模式，解耦 scheduler / realtime_monitor / dashboard
  - 事件结构化（JSON 序列化，带时间戳和来源标识）
  - 优雅降级：Redis 不可用时自动切换内存模式，不阻塞主业务
  - 线程安全：回调在独立 listener 线程中执行，不阻塞发布方
  - 支持事件过滤（按 channel / event_type）

核心事件类型：
  - trade.opened       — 开仓成功
  - trade.closed       — 平仓完成
  - trade.updated      — 持仓字段更新（浮动盈亏/移动止损等）
  - candidate.added    — 新候选加入
  - candidate.triggered — 候选确认触发
  - risk.alert         — 风控告警（日亏达限/连亏/暂停）
  - risk.state_changed — 风控状态变更
  - signal.scored      — 信号评分完成
  - system.health      — 系统健康状态
  - system.task_done   — 调度任务完成
  - config.changed     — 运行时配置变更

用法：
  from event_bus import get_event_bus, Event

  bus = get_event_bus()

  # 发布事件
  bus.publish('trade.opened', {
      'trade_id': 'xxx', 'symbol': 'PEPE/USDT', 'stake': 30
  })

  # 订阅事件
  def on_trade_opened(event: Event):
      print(f"新开仓: {event.data['symbol']}")

  bus.subscribe('trade.opened', on_trade_opened)
  bus.subscribe('trade.*', on_any_trade)  # 通配符订阅
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import fnmatch
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("event_bus")


# ══════════════════════════════════════════════════════════════════
#  事件数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class Event:
    """结构化事件"""
    channel: str                         # 事件通道（如 'trade.opened'）
    data: Dict[str, Any]                 # 事件负载
    timestamp: str = ''                  # ISO 8601 UTC
    source: str = ''                     # 发布来源（进程标识）
    event_id: str = ''                   # 唯一 ID（幂等去重）

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.event_id:
            import hashlib
            raw = f"{self.channel}:{self.timestamp}:{id(self.data)}"
            self.event_id = hashlib.md5(raw.encode()).hexdigest()[:12]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> Event:
        d = json.loads(raw)
        return cls(**d)


# ══════════════════════════════════════════════════════════════════
#  订阅者回调类型
# ══════════════════════════════════════════════════════════════════

EventCallback = Callable[[Event], None]


@dataclass
class Subscription:
    """一个订阅"""
    pattern: str                         # 通道模式（支持 * 通配符）
    callback: EventCallback
    subscriber_id: str = ''              # 订阅者标识（用于日志/取消）

    def matches(self, channel: str) -> bool:
        """检查事件通道是否匹配订阅模式"""
        return fnmatch.fnmatch(channel, self.pattern)


# ══════════════════════════════════════════════════════════════════
#  事件总线抽象基类
# ══════════════════════════════════════════════════════════════════

class EventBusBackend:
    """事件总线后端抽象"""

    def publish(self, event: Event) -> bool:
        raise NotImplementedError

    def subscribe(self, pattern: str, callback: EventCallback,
                  subscriber_id: str = '') -> str:
        raise NotImplementedError

    def unsubscribe(self, subscription_id: str) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    @property
    def is_running(self) -> bool:
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════
#  内存后端（单进程 / Redis 降级时使用）
# ══════════════════════════════════════════════════════════════════

class InMemoryBackend(EventBusBackend):
    """
    内存事件总线 — 线程安全，仅在同一进程内有效。
    适用于：单进程调试、Redis 不可用时的降级方案。
    """

    def __init__(self):
        self._subscriptions: Dict[str, Subscription] = {}
        self._lock = threading.Lock()
        self._running = False
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix='evtbus'
        )
        self._sub_counter = 0
        self._event_history: List[Event] = []
        self._max_history = 1000

    def publish(self, event: Event) -> bool:
        if not self._running:
            return False

        # 保留事件历史
        with self._lock:
            self._event_history.append(event)
            if len(self._event_history) > self._max_history:
                self._event_history = self._event_history[-self._max_history:]
            subs = list(self._subscriptions.values())

        # 分发到匹配的订阅者
        for sub in subs:
            if sub.matches(event.channel):
                self._executor.submit(self._safe_callback, sub, event)

        return True

    def subscribe(self, pattern: str, callback: EventCallback,
                  subscriber_id: str = '') -> str:
        with self._lock:
            self._sub_counter += 1
            sub_id = subscriber_id or f"sub_{self._sub_counter}"
            self._subscriptions[sub_id] = Subscription(
                pattern=pattern,
                callback=callback,
                subscriber_id=sub_id,
            )
        logger.debug(f"订阅注册: {sub_id} → {pattern}")
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            if subscription_id in self._subscriptions:
                del self._subscriptions[subscription_id]
                return True
        return False

    def start(self) -> None:
        self._running = True
        logger.info("📡 InMemory EventBus 已启动")

    def stop(self) -> None:
        self._running = False
        self._executor.shutdown(wait=False)
        logger.info("📡 InMemory EventBus 已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    def get_history(self, channel: Optional[str] = None,
                    limit: int = 50) -> List[Event]:
        """获取事件历史（调试用）"""
        with self._lock:
            events = self._event_history.copy()
        if channel:
            events = [e for e in events if fnmatch.fnmatch(e.channel, channel)]
        return events[-limit:]

    def _safe_callback(self, sub: Subscription, event: Event):
        """安全执行回调（捕获异常避免影响其他订阅者）"""
        try:
            sub.callback(event)
        except Exception as e:
            logger.error(
                f"事件回调异常 [{sub.subscriber_id}] channel={event.channel}: {e}",
                exc_info=True,
            )


# ══════════════════════════════════════════════════════════════════
#  Redis 后端（生产跨进程通信）
# ══════════════════════════════════════════════════════════════════

class RedisBackend(EventBusBackend):
    """
    Redis Pub/Sub 事件总线 — 跨进程通信。

    特性：
      - 自动重连（指数退避，最长 30s）
      - 消息序列化为 JSON
      - 支持通道通配符订阅 (PSUBSCRIBE)
      - 心跳检测（30s 无消息发 ping）
    """

    def __init__(
        self,
        url: str = '',
        prefix: str = 'altcoin:event:',
        max_reconnect_delay: float = 30.0,
    ):
        self._url = url or os.environ.get(
            'REDIS_URL', 'redis://localhost:6379/0'
        )
        self._prefix = prefix
        self._max_reconnect_delay = max_reconnect_delay

        self._redis = None
        self._pubsub = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._subscriptions: Dict[str, Subscription] = {}
        self._lock = threading.Lock()
        self._sub_counter = 0
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix='redis_evtbus'
        )
        self._reconnect_attempts = 0

    def _connect(self) -> bool:
        """连接 Redis"""
        try:
            import redis
            self._redis = redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=10,
                retry_on_timeout=True,
            )
            self._redis.ping()
            self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            self._reconnect_attempts = 0
            logger.info(f"📡 Redis EventBus 连接成功: {self._url}")
            return True
        except ImportError:
            logger.error("redis 包未安装，请 pip install redis")
            return False
        except Exception as e:
            self._reconnect_attempts += 1
            delay = min(
                2 ** self._reconnect_attempts, self._max_reconnect_delay
            )
            logger.warning(
                f"Redis 连接失败 (attempt {self._reconnect_attempts}): {e}, "
                f"{delay:.0f}s 后重试"
            )
            return False

    def publish(self, event: Event) -> bool:
        if not self._running or not self._redis:
            return False
        try:
            channel = f"{self._prefix}{event.channel}"
            payload = event.to_json()
            self._redis.publish(channel, payload)
            return True
        except Exception as e:
            logger.warning(f"Redis publish 失败: {e}")
            return False

    def subscribe(self, pattern: str, callback: EventCallback,
                  subscriber_id: str = '') -> str:
        with self._lock:
            self._sub_counter += 1
            sub_id = subscriber_id or f"rsub_{self._sub_counter}"
            self._subscriptions[sub_id] = Subscription(
                pattern=pattern,
                callback=callback,
                subscriber_id=sub_id,
            )

        # 在 Redis 层面订阅（带前缀）
        if self._pubsub:
            try:
                redis_pattern = f"{self._prefix}{pattern}"
                if '*' in pattern or '?' in pattern:
                    self._pubsub.psubscribe(redis_pattern)
                else:
                    self._pubsub.subscribe(redis_pattern)
            except Exception as e:
                logger.warning(f"Redis subscribe 失败: {e}")

        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            if subscription_id in self._subscriptions:
                sub = self._subscriptions.pop(subscription_id)
                # 从 Redis 取消订阅
                if self._pubsub:
                    try:
                        redis_pattern = f"{self._prefix}{sub.pattern}"
                        if '*' in sub.pattern or '?' in sub.pattern:
                            self._pubsub.punsubscribe(redis_pattern)
                        else:
                            self._pubsub.unsubscribe(redis_pattern)
                    except Exception:
                        pass
                return True
        return False

    def start(self) -> None:
        if not self._connect():
            logger.warning("Redis 连接失败，EventBus 将在后台重试")

        self._running = True
        self._listener_thread = threading.Thread(
            target=self._listen_loop,
            name='redis_eventbus_listener',
            daemon=True,
        )
        self._listener_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pubsub:
            try:
                self._pubsub.close()
            except Exception:
                pass
        if self._redis:
            try:
                self._redis.close()
            except Exception:
                pass
        self._executor.shutdown(wait=False)
        logger.info("📡 Redis EventBus 已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    def _listen_loop(self):
        """Redis 消息监听循环"""
        while self._running:
            try:
                if not self._pubsub:
                    if not self._connect():
                        time.sleep(min(
                            2 ** self._reconnect_attempts,
                            self._max_reconnect_delay,
                        ))
                        continue
                    # 重新订阅所有 pattern
                    with self._lock:
                        for sub in self._subscriptions.values():
                            redis_pattern = f"{self._prefix}{sub.pattern}"
                            if '*' in sub.pattern or '?' in sub.pattern:
                                self._pubsub.psubscribe(redis_pattern)
                            else:
                                self._pubsub.subscribe(redis_pattern)

                message = self._pubsub.get_message(timeout=1.0)
                if message and message['type'] in ('message', 'pmessage'):
                    self._handle_message(message)

            except Exception as e:
                if self._running:
                    logger.warning(f"Redis listener 异常: {e}")
                    self._pubsub = None
                    time.sleep(1)

    def _handle_message(self, message: dict):
        """处理收到的 Redis 消息"""
        try:
            data = message.get('data', '')
            if not isinstance(data, str):
                return

            event = Event.from_json(data)

            # 分发到本地订阅者
            with self._lock:
                subs = list(self._subscriptions.values())

            for sub in subs:
                if sub.matches(event.channel):
                    self._executor.submit(self._safe_callback, sub, event)

        except (json.JSONDecodeError, TypeError) as e:
            logger.debug(f"无法解析 Redis 消息: {e}")

    def _safe_callback(self, sub: Subscription, event: Event):
        """安全执行回调"""
        try:
            sub.callback(event)
        except Exception as e:
            logger.error(
                f"Redis 事件回调异常 [{sub.subscriber_id}] "
                f"channel={event.channel}: {e}",
                exc_info=True,
            )


# ══════════════════════════════════════════════════════════════════
#  统一门面（自动选择后端）
# ══════════════════════════════════════════════════════════════════

class EventBus:
    """
    事件总线统一门面。
    自动探测 Redis 可用性，不可用时降级到内存模式。

    用法：
      bus = EventBus()
      bus.start()
      bus.publish('trade.opened', {'symbol': 'PEPE/USDT'})
      bus.subscribe('trade.*', callback)
      bus.stop()
    """

    def __init__(self, backend: Optional[EventBusBackend] = None,
                 source: str = ''):
        self._source = source or f"pid_{os.getpid()}"
        self._backend = backend
        self._started = False

    def start(self) -> None:
        """启动事件总线（自动选择后端）"""
        if self._started:
            return

        if self._backend is None:
            self._backend = self._auto_select_backend()

        self._backend.start()
        self._started = True

    def stop(self) -> None:
        """停止事件总线"""
        if self._backend:
            self._backend.stop()
        self._started = False

    def publish(self, channel: str, data: Dict[str, Any] = None) -> bool:
        """
        发布事件。

        参数:
          channel: 事件通道（如 'trade.opened'）
          data: 事件负载 dict

        返回:
          是否成功发布
        """
        if not self._started:
            self.start()

        event = Event(
            channel=channel,
            data=data or {},
            source=self._source,
        )
        success = self._backend.publish(event)

        if success:
            logger.debug(f"📤 事件发布: {channel} → {json.dumps(data or {}, ensure_ascii=False)[:100]}")
        return success

    def subscribe(self, pattern: str, callback: EventCallback,
                  subscriber_id: str = '') -> str:
        """
        订阅事件。

        参数:
          pattern: 通道模式（支持 * 通配符，如 'trade.*'）
          callback: 回调函数 (Event) -> None
          subscriber_id: 可选的订阅者标识

        返回:
          订阅 ID（用于取消订阅）
        """
        if not self._started:
            self.start()
        return self._backend.subscribe(pattern, callback, subscriber_id)

    def unsubscribe(self, subscription_id: str) -> bool:
        """取消订阅"""
        if self._backend:
            return self._backend.unsubscribe(subscription_id)
        return False

    @property
    def backend_type(self) -> str:
        """当前后端类型"""
        if isinstance(self._backend, RedisBackend):
            return 'redis'
        return 'memory'

    @property
    def is_running(self) -> bool:
        return self._started and (self._backend.is_running if self._backend else False)

    def _auto_select_backend(self) -> EventBusBackend:
        """自动选择后端：优先 Redis，不可用则内存"""
        redis_url = os.environ.get('REDIS_URL', '')

        if redis_url:
            try:
                import redis
                r = redis.from_url(redis_url, socket_connect_timeout=2)
                r.ping()
                r.close()
                logger.info(f"✅ Redis 可用，使用 Redis Pub/Sub 后端")
                return RedisBackend(url=redis_url)
            except Exception as e:
                logger.info(f"⚠️ Redis 不可用 ({e})，降级到内存模式")

        # Redis 不可用或未配置 → 内存模式
        return InMemoryBackend()


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_global_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def get_event_bus(source: str = '') -> EventBus:
    """获取全局事件总线单例"""
    global _global_bus
    if _global_bus is None:
        with _bus_lock:
            if _global_bus is None:
                _global_bus = EventBus(source=source)
    return _global_bus


def reset_event_bus() -> None:
    """重置全局事件总线（仅用于测试）"""
    global _global_bus
    if _global_bus:
        _global_bus.stop()
    _global_bus = None


# ══════════════════════════════════════════════════════════════════
#  便捷发布函数（常用事件的快捷方式）
# ══════════════════════════════════════════════════════════════════

def emit_trade_opened(trade_id: str, symbol: str, direction: str,
                      stake: float, exchange: str = 'shadow',
                      account_id: str = '', **extra):
    """发布开仓事件"""
    get_event_bus().publish('trade.opened', {
        'trade_id': trade_id,
        'symbol': symbol,
        'direction': direction,
        'stake': stake,
        'exchange': exchange,
        'account_id': account_id,
        **extra,
    })


def emit_trade_closed(trade_id: str, symbol: str, pnl: float,
                      close_type: str, close_reason: str = '',
                      exchange: str = 'shadow', **extra):
    """发布平仓事件"""
    get_event_bus().publish('trade.closed', {
        'trade_id': trade_id,
        'symbol': symbol,
        'pnl': pnl,
        'close_type': close_type,
        'close_reason': close_reason,
        'exchange': exchange,
        **extra,
    })


def emit_trade_updated(trade_id: str, symbol: str, updates: Dict[str, Any]):
    """发布持仓更新事件"""
    get_event_bus().publish('trade.updated', {
        'trade_id': trade_id,
        'symbol': symbol,
        'updates': updates,
    })


def emit_candidate_added(symbol: str, rsi_1d: float, score: float = 0, **extra):
    """发布新候选事件"""
    get_event_bus().publish('candidate.added', {
        'symbol': symbol,
        'rsi_1d': rsi_1d,
        'score': score,
        **extra,
    })


def emit_risk_alert(alert_type: str, message: str, account_id: str = '', **extra):
    """发布风控告警事件"""
    get_event_bus().publish('risk.alert', {
        'alert_type': alert_type,
        'message': message,
        'account_id': account_id,
        **extra,
    })


def emit_signal_scored(symbol: str, strategy: str, score: int,
                       grade: str, triggered: bool = False, **extra):
    """发布信号评分事件"""
    get_event_bus().publish('signal.scored', {
        'symbol': symbol,
        'strategy': strategy,
        'score': score,
        'grade': grade,
        'triggered': triggered,
        **extra,
    })


def emit_config_changed(key: str, old_value: Any, new_value: Any,
                        changed_by: str = 'admin'):
    """发布配置变更事件"""
    get_event_bus().publish('config.changed', {
        'key': key,
        'old_value': old_value,
        'new_value': new_value,
        'changed_by': changed_by,
    })


def emit_system_health(component: str, status: str, details: Dict[str, Any] = None):
    """发布系统健康事件"""
    get_event_bus().publish('system.health', {
        'component': component,
        'status': status,
        'details': details or {},
    })
