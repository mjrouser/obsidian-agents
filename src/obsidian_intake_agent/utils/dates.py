from __future__ import annotations

from datetime import date, datetime


def monday_of_week(value: date | datetime | None) -> date:
    if value is None:
        current = date.today()
    elif isinstance(value, datetime):
        current = value.date()
    else:
        current = value
    return current.fromordinal(current.toordinal() - current.weekday())
