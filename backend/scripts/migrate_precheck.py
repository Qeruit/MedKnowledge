"""Add only the new Agent table/indexes to this product's dedicated database."""
from sqlalchemy import create_engine
from app.config import PROJECT_ROOT, settings


def main():
    if settings.database_name in ("postgres", "template0", "template1"):
        raise RuntimeError("Refuse system database migration")
    source = (PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8")
    marker = "-- Bounded model-directed prechecks."
    if source.count(marker) != 1:
        raise RuntimeError("Agent migration section ambiguous")
    sql = source[source.index(marker):]
    if "CREATE TABLE IF NOT EXISTS t_precheck_task" not in sql or "DROP " in sql.upper():
        raise RuntimeError("Unexpected migration")
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
    finally:
        engine.dispose()
    print("Agent table/indexes ready; existing documents/reviews/grants unchanged.")


if __name__ == "__main__":
    main()
