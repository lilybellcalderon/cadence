"""Single source of 'now'. Tests (and later the demo time-compression) swap it out; nothing else calls datetime.now()."""
from datetime import datetime, timedelta, timezone

_fixed: datetime | None = None


def now() -> datetime:
    return _fixed if _fixed is not None else datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ago(minutes: float, base: datetime | None = None) -> datetime:
    return (base or now()) - timedelta(minutes=minutes)


def ahead(hours: float, base: datetime | None = None) -> datetime:
    return (base or now()) + timedelta(hours=hours)


def freeze(dt: datetime | None) -> None:
    """Pin the clock (tests). Pass None to release."""
    global _fixed
    _fixed = dt
