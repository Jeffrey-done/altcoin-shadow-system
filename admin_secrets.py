#!/usr/bin/env python3
"""
管理员密钥存储 v1.0 — 独立于 .env 的安全凭证存储

设计目标：
  1. API key / TOTP secret / 密码 hash 存在独立文件，权限 0600
  2. 和 .env 解耦：admin_panel 改了 API key 不需要改 .env 或重启进程
  3. live_executor 读凭证时优先找本文件，回退到 .env，方便迁移
  4. 所有写入都是原子 + 自动设权限，避免半写状态

文件结构（admin_secrets.json）：
{
  "_version": 1,
  "admin": {
    "password_hash": "pbkdf2_sha256$600000$<salt>$<hash>",
    "totp_secret": "<base32 string>",
    "totp_enabled": true,
    "created_at": "2026-05-13T12:34:56+00:00"
  },
  "exchanges": {
    "binance": {
      "api_key": "...",
      "secret": "...",
      "updated_at": "..."
    },
    "okx": {
      "api_key": "...",
      "secret": "...",
      "passphrase": "...",
      "updated_at": "..."
    }
  }
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
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(SCRIPT_DIR, 'admin_secrets.json')

logger = logging.getLogger("admin_secrets")

# PBKDF2 参数（OWASP 2023 推荐）
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_BYTES = 16


# ══════════════════════════════════════════════════════════════════
#  基础读写
# ══════════════════════════════════════════════════════════════════

def _load_raw() -> dict:
    """读整个 secrets 文件；不存在或损坏返回空字典骨架。"""
    if not os.path.exists(SECRETS_FILE):
        return {'_version': 1, 'admin': {}, 'exchanges': {}}
    try:
        with open(SECRETS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data.setdefault('_version', 1)
        data.setdefault('admin', {})
        data.setdefault('exchanges', {})
        return data
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.error(f"admin_secrets.json 读取失败: {e}")
        return {'_version': 1, 'admin': {}, 'exchanges': {}}


def _save_raw(data: dict) -> None:
    """原子写 + 0600 权限。"""
    tmp = SECRETS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    # 必须先 chmod，再 replace，免得短暂出现 0644 的文件
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
#  管理员密码 (PBKDF2-SHA256)
# ══════════════════════════════════════════════════════════════════

def _hash_password(password: str, salt: bytes = None) -> str:
    """
    PBKDF2-SHA256，编码为 $pbkdf2_sha256$<iter>$<b64_salt>$<b64_hash>
    Django 风格，方便识别算法。
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
    未初始化时返回 False（避免泄露"用户不存在"信号）。
    """
    stored = _load_raw().get('admin', {}).get('password_hash', '')
    if not stored or not stored.startswith('$pbkdf2_sha256$'):
        # 故意执行一次假校验，避免通过响应时间判断用户是否存在
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

    d = _load_raw()
    d['admin']['password_hash'] = _hash_password(password)
    from datetime import datetime, timezone
    d['admin'].setdefault('created_at', datetime.now(timezone.utc).isoformat())
    _save_raw(d)


# ══════════════════════════════════════════════════════════════════
#  TOTP (Google Authenticator 兼容)
# ══════════════════════════════════════════════════════════════════

def generate_totp_secret() -> str:
    """生成新的 TOTP secret（base32，Google Authenticator 兼容）"""
    # 20 bytes = 160 bits = RFC 4226 推荐长度
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode('ascii').rstrip('=')


def set_totp_secret(secret_b32: str, issuer: str = "altcoin-shadow-admin",
                    account: str = "admin") -> str:
    """
    保存 TOTP secret 并返回 otpauth:// URL（给前端生成二维码用）。
    注意：保存后 totp_enabled 先设为 False，需要通过一次 verify 才能启用。
    """
    d = _load_raw()
    d['admin']['totp_secret'] = secret_b32
    d['admin']['totp_enabled'] = False  # 未验证先不启用
    _save_raw(d)

    # 返回标准 otpauth URL
    from urllib.parse import quote
    return "otpauth://totp/{}:{}?secret={}&issuer={}&digits=6&period=30".format(
        quote(issuer),
        quote(account),
        secret_b32,
        quote(issuer),
    )


def enable_totp() -> None:
    """首次验证通过后调用，正式启用 2FA"""
    d = _load_raw()
    if not d.get('admin', {}).get('totp_secret'):
        raise ValueError("TOTP secret 未设置，无法启用")
    d['admin']['totp_enabled'] = True
    _save_raw(d)


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
    window=1 表示接受当前时间片±1（±30s），容忍轻微时钟漂移。

    secret_b32 可显式传入（用于 setup 阶段"先验证再保存"），
    None 则从存储里读。
    """
    if secret_b32 is None:
        secret_b32 = get_totp_secret()
    if not secret_b32:
        return False

    code = code.strip().replace(' ', '')
    if len(code) != 6 or not code.isdigit():
        return False

    try:
        # base32 需要补 padding
        pad = '=' * (-len(secret_b32) % 8)
        key = base64.b32decode(secret_b32.upper() + pad)
    except Exception:
        return False

    import time
    t = int(time.time()) // 30

    for offset in range(-window, window + 1):
        if _totp_at(key, t + offset) == code:
            return True
    return False


def _totp_at(key: bytes, counter: int) -> str:
    """RFC 6238 TOTP 实现（SHA1, 6 位）"""
    import struct
    msg = struct.pack('>Q', counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code_int = ((h[offset] & 0x7F) << 24
                | (h[offset + 1] & 0xFF) << 16
                | (h[offset + 2] & 0xFF) << 8
                | (h[offset + 3] & 0xFF))
    return str(code_int % 1_000_000).zfill(6)


# ══════════════════════════════════════════════════════════════════
#  交易所 API 凭证
# ══════════════════════════════════════════════════════════════════

def get_exchange_credentials(exchange: str) -> dict:
    """
    返回指定交易所的凭证字典（可能为空）。
    先查 admin_secrets.json；没有就回退到 os.environ（兼容旧部署）。
    """
    exchange = exchange.lower()
    d = _load_raw().get('exchanges', {}).get(exchange, {})

    if exchange == 'binance':
        return {
            'api_key': d.get('api_key') or os.environ.get('BINANCE_API_KEY', ''),
            'secret': d.get('secret') or os.environ.get('BINANCE_SECRET', ''),
        }
    if exchange == 'okx':
        return {
            'api_key': d.get('api_key') or os.environ.get('OKX_API_KEY', ''),
            'secret': d.get('secret') or os.environ.get('OKX_SECRET', ''),
            'passphrase': d.get('passphrase') or os.environ.get('OKX_PASSPHRASE', ''),
        }
    return {}


def set_exchange_credentials(exchange: str, **kwargs) -> None:
    """
    更新交易所凭证。只更新传入的字段；传空字符串等于不改。

    用法：
      set_exchange_credentials('binance', api_key='...', secret='...')
      set_exchange_credentials('okx', api_key='...', secret='...', passphrase='...')
    """
    exchange = exchange.lower()
    if exchange not in ('binance', 'okx'):
        raise ValueError(f"不支持的交易所: {exchange}")

    d = _load_raw()
    current = d['exchanges'].get(exchange, {})

    from datetime import datetime, timezone
    for k, v in kwargs.items():
        if v:  # 空字符串不更新（允许只改部分字段）
            current[k] = v
    current['updated_at'] = datetime.now(timezone.utc).isoformat()

    d['exchanges'][exchange] = current
    _save_raw(d)


def clear_exchange_credentials(exchange: str) -> None:
    """清空某交易所的所有凭证（紧急止血用，比如 key 泄露）"""
    exchange = exchange.lower()
    d = _load_raw()
    if exchange in d.get('exchanges', {}):
        del d['exchanges'][exchange]
        _save_raw(d)


def mask_credentials(exchange: str) -> dict:
    """
    返回脱敏后的凭证（供 admin panel 显示）。
    只显示前 6 位 + 星号 + 后 4 位。
    """
    creds = get_exchange_credentials(exchange)
    out = {}
    for k, v in creds.items():
        if not v:
            out[k] = ''
        elif len(v) <= 10:
            out[k] = '*' * len(v)
        else:
            out[k] = f"{v[:6]}{'*' * 8}{v[-4:]}"
    return out
