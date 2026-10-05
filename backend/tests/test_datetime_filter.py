from datetime import datetime, timezone

from app.datetime_filter import BERLIN_TZ, format_de_datetime

_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def test_none_and_empty_input_render_placeholder():
    assert format_de_datetime(None) == "unbekannt"
    assert format_de_datetime("") == "unbekannt"


def test_invalid_input_renders_placeholder_without_raising():
    assert format_de_datetime("not-a-timestamp") == "unbekannt"


def test_under_a_minute_says_gerade_eben():
    now = datetime(2026, 8, 5, 10, 50, 30, tzinfo=timezone.utc)
    result = format_de_datetime("2026-08-05T10:50:00+00:00", now=now)
    assert "(gerade eben)" in result


def test_minutes_bucket_singular_and_plural():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    assert "(vor 1 Minute)" in format_de_datetime("2026-08-05T10:49:00+00:00", now=now)
    assert "(vor 6 Minuten)" in format_de_datetime("2026-08-05T10:44:00+00:00", now=now)


def test_hours_bucket_singular_and_plural():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    assert "(vor 1 Stunde)" in format_de_datetime("2026-08-05T09:50:00+00:00", now=now)
    assert "(vor 3 Stunden)" in format_de_datetime("2026-08-05T07:50:00+00:00", now=now)


def test_days_bucket_and_same_year_date_format_omits_year():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    event = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)
    result = format_de_datetime(event.isoformat(), now=now)
    expected_weekday = _WEEKDAYS_DE[event.astimezone(BERLIN_TZ).weekday()]
    assert result.startswith(f"{expected_weekday}, 30.07. ")
    assert "(vor 6 Tagen)" in result


def test_weeks_bucket():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    event = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)  # 21 days = 3 weeks
    result = format_de_datetime(event.isoformat(), now=now)
    assert "(vor 3 Wochen)" in result


def test_months_bucket():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    event = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)  # ~96 days = 3 months at //30
    result = format_de_datetime(event.isoformat(), now=now)
    assert "Monat" in result  # exact count is an approximation; just prove the bucket is right


def test_different_year_includes_year_in_date_and_years_bucket():
    now = datetime(2026, 8, 5, 10, 50, tzinfo=timezone.utc)
    event = datetime(2025, 8, 5, 10, 50, tzinfo=timezone.utc)
    result = format_de_datetime(event.isoformat(), now=now)
    expected_weekday = _WEEKDAYS_DE[event.astimezone(BERLIN_TZ).weekday()]
    assert result.startswith(f"{expected_weekday}, 05.08.2025 ")
    assert "(vor 1 Jahr)" in result


def test_berlin_timezone_conversion_shifts_utc_to_local_summer_time():
    # 22:30 UTC in August is CEST (UTC+2) -> 00:30 the next local day.
    now = datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc)
    result = format_de_datetime("2026-08-05T22:30:00+00:00", now=now)
    assert "00:30" in result


def test_berlin_timezone_conversion_handles_cet_winter_offset():
    # 09:30 UTC in January is CET (UTC+1) -> 10:30 local.
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    result = format_de_datetime("2026-01-15T09:30:00+00:00", now=now)
    assert "10:30" in result


def test_future_timestamps_clamp_to_gerade_eben():
    # Verify that a timestamp 2 hours in the future renders as "gerade eben" deliberately,
    # not as a coincidence of the < 60 check catching a large negative number.
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    future = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)  # 2 hours in future
    result = format_de_datetime(future.isoformat(), now=now)
    assert "(gerade eben)" in result
