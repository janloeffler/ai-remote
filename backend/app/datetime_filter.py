from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BERLIN_TZ = ZoneInfo("Europe/Berlin")
_WEEKDAYS = {
    "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_de_datetime(iso_timestamp: str | None, now: datetime | None = None) -> str:
    return format_datetime(iso_timestamp, "de", now)


def format_datetime(iso_timestamp: str | None, lang: str = "de", now: datetime | None = None) -> str:
    lang = lang if lang in _WEEKDAYS else "en"
    if not iso_timestamp:
        return "unbekannt" if lang == "de" else "unknown"
    try:
        moment = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return "unbekannt" if lang == "de" else "unknown"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    local_moment = moment.astimezone(BERLIN_TZ)
    local_reference = reference.astimezone(BERLIN_TZ)

    weekday = _WEEKDAYS[lang][local_moment.weekday()]
    same_year = local_moment.year == local_reference.year
    if lang == "de":
        date_part = f"{local_moment.day:02d}.{local_moment.month:02d}."
        if not same_year:
            date_part += str(local_moment.year)
    else:
        date_part = f"{_MONTHS_EN[local_moment.month - 1]} {local_moment.day}"
        if not same_year:
            date_part += f", {local_moment.year}"
    time_part = f"{local_moment.hour:02d}:{local_moment.minute:02d}"

    relative = _format_relative(reference - moment, lang)
    return f"{weekday}, {date_part} {time_part} ({relative})"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many.format(n=n)


def _format_relative(delta, lang: str = "de") -> str:
    # Clamp future timestamps (negative deltas) to zero so they render as "just now"
    # deliberately, not as a side effect of the < 60 check catching a large negative number.
    seconds = max(delta.total_seconds(), 0)
    if lang == "en":
        if seconds < 60:
            return "just now"
        minutes = int(seconds // 60)
        if minutes < 60:
            return _plural(minutes, "1 minute ago", "{n} minutes ago")
        hours = minutes // 60
        if hours < 24:
            return _plural(hours, "1 hour ago", "{n} hours ago")
        days = hours // 24
        if days < 7:
            return _plural(days, "1 day ago", "{n} days ago")
        if days < 31:
            return _plural(days // 7, "1 week ago", "{n} weeks ago")
        if days < 365:
            return _plural(days // 30, "1 month ago", "{n} months ago")
        return _plural(days // 365, "1 year ago", "{n} years ago")
    if seconds < 60:
        return "gerade eben"
    minutes = int(seconds // 60)
    if minutes < 60:
        return _plural(minutes, "vor 1 Minute", "vor {n} Minuten")
    hours = minutes // 60
    if hours < 24:
        return _plural(hours, "vor 1 Stunde", "vor {n} Stunden")
    days = hours // 24
    if days < 7:
        return _plural(days, "vor 1 Tag", "vor {n} Tagen")
    if days < 31:
        return _plural(days // 7, "vor 1 Woche", "vor {n} Wochen")
    if days < 365:
        return _plural(days // 30, "vor 1 Monat", "vor {n} Monaten")
    return _plural(days // 365, "vor 1 Jahr", "vor {n} Jahren")
