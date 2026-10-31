"""仅初始化本项目的本地配置；已存在的 .env 绝不覆盖。"""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    print("Local .env already exists; unchanged.")
else:
    app_password, admin_password, signing_key = (secrets.token_hex(32) for _ in range(3))
    template = (root / ".env.example").read_text(encoding="utf-8")
    content = (template.replace("REPLACE_APP_PASSWORD", app_password)
               .replace("REPLACE_ADMIN_PASSWORD", admin_password)
               .replace("REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS", signing_key))
    # 不在终端输出密钥、连接串或用户凭证。
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    print("Created local .env with independent random credentials; values hidden.")
