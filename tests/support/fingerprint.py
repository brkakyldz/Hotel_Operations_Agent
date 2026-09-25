"""Fingerprint every business table enumerated from ORM metadata.

The no-forbidden-mutation assertion must cover every business table (all tables
except the named observation tables), so a table added later is covered automatically.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from hotel_operations.storage.db import Database
from hotel_operations.storage.models import business_tables


def business_fingerprint(db: Database) -> dict[str, str]:
    out: dict[str, str] = {}
    with db.read() as s:
        for table in business_tables():
            rows = s.execute(text(f'SELECT * FROM "{table}" ORDER BY 1')).all()  # noqa: S608
            payload = json.dumps([list(map(str, r)) for r in rows])
            out[table] = hashlib.sha256(payload.encode()).hexdigest()
    return out
