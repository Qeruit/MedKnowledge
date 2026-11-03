"""Create an isolated local staging env using existing model credentials.

Never overwrites a staging env and never prints secrets. The database password
is new, even when the existing development database uses the same server.
"""

from pathlib import Path
import secrets

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "backend" / ".env"
LOCAL_OVERRIDE = ROOT / "backend" / ".env.local"
TEMPLATE = ROOT / "deploy" / "staging.env.example"
TARGET = ROOT / "deploy" / "staging.env"
MODEL_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "CUSTOMER_MODEL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS")


def main() -> None:
    if TARGET.exists():
        raise SystemExit("预发布配置已存在；不会覆盖 deploy/staging.env")
    source = {**dotenv_values(SOURCE), **dotenv_values(LOCAL_OVERRIDE)}
    if any(not source.get(key) for key in MODEL_KEYS):
        raise SystemExit("现有后端缺少模型配置；未创建预发布配置")
    replacements = {key: source[key] for key in MODEL_KEYS}
    replacements["POSTGRES_PASSWORD"] = secrets.token_hex(24)
    content = []
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0]
        content.append(f"{key}={replacements[key]}" if key in replacements else line)
    # Exclusive create protects an existing operator-managed secret file.
    with TARGET.open("x", encoding="utf-8") as output:
        output.write("\n".join(content) + "\n")
    print("已创建独立预发布配置 deploy/staging.env（密钥未输出）")


if __name__ == "__main__":
    main()
