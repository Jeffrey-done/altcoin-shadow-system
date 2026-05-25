"""
事件总线测试
覆盖: InMemory 后端、发布/订阅、通配符、序列化、便捷函数
"""

import time
import threading
from unittest.mock import MagicMock, patch

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault('ccxt', MagicMock())
sys.modules.setdefault('redis', MagicMock())


class TestEvent:
    """Event 数据结构测试"""

    def test_event_creation(self):
        from event_bus import Event
        evt = Event(channel='trade.opened', data={'symbol': 'PEPE/USDT'})
        assert evt.channel == 'trade.opened'
        assert evt.data['symbol'] == 'PEPE/USDT'
        assert evt.timestamp != ''
        assert evt.event_id != ''

    def test_event_serialization(self):
        from event_bus import Event
        evt = Event(channel='test.event', data={'key': 'value', 'num': 42})
        json_str = evt.to_json()
        assert '"test.event"' in json_str
        assert '"key"' in json_str

        restored = Event.from_json(json_str)
        assert restored.channel == 'test.event'
        assert restored.data['key'] == 'value'
        assert restored.data['num'] == 42

    def test_event_id_uniqueness(self):
        from event_bus import Event
        e1 = Event(channel='a', data={'x': 1})
        e2 = Event(channel='a', data={'x': 2})
        assert e1.event_id != e2.event_id


class TestInMemoryBackend:
    """InMemory 事件总线测试"""

    def test_publish_subscribe(self):
        from event_bus import InMemoryBackend
        backend = InMemoryBackend()
        backend.start()

        received = []

        def callback(event):
            received.append(event)

        backend.subscribe('trade.opened', callback)

        from event_bus import Event
        evt = Event(channel='trade.opened', data={'symbol': 'DOGE/USDT'})
        backend.publish(evt)

        time.sleep(0.1)
        assert len(received) == 1
        assert received[0].data['symbol'] == 'DOGE/USDT'
        backend.stop()

    def test_wildcard_subscription(self):
        from event_bus import InMemoryBackend, Event
        backend = InMemoryBackend()
        backend.start()

        received = []
        backend.subscribe('trade.*', lambda e: received.append(e))

        backend.publish(Event(channel='trade.opened', data={}))
        backend.publish(Event(channel='trade.closed', data={}))
        backend.publish(Event(channel='risk.alert', data={}))

        time.sleep(0.1)
        assert len(received) == 2
        backend.stop()

    def test_unsubscribe(self):
        from event_bus import InMemoryBackend, Event
        backend = InMemoryBackend()
        backend.start()

        received = []
        sub_id = backend.subscribe('test', lambda e: received.append(e))
        backend.publish(Event(channel='test', data={}))
        time.sleep(0.1)
        assert len(received) == 1

        backend.unsubscribe(sub_id)
        backend.publish(Event(channel='test', data={}))
        time.sleep(0.1)
        assert len(received) == 1  # No new events
        backend.stop()

    def test_publish_before_start_returns_false(self):
        from event_bus import InMemoryBackend, Event
        backend = InMemoryBackend()
        result = backend.publish(Event(channel='x', data={}))
        assert result is False

    def test_callback_exception_doesnt_crash(self):
        from event_bus import InMemoryBackend, Event
        backend = InMemoryBackend()
        backend.start()

        good_received = []

        def bad_callback(e):
            raise ValueError("intentional error")

        def good_callback(e):
            good_received.append(e)

        backend.subscribe('test', bad_callback)
        backend.subscribe('test', good_callback)

        backend.publish(Event(channel='test', data={}))
        time.sleep(0.2)
        assert len(good_received) == 1
        backend.stop()

    def test_history(self):
        from event_bus import InMemoryBackend, Event
        backend = InMemoryBackend()
        backend.start()

        for i in range(5):
            backend.publish(Event(channel=f'event.{i}', data={'i': i}))

        time.sleep(0.1)
        history = backend.get_history(limit=3)
        assert len(history) == 3
        backend.stop()


class TestEventBusFacade:
    """EventBus 统一门面测试"""

    def test_auto_start(self):
        from event_bus import EventBus, InMemoryBackend
        bus = EventBus(backend=InMemoryBackend())
        received = []
        bus.subscribe('test', lambda e: received.append(e))
        bus.publish('test', {'key': 'val'})
        time.sleep(0.1)
        assert len(received) == 1
        assert received[0].data['key'] == 'val'
        bus.stop()

    def test_backend_type_memory(self):
        from event_bus import EventBus, InMemoryBackend
        bus = EventBus(backend=InMemoryBackend())
        bus.start()
        assert bus.backend_type == 'memory'
        bus.stop()


class TestEmitHelpers:
    """便捷发布函数测试"""

    def test_emit_trade_opened(self):
        from event_bus import EventBus, InMemoryBackend, reset_event_bus
        reset_event_bus()

        # Patch global bus
        import event_bus
        backend = InMemoryBackend()
        bus = EventBus(backend=backend)
        bus.start()
        event_bus._global_bus = bus

        received = []
        bus.subscribe('trade.opened', lambda e: received.append(e))

        from event_bus import emit_trade_opened
        emit_trade_opened('t1', 'PEPE/USDT', 'SHORT', 30.0)
        time.sleep(0.1)

        assert len(received) == 1
        assert received[0].data['trade_id'] == 't1'
        assert received[0].data['symbol'] == 'PEPE/USDT'
        bus.stop()
        reset_event_bus()

    def test_emit_risk_alert(self):
        from event_bus import EventBus, InMemoryBackend, reset_event_bus
        reset_event_bus()

        import event_bus
        backend = InMemoryBackend()
        bus = EventBus(backend=backend)
        bus.start()
        event_bus._global_bus = bus

        received = []
        bus.subscribe('risk.alert', lambda e: received.append(e))

        from event_bus import emit_risk_alert
        emit_risk_alert('daily_loss_limit', 'Daily loss reached 30U')
        time.sleep(0.1)

        assert len(received) == 1
        assert received[0].data['alert_type'] == 'daily_loss_limit'
        bus.stop()
        reset_event_bus()
