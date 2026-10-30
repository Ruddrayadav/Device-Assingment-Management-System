from datetime import datetime, date, time
from typing import Union, Optional

def parse_dt(val: Union[datetime, date, time, str, None]) -> Optional[Union[datetime, time]]:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, time):
        return val
    if isinstance(val, date):
        return datetime.combine(val, time.min)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        # Try ISO format
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
            "%H:%M:%S",
            "%H:%M",
            "%Y-%m-%d %I:%M:%S %p",
            "%Y-%m-%d %I:%M %p",
            "%I:%M:%S %p",
            "%I:%M %p",
        ):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(val)
        except Exception:
            return None
    return None

def fmt_time(dt: Union[datetime, date, time, str, None], seconds: bool = True) -> str:
    parsed = parse_dt(dt)
    if parsed is None:
        return ""
    pattern = "%I:%M:%S %p" if seconds else "%I:%M %p"
    return parsed.strftime(pattern)

def fmt_datetime(dt: Union[datetime, date, time, str, None], seconds: bool = True) -> str:
    parsed = parse_dt(dt)
    if parsed is None:
        return ""
    pattern = "%Y-%m-%d %I:%M:%S %p" if seconds else "%Y-%m-%d %I:%M %p"
    return parsed.strftime(pattern)

