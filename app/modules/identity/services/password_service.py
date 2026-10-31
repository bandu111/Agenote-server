"""密码哈希与验证；不存在的账户也执行哈希校验。"""
import secrets
from pwdlib import PasswordHash

password_hasher = PasswordHash.recommended()
dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))
