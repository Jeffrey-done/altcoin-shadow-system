#!/usr/bin/env python3
"""
管理员密钥存储 v2.0 — 多账户安全凭证存储

设计目标：
  1. API key / TOTP secret / 密码 hash 存在独立文件，权限 0600
  2. 和 .env 解耦：admin_panel 改了 API key 不需要改 .env 或重启进程
  3. live_executor 读凭证时优先找本文件，回退到 .env，方便迁移
  4. 所有写入都是原子 + 自动设权限，避免半写状态
  5. v2: 多账户管理 — 每个账户独立的 exchange 凭证，统一管理员登录

文件结构（admin_secrets.json v2）：
{
  "_version": 2,
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
        "binance": { "api_key": "...", "secret": "...", "updated_at": "..." },
        "okx": { "api_key": "...", "secret": "...", "passphrase": "...", "updated_at": "..." }
      }
    }
  },
  "active_account": "acc_abc123"
}

⚠️ 这个文件绝对不能进 git。.gitignore 已经加了。
"""

import base64
import fcntl
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(SCRIPT_DIR, 'admin_secrets.json')
_SECRETS_LOCK = SECRETS_FILE + '.lock'

logger = logging.getLogger("admin_secrets")

# PBKDF2 参数（OWASP 2023 推荐）
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_BYTES = 16


# ══════════════════════════════════════════════════════════════════
#  基础读写 + v1→v2 迁移
# ══════════════════════════════════════════════════════════════════

def _empty_v2() -> dict:
    """返回空的 v2 骨架"""
    return {
        '_version': 2,
        'admin': {},
        'accounts': {},
        'active_account': '',
    }


def _migrate_v1_to_v2(data: dict) -> dict:
    """
    从 v1 单账户格式迁移到 v2 多账户格式。
    v1 的 exchanges 字段被移入第一个自动创建的账户。
    """
    v2 = _empty_v2()
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
    """读整个 secrets 文件；不存在或损坏返回空 v2 骨架。自动迁移 v1→v2。"""
    if not os.path.exists(SECRETS_FILE):
        return _empty_v2()
    try:
        with open(SECRETS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.error(f"admin_secrets.json 读取失败: {e}")
        return _empty_v2()

    version = data.get('_version', 1)

    if version < 2:
        # 自动迁移 v1 → v2
        logger.info("admin_secrets: 检测到 v1 格式，自动迁移到 v2（多账户）")
        v2 = _migrate_v1_to_v2(data)
        _save_raw(v2)
        return v2

    # v2 格式，确保字段完整
    data.setdefault('_version', 2)
    data.setdefault('admin', {})
    data.setdefault('accounts', {})
    data.setdefault('active_account', '')

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

    return data


def _save_raw(data: dict) -> None:
    """原子写 + 0600 权限。"""
    dir_name = os.path.dirname(SECRETS_FILE)
    fd, tmp = tempfile.mkstemp(suffix='.tmp', dir=dir_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, SECRETS_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_raw() -> dict:
    """读整个 secrets 文件；不存在或损坏返回空 v2 骨架。自动迁移 v1→v2。"""
    if not os.path.exists(SECRETS_FILE):
        return _empty_v2()
    try:
        with open(SECRETS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.error(f"admin_secrets.json 读取失败: {e}")
        return _empty_v2()

    version = data.get('_version', 1)

    if version < 2:
        # 自动迁移 v1 → v2
        logger.info("admin_secrets: 检测到 v1 格式，自动迁移到 v2（多账户）")
        v2 = _migrate_v1_to_v2(data)
        _save_raw(v2)
        return v2

    # v2 格式，确保字段完整
    data.setdefault('_version', 2)
    data.setdefault('admin', {})
    data.setdefault('accounts', {})
    data.setdefault('active_account', '')

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

    return data


def _save_raw(data: dict) -> None:
    """原子写 + 0600 权限。"""
    tmp = SECRETS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, SECRETS_FILE)


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
        account_id = _generate_account_id()
        d['accounts'][account_id] = {
            'name': name,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'exchanges': {},
        }
        # 如果是第一个账户，自动设为活跃
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
    [{id, name, created_at, has_binance, has_okx}]
    """
    d = _load_raw()
    result = []
    for acc_id, acc in d.get('accounts', {}).items():
        exchanges = acc.get('exchanges', {})
        result.append({
            'id': acc_id,
            'name': acc.get('name', ''),
            'created_at': acc.get('created_at', ''),
            'has_binance': bool(exchanges.get('binance', {}).get('api_key')),
            'has_okx': bool(exchanges.get('okx', {}).get('api_key')),
        })
    return result


def get_active_account_id() -> str:
    """返回当前活跃账户 ID。如果没有则返回空字符串。"""
    d = _load_raw()
    active = d.get('active_account', '')
    # 验证活跃账户确实存在
    if active and active in d.get('accounts', {}):
        return active
    # 如果活跃账户不存在，选第一个
    accounts = d.get('accounts', {})
    if accounts:
        first_id = next(iter(accounts))
        with _locked_secrets() as (d2, save):
            d2['active_account'] = first_id
            save(d2)
        return first_id
    return ''


def set_active_account(account_id: str) -> None:
    """切换活跃账户"""
    with _locked_secrets() as (d, save):
        if account_id not in d['accounts']:
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
        if account_id not in d['accounts']:
            raise ValueError(f"账户 {account_id} 不存在")
        d['accounts'][account_id]['name'] = new_name
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
    acc_id = _resolve_account_id(account_id)

    d = _load_raw()
    acc = d.get('accounts', {}).get(acc_id, {})
    exch_data = acc.get('exchanges', {}).get(exchange, {})

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
    return {}


def set_exchange_credentials(exchange: str, account_id: Optional[str] = None, **kwargs) -> None:
    """
    更新交易所凭证。只更新传入的字段；传空字符串等于不改。
    """
    exchange = exchange.lower()
    if exchange not in ('binance', 'okx'):
        raise ValueError(f"不支持的交易所: {exchange}")

    acc_id = _resolve_account_id(account_id)

    with _locked_secrets() as (d, save):
        if acc_id not in d['accounts']:
            raise ValueError(f"账户 {acc_id} 不存在")

        exchanges = d['accounts'][acc_id].setdefault('exchanges', {})
        current = exchanges.get(exchange, {})

        for k, v in kwargs.items():
            if v:
                current[k] = v
        current['updated_at'] = datetime.now(timezone.utc).isoformat()

        exchanges[exchange] = current
        d['accounts'][acc_id]['exchanges'] = exchanges
        save(d)


def clear_exchange_credentials(exchange: str, account_id: Optional[str] = None) -> None:
    """清空某交易所的所有凭证"""
    exchange = exchange.lower()
    acc_id = _resolve_account_id(account_id)

    with _locked_secrets() as (d, save):
        if acc_id not in d['accounts']:
            return
        exchanges = d['accounts'][acc_id].get('exchanges', {})
        if exchange in exchanges:
            del exchanges[exchange]
            d['accounts'][acc_id]['exchanges'] = exchanges
            save(d)


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
