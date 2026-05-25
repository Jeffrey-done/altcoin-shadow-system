"""
数据库连接管理
支持 SQLite（开发/单节点）和 PostgreSQL（生产/多节点）自动切换。

环境变量:
  DATABASE_URL: 完整数据库 URL（优先）
    - sqlite:///./altcoin_shadow.db       (SQLite)
    - postgresql://user:pass@host/dbname  (PostgreSQL)
  DB_ENGINE: 快捷选择 'sqlite' | 'postgresql'（DATABASE_URL 未设时生效）

默认: SQLite，数据库文件在项目根目录 altcoin_shadow.db
"""

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

# 项目根目录
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_database_url() -> str:
    """解析数据库 URL，优先环境变量，默认 SQLite"""
    url = os.environ.get('DATABASE_URL', '')
    if url:
        return url

    engine_type = os.environ.get('DB_ENGINE', 'sqlite').lower()
    if engine_type == 'postgresql':
        host = os.environ.get('DB_HOST', 'localhost')
        port = os.environ.get('DB_PORT', '5432')
        user = os.environ.get('DB_USER', 'altcoin')
        password = os.environ.get('DB_PASSWORD', 'altcoin')
        dbname = os.environ.get('DB_NAME', 'altcoin_shadow')
        return f'postgresql://{user}:{password}@{host}:{port}/{dbname}'

    # 默认 SQLite
    db_path = os.path.join(_SCRIPT_DIR, 'altcoin_shadow.db')
    return f'sqlite:///{db_path}'


def _create_engine():
    """创建 SQLAlchemy Engine（单例）"""
    url = _get_database_url()

    if url.startswith('sqlite'):
        # SQLite 优化：WAL 模式 + 连接池兼容
        engine = create_engine(
            url,
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
            echo=False,
        )

        @event.listens_for(engine, 'connect')
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA synchronous=NORMAL')
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.execute('PRAGMA busy_timeout=5000')
            cursor.close()

        return engine
    else:
        # PostgreSQL
        return create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=False,
        )


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_engine = None
_SessionFactory = None


def get_engine():
    """获取全局 Engine 单例"""
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


def get_session_factory() -> sessionmaker:
    """获取全局 Session 工厂"""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    获取数据库 Session 上下文管理器。

    用法:
        with get_session() as session:
            trades = session.query(TradeModel).filter(...).all()
            session.add(new_trade)
            # 自动 commit；异常自动 rollback
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """
    初始化数据库：创建所有表（如果不存在）。
    开发/测试环境直接调用；生产环境通过 Alembic 迁移。
    """
    from db.models import Base
    Base.metadata.create_all(get_engine())


def reset_db():
    """
    重置数据库（仅用于测试）：删除所有表后重建。
    """
    from db.models import Base
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())
