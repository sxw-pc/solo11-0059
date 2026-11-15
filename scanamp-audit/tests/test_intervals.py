from scanamp_audit.expr.intervals import (
    clamp, contains, intersects, numeric_overlap_fraction, overlap_fraction, sortable_number,
)

def test_intersects_and_contains():
    assert intersects(1, 5, 5, 9)
    assert not intersects(1, 4, 5, 9)
    assert contains(1, 5, 3) and not contains(1, 5, 6)

def test_overlap_fraction():
    assert overlap_fraction(0, 10, 0, 5) == 0.5
    assert overlap_fraction(0, 10, 20, 30) == 0.0
    assert overlap_fraction(3, 3, 1, 5) == 1.0
    assert clamp(1.5) == 1.0

def test_date_overlap_and_sortable_number():
    assert numeric_overlap_fraction("2024-01-01", "2024-01-09",
                                    "2024-01-05", "2024-01-20") == 0.5
    assert sortable_number("not-a-date") is None
    assert sortable_number("2024-01-01") < sortable_number("2024-02-01")
