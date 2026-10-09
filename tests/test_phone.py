import pytest

from app.phone import InvalidPhone, mask, normalize


@pytest.mark.parametrize("raw", [
    "9876543210",
    " 9876543210 ",
    "919876543210",
    "+919876543210",
    "+91 98765 43210",
    "+91-98765-43210",
    "(+91) 98765.43210",
    "0091 9876543210",
    "09876543210",
    9876543210,
])
def test_valid_forms_normalise_to_91_without_plus(raw):
    assert normalize(raw) == "919876543210"


def test_ten_digits_starting_with_91_are_a_national_number():
    assert normalize("9198765432") == "919198765432"


@pytest.mark.parametrize("raw, reason", [
    (None, "empty"),
    ("", "empty"),
    ("   ", "empty"),
    ("98765abc10", "digits"),
    ("98765+43210", "digits"),
    ("123456", "too short"),
    ("98765432", "too short"),
    ("12345678901", "unsupported country code"),
    ("91987654321", "wrong length"),
    ("9198765432101", "wrong length"),
    ("+44 7911 123456", "unsupported country code"),
    ("1234567890", "not an Indian mobile"),
    ("5876543210", "not an Indian mobile"),
    ("+91 5876543210", "not an Indian mobile"),
])
def test_invalid_numbers_are_rejected_with_a_reason(raw, reason):
    with pytest.raises(InvalidPhone, match=reason):
        normalize(raw)


def test_output_never_contains_plus():
    assert "+" not in normalize("+91 98765 43210")


def test_mask_keeps_only_edges():
    assert mask("919876543210") == "91XXXXXXX210"
    assert mask(None) == ""
    assert mask("123") == "***"
