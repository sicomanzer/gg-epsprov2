from datetime import datetime


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

