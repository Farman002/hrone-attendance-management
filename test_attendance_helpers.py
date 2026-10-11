
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.main import (
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    IST,
)

# Test 1: Late arrival with 10-minute grace period
punch = datetime(2026, 10, 12, 9, 40, tzinfo=IST)
assert compute_late_minutes(punch, "09:30") == 0

punch = datetime(2026, 10, 12, 9, 41, tzinfo=IST)
assert compute_late_minutes(punch, "09:30") == 11

# Test 2: Work hours
start = datetime(2026, 10, 12, 9, 30, tzinfo=IST)
end = datetime(2026, 10, 12, 18, 0, tzinfo=IST)
assert compute_work_hours(start, end) == 8.5

# Test 3: Daytime overtime threshold
punch_out = datetime(2026, 10, 12, 19, 0, tzinfo=IST)
assert compute_overtime(
    punch_out, "09:30", "18:30", "2026-10-12"
) == 30

punch_out = datetime(2026, 10, 12, 18, 59, tzinfo=IST)
assert compute_overtime(
    punch_out, "09:30", "18:30", "2026-10-12"
) == 0

# Test 4: Overnight overtime
punch_out = datetime(2026, 10, 13, 6, 30, tzinfo=IST)
assert compute_overtime(
    punch_out, "22:00", "06:00", "2026-10-12"
) == 30

print("All helper tests passed!")
