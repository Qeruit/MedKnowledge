"""Reject unsafe or example-only settings before a staging migration."""

import os
import re
from urllib.parse import urlparse

from sqlalchemy.engine import make_url

from app.config import settings


def check() -> list[str]:
    errors = []
    password = os.getenv("POSTGRES_PASSWORD", "")
    if not re.fullmatch(r"[0-9a-f]{48,128}", password):
        errors.append("POSTGRES_PASSWORD 应为至少 48 位的随机十六进制字符串")
    try:
        source = make_url(settings.source_database_url)
        if (source.host, source.username, source.database) != ("db", "medknowledge", "postgres"):
            errors.append("DATABASE_URL 必须指向此 Compose 的 db/medknowledge/postgres")
        if source.password != password:
            errors.append("数据库连接密码与 POSTGRES_PASSWORD 不一致")
    except Exception:
        errors.append("DATABASE_URL 无效")
    if settings.database_name != "medknowledge_stage":
        errors.append("预发布库名必须为 medknowledge_stage，防止误操作现有业务库")
    model_fields = (settings.llm_base_url, settings.llm_api_key,
                    settings.customer_model, settings.embedding_model)
    if any(not value or "replace" in value.lower() or "example" in value.lower() for value in model_fields):
        errors.append("模型服务地址、密钥或模型名仍是示例值")
    endpoint = urlparse(settings.llm_base_url)
    if endpoint.scheme != "https" and not (endpoint.scheme == "http" and endpoint.hostname in {
            "host.docker.internal", "localhost", "127.0.0.1"}):
        errors.append("外部模型接口应使用 HTTPS；本机模型仅允许明确的本机地址")
    origins = settings.cors_origins
    if len(origins) != 1:
        errors.append("预发布环境应设置唯一、准确的浏览器来源 CORS_ORIGINS")
    else:
        origin = urlparse(origins[0])
        if origin.scheme == "https" and not settings.auth_cookie_secure:
            errors.append("HTTPS 部署必须设置 AUTH_COOKIE_SECURE=true")
        if origin.scheme == "http" and (settings.auth_cookie_secure or origin.hostname not in {
                "localhost", "127.0.0.1"}):
            errors.append("HTTP 只允许本机演练，且 AUTH_COOKIE_SECURE=false")
        if origin.scheme not in {"http", "https"} or not origin.hostname or origin.path not in {"", "/"}:
            errors.append("CORS_ORIGINS 必须是单个完整站点来源，不含路径")
    return errors


def main():
    errors = check()
    if errors:
        for item in errors:
            print(f"预发布配置错误：{item}")
        raise SystemExit(1)
    print("预发布配置检查通过（不包含供应商连通性、证书或临床安全验证）")


if __name__ == "__main__":
    main()
