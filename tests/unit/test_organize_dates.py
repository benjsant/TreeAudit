"""The date of a photo: taken (read from it, D-071), else in its name (D-056),
else its modification date."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from treeaudit.organize.plan import MODIFIED, NAME, TAKEN, _name_year, photo_date


def ns(year: int) -> int:
    return int(datetime(year, 6, 1, tzinfo=UTC).timestamp()) * 1_000_000_000


@pytest.mark.parametrize(
    ("name", "year"),
    [
        ("IMG_20230512_142113.jpg", 2023),
        ("PXL_20210704_101500123.jpg", 2021),
        ("Capture d'écran 2024-01-02 à 10.15.00.png", 2024),
        ("Screenshot_2019-07-14-08-30-12.png", 2019),
        ("VID_20180101_000001.mp4", 2018),
        ("2017_12_31 réveillon.jpg", 2017),
        ("WhatsApp Image 2022-03-04 at 12.00.00.jpeg", 2022),
        ("vacances 2015.07.20.jpg", 2015),
    ],
)
def test_a_full_date_in_the_name_gives_the_year(name: str, year: int) -> None:
    assert _name_year(name, 2026) == year


@pytest.mark.parametrize(
    "name",
    [
        "DSC_2019.jpg",  # a counter, not a date
        "IMG_20230001_1421.jpg",  # month 00
        "IMG_20231301.jpg",  # month 13
        "IMG_20230532.jpg",  # day 32
        "IMG_20230231.jpg",  # 31 February
        "IMG_2023-05_12.jpg",  # mixed separators
        "photo_120230512.jpg",  # more digits before: not a date
        "IMG_202305121.jpg",  # more digits after
        "scan_18990101.jpg",  # too old for a photo
        "vacances bretagne.jpg",
    ],
)
def test_no_date_in_the_name(name: str) -> None:
    assert _name_year(name, 2026) is None


def test_a_date_later_than_the_modification_is_not_believed() -> None:
    # A file cannot have been modified before it was taken.
    assert _name_year("IMG_20250101_000000.jpg", 2023) is None
    assert photo_date("IMG_20250101_000000.jpg", ns(2023), None) == (date(2023, 6, 1), MODIFIED)


def test_the_name_wins_over_the_date_of_a_copy() -> None:
    assert photo_date("IMG_20170812_101010.jpg", ns(2026), None) == (date(2017, 8, 12), NAME)
    assert photo_date("plage.jpg", ns(2026), None) == (date(2026, 6, 1), MODIFIED)


def test_a_broken_modification_date_still_lets_the_name_speak() -> None:
    assert photo_date("IMG_20170812_101010.jpg", 10**30, None) == (date(2017, 8, 12), NAME)
    assert photo_date("plage.jpg", 10**30, None) == (None, MODIFIED)
    assert photo_date("plage.jpg", 10**30, "2015-05-05 10:00:00") == (date(2015, 5, 5), TAKEN)


def test_the_date_taken_wins_over_the_name_and_the_file() -> None:
    # A photo renamed or sent later: the camera's date is the one it was taken.
    taken = "2019-06-01 12:00:00"
    assert photo_date("IMG_20190617_160000.jpg", ns(2026), taken) == (date(2019, 6, 1), TAKEN)
    assert photo_date("plage.jpg", ns(2026), taken) == (date(2019, 6, 1), TAKEN)


@pytest.mark.parametrize(
    "taken",
    [
        "1980-06-01 10:00:00",  # too old for a photo
        "2000-01-01 00:00:00",  # a camera whose clock was never set
        "2004-01-01 00:00:00",
        "2000-01-01 00:00:00x",  # not as stored
        "2027-03-01 10:00:00",  # later than the modification
        "2023-02-30 10:00:00",  # impossible
    ],
)
def test_an_implausible_date_taken_is_not_believed(taken: str) -> None:
    assert photo_date("IMG_20170812_101010.jpg", ns(2026), taken) == (date(2017, 8, 12), NAME)
    assert photo_date("plage.jpg", ns(2026), taken) == (date(2026, 6, 1), MODIFIED)


def test_new_year_photos_are_believed() -> None:
    assert photo_date("plage.jpg", ns(2026), "2020-01-01 00:00:07") == (date(2020, 1, 1), TAKEN)
