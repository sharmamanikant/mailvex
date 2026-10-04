from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken


class CredentialStore:
    """Encrypts provider credentials before persistence; plaintext never leaves this boundary."""

    def __init__(self, key: str) -> None:
        digest = hashlib.sha256(key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, credentials: dict[str, object]) -> str:
        return self._fernet.encrypt(json.dumps(credentials, separators=(",", ":")).encode()).decode()

    def decrypt(self, encrypted: str) -> dict[str, object]:
        try:
            value = self._fernet.decrypt(encrypted.encode())
        except InvalidToken as exc:
            raise ValueError("Unable to decrypt provider credentials") from exc
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError("Invalid provider credential payload")
        return result
