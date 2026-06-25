from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache


DateValue = date | str

MONDAY = 0
THURSDAY = 3

FIXED_HOLIDAYS = (
    (1, 1),
    (6, 19),
    (7, 4),
    (11, 11),
    (12, 25),
)


@dataclass(frozen=True, init=False)
class BusinessCalendar:
    extra_closure_dates: frozenset[date]

    def __init__(self, extra_closure_dates: Iterable[DateValue] | None = None) -> None:
        object.__setattr__(
            self,
            "extra_closure_dates",
            frozenset(_coerce_date(value) for value in extra_closure_dates or ()),
        )

    def with_extra_closure_dates(
        self, extra_closure_dates: Iterable[DateValue] | None
    ) -> BusinessCalendar:
        if not extra_closure_dates:
            return self
        return BusinessCalendar([*self.extra_closure_dates, *extra_closure_dates])

    def is_weekend(self, value: date) -> bool:
        return value.weekday() >= 5

    def is_business_day(self, value: date) -> bool:
        return (
            not self.is_weekend(value)
            and value not in _us_holiday_dates(value.year)
            and value not in self.extra_closure_dates
        )

    def previous_business_day(self, value: date) -> date:
        adjusted = value
        while not self.is_business_day(adjusted):
            adjusted -= timedelta(days=1)
        return adjusted


@cache
def _us_holiday_dates(year: int) -> frozenset[date]:
    holidays: set[date] = set()
    for holiday_year in (year - 1, year, year + 1):
        for month, day in FIXED_HOLIDAYS:
            observed = _observed_fixed_holiday(date(holiday_year, month, day))
            if observed.year == year:
                holidays.add(observed)

    holidays.add(_nth_weekday(year, 1, MONDAY, 3))
    holidays.add(_nth_weekday(year, 2, MONDAY, 3))
    holidays.add(_last_weekday(year, 5, MONDAY))
    holidays.add(_nth_weekday(year, 9, MONDAY, 1))
    holidays.add(_nth_weekday(year, 10, MONDAY, 2))
    holidays.add(_nth_weekday(year, 11, THURSDAY, 4))
    return frozenset(holidays)


def _observed_fixed_holiday(holiday: date) -> date:
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first_day = date(year, month, 1)
    days_until_weekday = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_weekday + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        value = date(year, 12, 31)
    else:
        value = date(year, month + 1, 1) - timedelta(days=1)
    while value.weekday() != weekday:
        value -= timedelta(days=1)
    return value


def _coerce_date(value: DateValue) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)
