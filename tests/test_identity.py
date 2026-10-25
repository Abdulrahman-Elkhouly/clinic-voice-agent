"""Tests for the identification rules (agent/identity.py). The name judge is replaced by a fake,
so these are instant and deterministic; the real judge has its own tests in test_name_judge.py.

    python -m pytest tests -v
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import identity  # noqa: E402
from db import QATAR_TZ, ClinicDB, luhn_check_digit  # noqa: E402
from identity import CallIdentity  # noqa: E402
from name_judge import NameJudgeError  # noqa: E402

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=QATAR_TZ)


def judge_says(full_name_on_file: str | None):
    """A fake judge that 'matches' the given name on file (or nothing), and records what it was shown."""
    def judge(heard, candidates):
        judge.seen.append((heard, list(candidates)))
        return candidates.index(full_name_on_file) if full_name_on_file in candidates else None
    judge.seen = []
    return judge


def broken_judge(heard, candidates):
    raise NameJudgeError("timeout")


def no_judge_expected(heard, candidates):
    raise AssertionError("the judge should not be called here")

@pytest.fixture
def clinic(tmp_path):
    db = ClinicDB(tmp_path / "test.db")
    fatima = db.create_patient("Fatima Al-Naimi", date(1990, 3, 14), "5512 3456", now=NOW)
    twin1 = db.create_patient("Ahmed Ali", date(1992, 2, 2), "7712 0000", now=NOW)
    twin2 = db.create_patient("Omar Ali", date(1992, 2, 2), "7712 0000", now=NOW)
    return {"db": db, "fatima": fatima, "twin1": twin1, "twin2": twin2}


def valid_unknown_file_number() -> str:
    base = "11111"
    return base + luhn_check_digit(base)


def verify(clinic_or_db, state, file_number, name, turns=1, judge=None, now=NOW):
    db = clinic_or_db["db"] if isinstance(clinic_or_db, dict) else clinic_or_db
    return identity.verify(db, state, file_number, name, turns, now=now, judge=judge or judge_says(name))


def test_verify_reads_back_the_name_on_the_file_then_confirms(clinic):
    state = CallIdentity()
    judge = judge_says("Fatima Al-Naimi")
    msg = identity.verify(clinic["db"], state, clinic["fatima"]["file_number"], "فاطمة النعيمي", 1, now=NOW,
                          judge=judge)
    assert "Fatima Al-Naimi" in msg and "confirm_patient" in msg   # the name ON FILE, not the caller's version
    assert judge.seen == [("فاطمة النعيمي", ["Fatima Al-Naimi"])]
    assert state.patient_id is None                                # not yet: the caller must confirm
    assert "hasn't answered" in identity.confirm_patient(state, 1)   # same turn: refused
    msg = identity.confirm_patient(state, 2)
    assert msg.startswith("Confirmed") and "Fatima" in msg and clinic["fatima"]["file_number"] not in msg
    assert state.patient_id == clinic["fatima"]["patient_id"]


def test_confirm_with_nothing_waiting(clinic):
    assert "No file is waiting" in identity.confirm_patient(CallIdentity(), 5)


def test_wrong_name_and_unknown_file_give_the_same_message(clinic):
    s1, s2 = CallIdentity(), CallIdentity()
    wrong_name = identity.verify(clinic["db"], s1, clinic["fatima"]["file_number"], "Huda Saleh", 1, now=NOW,
                                 judge=judge_says(None))
    unknown = identity.verify(clinic["db"], s2, valid_unknown_file_number(), "Fatima Al-Naimi", 1, now=NOW,
                              judge=no_judge_expected)
    assert wrong_name == unknown   # never reveals whether the file exists
    assert "don't match" in wrong_name and s1.pending_patient is None and s1.patient_id is None


def test_a_first_name_alone_is_asked_again_without_counting(clinic):
    state = CallIdentity()
    msg = identity.verify(clinic["db"], state, clinic["fatima"]["file_number"], "Fatima", 1, now=NOW,
                          judge=no_judge_expected)
    assert "full name" in msg and state.failed_attempts == 0


def test_a_failed_judge_never_guesses_and_reveals_nothing(clinic):
    s1, s2 = CallIdentity(), CallIdentity()
    broken = identity.verify(clinic["db"], s1, clinic["fatima"]["file_number"], "Fatima Al-Naimi", 1, now=NOW,
                             judge=broken_judge)
    unknown = identity.verify(clinic["db"], s2, valid_unknown_file_number(), "Fatima Al-Naimi", 1, now=NOW,
                              judge=no_judge_expected)
    assert broken == unknown and s1.pending_patient is None


def test_check_digit_failure_does_not_count_as_an_attempt(clinic):
    state = CallIdentity()
    good = clinic["fatima"]["file_number"]
    misheard = good[:-1] + str((int(good[-1]) + 1) % 10)
    msg = identity.verify(clinic["db"], state, misheard, "Fatima Al-Naimi", 1, now=NOW, judge=no_judge_expected)
    assert "repeat" in msg and state.failed_attempts == 0


def test_three_failures_lock_the_call_even_for_correct_details(clinic):
    db, state = clinic["db"], CallIdentity()
    unknown = valid_unknown_file_number()
    assert "2 more times" in verify(db, state, unknown, "Fatima Al-Naimi")
    assert "1 more time" in verify(db, state, unknown, "Fatima Al-Naimi")
    assert "locked" in verify(db, state, unknown, "Fatima Al-Naimi")
    assert state.locked
    msg = verify(db, state, clinic["fatima"]["file_number"], "Fatima Al-Naimi")
    assert "locked" in msg and state.pending_patient is None
    assert "locked" in identity.find(db, state, "1990-03-14", "55123456", now=NOW)


def test_five_failures_across_calls_lock_the_file(tmp_path):
    db = ClinicDB(tmp_path / "lock.db")
    p = db.create_patient("Fatima Al-Naimi", date(1990, 3, 14), "5512 3456", now=NOW)
    for call in range(5):   # five separate calls, one wrong name each
        identity.verify(db, CallIdentity(), p["file_number"], f"Wrong Name{call}", 1,
                        now=NOW + timedelta(minutes=call), judge=judge_says(None))
    msg = verify(db, CallIdentity(), p["file_number"], "Fatima Al-Naimi", now=NOW + timedelta(minutes=10))
    assert "isn't possible" in msg   # even the right name is refused until reception unlocks it
    db.unlock_verification(p["file_number"], now=NOW + timedelta(minutes=11))
    msg = verify(db, CallIdentity(), p["file_number"], "Fatima Al-Naimi", now=NOW + timedelta(minutes=12))
    assert "Fatima Al-Naimi" in msg and "confirm_patient" in msg


@pytest.mark.parametrize("dob", ["2030-01-01", "1850-01-01", "14/03/1990", ""])
def test_impossible_or_malformed_dates_are_rejected_without_counting(clinic, dob):
    state = CallIdentity()
    msg = identity.find(clinic["db"], state, dob, "55123456", now=NOW, judge=no_judge_expected)
    assert "YYYY-MM-DD" in msg and state.failed_attempts == 0


def test_find_by_dob_and_phone_reads_the_file_number(clinic):
    state = CallIdentity()
    msg = identity.find(clinic["db"], state, "1990-03-14", "+974 5512 3456", now=NOW, judge=no_judge_expected)
    assert msg.startswith("Verified") and " ".join(clinic["fatima"]["file_number"]) in msg
    assert state.patient_id == clinic["fatima"]["patient_id"]


def test_find_no_match_counts_and_reveals_nothing(clinic):
    state = CallIdentity()
    msg = identity.find(clinic["db"], state, "1990-03-14", "5500 0000", now=NOW)
    assert "don't match" in msg and state.failed_attempts == 1


def test_find_twins_asks_for_the_name_then_asks_the_judge(clinic):
    db, state = clinic["db"], CallIdentity()
    msg = identity.find(db, state, "1992-02-02", "77120000", now=NOW, judge=no_judge_expected)
    assert "full name" in msg and state.patient_id is None and state.failed_attempts == 0
    judge = judge_says("Ahmed Ali")
    msg = identity.find(db, state, "1992-02-02", "77120000", full_name="Ahmad Aly", now=NOW, judge=judge)
    assert msg.startswith("Verified") and state.patient_id == clinic["twin1"]["patient_id"]
    assert judge.seen == [("Ahmad Aly", ["Ahmed Ali", "Omar Ali"])]
    assert "Omar" not in msg   # the other twin's name never reaches the conversation


def test_find_twins_with_no_judged_match_reveals_nothing(clinic):
    state = CallIdentity()
    msg = identity.find(clinic["db"], state, "1992-02-02", "77120000", full_name="Khalid Ali", now=NOW,
                        judge=judge_says(None))
    assert "can't confirm" in msg and "transfer" in msg and state.patient_id is None
    assert "Ahmed" not in msg and "Omar" not in msg


def test_find_when_the_judge_fails_never_guesses(clinic):
    state = CallIdentity()
    msg = identity.find(clinic["db"], state, "1992-02-02", "77120000", full_name="Ahmed Ali", now=NOW,
                        judge=broken_judge)
    assert "can't confirm" in msg and state.patient_id is None


def test_register_creates_one_file_and_reads_the_number(clinic):
    db, state = clinic["db"], CallIdentity()
    msg = identity.register(db, state, "Sara Mansour", "2001-09-09", "6600 1122", now=NOW, judge=no_judge_expected)
    assert msg.startswith("Registered") and state.patient_id is not None
    assert len(db.find_by_dob_phone(date(2001, 9, 9), "66001122")) == 1


def test_register_an_existing_patient_with_a_different_spelling_creates_no_duplicate(clinic):
    db, state = clinic["db"], CallIdentity()
    msg = identity.register(db, state, "Fatma Alnaimi", "1990-03-14", "5512 3456", now=NOW,
                            judge=judge_says("Fatima Al-Naimi"))
    assert "already has a file" in msg and state.patient_id == clinic["fatima"]["patient_id"]
    assert len(db.find_by_dob_phone(date(1990, 3, 14), "55123456")) == 1


def test_register_a_twin_with_a_different_name_creates_a_new_file(clinic):
    db, state = clinic["db"], CallIdentity()
    msg = identity.register(db, state, "Yusuf Ali", "1992-02-02", "7712 0000", now=NOW, judge=judge_says(None))
    assert msg.startswith("Registered")
    assert len(db.find_by_dob_phone(date(1992, 2, 2), "77120000")) == 3


def test_register_when_the_judge_fails_creates_nothing(clinic):
    db, state = clinic["db"], CallIdentity()
    msg = identity.register(db, state, "Yusuf Ali", "1992-02-02", "7712 0000", now=NOW, judge=broken_judge)
    assert "can't be completed" in msg and state.patient_id is None
    assert len(db.find_by_dob_phone(date(1992, 2, 2), "77120000")) == 2


@pytest.mark.parametrize("name, dob, phone", [
    ("", "2001-09-09", "6600 1122"),
    ("Sara Mansour", "2030-01-01", "6600 1122"),
    ("Sara Mansour", "2001-09-09", "6600"),
])
def test_register_with_invalid_details_creates_nothing(clinic, name, dob, phone):
    db, state = clinic["db"], CallIdentity()
    msg = identity.register(db, state, name, dob, phone, now=NOW)
    assert not msg.startswith("Registered") and state.patient_id is None
    assert db.find_by_dob_phone(date(2001, 9, 9), "66001122") == []


def test_registration_needs_the_caller_to_speak_after_the_read_back(clinic):
    db, state = clinic["db"], CallIdentity()
    msg = identity.prepare_registration(state, "Sara Mansour", "2001-09-09", "6600 1122", None, user_turns=1, now=NOW)
    assert "Nothing is registered yet" in msg and "S-A-R-A, M-A-N-S-O-U-R" in msg and "9 September 2001" in msg
    # The model tries to confirm in the same turn: refused, nothing created
    assert "hasn't confirmed" in identity.confirm_registration(db, state, user_turns=1, now=NOW)
    assert db.find_by_dob_phone(date(2001, 9, 9), "66001122") == []
    # The caller answered (a new user turn): created exactly once
    assert identity.confirm_registration(db, state, user_turns=2, now=NOW, judge=no_judge_expected) \
        .startswith("Registered")
    assert "no registration waiting" in identity.confirm_registration(db, state, user_turns=3, now=NOW)
    assert len(db.find_by_dob_phone(date(2001, 9, 9), "66001122")) == 1


def test_correcting_details_replaces_the_pending_registration(clinic):
    db, state = clinic["db"], CallIdentity()
    identity.prepare_registration(state, "Sara Mansor", "2001-09-09", "6600 1122", None, user_turns=1, now=NOW)
    identity.prepare_registration(state, "Sara Mansour", "2001-09-09", "6600 1122", None, user_turns=2, now=NOW)
    identity.confirm_registration(db, state, user_turns=3, now=NOW, judge=no_judge_expected)
    [p] = db.find_by_dob_phone(date(2001, 9, 9), "66001122")
    assert p["full_name"] == "Sara Mansour"


def test_switching_patient_is_announced(clinic):
    db, state = clinic["db"], CallIdentity()
    verify(db, state, clinic["fatima"]["file_number"], "Fatima Al-Naimi", turns=1)
    identity.confirm_patient(state, 2)
    verify(db, state, clinic["twin1"]["file_number"], "Ahmed Ali", turns=3)
    msg = identity.confirm_patient(state, 4)
    assert "active patient is now" in msg and state.patient_id == clinic["twin1"]["patient_id"]
