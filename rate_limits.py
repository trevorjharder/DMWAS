from datetime import datetime, timezone

from sqlalchemy import case, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from extensions import db


def account_quota_retry_after(model, user_id, quota, *, period_seconds=60, bucket=None):
    """Consume a fixed-window account quota across sessions and processes."""
    timestamp = int(datetime.now(timezone.utc).timestamp())
    window = timestamp // period_seconds
    insert = pg_insert if db.engine.dialect.name == "postgresql" else sqlite_insert
    key = {"user_id": user_id}
    if bucket is not None:
        key["bucket"] = bucket
    statement = insert(model).values(**key, window_number=window, requests=1)
    statement = statement.on_conflict_do_update(
        index_elements=list(key),
        set_={"window_number": window, "requests": case(
            (model.window_number == window, model.requests + 1), else_=1)},
        where=or_(model.window_number != window, model.requests < quota),
    ).returning(model.requests)
    allowed = db.session.execute(statement).scalar_one_or_none()
    db.session.commit()
    return period_seconds - timestamp % period_seconds if allowed is None else None
