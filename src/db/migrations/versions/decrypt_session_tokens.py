"""Decrypt session tokens in fragment_accounts

Revision ID: g7h8i9j0k1l2
Revises: f6g7h8i9j0k1
Create Date: 2026-01-24

Убираем шифрование для сессионных токенов Fragment.
Мнемоника остаётся зашифрованной.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'g7h8i9j0k1l2'
down_revision: Union[str, None] = 'f6g7h8i9j0k1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Добавляем новые колонки
    op.add_column('fragment_accounts', sa.Column('stel_token', sa.Text(), nullable=True))
    op.add_column('fragment_accounts', sa.Column('stel_ssid', sa.Text(), nullable=True))
    op.add_column('fragment_accounts', sa.Column('stel_ton_token', sa.Text(), nullable=True))

    # 2. Мигрируем данные (расшифровываем)
    from src.core.crypto import decrypt

    connection = op.get_bind()
    result = connection.execute(
        sa.text("SELECT id, stel_token_encrypted, stel_ssid_encrypted, stel_ton_token_encrypted FROM fragment_accounts")
    )

    for row in result:
        account_id = row[0]
        try:
            stel_token = decrypt(row[1]) if row[1] else None
            stel_ssid = decrypt(row[2]) if row[2] else None
            stel_ton_token = decrypt(row[3]) if row[3] else None

            connection.execute(
                sa.text("""
                    UPDATE fragment_accounts
                    SET stel_token = :stel_token,
                        stel_ssid = :stel_ssid,
                        stel_ton_token = :stel_ton_token
                    WHERE id = :id
                """),
                {
                    "id": account_id,
                    "stel_token": stel_token,
                    "stel_ssid": stel_ssid,
                    "stel_ton_token": stel_ton_token,
                }
            )
        except Exception as e:
            print(f"Warning: Failed to decrypt tokens for account {account_id}: {e}")

    # 3. Делаем новые колонки NOT NULL
    op.alter_column('fragment_accounts', 'stel_token', nullable=False)
    op.alter_column('fragment_accounts', 'stel_ssid', nullable=False)
    op.alter_column('fragment_accounts', 'stel_ton_token', nullable=False)

    # 4. Удаляем старые зашифрованные колонки
    op.drop_column('fragment_accounts', 'stel_token_encrypted')
    op.drop_column('fragment_accounts', 'stel_ssid_encrypted')
    op.drop_column('fragment_accounts', 'stel_ton_token_encrypted')


def downgrade() -> None:
    # 1. Добавляем обратно зашифрованные колонки
    op.add_column('fragment_accounts', sa.Column('stel_token_encrypted', sa.Text(), nullable=True))
    op.add_column('fragment_accounts', sa.Column('stel_ssid_encrypted', sa.Text(), nullable=True))
    op.add_column('fragment_accounts', sa.Column('stel_ton_token_encrypted', sa.Text(), nullable=True))

    # 2. Шифруем данные обратно
    from src.core.crypto import encrypt

    connection = op.get_bind()
    result = connection.execute(
        sa.text("SELECT id, stel_token, stel_ssid, stel_ton_token FROM fragment_accounts")
    )

    for row in result:
        account_id = row[0]
        stel_token_enc = encrypt(row[1]) if row[1] else None
        stel_ssid_enc = encrypt(row[2]) if row[2] else None
        stel_ton_token_enc = encrypt(row[3]) if row[3] else None

        connection.execute(
            sa.text("""
                UPDATE fragment_accounts
                SET stel_token_encrypted = :stel_token,
                    stel_ssid_encrypted = :stel_ssid,
                    stel_ton_token_encrypted = :stel_ton_token
                WHERE id = :id
            """),
            {
                "id": account_id,
                "stel_token": stel_token_enc,
                "stel_ssid": stel_ssid_enc,
                "stel_ton_token": stel_ton_token_enc,
            }
        )

    # 3. Делаем колонки NOT NULL
    op.alter_column('fragment_accounts', 'stel_token_encrypted', nullable=False)
    op.alter_column('fragment_accounts', 'stel_ssid_encrypted', nullable=False)
    op.alter_column('fragment_accounts', 'stel_ton_token_encrypted', nullable=False)

    # 4. Удаляем расшифрованные колонки
    op.drop_column('fragment_accounts', 'stel_token')
    op.drop_column('fragment_accounts', 'stel_ssid')
    op.drop_column('fragment_accounts', 'stel_ton_token')
