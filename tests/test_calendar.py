from datetime import date

from financial_agent.calendar import BusinessCalendar


def test_business_calendar_detects_weekends_and_observed_fixed_holidays():
    business_calendar = BusinessCalendar()

    assert business_calendar.is_weekend(date(2026, 7, 4))
    assert not business_calendar.is_business_day(date(2026, 7, 3))
    assert business_calendar.previous_business_day(date(2026, 7, 4)) == date(2026, 7, 2)
