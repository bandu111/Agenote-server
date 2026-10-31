"""Provision a local-only encryption key without ever printing it or replacing one."""
import base64
import os
from pathlib import Path
from dotenv import dotenv_values, set_key

root = Path(__file__).resolve().parents[1]
env = root / '.env'
if not env.is_file():
    raise SystemExit('Existing backend .env required; configure the database first.')
values = dotenv_values(env)
if not values.get('VOICEPRINT_KEY'):
    set_key(env, 'VOICEPRINT_KEY', base64.b64encode(os.urandom(32)).decode())
print('Local voiceprint key configured. Key contents are intentionally hidden.')
print('Keep .env separate from database dumps. Do not overwrite this key while templates exist.')
