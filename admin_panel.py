#!/usr/bin/env python3
"""
管理员配置面板 v2.0 — 多账户高安全实盘接入面板

安全多层防御（按请求流经顺序）：
  L1  IP 白名单（可选）
  L2  Secret URL 前缀
  L3  IP 限速 + 失败锁定
  L4  密码 (PBKDF2-SHA256, 600k iter) + TOTP
  L5  Session: 30 分钟空闲过期 / 4 小时绝对过期
  L6  CSRF: 所有 POST 必须带 X-Admin-CSRF 头
  L7  审计日志
  L8  响应头: noindex, no-cache, X-Frame-Options=DENY, CSP 严格
"""

import json
import logging
import os
import secrets
import stat
import time
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Blueprint, request, session, redirect, url_for,
    render_template, jsonify, abort, g,
)

import admin_secrets
import runtime_config

logger = logging.getLogger("admin_panel")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG = os.path.join(SCRIPT_DIR, 'admin_audit.log')
RATE_LIMIT_FILE = os.path.join(SCRIPT_DIR, '.admin_ratelimit.json')

# ── 安全参数 ──
SESSION_IDLE_TIMEOUT = 30 * 60
SESSION_ABSOLUTE_TIMEOUT = 4 * 3600
FRESH_TOTP_WINDOW = 5 * 60
RATE_LIMIT_MAX_FAILURES = 5
RATE_LIMIT_LOCKOUT_SEC = 30 * 60


# ══════════════════════════════════════════════════════════════════
#  IP 限速（跨进程持久化）
# ══════════════════════════════════════════════════════════════════
# L-4 修复（2026-05）：用 LockedJsonFile 上下文，防止两个失败请求并发时
# 丢失增量；多个 dashboard 实例（多 worker）下也能正确累计。

def _client_ip() -> str:
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _is_ip_locked(ip: str) -> bool:
    from common import load_json as _ljson
    data = _ljson(RATE_LIMIT_FILE, {}) or {}
    entry = data.get(ip)
    if not entry:
        return False
    if entry.get('failures', 0) < RATE_LIMIT_MAX_FAILURES:
        return False
    locked_until = entry.get('locked_until', 0)
    if time.time() >= locked_until:
        _clear_ip_failures(ip)
        return False
    return True


def _record_failure(ip: str) -> None:
    from common import LockedJsonFile as _LJF
    triggered_lockout = False
    locked_until = 0
    final_failures = 0
    with _LJF(RATE_LIMIT_FILE, default={}) as (data, save):
        if not isinstance(data, dict):
            data = {}
        entry = data.get(ip, {'failures': 0, 'locked_until': 0})
        entry['failures'] = entry.get('failures', 0) + 1
        if entry['failures'] >= RATE_LIMIT_MAX_FAILURES:
            entry['locked_until'] = time.time() + RATE_LIMIT_LOCKOUT_SEC
            triggered_lockout = entry.get('locked_until', 0) > 0
            locked_until = entry['locked_until']
        final_failures = entry['failures']
        data[ip] = entry
        save(data)
        try:
            os.chmod(RATE_LIMIT_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    if triggered_lockout:
        _audit('rate_limit.lockout', ip=ip, failures=final_failures)
        try:
            from common import send_tg, tg_escape
            send_tg(
                f"🚨 <b>Admin Panel 登录失败锁定</b>\n\n"
                f"IP: <code>{tg_escape(ip)}</code>\n"
                f"连续失败: {final_failures} 次\n"
                f"锁定至: {datetime.fromtimestamp(locked_until, tz=timezone.utc).isoformat()[:19]} UTC"
            )
        except Exception:
            pass


def _clear_ip_failures(ip: str) -> None:
    from common import LockedJsonFile as _LJF
    with _LJF(RATE_LIMIT_FILE, default={}) as (data, save):
        if isinstance(data, dict) and ip in data:
            del data[ip]
            save(data)


# ══════════════════════════════════════════════════════════════════
#  审计日志
# ══════════════════════════════════════════════════════════════════

def _audit(event: str, **kwargs) -> None:
    rec = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': event,
        'ip': _client_ip() if request else None,
        **kwargs,
    }
    try:
        with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
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
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = session.get('admin_auth')
        if not auth:
            return redirect(url_for('admin.login'))
        now = time.time()
        if now - auth.get('created_at', 0) > SESSION_ABSOLUTE_TIMEOUT:
            session.clear()
            return redirect(url_for('admin.login'))
        if now - auth.get('last_seen', 0) > SESSION_IDLE_TIMEOUT:
            session.clear()
            return redirect(url_for('admin.login'))
        auth['last_seen'] = now
        session['admin_auth'] = auth
        g.admin_auth = auth
        return f(*args, **kwargs)
    return decorated


def _require_fresh_totp(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 如果 TOTP 未启用，直接放行（不强制弹窗）
        if not admin_secrets.is_totp_enabled():
            return f(*args, **kwargs)
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
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
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
    prefix = '/' + url_secret.strip('/')
    bp = Blueprint('admin', __name__,
                   url_prefix=prefix,
                   template_folder=os.path.join(SCRIPT_DIR, 'templates', 'admin'))

    # ── 全局前置守卫 ──
    @bp.before_request
    def _before():
        allowed_ips = os.environ.get('ADMIN_ALLOWED_IPS', '').strip()
        if allowed_ips:
            allowed = [ip.strip() for ip in allowed_ips.split(',') if ip.strip()]
            if _client_ip() not in allowed:
                abort(404)
        if _is_ip_locked(_client_ip()):
            abort(404)
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
        sent_csrf = request.form.get('csrf_token', '')
        if not secrets.compare_digest(sent_csrf, session.get('csrf_token', '')):
            _audit('login.csrf_fail')
            abort(400)

        if not admin_secrets.is_initialized():
            return redirect(url_for('admin.setup'))

        password = request.form.get('password', '')
        totp_code = request.form.get('totp', '').strip()

        pw_ok = admin_secrets.verify_password(password)
        totp_ok = True
        if admin_secrets.is_totp_enabled():
            totp_ok = admin_secrets.verify_totp(totp_code)

        if not (pw_ok and totp_ok):
            _record_failure(_client_ip())
            _audit('login.fail', pw_ok=pw_ok, totp_ok=totp_ok,
                   totp_enabled=admin_secrets.is_totp_enabled())
            return render_template('admin/login.html',
                                   error="凭证错误",
                                   csrf_token=session['csrf_token']), 401

        _clear_ip_failures(_client_ip())
        now = time.time()
        session['admin_auth'] = {
            'created_at': now,
            'last_seen': now,
            'last_totp_at': now if totp_ok and admin_secrets.is_totp_enabled() else 0,
        }
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
    #  页面：首次初始化
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/setup', methods=['GET'])
    def setup():
        if admin_secrets.is_initialized():
            return redirect(url_for('admin.login'))

        # M-8 修复：要求宿主机存在 .admin_setup_token 文件才允许访问 setup
        # 防止 ADMIN_URL_SECRET 通过启动日志/共享终端泄露后攻击者抢先 setup。
        # 用户首次部署需手动 `touch .admin_setup_token`，setup 成功后自动删除。
        # 文件存在表示"运维明确允许此次 setup"。
        setup_token_file = os.path.join(SCRIPT_DIR, '.admin_setup_token')
        if not os.path.exists(setup_token_file):
            logger.warning(
                f"setup 被访问但 {setup_token_file} 不存在 - 拒绝。"
                f"请运维 SSH 上服务器 `touch .admin_setup_token` 授权初始化"
            )
            return abort(404)

        if 'setup_totp_secret' not in session:
            session['setup_totp_secret'] = admin_secrets.generate_totp_secret()

        secret = session['setup_totp_secret']
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
        # M-8: 同时校验 setup_token 文件，防止绕过 GET 直接 POST
        setup_token_file = os.path.join(SCRIPT_DIR, '.admin_setup_token')
        if not os.path.exists(setup_token_file):
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
        if not admin_secrets.verify_totp(totp_code, secret_b32=setup_secret):
            return render_template('admin/setup.html',
                                   error="TOTP 码错误，请确认 Authenticator 时间同步后重试",
                                   csrf_token=session['csrf_token'],
                                   totp_secret=setup_secret,
                                   otpauth_url=''), 400
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
        # 创建默认账户
        admin_secrets.create_account('主账户')

        # M-8: setup 成功后立即删除 token 文件，下次 setup 需要运维重新授权
        try:
            os.unlink(setup_token_file)
        except OSError:
            pass

        session.pop('setup_totp_secret', None)
        _audit('setup.complete')
        try:
            from common import send_tg
            send_tg(
                f"🔐 <b>Admin Panel 初始化完成</b>\n\n"
                f"IP: <code>{_client_ip()}</code>\n"
                f"管理员密码和 TOTP 已绑定"
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
    #  API：多账户管理
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/accounts', methods=['GET'])
    @_require_login
    def api_list_accounts():
        accounts = admin_secrets.list_accounts()
        active_id = admin_secrets.get_active_account_id()
        return jsonify({'accounts': accounts, 'active_account': active_id})

    @bp.route('/api/accounts', methods=['POST'])
    @_require_login
    @_require_csrf
    @_require_fresh_totp
    def api_create_account():
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': '账户名称不能为空'}), 400
        try:
            account_id = admin_secrets.create_account(name)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        _audit('account.create', account_id=account_id, name=name)
        return jsonify({'ok': True, 'account_id': account_id})

    @bp.route('/api/accounts/<account_id>', methods=['DELETE'])
    @_require_login
    @_require_csrf
    @_require_fresh_totp
    def api_delete_account(account_id):
        try:
            admin_secrets.delete_account(account_id)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        _audit('account.delete', account_id=account_id)
        return jsonify({'ok': True})

    @bp.route('/api/accounts/<account_id>/activate', methods=['POST'])
    @_require_login
    @_require_csrf
    def api_activate_account(account_id):
        try:
            admin_secrets.set_active_account(account_id)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        # 重新应用该账户配置
        runtime_config.apply_overrides(force=True)
        _audit('account.activate', account_id=account_id)
        return jsonify({'ok': True})

    @bp.route('/api/accounts/<account_id>', methods=['PATCH'])
    @_require_login
    @_require_csrf
    def api_rename_account(account_id):
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': '名称不能为空'}), 400
        try:
            admin_secrets.rename_account(account_id, name)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        _audit('account.rename', account_id=account_id, new_name=name)
        return jsonify({'ok': True})

    @bp.route('/api/accounts/<account_id>/trading', methods=['POST'])
    @_require_login
    @_require_csrf
    def api_toggle_account_trading(account_id):
        """
        切换单个账户的交易开关。
        请求体: {"enabled": true/false}
        关闭后：下一个信号不再为该账户开新仓，但已有持仓继续被 tracker 监控。
        """
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get('enabled', True))
        try:
            admin_secrets.set_account_trading_enabled(account_id, enabled)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        _audit('account.trading_toggle', account_id=account_id, enabled=enabled)
        return jsonify({'ok': True, 'account_id': account_id, 'enabled': enabled})

    # ══════════════════════════════════════════════════════════════════
    #  API：状态 + 配置读取
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/state', methods=['GET'])
    @_require_login
    def api_state():
        auth = session.get('admin_auth', {})
        now = time.time()

        bn_masked = admin_secrets.mask_credentials('binance')
        okx_masked = admin_secrets.mask_credentials('okx')

        current = runtime_config.get_current_values()

        fields_meta = {}
        for key, (t, _v, label) in runtime_config.ALLOWED.items():
            fields_meta[key] = {'type': t.__name__, 'label': label}

        # 复利实时数据
        try:
            from common import get_compound_stake, get_dynamic_balance
            import config as _cfg
            compound_stake = round(get_compound_stake(), 2)
            dynamic_balance = round(get_dynamic_balance(), 2)
            compound_enabled = getattr(_cfg, 'AUTO_COMPOUND_ENABLED', True)
        except Exception:
            compound_stake = current.get('DEFAULT_STAKE', 50)
            dynamic_balance = current.get('ACCOUNT_BALANCE', 100)
            compound_enabled = True

        # 账户信息
        accounts = admin_secrets.list_accounts()
        active_id = admin_secrets.get_active_account_id()

        data = {
            'config': current,
            'fields_meta': fields_meta,
            'compound': {
                'current_stake': compound_stake,
                'dynamic_balance': dynamic_balance,
                'enabled': compound_enabled,
            },
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
            'accounts': accounts,
            'active_account': active_id,
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
        if not admin_secrets.is_totp_enabled():
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
        data = request.get_json(silent=True) or {}
        changes = data.get('changes') or {}
        if not isinstance(changes, dict):
            return jsonify({'error': 'changes must be object'}), 400

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

        # 跨字段一致性硬阻塞（2026-05 修复）：
        # 致命组合（如 DEFAULT_STAKE > ACCOUNT_BALANCE）必须在保存前拦截，
        # 否则会让风控永远拒绝开仓。warnings 不阻止保存，仅在响应里反馈让 UI 提示。
        active_id = ''
        try:
            active_id = admin_secrets.get_active_account_id()
        except Exception:
            pass
        merged_for_check = {**runtime_config.load_overrides(), **cleaned}
        consistency_errors, consistency_warnings = (
            runtime_config.validate_cross_field_consistency(
                merged_for_check, account_id=active_id
            )
        )
        if consistency_errors:
            _audit('config.update.blocked', changes=cleaned,
                   reason='cross_field_consistency', errors=consistency_errors)
            try:
                from common import send_tg, tg_escape
                send_tg(
                    f"🚫 <b>Admin 配置保存被拒</b>\n\n"
                    f"IP: <code>{tg_escape(_client_ip())}</code>\n"
                    + "\n".join(f"• {tg_escape(e)}" for e in consistency_errors)
                )
            except Exception:
                pass
            return jsonify({
                'error': 'consistency',
                'details': consistency_errors,
                'warnings': consistency_warnings,
            }), 400

        existing = runtime_config.load_overrides()
        merged = {**existing, **cleaned}
        try:
            runtime_config.save_overrides(merged)
        except ValueError as ve:
            # 防御纵深：save_overrides 内部最后一道关卡（理论上前面已经拦截）
            return jsonify({'error': 'consistency', 'details': [str(ve)]}), 400
        applied = runtime_config.apply_overrides(force=True)

        _audit('config.update', changes=cleaned, warnings=consistency_warnings)
        try:
            from common import send_tg, tg_escape
            lines = [f"• {k}: {v['old']} → {v['new']}" for k, v in applied.items()]
            extra = ""
            if consistency_warnings:
                extra = "\n\n⚠️ 警告：\n" + "\n".join(
                    f"• {tg_escape(w)}" for w in consistency_warnings
                )
            send_tg(
                f"⚙️ <b>Admin 修改运行时配置</b>\n\n"
                f"IP: <code>{tg_escape(_client_ip())}</code>\n\n"
                + "\n".join(lines[:10]) + extra
            )
        except Exception:
            pass

        return jsonify({
            'ok': True,
            'applied': applied,
            'warnings': consistency_warnings,
        })

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

        if exchange == 'binance' and 'passphrase' in kwargs:
            del kwargs['passphrase']

        try:
            admin_secrets.set_exchange_credentials(exchange, **kwargs)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

        _audit('credentials.update', exchange=exchange, fields=list(kwargs.keys()))
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
    #  API：系统自检 (Smoke Test)
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/smoke-test', methods=['POST'])
    @_require_login
    @_require_csrf
    def api_smoke_test():
        """
        端到端自检入口 —— 调用 smoke_test.run_phases。

        Body (JSON):
            {"phases": ["A","B","D"]}   # 默认 ABD（不带 C，避免触发实盘鉴权）

        返回:
            {"results": [...], "summary": {...}}
        Phase 列表中包含 "C" 时会调用交易所 fetch_balance 验证凭证（仍不下单）。
        """
        import smoke_test  # 延迟 import，避免影响 admin panel 启动
        data = request.get_json(silent=True) or {}
        phases = data.get('phases') or list(smoke_test.DEFAULT_PHASES)
        # 防止恶意巨大请求
        if not isinstance(phases, list) or len(phases) > 10:
            return jsonify({'error': 'phases 字段必须是长度 ≤ 10 的列表'}), 400
        phases = [str(p).strip().upper() for p in phases]
        invalid = [p for p in phases if p not in smoke_test.VALID_PHASES]
        if invalid:
            return jsonify({
                'error': f'非法阶段: {",".join(invalid)}（合法值 {",".join(smoke_test.VALID_PHASES)}）'
            }), 400

        try:
            payload = smoke_test.run_phases(phases)
        except Exception as e:
            logger.exception("smoke_test.run_phases 失败")
            return jsonify({'error': f'内部错误: {e}'}), 500

        _audit('smoke_test.run', phases=phases,
               all_ok=payload['summary']['all_ok'],
               total=payload['summary']['total'])
        return jsonify(payload)

    # ══════════════════════════════════════════════════════════════════
    #  API：实盘自检
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/preflight-check', methods=['POST'])
    @_require_login
    @_require_csrf
    def api_preflight_check():
        import config as _cfg

        data = request.get_json(silent=True) or {}
        targets = data.get('exchanges') or []
        if not targets:
            bn_creds = admin_secrets.get_exchange_credentials('binance')
            okx_creds = admin_secrets.get_exchange_credentials('okx')
            if bn_creds.get('api_key'):
                targets.append('binance')
            if okx_creds.get('api_key'):
                targets.append('okx')

        if not targets:
            return jsonify({
                'ok': False,
                'error': '没有已配置凭证的交易所，请先在上方保存 API 凭证',
                'results': {},
                'routing': _build_routing_summary(_cfg),
            })

        results = {}
        if 'binance' in targets:
            results['binance'] = _run_binance_check(_cfg)
        if 'okx' in targets:
            results['okx'] = _run_okx_check(_cfg)

        all_pass = all(r['pass'] for r in results.values())
        _audit('preflight_check', targets=targets, all_pass=all_pass)

        return jsonify({
            'ok': all_pass,
            'results': results,
            'routing': _build_routing_summary(_cfg),
        })

    def _build_routing_summary(_cfg) -> dict:
        binance_on = _cfg.LIVE_MODE
        okx_on = _cfg.OKX_LIVE_MODE
        mode = getattr(_cfg, 'PRIMARY_EXCHANGE', 'binance').lower()

        if not binance_on and not okx_on:
            behavior = "纸上交易（不会下任何真实单）"
        elif binance_on and not okx_on:
            behavior = "所有信号只在 Binance 下单"
        elif okx_on and not binance_on:
            behavior = "所有信号只在 OKX 下单"
        else:
            if mode == 'both':
                behavior = "Binance + OKX 同时开仓（保证金各 50%）"
            elif mode in ('binance', 'okx'):
                behavior = f"两所都启用，但信号只在 {mode.upper()} 下单"
            elif mode == 'auto':
                behavior = f"两所都启用，按币种覆盖自动选择（fallback={getattr(_cfg, 'PRIMARY_EXCHANGE_FALLBACK', 'binance')}）"
            else:
                behavior = f"未知路由模式: {mode}"

        return {
            'LIVE_MODE': binance_on,
            'OKX_LIVE_MODE': okx_on,
            'PRIMARY_EXCHANGE': mode,
            'DEFAULT_STAKE': _cfg.DEFAULT_STAKE,
            'LEVERAGE': _cfg.LEVERAGE,
            'OKX_DEFAULT_LEVERAGE': _cfg.OKX_DEFAULT_LEVERAGE,
            'behavior': behavior,
        }

    def _run_binance_check(_cfg) -> dict:
        checks = []
        try:
            creds = admin_secrets.get_exchange_credentials('binance')
            api_key = creds.get('api_key', '')
            secret_key = creds.get('secret', '')
        except Exception:
            api_key = os.environ.get('BINANCE_API_KEY', '')
            secret_key = os.environ.get('BINANCE_SECRET', '')

        if not api_key or not secret_key:
            checks.append({'name': 'API 凭证', 'status': 'fail',
                           'msg': '未配置 API Key 或 Secret'})
            return {'pass': False, 'checks': checks}

        checks.append({'name': 'API 凭证', 'status': 'pass',
                       'msg': f'已配置（key 前缀={api_key[:6]}...）'})

        try:
            import ccxt  # noqa: F401  仅用于检测依赖
            from exchange_manager import make_exchange
            exchange = make_exchange(
                'binance',
                api_key=api_key,
                secret=secret_key,
                default_type='future',
            )
        except ImportError:
            checks.append({'name': '依赖库', 'status': 'fail', 'msg': 'ccxt 未安装'})
            return {'pass': False, 'checks': checks}
        except Exception as e:
            checks.append({'name': '连接初始化', 'status': 'fail', 'msg': str(e)})
            return {'pass': False, 'checks': checks}

        try:
            balance = exchange.fetch_balance({'type': 'future'})
            usdt = balance.get('USDT', {})
            total = float(usdt.get('total', 0))
            free = float(usdt.get('free', 0))
            if total < _cfg.DEFAULT_STAKE:
                checks.append({'name': '合约余额', 'status': 'warn',
                               'msg': f'总={total:.2f}U / 可用={free:.2f}U（低于 DEFAULT_STAKE={_cfg.DEFAULT_STAKE}U）'})
            else:
                checks.append({'name': '合约余额', 'status': 'pass',
                               'msg': f'总={total:.2f}U / 可用={free:.2f}U'})
        except Exception as e:
            checks.append({'name': '合约余额', 'status': 'fail', 'msg': f'查询失败: {e}'})
            return {'pass': False, 'checks': checks}

        try:
            result = exchange.fapiPrivateGetPositionSideDual()
            dual_side = bool(result.get('dualSidePosition', False))
            if dual_side:
                checks.append({'name': '持仓模式', 'status': 'pass', 'msg': 'Hedge Mode（对冲模式）✓'})
            else:
                checks.append({'name': '持仓模式', 'status': 'fail',
                               'msg': '当前为单向模式！需切换为「对冲模式 / Hedge Mode」'})
                return {'pass': False, 'checks': checks}
        except Exception as e:
            checks.append({'name': '持仓模式', 'status': 'warn', 'msg': f'查询失败（不一定致命）: {e}'})

        try:
            exchange.set_leverage(_cfg.LEVERAGE, 'BTC/USDT')
            checks.append({'name': '杠杆接口', 'status': 'pass',
                           'msg': f'可用（BTC/USDT 杠杆={_cfg.LEVERAGE}x 已试设）'})
        except Exception as e:
            checks.append({'name': '杠杆接口', 'status': 'warn', 'msg': f'异常（可能是权限问题）: {e}'})

        all_pass = all(c['status'] != 'fail' for c in checks)
        return {'pass': all_pass, 'checks': checks}

    def _run_okx_check(_cfg) -> dict:
        checks = []
        try:
            creds = admin_secrets.get_exchange_credentials('okx')
            api_key = creds.get('api_key', '')
            secret_key = creds.get('secret', '')
            passphrase = creds.get('passphrase', '')
        except Exception:
            api_key = os.environ.get('OKX_API_KEY', '')
            secret_key = os.environ.get('OKX_SECRET', '')
            passphrase = os.environ.get('OKX_PASSPHRASE', '')

        missing = [n for n, v in [
            ('API Key', api_key), ('Secret', secret_key), ('Passphrase', passphrase)
        ] if not v]
        if missing:
            checks.append({'name': 'API 凭证', 'status': 'fail',
                           'msg': f'缺少: {", ".join(missing)}'})
            return {'pass': False, 'checks': checks}

        checks.append({'name': 'API 凭证', 'status': 'pass',
                       'msg': f'已配置（key 前缀={api_key[:6]}...）'})

        try:
            import ccxt  # noqa: F401  仅用于检测依赖
            from exchange_manager import make_exchange
            exchange = make_exchange(
                'okx',
                api_key=api_key,
                secret=secret_key,
                passphrase=passphrase,
            )
        except ImportError:
            checks.append({'name': '依赖库', 'status': 'fail', 'msg': 'ccxt 未安装'})
            return {'pass': False, 'checks': checks}
        except Exception as e:
            checks.append({'name': '连接初始化', 'status': 'fail', 'msg': str(e)})
            return {'pass': False, 'checks': checks}

        try:
            balance = exchange.fetch_balance({'type': 'swap'})
            usdt = balance.get('USDT', {})
            total = float(usdt.get('total', 0))
            free = float(usdt.get('free', 0))
            if total < _cfg.DEFAULT_STAKE:
                checks.append({'name': '合约余额', 'status': 'warn',
                               'msg': f'总={total:.2f}U / 可用={free:.2f}U（低于 DEFAULT_STAKE={_cfg.DEFAULT_STAKE}U）'})
            else:
                checks.append({'name': '合约余额', 'status': 'pass',
                               'msg': f'总={total:.2f}U / 可用={free:.2f}U'})
        except Exception as e:
            checks.append({'name': '合约余额', 'status': 'fail', 'msg': f'查询失败: {e}'})
            return {'pass': False, 'checks': checks}

        try:
            result = exchange.privateGetAccountConfig()
            data_list = result.get('data', [{}])
            acct_data = data_list[0] if data_list else {}
            pos_mode = acct_data.get('posMode', '')
            if pos_mode == 'long_short_mode':
                checks.append({'name': '持仓模式', 'status': 'pass', 'msg': 'long_short_mode（双向持仓）✓'})
            elif pos_mode == 'net_mode':
                checks.append({'name': '持仓模式', 'status': 'fail',
                               'msg': '当前为 net_mode！需切换为「双向持仓」'})
                return {'pass': False, 'checks': checks}
            else:
                checks.append({'name': '持仓模式', 'status': 'warn', 'msg': f'未知模式: {pos_mode}'})
        except Exception as e:
            checks.append({'name': '持仓模式', 'status': 'warn', 'msg': f'查询失败: {e}'})

        try:
            exchange.set_leverage(_cfg.OKX_DEFAULT_LEVERAGE, 'BTC/USDT', params={'mgnMode': 'cross'})
            checks.append({'name': '杠杆接口', 'status': 'pass',
                           'msg': f'可用（BTC/USDT 杠杆={_cfg.OKX_DEFAULT_LEVERAGE}x 已试设）'})
        except Exception as e:
            checks.append({'name': '杠杆接口', 'status': 'warn', 'msg': f'异常: {e}'})

        all_pass = all(c['status'] != 'fail' for c in checks)
        return {'pass': all_pass, 'checks': checks}

    # ══════════════════════════════════════════════════════════════════
    #  API：审计日志查询
    # ══════════════════════════════════════════════════════════════════

    @bp.route('/api/audit', methods=['GET'])
    @_require_login
    def api_audit():
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
