"""Add strategy and direction columns to candidates table

Revision ID: 2026_05_26_002
Revises: 2026_05_25_001
Create Date: 2026-05-26

Changes:
  - Add 'strategy' column (VARCHAR(64), NOT NULL, default 'short_overbought')
  - Add 'direction' column (VARCHAR(8), NOT NULL, default 'SHORT')
  - Add 'score' column (FLOAT, default 0.0)
  - Add 'metadata_json' column (TEXT, nullable)
  - Drop old unique constraint on symbol alone
  - Add new composite unique index (symbol, strategy)
  - Add indexes on strategy, direction
  - Backfill existing rows with strategy='short_overbought', direction='SHORT'
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '2026_05_26_002'
down_revision = '2026_05_25_001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add new columns with defaults (so existing rows get backfilled)
    op.add_column('candidates', sa.Column(
        'strategy', sa.String(64), nullable=False,
        server_default='short_overbought'
    ))
    op.add_column('candidates', sa.Column(
        'direction', sa.String(8), nullable=False,
        server_default='SHORT'
    ))
    op.add_column('candidates', sa.Column(
        'score', sa.Float(), nullable=True, server_default='0.0'
    ))
    op.add_column('candidates', sa.Column(
        'metadata_json', sa.Text(), nullable=True
    ))

    # 2. Make yao_score nullable (it's short_overbought specific)
    op.alter_column('candidates', 'yao_score',
                    existing_type=sa.Integer(),
                    nullable=True)

    # 3. Drop old unique constraint on symbol
    # SQLite doesn't support DROP CONSTRAINT, so we handle both dialects
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        # For SQLite, we need to recreate the table (batch mode)
        with op.batch_alter_table('candidates') as batch_op:
            # Remove old unique index on symbol if exists
            try:
                batch_op.drop_index('ix_candidates_symbol')
            except Exception:
                pass
            try:
                batch_op.drop_constraint('uq_candidates_symbol', type_='unique')
            except Exception:
                pass
    else:
        # PostgreSQL / MySQL
        try:
            op.drop_index('ix_candidates_symbol', table_name='candidates')
        except Exception:
            pass
        try:
            op.drop_constraint('uq_candidates_symbol', 'candidates', type_='unique')
        except Exception:
            pass

    # 4. Create new composite unique index
    op.create_index(
        'ix_candidates_symbol_strategy',
        'candidates',
        ['symbol', 'strategy'],
        unique=True
    )
    op.create_index('ix_candidates_strategy', 'candidates', ['strategy'])
    op.create_index('ix_candidates_direction', 'candidates', ['direction'])


def downgrade() -> None:
    # Drop new indexes
    op.drop_index('ix_candidates_direction', table_name='candidates')
    op.drop_index('ix_candidates_strategy', table_name='candidates')
    op.drop_index('ix_candidates_symbol_strategy', table_name='candidates')

    # Restore old unique index on symbol
    op.create_index('ix_candidates_symbol', 'candidates', ['symbol'], unique=True)

    # Drop new columns
    op.drop_column('candidates', 'metadata_json')
    op.drop_column('candidates', 'score')
    op.drop_column('candidates', 'direction')
    op.drop_column('candidates', 'strategy')

    # Make yao_score non-nullable again
    op.alter_column('candidates', 'yao_score',
                    existing_type=sa.Integer(),
                    nullable=False, server_default='0')
