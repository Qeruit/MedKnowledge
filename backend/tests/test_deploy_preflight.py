from types import SimpleNamespace

from scripts import deploy_preflight


def staging_settings(**overrides):
    values = {
        "source_database_url": "postgresql+psycopg://medknowledge:" + "a" * 48 + "@db:5432/postgres",
        "database_name": "medknowledge_stage",
        "llm_base_url": "https://provider.invalid/v1",
        "llm_api_key": "test-key-not-for-real-requests",
        "customer_model": "chat-model",
        "embedding_model": "embedding-model",
        "cors_origins": ("http://127.0.0.1:8088",),
        "auth_cookie_secure": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_valid_isolated_staging_settings(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "a" * 48)
    monkeypatch.setattr(deploy_preflight, "settings", staging_settings())
    assert deploy_preflight.check() == []


def test_refuses_original_database_and_placeholder_secrets(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "replace_with_password")
    monkeypatch.setattr(deploy_preflight, "settings", staging_settings(
        database_name="agent_customer_service", llm_api_key="replace-me"))
    errors = deploy_preflight.check()
    assert any("medknowledge_stage" in item for item in errors)
    assert any("示例值" in item for item in errors)
    assert any("POSTGRES_PASSWORD" in item for item in errors)


def test_refuses_public_http_and_insecure_https_cookie(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "a" * 48)
    monkeypatch.setattr(deploy_preflight, "settings", staging_settings(
        cors_origins=("http://example.net",)))
    assert any("HTTP 只允许本机" in item for item in deploy_preflight.check())
    monkeypatch.setattr(deploy_preflight, "settings", staging_settings(
        cors_origins=("https://example.net",)))
    assert any("AUTH_COOKIE_SECURE" in item for item in deploy_preflight.check())
