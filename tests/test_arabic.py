"""Tests for Arabic input handled in code (agent/arabic.py) and its use in identification. No LLM involved.

    python -m pytest tests -v
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import identity  # noqa: E402
from arabic import digits_only, hijri_text, parse_iso_date, to_ascii_digits  # noqa: E402
from db import QATAR_TZ, ClinicDB  # noqa: E402
from identity import CallIdentity  # noqa: E402

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=QATAR_TZ)


@pytest.mark.parametrize("text, digits", [
    ("٥٥١٢ ٣٤٥٦", "55123456"),                                   # Eastern Arabic digits
    ("۵۵۱۲۳۴۵۶", "55123456"),                                     # Persian digits
    ("خمسة خمسة واحد اثنين ثلاثة أربعة خمسة ستة", "55123456"),    # spoken digit words
    ("خمسه وخمسه، واحد إثنين", "5512"),                           # variants: ه, "و" prefix, إ
    ("ثنين تلاتة ثمانية تسعة صفر سبعة", "238907"),                # Gulf/Egyptian forms
    ("55 12 34 56", "55123456"),
    ("+974 5512 3456", "97455123456"),
])
def test_digits_in_any_form(text, digits):
    assert digits_only(text) == digits


def test_other_words_are_left_alone():
    assert to_ascii_digits("رقم ملفي ٤٨") == "رقم ملفي 48"


def test_hijri_dates_are_converted():
    assert parse_iso_date("1410-08-17") == (date(1990, 3, 14), (1410, 8, 17))
    assert parse_iso_date("١٤١٠-٠٨-١٧") == (date(1990, 3, 14), (1410, 8, 17))   # Eastern digits too
    assert hijri_text(1410, 8, 17) == "17 Sha'ban 1410 AH"


def test_gregorian_dates_stay_as_they_are():
    assert parse_iso_date("1990-03-14") == (date(1990, 3, 14), None)


@pytest.mark.parametrize("text", ["1410-13-01", "1410-08-31", "1990-02-30", "14/3/1990", ""])
def test_invalid_dates(text):
    assert parse_iso_date(text) is None


@pytest.fixture
def clinic(tmp_path):
    db = ClinicDB(tmp_path / "test.db")
    fatima = db.create_patient("Fatima Al-Naimi", date(1990, 3, 14), "5512 3456", now=NOW)
    return db, fatima


def test_verify_with_arabic_digit_words_and_an_arabic_name(clinic):
    db, fatima = clinic
    spoken = " ".join({"0": "صفر", "1": "واحد", "2": "اثنين", "3": "ثلاثة", "4": "أربعة", "5": "خمسة", "6": "ستة",
                       "7": "سبعة", "8": "ثمانية", "9": "تسعة"}[ch] for ch in fatima["file_number"])
    state = CallIdentity()
    msg = identity.verify(db, state, spoken, "فاطمة النعيمي", 1, now=NOW,
                          judge=lambda heard, names: 0 if heard == "فاطمة النعيمي" else None)
    assert "Fatima Al-Naimi" in msg   # read back as written on the file
    identity.confirm_patient(state, 2)
    assert state.patient_id == fatima["patient_id"]


def test_find_with_eastern_digits_and_a_hijri_birth_date(clinic):
    db, fatima = clinic
    assert identity.find(db, CallIdentity(), "1990-03-14", "٥٥١٢٣٤٥٦", now=NOW).startswith("Verified")
    assert identity.find(db, CallIdentity(), "1410-08-17", "55123456", now=NOW).startswith("Verified")


def test_registration_reads_back_a_hijri_date_as_given(clinic):
    msg = identity.prepare_registration(CallIdentity(), "Sara Mansour", "1422-06-21", "66001122", None,
                                        user_turns=1, now=NOW)
    assert "21 Jumada al-Akhirah 1422 AH" in msg and "that is 9 September 2001" in msg


def test_registration_needs_the_name_in_english_letters(clinic):
    state = CallIdentity()
    msg = identity.prepare_registration(state, "سارة منصور", "2001-09-09", "66001122", None, user_turns=1, now=NOW)
    assert "English letters" in msg and state.pending_registration is None
