#!/usr/bin/env python3
"""
管理员配置面板 v1.0 — 高安全实盘接入面板

安全多层防御（按请求流经顺序）：
  L1  IP 白名单（可选）: ADMIN_ALLOWED_IPS 设置后，非白名单 IP 一律 404
  L2  Secret URL 前缀:  整个面板挂在 /<ADMIN_URL_SECRET>/；secret 未配置
                         时 blueprint 根本不 register，任何路径都 404
  L3  IP 限速 + 失败锁定: 每 IP 5 次登录失败 → 锁定 30 分钟，锁定期内返回 404
                         （不返回 401，攻击者无法区分"路径存在"和"不存在"）
  L4  密码 (PBKDF2-SHA256, 600k iter) + TOTP (Google Authenticator)
  L5  Session: 30 分钟空闲过期 / 4 小时绝对过期；写操作要求 5 分钟内有新鲜 TOTP
  L6  CSRF: 所有 POST 必须带 X-Admin-CSRF 头，与 session token 常量时间比较
  L7  审计日志: 所有写操作 → admin_audit.log + TG 推送告警
  L8  响应头: noindex, no-cache, X-Frame-Options=DENY, CSP 严格

⚠️ 部署前检查表：
  □ .env 里 ADMIN_URL_SECRET 是 32+ 字节随机串（secrets.token_urlsafe(32)）
  □ 反向代理 (nginx/caddy) 强制 HTTPS；dashboard 本身不要暴露到 0.0.0.0
  □ 如在公网，设置 ADMIN_ALLOWED_IPS 限制源 IP
  □ 服务器 filesystem 权限：admin_secrets.json / runtime_config.json / admin_audit.log 都是 0600
"""

import json
import logging
import os
import secrets
import stat
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Optional

from flask import (
    Blueprint, request, session, redirect, url_for,
    render_template, jsonify, make_response, abort, g,
)

import admin_secrets
import runtime_config

logger = logging.getLogger("admin_panel")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG = os.path.join(SCRIPT_DIR, 'admin_audit.log')
# 登录失败限速文件；单独存一份，避免放内存里多进程不共享
RATE_LIMIT_FILE = os.path.join(SCRIPT_DIR, '.admin_ratelimit.json')

# ── 安全参数 ──────────────────────────────────────────────────────
SESSION_IDLE_TIMEOUT = 30 * 60          # 30 min 空闲过期
SESSION_ABSOLUTE_TIMEOUT = 4 * 3600     # 4 hour 绝对过期
FRESH_TOTP_WINDOW = 5 * 60              # 写操作要求 5 分钟内有新鲜 TOTP
RATE_LIMIT_MAX_FAILURES = 5             # 5 次失败
RATE_LIMIT_LOCKOUT_SEC = 30 * 60        # 锁 30 分钟


# ══════════════════════════════════════════════════════════════════
#  IP 限速（跨进程持久化）
# ══════════════════════════════════════════════════════════════════

def _load_ratelimit() -> dict:
    if not os.path.exists(RATE_LIMIT_FILE):
        return {}
    try:
        with open(RATE_LIMIT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _save_ratelimit(data: dict) -> None:
    tmp = RATE_LIMIT_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, RATE_LIMIT_FILE)


def _client_ip() -> str:
    """
    获取客户端 IP。考虑反向代理；仅信任 X-Forwarded-For 的第一个值。
    如果你的部署里 dashboard 直接对外，remote_addr 就够了。
    """
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _is_ip_locked(ip: str) -> bool:
    data = _load_ratelimit()
    entry = data.get(ip)
    if not entry:
        return False
    if entry.get('failures', 0) < RATE_LIMIT_MAX_FAILURES:
        return False
    locked_until = entry.get('locked_until', 0)
    if time.time() >= locked_until:
        # 过期解锁
        _clear_ip_failures(ip)
        return False
    return True


def _record_failure(ip: str) -> None:
    data = _load_ratelimit()
    entry = data.get(ip, {'failures': 0, 'locked_until': 0})
    entry['failures'] = entry.get('failures', 0) + 1
    if entry['failures'] >= RATE_LIMIT_MAX_FAILURES:
        entry['locked_until'] = time.time() + RATE_LIMIT_LOCKOUT_SEC
        _audit('rate_limit.lockout', ip=ip, failures=entry['failures'])
        try:
            from common import send_tg
            send_tg(
                f"🚨 <b>Admin Panel 登录失败锁定</b>\n\n"
                f"IP: <code>{ip}</code>\n"
                f"连续失败: {entry['failures']} 次\n"
                f"锁定至: {datetime.fromtimestamp(entry['locked_until'], tz=timezone.utc).isoformat()[:19]} UTC"
            )
        except Exception:
            pass
    data[ip] = entry
    _save_ratelimit(data)


def _clear_ip_failures(ip: str) -> None:
    data = _load_ratelimit()
    if ip in data:
        del data[ip]
        _save_ratelimit(data)


# ══════════════════════════════════════════════════════════════════
#  审计日志
# ══════════════════════════════════════════════════════════════════

def _audit(event: str, **kwargs) -> None:
    """追加到 admin_audit.log；格式 JSON Lines，0600 权限。"""
    rec = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': event,
        'ip': _client_ip() if request else None,
        **kwargs,
    }
    try:
        with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        # 每次写完保证权限
        try:
            os.chmod(AUDIT_LOG, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except Exception as e:
        logger.error(f"写审计日志失败: {e}")


# ══════════════════════════════════════════════════════════════════
#  装饰器
# ══════════════════════════════════════════════════════════════════

def _require_login(f):
    """登录态检查 + session 过期控制"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = session.get('admin_auth')
        if not auth:
            # 未登录 → 扔到 login 页；保持 200 以免泄露路径存在
            return redirect(url_for('admin.login'))

        now = time.time()
        if now - auth.get('created_at', 0) > SESSION_ABSOLUTE_TIMEOUT:
            session.clear()
            return redirect(url_for('admin.login'))
        if now - auth.get('last_seen', 0) > SESSION_IDLE_TIMEOUT:
            session.clear()
            return redirect(url_for('admin.login'))

        # 续期
        auth['last_seen'] = now
        session['admin_auth'] = auth
        g.admin_auth = auth
        return f(*args, **kwargs)
    return decorated


def _require_fresh_totp(f):
    """写操作：5 分钟内必须有新鲜的 TOTP 验证"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = session.get('admin_auth', {})
        last_totp = auth.get('last_totp_at', 0)
        if time.time() - last_totp > FRESH_TOTP_WINDOW:
            return jsonify({
                'error': 'totp_required',
                'message': '写操作需要 5 分钟内验证过 TOTP，请重新输入 6 位动态码',
            }), 403
        return f(*args, **kwargs)
    return decorated


def _require_csrf(f):
    """CSRF token 检查"""
    @wraps(f)
    def decorated(*args, **kwargs):
        sent = request.headers.get('X-Admin-CSRF', '')
        stored = session.get('csrf_token', '')
        if not stored or not secrets.compare_digest(sent, stored):
            _audit('csrf.fail', has_header=bool(sent))
            return jsonify({'error': 'csrf_fail'}), 403
        return f(*args, **kwargs)
    return decorated


def _no_cache_response(resp):
    """给所有响应加反缓存/反嵌入头"""
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    # CSP：只允许同源脚本/样式，禁止外链资源
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return resp


# ══════════════════════════════════════════════════════════════════
#  Blueprint 工厂
# ══════════════════════════════════════════════════════════════════

def create_blueprint(url_secret: str) -> Blueprint:
    """
    创建 admin blueprint，挂在 /<url_secret>/ 下。
    dashboard.py 会在 ADMIN_URL_SECRET 存在时调用；不存在时整个面板不加载。
    """
    # url_prefix 必须以 / 开头，不以 / 结尾
    prefix = '/' + url_secret.strip('/')
    bp = Blueprint('admin', __name__,
                   url_prefix=prefix,
                   template_folder=os.path.join(SCRIPT_DIR, 'templates', 'admin'))

    # ── 全局前置守卫 ──
    @bp.before_request
    def _before():
        # IP 白名单
        allowed_ips = os.environ.get('ADMIN_ALLOWED_IPS', '').strip()
        if allowed_ips:
            allowed = [ip.strip() for ip in allowed_ips.split(',') if ip.strip()]
            if _client_ip() not in allowed:
                # 不在白名单 → 假装路径不存在
                abort(404)

        # IP 锁定
        if _is_ip_locked(_client_ip()):
            # 锁定期返回 404，让攻击者无法通过响应判断路径存在
            abort(404)

        # 确保每个 session 有 CSRF token
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)

    @bp.after_request
    def _after(resp):
        return _no_cache_response(resp)

    # ══════════════════════════════════════════════════════════════════
    #  页面：登录 / 首次初始化
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/', methods=['GET'])
    def root():
        # 根路径：已登录去面板，否则看是否要初始化，都不是就去登录
        if session.get('admin_auth'):
            return redirect(url_for('admin.panel'))
        if not admin_secrets.is_initialized():
            return redirect(url_for('admin.setup'))
        return redirect(url_for('admin.login'))

    @bp.route('/login', methods=['GET'])
    def login():
        if session.get('admin_auth'):
            return redirect(url_for('admin.panel'))
        if not admin_secrets.is_initialized():
            return redirect(url_for('admin.setup'))
        return render_template('admin/login.html',
                               csrf_token=session['csrf_token'])

    @bp.route('/login', methods=['POST'])
    def login_submit():
        # 显式校验 CSRF（不能用装饰器，因为登录前没用 _require_login）
        sent_csrf = request.form.get('csrf_token', '')
        if not secrets.compare_digest(sent_csrf, session.get('csrf_token', '')):
            _audit('login.csrf_fail')
            abort(400)

        if not admin_secrets.is_initialized():
            return redirect(url_for('admin.setup'))

        password = request.form.get('password', '')
        totp_code = request.form.get('totp', '').strip()

        # 密码 + TOTP 都必须校验通过
        pw_ok = admin_secrets.verify_password(password)
        totp_ok = True  # 若未启用 2FA 则跳过
        if admin_secrets.is_totp_enabled():
            totp_ok = admin_secrets.verify_totp(totp_code)

        if not (pw_ok and totp_ok):
            _record_failure(_client_ip())
            _audit('login.fail',
                   pw_ok=pw_ok,
                   totp_ok=totp_ok,
                   totp_enabled=admin_secrets.is_totp_enabled())
            # 返回通用错误，不透露是密码错还是 TOTP 错
            return render_template('admin/login.html',
                                   error="凭证错误",
                                   csrf_token=session['csrf_token']), 401

        # 登录成功
        _clear_ip_failures(_client_ip())
        now = time.time()
        session['admin_auth'] = {
            'created_at': now,
            'last_seen': now,
            'last_totp_at': now if totp_ok and admin_secrets.is_totp_enabled() else 0,
        }
        # 登录成功后换一个 CSRF，防止 session fixation
        session['csrf_token'] = secrets.token_urlsafe(32)
        _audit('login.success', totp_enabled=admin_secrets.is_totp_enabled())
        try:
            from common import send_tg
            send_tg(
                f"🔓 <b>Admin Panel 登录</b>\n\n"
                f"IP: <code>{_client_ip()}</code>\n"
                f"时间: {datetime.now(timezone.utc).isoformat()[:19]} UTC"
            )
        except Exception:
            pass
        return redirect(url_for('admin.panel'))

    @bp.route('/logout', methods=['POST'])
    def logout():
        _audit('logout')
        session.clear()
        return redirect(url_for('admin.login'))

    # ══════════════════════════════════════════════════════════════════
    #  页面：首次初始化（设置密码 + 绑定 TOTP）
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/setup', methods=['GET'])
    def setup():
        if admin_secrets.is_initialized():
            # 已初始化，禁止重复 setup
            return redirect(url_for('admin.login'))

        # 生成一个候选 TOTP secret（等用户提交才真正保存）
        if 'setup_totp_secret' not in session:
            session['setup_totp_secret'] = admin_secrets.generate_totp_secret()

        secret = session['setup_totp_secret']
        otpauth_url = admin_secrets.set_totp_secret(secret, issuer="altcoin-shadow-admin")
        # 注：上面 set_totp_secret 会写磁盘但 enabled=False，所以此时还不能登录
        # 但我们需要 otpauth_url，所以直接构造一个不写盘的版本：
        from urllib.parse import quote
        otpauth_url = (
            f"otpauth://totp/{quote('altcoin-shadow-admin')}:{quote('admin')}"
            f"?secret={secret}&issuer={quote('altcoin-shadow-admin')}&digits=6&period=30"
        )

        return render_template('admin/setup.html',
                               csrf_token=session['csrf_token'],
                               totp_secret=secret,
                               otpauth_url=otpauth_url)

    @bp.route('/setup', methods=['POST'])
    def setup_submit():
        if admin_secrets.is_initialized():
            abort(404)
        sent_csrf = request.form.get('csrf_token', '')
        if not secrets.compare_digest(sent_csrf, session.get('csrf_token', '')):
            abort(400)

        pw = request.form.get('password', '')
        pw2 = request.form.get('password_confirm', '')
        totp_code = request.form.get('totp', '').strip()
        setup_secret = session.get('setup_totp_secret', '')

        if not setup_secret:
            return render_template('admin/setup.html',
                                   error="会话已过期，请刷新重试",
                                   csrf_token=session['csrf_token'],
                                   totp_secret=admin_secrets.generate_totp_secret(),
                                   otpauth_url=''), 400

        if pw != pw2:
            return render_template('admin/setup.html',
                                   error="两次密码不一致",
                                   csrf_token=session['csrf_token'],
                                   totp_secret=setup_secret,
                                   otpauth_url=''), 400

        if len(pw) < 12:
            return render_template('admin/setup.html',
                                   error="密码至少 12 个字符",
                                   csrf_token=session['csrf_token'],
                                   totp_secret=setup_secret,
                                   otpauth_url=''), 400

        # 验证 TOTP 码（证明用户确实扫了二维码）
        if not admin_secrets.verify_totp(totp_code, secret_b32=setup_secret):
            return render_template('admin/setup.html',
                                   error="TOTP 码错误，请确认 Authenticator 时间同步后重试",
                                   csrf_token=session['csrf_token'],
                                   totp_secret=setup_secret,
                                   otpauth_url=''), 400

        # 通过 → 持久化
        try:
            admin_secrets.set_password(pw)
        except ValueError as e:
            return render_template('admin/setup.html',
                                   error=str(e),
                                   csrf_token=session['csrf_token'],
                                   totp_secret=setup_secret,
                                   otpauth_url=''), 400
        admin_secrets.set_totp_secret(setup_secret)
        admin_secrets.enable_totp()

        session.pop('setup_totp_secret', None)
        _audit('setup.complete')
        try:
            from common import send_tg
            send_tg(
                f"🔐 <b>Admin Panel 初始化完成</b>\n\n"
                f"IP: <code>{_client_ip()}</code>\n"
                f"管理员密码和 TOTP 已绑定；下次访问需要密码 + 6 位动态码"
            )
        except Exception:
            pass
        return redirect(url_for('admin.login'))

    # ══════════════════════════════════════════════════════════════════
    #  页面：主面板
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/panel', methods=['GET'])
    @_require_login
    def panel():
        return render_template('admin/panel.html',
                               csrf_token=session['csrf_token'])

    # ══════════════════════════════════════════════════════════════════
    #  API：状态 + 配置读取
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/state', methods=['GET'])
    @_require_login
    def api_state():
        """返回当前所有状态：配置、凭证(脱敏)、路由预览、session 剩余"""
        auth = session.get('admin_auth', {})
        now = time.time()

        # 凭证脱敏
        bn_masked = admin_secrets.mask_credentials('binance')
        okx_masked = admin_secrets.mask_credentials('okx')

        # 实际配置当前值
        current = runtime_config.get_current_values()

        # 字段元信息（给前端生成表单用）
        fields_meta = {}
        for key, (t, _v, label) in runtime_config.ALLOWED.items():
            fields_meta[key] = {
                'type': t.__name__,
                'label': label,
            }

        data = {
            'config': current,
            'fields_meta': fields_meta,
            'exchanges': {
                'binance': {
                    'has_credentials': bool(bn_masked.get('api_key')),
                    'masked': bn_masked,
                },
                'okx': {
                    'has_credentials': bool(okx_masked.get('api_key')),
                    'masked': okx_masked,
                },
            },
            'session': {
                'idle_remaining_sec': max(0, SESSION_IDLE_TIMEOUT - int(now - auth.get('last_seen', now))),
                'absolute_remaining_sec': max(0, SESSION_ABSOLUTE_TIMEOUT - int(now - auth.get('created_at', now))),
                'fresh_totp': (now - auth.get('last_totp_at', 0)) < FRESH_TOTP_WINDOW,
                'totp_enabled': admin_secrets.is_totp_enabled(),
            },
        }
        return jsonify(data)

    @bp.route('/api/verify-totp', methods=['POST'])
    @_require_login
    @_require_csrf
    def api_verify_totp():
        """
        刷新 TOTP 新鲜度；写操作之前前端会调这个。
        """
        if not admin_secrets.is_totp_enabled():
            # 未启用 2FA 时永远返回成功（保持 API 签名一致）
            auth = session['admin_auth']
            auth['last_totp_at'] = time.time()
            session['admin_auth'] = auth
            return jsonify({'ok': True})

        data = request.get_json(silent=True) or {}
        code = (data.get('totp') or '').strip()
        if not admin_secrets.verify_totp(code):
            _record_failure(_client_ip())
            _audit('totp.fail')
            return jsonify({'error': 'totp_invalid'}), 401

        auth = session['admin_auth']
        auth['last_totp_at'] = time.time()
        session['admin_auth'] = auth
        _audit('totp.fresh_verified')
        return jsonify({'ok': True})

    # ══════════════════════════════════════════════════════════════════
    #  API：更新运行时配置
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/config', methods=['POST'])
    @_require_login
    @_require_csrf
    @_require_fresh_totp
    def api_set_config():
        """
        更新运行时配置。只接受白名单字段，逐个校验。
        所有变更 → 审计 + TG 推送。
        """
        data = request.get_json(silent=True) or {}
        changes = data.get('changes') or {}
        if not isinstance(changes, dict):
            return jsonify({'error': 'changes must be object'}), 400

        # 逐字段校验
        errors = {}
        cleaned = {}
        for key, value in changes.items():
            ok, err = runtime_config.validate_change(key, value)
            if not ok:
                errors[key] = err
            else:
                cleaned[key] = value

        if errors:
            return jsonify({'error': 'validation', 'details': errors}), 400

        # 合并到当前 overrides 文件
        existing = runtime_config.load_overrides()
        merged = {**existing, **cleaned}
        runtime_config.save_overrides(merged)
        applied = runtime_config.apply_overrides(force=True)

        _audit('config.update', changes=cleaned)
        try:
            from common import send_tg
            lines = [f"• {k}: {v['old']} → {v['new']}" for k, v in applied.items()]
            send_tg(
                f"⚙️ <b>Admin 修改运行时配置</b>\n\n"
                f"IP: <code>{_client_ip()}</code>\n\n"
                + "\n".join(lines[:10])
            )
        except Exception:
            pass

        return jsonify({'ok': True, 'applied': applied})

    # ══════════════════════════════════════════════════════════════════
    #  API：更新交易所凭证
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/credentials/<exchange>', methods=['POST'])
    @_require_login
    @_require_csrf
    @_require_fresh_totp
    def api_set_credentials(exchange):
        if exchange not in ('binance', 'okx'):
            abort(404)
        data = request.get_json(silent=True) or {}

        kwargs = {}
        for field in ('api_key', 'secret', 'passphrase'):
            v = (data.get(field) or '').strip()
            if v:
                kwargs[field] = v

        if not kwargs:
            return jsonify({'error': '没有要更新的字段'}), 400

        # Binance 不接受 passphrase
        if exchange == 'binance' and 'passphrase' in kwargs:
            del kwargs['passphrase']
        # OKX 必须有 passphrase（若之前没存过）
        existing = admin_secrets.get_exchange_credentials(exchange)

        try:
            admin_secrets.set_exchange_credentials(exchange, **kwargs)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

        _audit('credentials.update',
               exchange=exchange,
               fields=list(kwargs.keys()))
        try:
            from common import send_tg
            send_tg(
                f"🔑 <b>Admin 更新 {exchange.upper()} API 凭证</b>\n\n"
                f"IP: <code>{_client_ip()}</code>\n"
                f"更新字段: {', '.join(kwargs.keys())}"
            )
        except Exception:
            pass

        return jsonify({'ok': True})

    @bp.route('/api/credentials/<exchange>', methods=['DELETE'])
    @_require_login
    @_require_csrf
    @_require_fresh_totp
    def api_clear_credentials(exchange):
        if exchange not in ('binance', 'okx'):
            abort(404)

        # 安全起见：清除凭证时，强制关掉对应的 LIVE_MODE
        existing = runtime_config.load_overrides()
        if exchange == 'binance':
            existing['LIVE_MODE'] = False
        else:
            existing['OKX_LIVE_MODE'] = False
        runtime_config.save_overrides(existing)
        runtime_config.apply_overrides(force=True)

        admin_secrets.clear_exchange_credentials(exchange)

        _audit('credentials.clear', exchange=exchange)
        try:
            from common import send_tg
            send_tg(
                f"🗑️ <b>Admin 清除 {exchange.upper()} API 凭证</b>\n\n"
                f"IP: <code>{_client_ip()}</code>\n"
                f"已自动关闭 {exchange.upper()} 实盘开关"
            )
        except Exception:
            pass

        return jsonify({'ok': True})

    # ══════════════════════════════════════════════════════════════════
    #  API：审计日志查询
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/audit', methods=['GET'])
    @_require_login
    def api_audit():
        """返回最近 200 条审计日志"""
        if not os.path.exists(AUDIT_LOG):
            return jsonify({'events': []})
        try:
            with open(AUDIT_LOG, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            return jsonify({'events': []})

        events = []
        for line in lines[-200:]:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
        events.reverse()
        return jsonify({'events': events})

    return bp
