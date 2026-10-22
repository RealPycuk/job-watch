"""Шифрование ключей GigaChat пользователей в БД (Fernet: AES-128-CBC + HMAC-SHA256)."""
from cryptography.fernet import Fernet, InvalidToken


class Vault:
    def __init__(self, master_key: str):
        if not master_key:
            raise ValueError("JOBWATCH_SECRET не задан — сгенерируйте: python -c "
                             "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")
        self.f = Fernet(master_key.encode())

    def encrypt(self, value: str) -> str:
        return self.f.encrypt(value.encode()).decode()

    def decrypt(self, token: str | None) -> str:
        if not token:
            return ""
        try:
            return self.f.decrypt(token.encode()).decode()
        except InvalidToken:
            return ""
