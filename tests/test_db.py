"""Tests for agent/db.py: availability, holds, double booking, rescheduling, and the rules db.py enforces.

    python -m pytest tests -v
"""
import multiprocessing as mp
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import db as dbmod  # noqa: E402
from db import (QATAR_TZ, ClinicDB, HoldExpired, NotAllowed, NotFound, PatientBusy,  # noqa: E402
                SlotTaken, SlotUnavailable, is_valid_file_number, luhn_check_digit)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=QATAR_TZ)   # a Monday morning
SAT_17 = date(2026, 10, 17)


def at(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=QATAR_TZ)


@pytest.fixture
def clinic(tmp_path):
    db = ClinicDB(tmp_path / "test.db")
    benali = db.add_doctor("Dr. Omar Benali", "orthopedics", "male", ["Arabic", "English", "French"])
    for wd in (SAT, MON, WED):
        db.add_schedule(benali, wd, "09:00", "15:00", 20, date(2026, 1, 1))
    santos = db.add_doctor("Dr. Maria Santos", "family medicine", "female", ["English", "Tagalog"])
    db.add_schedule(santos, SUN, "15:00", "22:00", 15, date(2026, 1, 1), last_start="21:30")
    p1 = db.create_patient("Fatima Al-Naimi", date(1990, 3, 14), "5512 3456", now=NOW)
    p2 = db.create_patient("Mohammed Al-Kaabi", date(1978, 11, 2), "3345 6789", now=NOW)
    return {"db": db, "benali": benali, "santos": santos,
            "p1": p1["patient_id"], "p2": p2["patient_id"], "file1": p1["file_number"]}


# --- File numbers ---

def test_generated_file_numbers_pass_the_check_digit(clinic):
    assert is_valid_file_number(clinic["file1"])
    assert len(clinic["file1"]) == 6


def test_a_single_misheard_digit_fails_the_check():
    base = "48213"
    good = base + luhn_check_digit(base)
    for i in range(len(good)):
        for wrong in "0123456789":
            if wrong != good[i]:
                assert not is_valid_file_number(good[:i] + wrong + good[i + 1:])


# --- Availability: schedule - exceptions - taken - window ---

def test_slot_grid_follows_the_weekly_schedule(clinic):
    slots = clinic["db"].free_slots(clinic["benali"], SAT_17, SAT_17, now=NOW)
    starts = [s["start_at"].strftime("%H:%M") for s in slots]
    assert len(starts) == 18 and starts[0] == "09:00" and starts[-1] == "14:40"


def test_no_slots_on_a_day_the_doctor_does_not_work(clinic):
    assert clinic["db"].free_slots(clinic["benali"], date(2026, 10, 18), date(2026, 10, 18), now=NOW) == []


def test_last_start_is_respected(clinic):
    sunday = date(2026, 10, 18)
    slots = clinic["db"].free_slots(clinic["santos"], sunday, sunday, now=NOW)
    assert slots[-1]["start_at"] == at(sunday, "21:30")


def test_leave_and_clinic_closure_remove_the_day(clinic):
    db = clinic["db"]
    db.add_exception(SAT_17, SAT_17, "leave", doctor_id=clinic["benali"])
    assert db.free_slots(clinic["benali"], SAT_17, SAT_17, now=NOW) == []
    wed = date(2026, 10, 21)
    db.add_exception(wed, wed, "closed")   # whole clinic
    assert db.free_slots(clinic["benali"], wed, wed, now=NOW) == []


def test_special_hours_narrow_the_day(clinic):
    db = clinic["db"]
    db.add_exception(SAT_17, SAT_17, "special_hours", doctor_id=clinic["benali"],
                     start_time="09:00", end_time="11:00")
    starts = [s["start_at"].strftime("%H:%M") for s in db.free_slots(clinic["benali"], SAT_17, SAT_17, now=NOW)]
    assert starts == ["09:00", "09:20", "09:40", "10:00", "10:20", "10:40"]


def test_schedule_change_takes_effect_on_its_date(clinic):
    db = clinic["db"]
    doc = db.add_doctor("Dr. Test", "dermatology", "female", ["English"])
    db.add_schedule(doc, SAT, "09:00", "12:00", 30, date(2026, 1, 1), valid_to=date(2026, 10, 23))
    db.add_schedule(doc, SAT, "14:00", "17:00", 30, date(2026, 10, 24))
    assert db.free_slots(doc, SAT_17, SAT_17, now=NOW)[0]["start_at"] == at(SAT_17, "09:00")
    sat_24 = date(2026, 10, 24)
    assert db.free_slots(doc, sat_24, sat_24, now=NOW)[0]["start_at"] == at(sat_24, "14:00")


def test_minimum_notice_and_horizon(clinic):
    db = clinic["db"]
    now = at(SAT_17, "09:10")
    first = db.free_slots(clinic["benali"], SAT_17, SAT_17, now=now)[0]
    assert first["start_at"] == at(SAT_17, "09:40")   # 09:00 is past, 09:20 is inside the 30-min notice
    far = (NOW + timedelta(days=62)).date()
    assert db.free_slots(clinic["benali"], far, far + timedelta(days=7), now=NOW) == []


def test_booked_and_held_slots_are_not_offered(clinic):
    db = clinic["db"]
    db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "09:00"), "reception", now=NOW)
    db.hold_slot(clinic["p2"], clinic["benali"], at(SAT_17, "09:20"), call_id="call-B", now=NOW)
    starts = [s["start_at"] for s in db.free_slots(clinic["benali"], SAT_17, SAT_17, now=NOW)]
    assert at(SAT_17, "09:00") not in starts and at(SAT_17, "09:20") not in starts
    assert len(starts) == 16


# --- Holds ---

def test_a_held_slot_cannot_be_taken_by_someone_else(clinic):
    db = clinic["db"]
    db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-A", now=NOW)
    with pytest.raises(SlotTaken):
        db.hold_slot(clinic["p2"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-B", now=NOW)
    with pytest.raises(SlotTaken):
        db.book_slot(clinic["p2"], clinic["benali"], at(SAT_17, "10:00"), "whatsapp", now=NOW)


def test_an_expired_hold_frees_the_slot(clinic):
    db = clinic["db"]
    hold = db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-A", now=NOW)
    later = NOW + timedelta(minutes=6)
    assert at(SAT_17, "10:00") in [s["start_at"] for s in db.free_slots(clinic["benali"], SAT_17, SAT_17, now=later)]
    db.hold_slot(clinic["p2"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-B", now=later)
    with pytest.raises(HoldExpired):
        db.confirm_hold(hold["appointment_id"], now=later)


def test_hanging_up_releases_the_hold(clinic):
    db = clinic["db"]
    db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-A", now=NOW)
    assert db.release_hold(call_id="call-A", now=NOW) == 1
    db.hold_slot(clinic["p2"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-B", now=NOW)


def test_one_hold_per_call(clinic):
    db = clinic["db"]
    db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-A", now=NOW)
    db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "11:00"), call_id="call-A", now=NOW)
    starts = [s["start_at"] for s in db.free_slots(clinic["benali"], SAT_17, SAT_17, now=NOW)]
    assert at(SAT_17, "10:00") in starts and at(SAT_17, "11:00") not in starts


def test_confirming_a_hold_books_it(clinic):
    db = clinic["db"]
    hold = db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), call_id="call-A", now=NOW)
    db.confirm_hold(hold["appointment_id"], now=NOW + timedelta(minutes=1))
    [appt] = db.list_appointments(clinic["p1"], now=NOW)
    assert appt["status"] == "booked" and appt["doctor_name"] == "Dr. Omar Benali"


# --- Slot validation ---

@pytest.mark.parametrize("start", ["10:10", "08:40", "15:00"])   # off the grid, before hours, at closing
def test_times_that_are_not_real_slots_are_rejected(clinic, start):
    with pytest.raises(SlotUnavailable):
        clinic["db"].book_slot(clinic["p1"], clinic["benali"], at(SAT_17, start), "reception", now=NOW)


def test_naive_datetimes_are_rejected(clinic):
    with pytest.raises(ValueError):
        clinic["db"].book_slot(clinic["p1"], clinic["benali"], datetime(2026, 10, 17, 10, 0), "reception", now=NOW)


def test_a_patient_cannot_be_in_two_places_at_once(clinic):
    db = clinic["db"]
    db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)
    other = db.add_doctor("Dr. Other", "dermatology", "female", ["English"])
    db.add_schedule(other, SAT, "09:00", "15:00", 30, date(2026, 1, 1))
    with pytest.raises(PatientBusy):
        db.book_slot(clinic["p1"], other, at(SAT_17, "10:00"), "reception", now=NOW)


# --- Cancel and reschedule ---

def test_cannot_cancel_a_past_or_started_appointment(clinic):
    db = clinic["db"]
    appt = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)
    with pytest.raises(NotAllowed):
        db.cancel_appointment(appt["appointment_id"], clinic["p1"], now=at(SAT_17, "10:00"))
    with pytest.raises(NotAllowed):
        db.cancel_appointment(appt["appointment_id"], clinic["p1"], now=at(SAT_17, "12:00"))


def test_cannot_change_a_completed_appointment(clinic):
    db = clinic["db"]
    appt = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)
    db.mark_outcome(appt["appointment_id"], "completed", now=at(SAT_17, "11:00"))
    with pytest.raises(NotAllowed):
        db.cancel_appointment(appt["appointment_id"], clinic["p1"], now=at(SAT_17, "11:00"))


def test_cannot_cancel_another_patients_appointment(clinic):
    db = clinic["db"]
    appt = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)
    with pytest.raises(NotFound):
        db.cancel_appointment(appt["appointment_id"], clinic["p2"], now=NOW)


def test_cancel_within_four_hours_is_a_late_change(clinic):
    db = clinic["db"]
    appt = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "13:00"), "reception", now=NOW)
    assert db.cancel_appointment(appt["appointment_id"], clinic["p1"], now=at(SAT_17, "10:00"))["late_change"]
    appt2 = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "14:00"), "reception", now=NOW)
    assert not db.cancel_appointment(appt2["appointment_id"], clinic["p1"], now=NOW)["late_change"]


def test_cancelled_slot_can_be_booked_again(clinic):
    db = clinic["db"]
    appt = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)
    db.cancel_appointment(appt["appointment_id"], clinic["p1"], now=NOW)
    db.book_slot(clinic["p2"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)


def test_reschedule_moves_the_booking(clinic):
    db = clinic["db"]
    old = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)
    hold = db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "12:00"), call_id="call-A",
                        replaces=old["appointment_id"], now=NOW)
    db.reschedule(old["appointment_id"], clinic["p1"], hold["appointment_id"], now=NOW)
    [appt] = db.list_appointments(clinic["p1"], now=NOW)
    assert appt["start_at"] == at(SAT_17, "12:00")


def test_failed_reschedule_keeps_the_old_appointment(clinic):
    db = clinic["db"]
    old = db.book_slot(clinic["p1"], clinic["benali"], at(SAT_17, "10:00"), "reception", now=NOW)
    hold = db.hold_slot(clinic["p1"], clinic["benali"], at(SAT_17, "12:00"), call_id="call-A",
                        replaces=old["appointment_id"], now=NOW)
    with pytest.raises(HoldExpired):   # the caller took too long; the hold ran out
        db.reschedule(old["appointment_id"], clinic["p1"], hold["appointment_id"], now=NOW + timedelta(minutes=10))
    [appt] = db.list_appointments(clinic["p1"], now=NOW)
    assert appt["start_at"] == at(SAT_17, "10:00") and appt["status"] == "booked"


# --- Patients ---

def test_verification_needs_both_file_number_and_dob(clinic):
    db = clinic["db"]
    assert db.verify_patient(clinic["file1"], date(1990, 3, 14))["patient_id"] == clinic["p1"]
    assert db.verify_patient(clinic["file1"], date(1990, 3, 15)) is None


def test_find_by_details_ignores_case_and_spacing(clinic):
    db = clinic["db"]
    assert len(db.find_by_details("  fatima   al-naimi ", date(1990, 3, 14))) == 1
    assert db.find_by_details("Fatima Al-Naimi", date(1990, 3, 14), phone="0000 0000") == []


# --- Two callers, same slot, same moment (separate processes, like separate calls) ---

def _race_worker(path, start_iso, patient_id, doctor_id, barrier, results):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
    from db import ClinicDB as DB, SlotTaken as Taken
    db = DB(path)
    barrier.wait()   # both processes fire at the same moment
    try:
        db.hold_slot(patient_id, doctor_id, datetime.fromisoformat(start_iso), call_id=f"call-{patient_id}", now=NOW)
        results.put("won")
    except Taken:
        results.put("taken")


def test_simultaneous_holds_exactly_one_wins(clinic):
    ctx = mp.get_context("spawn")
    for start in ("09:00", "09:20", "09:40", "10:00", "10:20"):
        barrier, results = ctx.Barrier(2), ctx.Queue()
        procs = [ctx.Process(target=_race_worker,
                             args=(str(clinic["db"].path), at(SAT_17, start).isoformat(), p, clinic["benali"],
                                   barrier, results))
                 for p in (clinic["p1"], clinic["p2"])]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        outcome = sorted(results.get(timeout=5) for _ in procs)
        assert outcome == ["taken", "won"], f"{start}: {outcome}"
        clinic["db"].release_hold(call_id=f"call-{clinic['p1']}", now=NOW)
        clinic["db"].release_hold(call_id=f"call-{clinic['p2']}", now=NOW)


# --- The knowledge base / database consistency check ---

def test_consistency_check_agrees_and_catches_a_difference(tmp_path, monkeypatch):
    import check_schedules
    import seed_db
    db = ClinicDB(tmp_path / "seeded.db")
    for name, specialty, gender, languages, schedule in seed_db.DOCTORS:
        doc = db.add_doctor(name, specialty, gender, languages)
        for weekdays, start, end in schedule:
            for wd in weekdays:
                db.add_schedule(doc, wd, start, end, 15, date(2026, 1, 1))
    monkeypatch.setattr(check_schedules, "now_qatar", lambda: NOW)
    assert check_schedules.kb_doctors() == check_schedules.db_doctors(db)

    benali = next(d for d in db.list_doctors() if d["name"] == "Dr. Omar Benali")
    db.add_schedule(benali["doctor_id"], THU, "09:00", "15:00", 20, date(2026, 1, 1))   # not in the KB
    assert check_schedules.kb_doctors() != check_schedules.db_doctors(db)
