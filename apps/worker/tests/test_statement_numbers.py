from rastro_worker.statement_numbers import same_numbers, statement_numbers


def test_integers_decimals_percentages_and_units_are_read() -> None:
    found = statement_numbers("Allow 90 days, a 2.5% fee and 24h of follow-up; 2,5 again")

    assert found == {"90": 1, "2.5": 2, "24": 1}


def test_notation_is_normalised() -> None:
    assert same_numbers("fee of 2.50", "fee of 2,5")
    assert same_numbers("up to 024 items", "up to 24 items")
    assert statement_numbers("100 and 1.0") == {"100": 1, "1": 1}


def test_a_different_number_or_count_differs() -> None:
    assert not same_numbers("at most 90 days", "at most 60 days")
    assert not same_numbers("1 and 2", "1 and 1 and 2")
    assert same_numbers("1 then 2", "2 then 1")  # multiset: order does not matter


def test_no_numbers_on_either_side_is_a_match_and_one_side_is_not() -> None:
    assert same_numbers("No bookings on Fridays", "Fridays are closed")
    assert not same_numbers("No bookings on Fridays", "At most 3 bookings on Fridays")
