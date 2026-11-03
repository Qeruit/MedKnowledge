from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy.engine import make_url


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env.local", override=True)


@dataclass(frozen=True)
class Settings:
    # Keep this product in its own database even when credentials are reused.
    source_database_url: str = os.getenv("DATABASE_URL", "")  #返回DATABASE_URL，没有就返回“”，getenv返回的是字符串
    database_name: str = os.getenv("PROJECT_DB_NAME", "agent_customer_service")
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    auth_cookie_secure: bool = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true" #转小写，然后判断是否等于"true"
    auth_session_hours: int = int(os.getenv("AUTH_SESSION_HOURS", "12"))
    local_demo_allow_self_approval: bool = os.getenv("LOCAL_DEMO_ALLOW_SELF_APPROVAL", "false").lower() == "true"
    llm_base_url: str = os.getenv("LLM_BASE_URL", "").rstrip("/") #删除末尾斜杠
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    customer_model: str = os.getenv("CUSTOMER_MODEL", "deepseek-v3")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
    chat_requests_per_minute: int = int(os.getenv("CHAT_REQUESTS_PER_MINUTE", "30"))
    chat_global_concurrency: int = int(os.getenv("CHAT_GLOBAL_CONCURRENCY", "20"))
    chat_user_concurrency: int = int(os.getenv("CHAT_USER_CONCURRENCY", "2"))
    chat_daily_token_limit: int = int(os.getenv("CHAT_DAILY_TOKEN_LIMIT", "100000"))
    chat_monthly_token_limit: int = int(os.getenv("CHAT_MONTHLY_TOKEN_LIMIT", "1000000"))
    chat_max_prompt_tokens: int = int(os.getenv("CHAT_MAX_PROMPT_TOKENS", "12000"))
    chat_max_output_tokens: int = int(os.getenv("CHAT_MAX_OUTPUT_TOKENS", "2048"))
    chat_input_cost_per_million: float = float(os.getenv("CHAT_INPUT_COST_PER_MILLION", "0"))
    chat_output_cost_per_million: float = float(os.getenv("CHAT_OUTPUT_COST_PER_MILLION", "0"))
    embedding_cost_per_million: float = float(os.getenv("EMBEDDING_COST_PER_MILLION", "0"))
    rag_splitter_enabled: bool = os.getenv("RAG_SPLITTER_ENABLED", "true").lower() == "true"
    rag_chunk_tokens: int = int(os.getenv("RAG_CHUNK_TOKENS", "800"))
    rag_overlap_tokens: int = int(os.getenv("RAG_OVERLAP_TOKENS", "80"))
    rag_token_encoding: str = os.getenv("RAG_TOKEN_ENCODING", "cl100k_base")
    tika_server_url: str = os.getenv("TIKA_SERVER_URL", "").rstrip("/")
    tika_text_path: str = os.getenv("TIKA_TEXT_PATH", "/tika")
    document_max_pages: int = int(os.getenv("DOCUMENT_MAX_PAGES", "1000"))
    document_max_chars: int = int(os.getenv("DOCUMENT_MAX_CHARS", "10000000"))
    task_max_attempts: int = int(os.getenv("TASK_MAX_ATTEMPTS", "3"))
    task_lease_seconds: int = int(os.getenv("TASK_LEASE_SECONDS", "90"))
    task_heartbeat_seconds: int = int(os.getenv("TASK_HEARTBEAT_SECONDS", "20"))
    task_timeout_seconds: int = int(os.getenv("TASK_TIMEOUT_SECONDS", "1800"))
    task_retry_seconds: int = int(os.getenv("TASK_RETRY_SECONDS", "15"))
    task_poll_seconds: float = float(os.getenv("TASK_POLL_SECONDS", "2"))
    monitor_window_seconds: int = int(os.getenv("MONITOR_WINDOW_SECONDS", "300"))
    monitor_interval_seconds: int = int(os.getenv("MONITOR_INTERVAL_SECONDS", "15"))
    alert_hold_seconds: int = int(os.getenv("ALERT_HOLD_SECONDS", "30"))
    alert_queue_age_seconds: int = int(os.getenv("ALERT_QUEUE_AGE_SECONDS", "600"))
    alert_model_min_calls: int = int(os.getenv("ALERT_MODEL_MIN_CALLS", "3"))
    alert_model_error_ratio: float = float(os.getenv("ALERT_MODEL_ERROR_RATIO", "0.5"))
    backup_root: Path = Path(os.getenv("BACKUP_ROOT", str(PROJECT_ROOT / "backups"))).resolve()
    backup_pg_container: str = os.getenv("BACKUP_PG_CONTAINER", "")
    backup_interval_hours: int = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
    backup_retry_seconds: int = int(os.getenv("BACKUP_RETRY_SECONDS", "3600"))
    backup_keep_count: int = int(os.getenv("BACKUP_KEEP_COUNT", "7"))
    backup_lock_timeout_seconds: int = int(os.getenv("BACKUP_LOCK_TIMEOUT_SECONDS", "120"))
    storage_lock_timeout_seconds: int = int(os.getenv("STORAGE_LOCK_TIMEOUT_SECONDS", "5"))

    def __post_init__(self):
        if self.local_demo_allow_self_approval:
            origins = [urlsplit(origin) for origin in self.cors_origins]
            if len(origins) != 1 or any(origin.scheme != "http" or origin.hostname not in
                    {"localhost", "127.0.0.1", "::1"} or origin.username or origin.password
                    or origin.path not in {"", "/"} or origin.query or origin.fragment for origin in origins):
                raise ValueError("单管理员例外审批仅允许单一本机 HTTP 演示来源；正式部署必须关闭 LOCAL_DEMO_ALLOW_SELF_APPROVAL")
        if not (self.chat_requests_per_minute > 0 and self.chat_global_concurrency > 0
                and self.chat_user_concurrency > 0 and self.chat_daily_token_limit > 0
                and self.chat_monthly_token_limit > 0 and self.chat_max_prompt_tokens > 0
                and self.chat_max_output_tokens > 0 and self.chat_input_cost_per_million >= 0
                and self.chat_output_cost_per_million >= 0 and self.embedding_cost_per_million >= 0
                and self.chat_user_concurrency <= self.chat_global_concurrency
                and self.chat_daily_token_limit >= self.chat_max_output_tokens
                and self.chat_monthly_token_limit >= self.chat_daily_token_limit):
            raise ValueError("问答限流、Token 限额或计价配置无效")
        if not (self.monitor_window_seconds >= 10 and self.monitor_interval_seconds >= 1
                and self.alert_hold_seconds >= 0 and self.alert_queue_age_seconds >= 1
                and self.alert_model_min_calls >= 1 and 0 < self.alert_model_error_ratio <= 1
                and self.backup_interval_hours >= 1 and self.backup_retry_seconds >= 1
                and self.backup_keep_count >= 1 and self.backup_lock_timeout_seconds >= 1
                and self.storage_lock_timeout_seconds >= 1):
            raise ValueError("监控或备份配置无效")
        if not (1 <= self.task_max_attempts <= 10 and 1 <= self.task_heartbeat_seconds < self.task_lease_seconds / 2
                and self.task_timeout_seconds >= self.task_lease_seconds and self.task_retry_seconds >= 1
                and self.task_poll_seconds >= 0.1):
            raise ValueError("任务配置无效：续租间隔应小于租约的一半，执行时限不小于租约")
        if self.rag_chunk_tokens < 128 or not 0 <= self.rag_overlap_tokens < self.rag_chunk_tokens // 2:
            raise ValueError("RAG_CHUNK_TOKENS 必须 >=128，重叠 Token 必须小于块大小的一半")
    md_storage_path: Path = Path(os.getenv("MD_STORAGE_PATH", str(PROJECT_ROOT / "storage" / "md"))).resolve()
    cors_origins: tuple[str, ...] = tuple(
        origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5174,http://127.0.0.1:5174").split(",") if origin.strip()
    )

    @property
    def database_url(self) -> str:
        if not self.source_database_url:
            return ""
        return make_url(self.source_database_url).set(database=self.database_name).render_as_string(hide_password=False)


settings = Settings()
