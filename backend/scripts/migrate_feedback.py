"""Add feedback tables/evidence column only. Safe to repeat; no job reconciliation."""
from sqlalchemy import create_engine, text
from app.config import settings, PROJECT_ROOT


def main():
    assert settings.database_name != 'postgres', 'Do not migrate the server maintenance database'
    schema = (PROJECT_ROOT / 'schema.sql').read_text(encoding='utf-8')
    start = schema.index('ALTER TABLE t_chat_turn ADD COLUMN IF NOT EXISTS evidence_snapshot JSON;')
    end = schema.index('CREATE TABLE IF NOT EXISTS t_ops_alert', start)
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext('feedback-schema-v1'))"))
            db.exec_driver_sql(schema[start:end])
        print('Feedback schema ready: additive tables and nullable evidence column only.')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
