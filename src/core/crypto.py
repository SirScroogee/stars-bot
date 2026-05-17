"""
Модуль шифрования для защиты чувствительных данных.

Использует Fernet (симметричное шифрование) из библиотеки cryptography.
Ключ шифрования задаётся через переменную окружения ENCRYPTION_KEY (обязательно).
"""
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class EncryptionError(Exception):
    """Ошибка шифрования/расшифровки."""
    pass


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """
    Получить экземпляр Fernet с ключом из переменной окружения.

    ENCRYPTION_KEY обязателен. Сгенерируйте его с помощью:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    """
    encryption_key = os.getenv("ENCRYPTION_KEY")

    if not encryption_key:
        raise EncryptionError(
            "ENCRYPTION_KEY не задан! Сгенерируйте ключ командой:\n"
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )

    try:
        return Fernet(encryption_key.encode())
    except Exception as e:
        raise EncryptionError(f"Неверный формат ключа шифрования: {e}")


def encrypt(plaintext: str) -> str:
    """
    Зашифровать строку.

    Args:
        plaintext: Исходная строка

    Returns:
        Зашифрованная строка в base64

    Raises:
        EncryptionError: Если шифрование не удалось
    """
    if not plaintext:
        return ""

    try:
        fernet = _get_fernet()
        encrypted = fernet.encrypt(plaintext.encode())
        return encrypted.decode()
    except Exception as e:
        raise EncryptionError(f"Ошибка шифрования: {e}")


def decrypt(ciphertext: str) -> str:
    """
    Расшифровать строку.

    Args:
        ciphertext: Зашифрованная строка в base64

    Returns:
        Расшифрованная строка

    Raises:
        EncryptionError: Если расшифровка не удалась
    """
    if not ciphertext:
        return ""

    try:
        fernet = _get_fernet()
        decrypted = fernet.decrypt(ciphertext.encode())
        return decrypted.decode()
    except InvalidToken:
        raise EncryptionError("Неверный токен или ключ шифрования")
    except Exception as e:
        raise EncryptionError(f"Ошибка расшифровки: {e}")


def generate_encryption_key() -> str:
    """
    Сгенерировать новый ключ шифрования.

    Этот ключ нужно сохранить в переменную ENCRYPTION_KEY.

    Returns:
        Новый ключ в формате Fernet (base64)
    """
    return Fernet.generate_key().decode()


def encrypt_mnemonic(mnemonic: list[str]) -> str:
    """
    Зашифровать мнемонику кошелька.

    Args:
        mnemonic: Список из 24 слов мнемоники

    Returns:
        Зашифрованная строка
    """
    return encrypt(" ".join(mnemonic))


def decrypt_mnemonic(encrypted: str) -> list[str]:
    """
    Расшифровать мнемонику кошелька.

    Args:
        encrypted: Зашифрованная строка

    Returns:
        Список из 24 слов мнемоники
    """
    decrypted = decrypt(encrypted)
    return decrypted.split()
