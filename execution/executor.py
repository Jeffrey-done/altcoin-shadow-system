"""
统一订单执行器
封装所有交易所下单逻辑，提供统一的异步执行接口。

特性:
  - 并发下单（asyncio 或 ThreadPoolExecutor）
  - 自动重试（指数退避，最多3次）
  - 超时控制（单笔10s，整批45s）
  - 幂等键自动生成
  - In-flight Journal 集成
  - 结构化执行事件记录
"""

from __future__ import annotations

import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from strategies.base import Signal, SignalDirection

logger = logging.getLogger("execution")


@dataclass
class ExecutionConfig:
    """执行器配置"""
    max_concurrent: int = 4            # 最大并发下单数
    single_timeout_sec: float = 10.0   # 单笔下单超时
    batch_timeout_sec: float = 45.0    # 批量下单总超时
    max_retries: int = 3               # 最大重试次数
    retry_backoff_sec: float = 1.0     # 重试退避基数
    dry_run: bool = False              # 模拟模式（不实际下单）


@dataclass
class ExecutionResult:
    """单笔执行结果"""
    success: bool = False
    exchange: str = ''
    symbol: str = ''
    direction: str = ''
    order_id: str = ''
    fill_price: float = 0.0
    fill_amount: float = 0.0
    stake: float = 0.0
    error: str = ''
    error_code: str = ''
    client_order_id: str = ''
    account_id: str = ''
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BatchExecutionResult:
    """批量执行结果"""
    results: List[ExecutionResult] = field(default_factory=list)
    total: int = 0
    success_count: int = 0
    failed_count: int = 0
    elapsed_ms: float = 0.0

    @property
    def all_success(self) -> bool:
        return self.failed_count == 0 and self.success_count > 0


class OrderExecutor:
    """
    统一订单执行器。

    用法:
      executor = OrderExecutor(config)

      # 单笔执行
      result = executor.execute_signal(signal, account_id='acc_main')

      # 多账户并发执行
      batch = executor.execute_multi_account(signal, ['acc_main', 'acc_hedge'])
    """

    def __init__(self, config: Optional[ExecutionConfig] = None):
        self.config = config or ExecutionConfig()
        self._pool = ThreadPoolExecutor(
            max_workers=self.config.max_concurrent,
            thread_name_prefix='executor',
        )
        # 标记 worker 为 daemon
        for t in list(self._pool._threads):
            try:
                t.daemon = True
            except RuntimeError:
                pass

    def execute_signal(
        self,
        signal: Signal,
        account_id: str = '',
        exchange_name: str = 'binance',
    ) -> ExecutionResult:
        """
        执行单个信号的下单。

        参数:
          signal: 策略产生的 Signal 对象
          account_id: 目标账户
          exchange_name: 目标交易所

        返回:
          ExecutionResult
        """
        cfg = self.config
        t0 = time.monotonic()

        if cfg.dry_run:
            fill_price = float(signal.metadata.get('entry_ref_price') or signal.metadata.get('price') or 0)
            fill_amount = round(signal.stake * signal.leverage / fill_price, 4) if fill_price > 0 else 0.0
            return ExecutionResult(
                success=True,
                exchange='shadow',
                symbol=signal.symbol,
                direction=signal.direction.value,
                order_id='DRY_RUN',
                fill_price=fill_price,
                fill_amount=fill_amount,
                stake=signal.stake,
                account_id=account_id,
                latency_ms=0.0,
            )

        # 生成幂等键
        from common import make_idempotency_key, now_ms
        ts = now_ms()
        prefix = 'osh' if signal.direction == SignalDirection.SHORT else 'oln'
        client_order_id = make_idempotency_key(
            signal.strategy_name, prefix, signal.symbol, ts
        )

        # In-flight Journal
        try:
            from db.repositories import JournalRepo
            JournalRepo.add_pending(
                client_order_id=client_order_id,
                exchange=exchange_name,
                account_id=account_id,
                symbol=signal.symbol,
                direction=signal.direction.value,
                stake=signal.stake,
                leverage=signal.leverage,
            )
        except Exception as e:
            logger.warning(f"Journal add_pending 失败: {e}")

        # 带重试的下单
        last_error = ''
        for attempt in range(cfg.max_retries):
            try:
                result = self._place_order(
                    signal, exchange_name, account_id, client_order_id
                )
                latency = (time.monotonic() - t0) * 1000
                result.latency_ms = round(latency, 1)
                result.client_order_id = client_order_id
                result.account_id = account_id

                if result.success:
                    # 确认 Journal
                    try:
                        from db.repositories import JournalRepo
                        JournalRepo.mark_confirmed(client_order_id)
                    except Exception:
                        pass
                    return result

                last_error = result.error

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"下单异常 (attempt {attempt+1}/{cfg.max_retries}): {e}"
                )

            # 退避重试
            if attempt < cfg.max_retries - 1:
                backoff = cfg.retry_backoff_sec * (2 ** attempt)
                time.sleep(backoff)

        # 全部重试失败
        try:
            from db.repositories import JournalRepo
            JournalRepo.mark_failed(client_order_id, last_error)
        except Exception:
            pass

        latency = (time.monotonic() - t0) * 1000
        return ExecutionResult(
            success=False,
            exchange=exchange_name,
            symbol=signal.symbol,
            direction=signal.direction.value,
            stake=signal.stake,
            error=last_error,
            error_code='MAX_RETRIES_EXCEEDED',
            client_order_id=client_order_id,
            account_id=account_id,
            latency_ms=round(latency, 1),
        )

    def execute_multi_account(
        self,
        signal: Signal,
        account_ids: List[str],
        exchange_name: str = 'binance',
    ) -> BatchExecutionResult:
        """
        多账户并发执行同一信号。

        参数:
          signal: 信号
          account_ids: 要执行的账户列表
          exchange_name: 交易所

        返回:
          BatchExecutionResult 包含每个账户的结果
        """
        cfg = self.config
        t0 = time.monotonic()
        batch = BatchExecutionResult(total=len(account_ids))

        futures = {}
        for acc_id in account_ids:
            future = self._pool.submit(
                self.execute_signal, signal, acc_id, exchange_name
            )
            futures[future] = acc_id

        # 等待所有完成（有总超时）
        for future in as_completed(futures, timeout=cfg.batch_timeout_sec):
            try:
                result = future.result(timeout=cfg.single_timeout_sec)
                batch.results.append(result)
                if result.success:
                    batch.success_count += 1
                else:
                    batch.failed_count += 1
            except TimeoutError:
                acc_id = futures[future]
                batch.results.append(ExecutionResult(
                    success=False,
                    exchange=exchange_name,
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    stake=signal.stake,
                    error='执行超时',
                    error_code='TIMEOUT',
                    account_id=acc_id,
                ))
                batch.failed_count += 1
            except Exception as e:
                acc_id = futures[future]
                batch.results.append(ExecutionResult(
                    success=False,
                    exchange=exchange_name,
                    symbol=signal.symbol,
                    error=str(e),
                    error_code='UNEXPECTED',
                    account_id=acc_id,
                ))
                batch.failed_count += 1

        batch.elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return batch

    def _place_order(
        self,
        signal: Signal,
        exchange_name: str,
        account_id: str,
        client_order_id: str,
    ) -> ExecutionResult:
        """实际下单（调用 live_executor）"""
        from live_executor import execute_open

        result = execute_open(
            symbol=signal.symbol,
            direction=signal.direction.value,
            stake=signal.stake,
            exchange_name=exchange_name,
            leverage=signal.leverage,
            client_order_id=client_order_id,
            account_id=account_id or None,
        )

        return ExecutionResult(
            success=result.get('success', False),
            exchange=exchange_name,
            symbol=signal.symbol,
            direction=signal.direction.value,
            order_id=result.get('order_id', ''),
            fill_price=result.get('price', 0),
            fill_amount=result.get('amount', 0),
            stake=signal.stake,
            error=result.get('error', ''),
            error_code=result.get('error_code', ''),
        )

    def shutdown(self):
        """关闭线程池"""
        self._pool.shutdown(wait=False)
