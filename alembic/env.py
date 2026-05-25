"""
Alembic 环境配置
支持 online（连数据库）和 offline（生成 SQL 文件）两种模式。
自动从环境变量读取 DATABASE_URL。
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# 让 alembic 能找到项目根目录的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import Base

# Alembic Config 对象
config = context.config

# 从环境变量覆盖数据库 URL
database_url = os.environ.get('DATABASE_URL', '')
if database_url:
    config.set_main_option('sqlalchemy.url', database_url)

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLAlchemy MetaData（Alembic autogenerate 需要）
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    离线模式：生成 SQL 脚本而不连接数据库。
    用于 DBA 审核或无法直连数据库的环境。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    在线模式：连接数据库执行迁移。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
