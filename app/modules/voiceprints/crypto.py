"""AES-GCM binds ciphertext to the account, record, purpose and model version."""
import base64
import json
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TemplateCipher:
    def __init__(self, encoded_key: str):
        key = base64.b64decode(encoded_key, validate=True)
        if len(key) != 32:
            raise ValueError('voiceprint_key_must_be_32_bytes')
        self.aes = AESGCM(key)

    @staticmethod
    def aad(owner, record, model, key_id):
        return json.dumps([owner, record, model, key_id], separators=(',', ':')).encode()

    def encrypt(self, value, *, owner, record, model, key_id):
        nonce = os.urandom(12)
        raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
        return nonce + self.aes.encrypt(nonce, raw, self.aad(owner, record, model, key_id))

    def decrypt(self, value, *, owner, record, model, key_id):
        return json.loads(self.aes.decrypt(value[:12], value[12:], self.aad(owner, record, model, key_id)))
