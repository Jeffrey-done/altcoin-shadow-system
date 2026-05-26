#!/usr/bin/env python3
"""
管理员密钥存储 v3.0 — 多账户安全凭证 + 每交易所独立账户配置

设计目标：
  1. API key / TOTP secret / 密码 hash 存在独立文件，权限 0600
  2. 和 .env 解耦：admin_panel 改了 API key 不需要改 .env 或重启进程
  3. live_executor 读凭证时优先找本文件，回退到 .env，方便迁移
  4. 所有写入都是原子 + 自动设权限，避免半写状态
  5. v2: 多账户管理 — 每个账户独立的 exchange 凭证，统一管理员登录
  6. v3: 每交易所独立账户 — 每个交易所拥有自己的资金池、杠杆、仓位、风控参数

文件结构（admin_secrets.json v3）：
{
  "_version": 3,
  "admin": {
    "password_hash": "pbkdf2_sha256$600000$<salt>$<hash>",
    "totp_secret": "<base32 string>",
    "totp_enabled": true,
    "created_at": "2026-05-13T12:34:56+00:00"
  },
  "accounts": {
    "acc_abc123": {
      "name": "主账户",
      "created_at": "2026-05-13T12:34:56+00:00",
      "exchanges": {
        "binance": {
          "api_key": "...",
          "secret": "...",
          "updated_at": "...",
          "settings": {
            "account_balance": 100,
            "leverage": 10,
            "default_stake": 30,
            "live_mode": false,
            "slippage_alert_pct": 1.0,
            "risk": { "max_daily_loss": 30, "max_daily_trades": 3, ... },
            "compound": { "enabled": true, "step": 50, ... },
            "tp_sl": { "tp1_multiplier": 0.95, ... }
          }
        },
        "okx": {
          "api_key": "...",
          "secret": "...",
          "passphrase": "...",
          "updated_at": "...",
          "settings": { ... }
        },
        "gate": {
          "api_key": "...",
          "secret": "...",
          "updated_at": "...",
          "settings": { ... }
        }
      }
    }
  },
  "active_account": "acc_abc123"
}

⚠️ 这个文件绝对不能进 git。.gitignore 已经加了。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import struct
import tempfile
import time as _time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote


# NF-5 修复（基于 L-1）：跨平台文件锁不再重复定义；统一从 common 导入
# common.fcntl 在 Windows 是 common._LockShim() 实例（含 NF-1 的 lseek(0) 修复），
# 在 Linux/Mac 是真正的 fcntl 模块。两边都暴露 flock / LOCK_EX / LOCK_UN，
# 从而 admin_secrets 全文 fcntl.flock(...) / fcntl.LOCK_EX 用法不变。
from common import fcntl  # noqa: F401  (共用 shim，避免 NF-1 类修复需要改两处)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(SCRIPT_DIR, 'admin_secrets.json')
_SECRETS_LOCK = SECRETS_FILE + '.lock'

logger = logging.getLogger("admin_secrets")

# PBKDF2 参数（OWASP 2023 推荐）
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_BYTES = 16


# ══════════════════════════════════════════════════════════════════
#  基础读写 + v1→v2→v3 迁移
# ══════════════════════════════════════════════════════════════════

# 支持的交易所列表
SUPPORTED_EXCHANGES = ('binance', 'okx', 'gate')


def _default_exchange_settings() -> dict:
    """返回每个交易所账户的默认 settings 结构"""
    return {
        'account_balance': 100,
        'leverage': 10,
        'default_stake': 30,
        'live_mode': False,
        'slippage_alert_pct': 1.0,
        'risk': {
            'max_daily_loss': 30,
            'max_daily_trades': 3,
            'consecutive_loss_pause': 3,
            'max_position_pct': 0.5,
            'cooldown_hours': 24,
        },
        'compound': {
            'enabled': True,
            'step': 50,
            'increase': 25,
            'max_stake': 300,
        },
        'tp_sl': {
            'tp1_multiplier': 0.95,
            'tp2_multiplier': 0.92,
            'tp1_close_ratio': 0.5,
            'hard_stop_loss_pct': 5.0,
        },
    }


def _empty_v3() -> dict:
    """返回空的 v3 骨架"""
    return {
        '_version': 3,
        'admin': {},
        'accounts': {},
        'exchange_accounts': {},
        'active_account': '',
    }


def _empty_v2() -> dict:
    """返回空的 v2 骨架（向后兼容）"""
    return _empty_v3()


def _migrate_v1_to_v2(data: dict) -> dict:
    """
    从 v1 单账户格式迁移到 v2 多账户格式。
    v1 的 exchanges 字段被移入第一个自动创建的账户。
    """
    v2 = _empty_v3()
    v2['admin'] = data.get('admin', {})

    # 从 v1 exchanges 创建默认账户
    old_exchanges = data.get('exchanges', {})
    if old_exchanges:
        account_id = _generate_account_id()
        v2['accounts'][account_id] = {
            'name': '主账户',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'exchanges': old_exchanges,
        }
        v2['active_account'] = account_id
    return v2


def _migrate_v2_to_v3(data: dict) -> dict:
    """
    从 v2 迁移到 v3：为每个交易所的凭证添加独立的 settings。
    v2 的交易所条目只有 api_key/secret/passphrase，v3 新增 settings 字段。
    """
    data['_version'] = 3
    for acc_id, acc in data.get('accounts', {}).items():
        exchanges = acc.get('exchanges', {})
        for exch_name, exch_data in exchanges.items():
            if 'settings' not in exch_data:
                exch_data['settings'] = _default_exchange_settings()
    return data


def _exchange_display_name(exchange: str) -> str:
    names = {'binance': 'Binance 账户', 'okx': 'OKX 账户', 'gate': 'Gate 账户'}
    return names.get(exchange.lower(), f"{exchange.upper()} 账户")


def _ensure_exchange_account(d: dict, exchange: str) -> dict:
    exchange = exchange.lower()
    accounts = d.setdefault('exchange_accounts', {})
    current = accounts.setdefault(exchange, {})
    current.setdefault('id', exchange)
    current.setdefault('name', _exchange_display_name(exchange))
    current.setdefault('created_at', datetime.now(timezone.utc).isoformat())
    current.setdefault('trading_enabled', True)
    current.setdefault('settings', _default_exchange_settings())
    return current


def _migrate_legacy_accounts_to_exchange_accounts(data: dict) -> dict:
    """把旧 accounts[*].exchanges 合并为 exchange_accounts[exchange]。"""
    data.setdefault('exchange_accounts', {})
    for _acc_id, acc in data.get('accounts', {}).items():
        if acc.get('system'):
            continue
        for exchange, exch_data in acc.get('exchanges', {}).items():
            exchange = exchange.lower()
            if exchange not in SUPPORTED_EXCHANGES:
                continue
            target = _ensure_exchange_account(data, exchange)
            for key in ('api_key', 'secret', 'passphrase', 'updated_at'):
                if exch_data.get(key) and not target.get(key):
                    target[key] = exch_data[key]
            if exch_data.get('settings'):
                target['settings'] = _deep_merge(target.get('settings', _default_exchange_settings()), exch_data['settings'])
    return data


SHADOW_ACCOUNT_ID = 'acc_shadow_system'  # 固定ID，影子账户不可删除


def _generate_account_id() -> str:
    """生成唯一账户 ID"""
    return 'acc_' + secrets.token_hex(6)


def ensure_shadow_account() -> str:
    """
    确保影子账户存在（系统内置，不可删除）。
    返回影子账户 ID。在系统首次启动或 admin 初始化时调用。
    """
    with _locked_secrets() as (d, save):
        if SHADOW_ACCOUNT_ID not in d.get('accounts', {}):
            d.setdefault('accounts', {})[SHADOW_ACCOUNT_ID] = {
                'name': '影子账户（系统）',
                'created_at': datetime.now(timezone.utc).isoformat(),
                'exchanges': {},
                'system': True,  # 标记为系统账户，不可删除
            }
            # 如果没有活跃账户，默认激活影子账户
            if not d.get('active_account'):
                d['active_account'] = SHADOW_ACCOUNT_ID
            save(d)
    return SHADOW_ACCOUNT_ID


@contextmanager
def _locked_secrets():
    """
    上下文管理器：对 secrets 文件加排他锁，确保 read-modify-write 原子性。
    防止 admin panel 并发请求导致的丢失写入。

    用法：
        with _locked_secrets() as (data, save):
            data['admin']['totp_enabled'] = True
            save(data)
    """
    lock_fd = open(_SECRETS_LOCK, 'a')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _load_raw()

        def save(new_data):
            _save_raw(new_data)

        yield data, save
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _load_raw() -> dict:
    """读整个 secrets 文件；不存在或损坏返回空 v3 骨架。自动迁移 v1→v2→v3。"""
    if not os.path.exists(SECRETS_FILE):
        return _empty_v3()
    try:
        with open(SECRETS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.error(f"admin_secrets.json 读取失败: {e}")
        return _empty_v3()

    version = data.get('_version', 1)

    if version < 2:
        # 自动迁移 v1 → v2 → v3
        logger.info("admin_secrets: 检测到 v1 格式，自动迁移到 v3（每交易所独立账户）")
        v2 = _migrate_v1_to_v2(data)
        v3 = _migrate_v2_to_v3(v2)
        _save_raw(v3)
        return v3

    if version < 3:
        # 自动迁移 v2 → v3
        logger.info("admin_secrets: 检测到 v2 格式，自动迁移到 v3（每交易所独立账户）")
        v3 = _migrate_v2_to_v3(data)
        _save_raw(v3)
        return v3

    # v3 格式，确保字段完整
    data.setdefault('_version', 3)
    data.setdefault('admin', {})
    data.setdefault('accounts', {})
    data.setdefault('exchange_accounts', {})
    data.setdefault('active_account', '')

    before = json.dumps(data.get('exchange_accounts', {}), sort_keys=True)
    _migrate_legacy_accounts_to_exchange_accounts(data)
    for _exchange in SUPPORTED_EXCHANGES:
        _ensure_exchange_account(data, _exchange)
    after = json.dumps(data.get('exchange_accounts', {}), sort_keys=True)

    # 确保影子账户始终存在
    if SHADOW_ACCOUNT_ID not in data['accounts']:
        data['accounts'][SHADOW_ACCOUNT_ID] = {
            'name': '影子账户（系统）',
            'created_at': '',
            'exchanges': {},
            'system': True,
        }
        if not data['active_account']:
            data['active_account'] = SHADOW_ACCOUNT_ID
        _save_raw(data)

    if before != after:
        _save_raw(data)

    return data


def _save_raw(data: dict) -> None:
    """原子写 + 0600 权限。"""
    dir_name = os.path.dirname(SECRETS_FILE)
    fd, tmp = tempfile.mkstemp(suffix='.tmp', dir=dir_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        # bind-mount 兼容：见 common._replace_or_inplace_overwrite
        from common import _replace_or_inplace_overwrite as _replace
        _replace(tmp, SECRETS_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def file_exists() -> bool:
    """admin 是否做过首次初始化"""
    return os.path.exists(SECRETS_FILE)


def is_initialized() -> bool:
    """是否已设置管理员密码（能不能登录）"""
    d = _load_raw()
    return bool(d.get('admin', {}).get('password_hash'))


# ══════════════════════════════════════════════════════════════════
#  多账户管理
# ══════════════════════════════════════════════════════════════════

def create_account(name: str) -> str:
    """
    创建新交易账户，返回 account_id。
    """
    if not name or not name.strip():
        raise ValueError("账户名称不能为空")
    name = name.strip()
    if len(name) > 50:
        raise ValueError("账户名称不能超过 50 个字符")

    with _locked_secrets() as (d, save):
        exchange = name.strip().lower()
        if exchange in SUPPORTED_EXCHANGES:
            acc = _ensure_exchange_account(d, exchange)
            acc['name'] = _exchange_display_name(exchange)
            save(d)
            return exchange
        account_id = _generate_account_id()
        d['accounts'][account_id] = {
            'name': name,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'exchanges': {},
        }
        if not d['active_account']:
            d['active_account'] = account_id
        save(d)
    return account_id


def delete_account(account_id: str) -> None:
    """
    删除交易账户。不能删除最后一个账户，也不能删除系统影子账户。
    """
    if account_id == SHADOW_ACCOUNT_ID:
        raise ValueError("影子账户（系统）不可删除")

    with _locked_secrets() as (d, save):
        if account_id not in d['accounts']:
            raise ValueError(f"账户 {account_id} 不存在")
        if len(d['accounts']) <= 1:
            raise ValueError("不能删除最后一个账户")

        del d['accounts'][account_id]

        # 如果删除的是活跃账户，切换到第一个
        if d['active_account'] == account_id:
            d['active_account'] = next(iter(d['accounts']))

        save(d)


def list_accounts() -> list:
    """
    返回所有账户列表:
    [{id, name, created_at, has_binance, has_okx, has_gate, trading_enabled, is_system}]
    """
    d = _load_raw()
    result = []
    for exchange in SUPPORTED_EXCHANGES:
        acc = _ensure_exchange_account(d, exchange)
        has_creds = bool(acc.get('api_key'))
        result.append({
            'id': exchange,
            'name': acc.get('name', ''),
            'created_at': acc.get('created_at', ''),
            'exchange': exchange,
            'has_credentials': has_creds,
            'has_binance': has_creds if exchange == 'binance' else False,
            'has_okx': has_creds if exchange == 'okx' else False,
            'has_gate': has_creds if exchange == 'gate' else False,
            'trading_enabled': acc.get('trading_enabled', True),
            'is_system': False,
        })
    if SHADOW_ACCOUNT_ID in d.get('accounts', {}):
        shadow = d['accounts'][SHADOW_ACCOUNT_ID]
        result.append({
            'id': SHADOW_ACCOUNT_ID,
            'name': shadow.get('name', '影子账户（系统）'),
            'created_at': shadow.get('created_at', ''),
            'exchange': 'shadow',
            'has_credentials': False,
            'has_binance': False,
            'has_okx': False,
            'has_gate': False,
            'trading_enabled': shadow.get('trading_enabled', True),
            'is_system': True,
        })
    return result


def is_account_trading_enabled(account_id: str) -> bool:
    """
    返回指定账户的交易开关状态。默认 True（未显式关闭的账户都参与同步开单）。
    """
    if not account_id:
        return True
    d = _load_raw()
    if account_id == SHADOW_ACCOUNT_ID:
        acc = d.get('accounts', {}).get(account_id)
        return bool(acc and acc.get('trading_enabled', True))
    if account_id not in SUPPORTED_EXCHANGES:
        return False
    acc = d.get('exchange_accounts', {}).get(account_id, {})
    return acc.get('trading_enabled', True)


def set_account_trading_enabled(account_id: str, enabled: bool) -> None:
    """
    切换账户的交易开关。关闭后信号触发时该账户不再参与同步开单，
    但已有持仓继续被 tracker / realtime_monitor 监控直至平仓。
    """
    with _locked_secrets() as (d, save):
        if account_id == SHADOW_ACCOUNT_ID:
            if account_id not in d.get('accounts', {}):
                raise ValueError(f"账户 {account_id} 不存在")
            d['accounts'][account_id]['trading_enabled'] = bool(enabled)
            save(d)
            return
        if account_id not in SUPPORTED_EXCHANGES:
            raise ValueError(f"账户 {account_id} 不存在")
        _ensure_exchange_account(d, account_id)['trading_enabled'] = bool(enabled)
        save(d)


def get_active_account_id() -> str:
    """返回默认交易账号 ID。交易配置以交易所名作为账号 ID。"""
    d = _load_raw()
    active = d.get('active_account', '')
    if active in SUPPORTED_EXCHANGES:
        return active
    for exchange in SUPPORTED_EXCHANGES:
        acc = d.get('exchange_accounts', {}).get(exchange, {})
        if acc.get('trading_enabled', True):
            return exchange
    return 'binance'


def set_active_account(account_id: str) -> None:
    """切换活跃账户"""
    with _locked_secrets() as (d, save):
        if account_id not in SUPPORTED_EXCHANGES and account_id != SHADOW_ACCOUNT_ID:
            raise ValueError(f"账户 {account_id} 不存在")
        d['active_account'] = account_id
        save(d)


def rename_account(account_id: str, new_name: str) -> None:
    """重命名账户"""
    if not new_name or not new_name.strip():
        raise ValueError("账户名称不能为空")
    new_name = new_name.strip()
    if len(new_name) > 50:
        raise ValueError("账户名称不能超过 50 个字符")

    with _locked_secrets() as (d, save):
        if account_id not in SUPPORTED_EXCHANGES:
            raise ValueError(f"账户 {account_id} 不存在")
        _ensure_exchange_account(d, account_id)['name'] = new_name
        save(d)


# ══════════════════════════════════════════════════════════════════
#  管理员密码 (PBKDF2-SHA256)
# ══════════════════════════════════════════════════════════════════

def _hash_password(password: str, salt: bytes = None) -> str:
    """
    PBKDF2-SHA256，编码为 $pbkdf2_sha256$<iter>$<b64_salt>$<b64_hash>
    """
    if salt is None:
        salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt,
        PBKDF2_ITERATIONS,
    )
    return "$pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str) -> bool:
    """
    验证输入密码是否正确。使用 hmac.compare_digest 常量时间比较。
    """
    stored = _load_raw().get('admin', {}).get('password_hash', '')
    if not stored or not stored.startswith('$pbkdf2_sha256$'):
        _hash_password(password, salt=b'dummy' + b'\x00' * 11)
        return False

    try:
        _, algo, iters, b64_salt, b64_hash = stored.split('$')
        iters = int(iters)
        salt = base64.b64decode(b64_salt)
        expected = base64.b64decode(b64_hash)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iters)
        return hmac.compare_digest(dk, expected)
    except Exception as e:
        logger.warning(f"密码哈希格式错误: {e}")
        return False


def set_password(password: str) -> None:
    """设置/更改管理员密码。"""
    if len(password) < 12:
        raise ValueError("密码至少 12 个字符")
    if password.lower() in ('password', 'admin', '123456789012'):
        raise ValueError("密码太弱，换一个")

    with _locked_secrets() as (d, save):
        d['admin']['password_hash'] = _hash_password(password)
        d['admin'].setdefault('created_at', datetime.now(timezone.utc).isoformat())
        save(d)


# ══════════════════════════════════════════════════════════════════
#  TOTP (Google Authenticator 兼容)
# ══════════════════════════════════════════════════════════════════

def generate_totp_secret() -> str:
    """生成新的 TOTP secret（base32，Google Authenticator 兼容）"""
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode('ascii').rstrip('=')


def set_totp_secret(secret_b32: str, issuer: str = "altcoin-shadow-admin",
                    account: str = "admin") -> str:
    """
    保存 TOTP secret 并返回 otpauth:// URL。
    """
    with _locked_secrets() as (d, save):
        d['admin']['totp_secret'] = secret_b32
        d['admin']['totp_enabled'] = False
        save(d)

    return "otpauth://totp/{}:{}?secret={}&issuer={}&digits=6&period=30".format(
        quote(issuer),
        quote(account),
        secret_b32,
        quote(issuer),
    )


def enable_totp() -> None:
    """首次验证通过后调用，正式启用 2FA"""
    with _locked_secrets() as (d, save):
        if not d.get('admin', {}).get('totp_secret'):
            raise ValueError("TOTP secret 未设置，无法启用")
        d['admin']['totp_enabled'] = True
        save(d)


def get_totp_secret() -> Optional[str]:
    """返回已保存的 TOTP secret；没设置返回 None"""
    return _load_raw().get('admin', {}).get('totp_secret') or None


def is_totp_enabled() -> bool:
    """TOTP 是否已启用（绑定并验证通过）"""
    a = _load_raw().get('admin', {})
    return bool(a.get('totp_secret')) and bool(a.get('totp_enabled'))


def verify_totp(code: str, secret_b32: Optional[str] = None,
                window: int = 1) -> bool:
    """
    验证 6 位 TOTP 码。
    window=1 表示接受当前时间片±1（±30s）。
    """
    if secret_b32 is None:
        secret_b32 = get_totp_secret()
    if not secret_b32:
        return False

    code = code.strip().replace(' ', '')
    if len(code) != 6 or not code.isdigit():
        return False

    try:
        pad = '=' * (-len(secret_b32) % 8)
        key = base64.b32decode(secret_b32.upper() + pad)
    except Exception:
        return False

    t = int(_time.time()) // 30

    for offset in range(-window, window + 1):
        if _totp_at(key, t + offset) == code:
            return True
    return False


def _totp_at(key: bytes, counter: int) -> str:
    """RFC 6238 TOTP 实现（SHA1, 6 位）"""
    msg = struct.pack('>Q', counter)
    h = hmac.HMAC(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code_int = ((h[offset] & 0x7F) << 24
                | (h[offset + 1] & 0xFF) << 16
                | (h[offset + 2] & 0xFF) << 8
                | (h[offset + 3] & 0xFF))
    return str(code_int % 1_000_000).zfill(6)


# ══════════════════════════════════════════════════════════════════
#  交易所 API 凭证（多账户版）
# ══════════════════════════════════════════════════════════════════

def _resolve_account_id(account_id: Optional[str] = None) -> str:
    """解析账户 ID，None 表示使用活跃账户"""
    if account_id is None:
        account_id = get_active_account_id()
    if not account_id:
        raise ValueError("没有可用的交易账户，请先创建一个")
    return account_id


def get_exchange_credentials(exchange: str, account_id: Optional[str] = None) -> dict:
    """
    返回指定交易所的凭证字典（可能为空）。
    先查 admin_secrets.json 的对应账户；没有就回退到 os.environ（兼容旧部署）。
    """
    exchange = exchange.lower()

    d = _load_raw()
    exch_data = d.get('exchange_accounts', {}).get(exchange, {})

    if exchange == 'binance':
        return {
            'api_key': exch_data.get('api_key') or os.environ.get('BINANCE_API_KEY', ''),
            'secret': exch_data.get('secret') or os.environ.get('BINANCE_SECRET', ''),
        }
    if exchange == 'okx':
        return {
            'api_key': exch_data.get('api_key') or os.environ.get('OKX_API_KEY', ''),
            'secret': exch_data.get('secret') or os.environ.get('OKX_SECRET', ''),
            'passphrase': exch_data.get('passphrase') or os.environ.get('OKX_PASSPHRASE', ''),
        }
    if exchange == 'gate':
        return {
            'api_key': exch_data.get('api_key') or os.environ.get('GATE_API_KEY', ''),
            'secret': exch_data.get('secret') or os.environ.get('GATE_SECRET', ''),
        }
    return {}


def set_exchange_credentials(exchange: str, account_id: Optional[str] = None, **kwargs) -> None:
    """
    更新交易所凭证。只更新传入的字段；传空字符串等于不改。
    """
    exchange = exchange.lower()
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"不支持的交易所: {exchange}（支持: {SUPPORTED_EXCHANGES}）")

    with _locked_secrets() as (d, save):
        current = _ensure_exchange_account(d, exchange)

        for k, v in kwargs.items():
            if k == 'settings':
                # settings 用 update 合并，不是整体覆盖
                current.setdefault('settings', _default_exchange_settings()).update(v)
            elif v:
                current[k] = v
        current['updated_at'] = datetime.now(timezone.utc).isoformat()

        # 确保 settings 字段存在
        if 'settings' not in current:
            current['settings'] = _default_exchange_settings()
        save(d)

    # H-3 修复：凭证更新后清除对应的认证实例缓存，
    # 强制下次调用时使用新凭证创建新实例
    try:
        from exchange_manager import invalidate_authenticated_cache
        invalidate_authenticated_cache(exchange=exchange)
    except Exception:
        pass  # exchange_manager 未加载时（测试环境）不阻塞
    try:
        from live_executor import invalidate_live_exchange_cache
        invalidate_live_exchange_cache(exchange_name=exchange)
    except Exception:
        pass


def clear_exchange_credentials(exchange: str, account_id: Optional[str] = None) -> None:
    """清空某交易所的所有凭证"""
    exchange = exchange.lower()

    with _locked_secrets() as (d, save):
        if exchange not in d.get('exchange_accounts', {}):
            return
        current = _ensure_exchange_account(d, exchange)
        for key in ('api_key', 'secret', 'passphrase'):
            current.pop(key, None)
        current['updated_at'] = datetime.now(timezone.utc).isoformat()
        save(d)

    # H-3 修复：凭证清除后也需要清除认证实例缓存
    try:
        from exchange_manager import invalidate_authenticated_cache
        invalidate_authenticated_cache(exchange=exchange)
    except Exception:
        pass
    try:
        from live_executor import invalidate_live_exchange_cache
        invalidate_live_exchange_cache(exchange_name=exchange)
    except Exception:
        pass


def get_all_trading_accounts() -> list:
    """
    返回所有配置了凭证且交易开关打开的交易所账户。
    排除:
      - 系统影子账户（它不持有真实凭证，由 scanner 单独注入）
      - 未配置凭证的空账户
      - 交易开关被显式关闭（trading_enabled=False）的账户

    返回:
        [{'id': 'binance', 'name': 'Binance 账户', 'exchange': 'binance', 'exchanges': {'binance': {...}}}]
    """
    d = _load_raw()
    result = []
    for exchange in SUPPORTED_EXCHANGES:
        acc = _ensure_exchange_account(d, exchange)
        if not acc.get('trading_enabled', True):
            continue
        if acc.get('api_key'):
            result.append({
                'id': exchange,
                'name': acc.get('name', ''),
                'exchange': exchange,
                'exchanges': {exchange: acc},
            })
    return result


def get_account_exchange_credentials(exchange: str, account_id: str) -> dict:
    """
    获取指定账户的交易所凭证（不回退到环境变量）。
    用于多账户并行下单场景，每个账户使用自己独立的凭证。
    """
    exchange = exchange.lower()
    d = _load_raw()
    exch_data = d.get('exchange_accounts', {}).get(exchange, {})

    if exchange == 'binance':
        return {
            'api_key': exch_data.get('api_key', ''),
            'secret': exch_data.get('secret', ''),
        }
    if exchange == 'okx':
        return {
            'api_key': exch_data.get('api_key', ''),
            'secret': exch_data.get('secret', ''),
            'passphrase': exch_data.get('passphrase', ''),
        }
    if exchange == 'gate':
        return {
            'api_key': exch_data.get('api_key', ''),
            'secret': exch_data.get('secret', ''),
        }
    return {}


def mask_credentials(exchange: str, account_id: Optional[str] = None) -> dict:
    """
    返回脱敏后的凭证（供 admin panel 显示）。
    只显示前 6 位 + 星号 + 后 4 位。
    """
    creds = get_exchange_credentials(exchange, account_id=account_id)
    out = {}
    for k, v in creds.items():
        if not v:
            out[k] = ''
        elif len(v) <= 10:
            out[k] = '*' * len(v)
        else:
            out[k] = f"{v[:6]}{'*' * 8}{v[-4:]}"
    return out



# ══════════════════════════════════════════════════════════════════
#  每交易所独立账户设置 (v3.0)
# ══════════════════════════════════════════════════════════════════

def get_exchange_settings(exchange: str, account_id: Optional[str] = None) -> dict:
    """
    获取指定交易所的独立账户设置。

    返回该交易所在指定账户下的完整 settings 字典。
    如果没有自定义设置，返回默认值。

    Args:
        exchange: 交易所名称 ('binance', 'okx', 'gate')
        account_id: 账户 ID，None 表示活跃账户

    Returns:
        settings 字典，包含 account_balance, leverage, default_stake, risk, compound, tp_sl 等

    用法:
        settings = get_exchange_settings('binance')
        leverage = settings['leverage']           # 10
        max_loss = settings['risk']['max_daily_loss']  # 30
    """
    exchange = exchange.lower()

    d = _load_raw()
    exch_data = d.get('exchange_accounts', {}).get(exchange, {})
    settings = exch_data.get('settings', {})

    # 合并默认值（确保所有字段都存在）
    defaults = _default_exchange_settings()
    merged = _deep_merge(defaults, settings)
    return merged


def set_exchange_settings(exchange: str, settings: dict,
                          account_id: Optional[str] = None) -> None:
    """
    更新指定交易所的独立账户设置。
    使用深度合并，只更新传入的字段。

    Args:
        exchange: 交易所名称 ('binance', 'okx', 'gate')
        settings: 要更新的设置字典（部分更新即可）
        account_id: 账户 ID，None 表示活跃账户

    用法:
        # 只改 OKX 的杠杆和风控
        set_exchange_settings('okx', {
            'leverage': 5,
            'risk': {'max_daily_loss': 50}
        })
    """
    exchange = exchange.lower()
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"不支持的交易所: {exchange}（支持: {SUPPORTED_EXCHANGES}）")

    with _locked_secrets() as (d, save):
        exch_data = _ensure_exchange_account(d, exchange)
        current_settings = exch_data.get('settings', _default_exchange_settings())

        # 深度合并
        merged = _deep_merge(current_settings, settings)
        exch_data['settings'] = merged
        exch_data['updated_at'] = datetime.now(timezone.utc).isoformat()
        save(d)


def get_exchange_setting(exchange: str, key: str, account_id: Optional[str] = None,
                         default=None):
    """
    获取指定交易所的单个设置值。支持点号分隔的嵌套路径。

    Args:
        exchange: 交易所名称
        key: 设置路径 ('leverage', 'risk.max_daily_loss', 'compound.step')
        account_id: 账户 ID
        default: 未找到时的默认值

    Returns:
        设置值

    用法:
        get_exchange_setting('okx', 'leverage')            → 10
        get_exchange_setting('binance', 'risk.max_daily_loss')  → 30
    """
    settings = get_exchange_settings(exchange, account_id)
    keys = key.split('.')
    current = settings
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return default
        if current is None:
            return default
    return current


def is_exchange_live_mode(exchange: str, account_id: Optional[str] = None) -> bool:
    """
    检查指定交易所在指定账户下是否开启了实盘模式。
    """
    settings = get_exchange_settings(exchange, account_id)
    return bool(settings.get('live_mode', False))


def get_live_exchanges(account_id: Optional[str] = None) -> list:
    """
    返回指定账户下所有开启了实盘模式的交易所名称列表。

    Args:
        account_id: 账户 ID，None 表示活跃账户

    Returns:
        ['binance', 'okx'] — 所有 live_mode=True 的交易所
    """
    d = _load_raw()
    result = []
    for exch_name, exch_data in d.get('exchange_accounts', {}).items():
        settings = exch_data.get('settings', {})
        if exch_data.get('trading_enabled', True) and settings.get('live_mode', False):
            result.append(exch_name)
    return result


def get_all_exchange_settings(account_id: Optional[str] = None) -> dict:
    """
    获取指定账户下所有交易所的完整设置。

    Returns:
        {
            'binance': { 'account_balance': 100, 'leverage': 10, ... },
            'okx': { 'account_balance': 200, 'leverage': 5, ... },
            'gate': { ... }
        }
    """
    result = {}
    for exch_name in SUPPORTED_EXCHANGES:
        result[exch_name] = get_exchange_settings(exch_name, account_id)
    return result


def _deep_merge(base: dict, override: dict) -> dict:
    """
    深度合并两个字典。override 中的值覆盖 base。
    对于嵌套字典，递归合并而非整体替换。
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
