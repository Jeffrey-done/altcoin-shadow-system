"""
Pre-Pump Sniffer 策略 — 妖币起飞前嗅探器
在价格还在横盘时，通过异常资金流/OI/成交量检测主力吸筹，
等待突破确认后做多。

注册方式: registry.auto_discover() 自动发现
"""

from strategies.prepump_sniffer.strategy import PrePumpSnifferStrategy

strategy_class = PrePumpSnifferStrategy
