r"""Builds the mock clinic database with fake doctors, schedules, patients and appointments.

    python agent\seed_db.py           # create data/clinic.db (refuses if it exists)
    python agent\seed_db.py --reset   # delete and rebuild it

The doctor list mirrors knowledge_base/doctors_and_specialties.md. The database is the source
of truth for booking; run check_schedules.py to confirm the two still agree.
Appointments are placed relative to today, so the data never goes stale.
"""
import random
import sys
from datetime import date, timedelta

from db import DB_PATH, ClinicDB, now_qatar

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
SAT_TO_WED = [SAT, SUN, MON, TUE, WED]
SUN_TO_THU = [SUN, MON, TUE, WED, THU]
SCHEDULES_FROM = date(2026, 1, 1)
CLOSING_LAST_START = "21:30"   # the last appointment starts 30 min before the clinic closes at 22:00

# Appointment length per specialty, in minutes (fictional)
SLOT_MINUTES = {
    "family medicine": 15, "pediatrics": 15, "internal medicine": 20,
    "obstetrics and gynecology": 30, "orthopedics": 20, "dermatology": 20, "ENT": 20,
}

# (name, specialty, gender, languages, [(weekdays, start, end), ...])
DOCTORS = [
    ("Dr. Khalid Al-Mahmoud", "family medicine", "male", ["Arabic", "English"], [(SAT_TO_WED, "08:00", "14:00")]),
    ("Dr. Maria Santos", "family medicine", "female", ["English", "Tagalog"], [(SUN_TO_THU, "15:00", "22:00")]),
    ("Dr. Imran Qureshi", "family medicine", "male", ["English", "Urdu", "Hindi"],
     [([FRI], "16:00", "22:00"), ([SAT, MON], "15:00", "22:00")]),
    ("Dr. Layla Haddad", "pediatrics", "female", ["Arabic", "English", "French"], [(SAT_TO_WED, "08:00", "14:00")]),
    ("Dr. Ahmed Fathy", "pediatrics", "male", ["Arabic", "English"], [(SUN_TO_THU, "15:00", "21:00")]),
    ("Dr. Samir Nasser", "internal medicine", "male", ["Arabic", "English"], [([SUN, TUE, THU], "08:00", "14:00")]),
    ("Dr. Priya Menon", "internal medicine", "female", ["English", "Malayalam", "Hindi"],
     [([SAT, MON, WED], "15:00", "21:00")]),
    ("Dr. Huda Al-Kuwari", "obstetrics and gynecology", "female", ["Arabic", "English"], [(SAT_TO_WED, "09:00", "15:00")]),
    ("Dr. Rania Saleh", "obstetrics and gynecology", "female", ["Arabic", "English"], [([SUN, TUE, THU], "16:00", "21:00")]),
    ("Dr. Omar Benali", "orthopedics", "male", ["Arabic", "English", "French"], [([SAT, MON, WED], "09:00", "15:00")]),
    ("Dr. Noura Al-Sulaiti", "dermatology", "female", ["Arabic", "English"], [([SUN, TUE, THU], "09:00", "15:00")]),
    ("Dr. Daniel Okafor", "ENT", "male", ["English"], [([SAT, MON, WED], "16:00", "21:00")]),
]

# Fake patients. Two "Ahmed Ali"s with the same birthday test the multiple-match path (Stage 3);
# the Al-Kaabi father and son share one phone number.
PATIENTS = [
    ("Fatima Al-Naimi", date(1990, 3, 14), "5512 3456", None),
    ("Mohammed Al-Kaabi", date(1978, 11, 2), "3345 6789", None),
    ("Yousef Al-Kaabi", date(2019, 5, 10), "3345 6789", "3345 6789"),
    ("Aisha Rahman", date(1985, 7, 21), "6678 1122", None),
    ("Joseph Mathew", date(1969, 1, 30), "5590 4433", None),
    ("Ahmed Ali", date(1992, 2, 2), "7712 0001", None),
    ("Ahmed Ali", date(1992, 2, 2), "7712 0002", None),
]


def next_weekday(start: date, weekday: int) -> date:
    return start + timedelta(days=(weekday - start.weekday()) % 7 or 7)


def main():
    if DB_PATH.exists():
        if "--reset" not in sys.argv:
            sys.exit(f"{DB_PATH} already exists. Use --reset to rebuild it.")
        for suffix in ("", "-wal", "-shm"):
            DB_PATH.with_name(DB_PATH.name + suffix).unlink(missing_ok=True)

    random.seed(42)   # same file numbers on every rebuild
    db = ClinicDB()
    now = now_qatar()
    today = now.date()

    ids = {}
    for name, specialty, gender, languages, schedule in DOCTORS:
        ids[name] = db.add_doctor(name, specialty, gender, languages)
        for weekdays, start, end in schedule:
            for wd in weekdays:
                db.add_schedule(ids[name], wd, start, end, SLOT_MINUTES[specialty], SCHEDULES_FROM,
                                last_start=CLOSING_LAST_START if end == "22:00" else None)

    # Exceptions: Qatar National Day (whole clinic) and a day of leave for Dr. Benali
    db.add_exception(date(2026, 12, 18), date(2026, 12, 18), "closed", reason="Qatar National Day")
    db.add_exception(date(2027, 12, 18), date(2027, 12, 18), "closed", reason="Qatar National Day")
    leave_day = next_weekday(today, MON)
    db.add_exception(leave_day, leave_day, "leave", doctor_id=ids["Dr. Omar Benali"], reason="Conference")

    patients = []
    for full_name, dob, phone, guardian in PATIENTS:
        patients.append(db.create_patient(full_name, dob, phone, guardian, now=now))
    pid = [p["patient_id"] for p in patients]

    def book_first_free(patient_id, doctor, day, channel="reception", booked_at=now):
        slot = db.free_slots(ids[doctor], day, day, now=booked_at)[0]
        return db.book_slot(patient_id, ids[doctor], slot["start_at"], channel, now=booked_at)

    # Upcoming appointments
    book_first_free(pid[0], "Dr. Huda Al-Kuwari", next_weekday(today, SAT), "phone_agent")
    book_first_free(pid[1], "Dr. Omar Benali", next_weekday(today, WED), "whatsapp")
    book_first_free(pid[2], "Dr. Layla Haddad", next_weekday(today, SUN))
    book_first_free(pid[3], "Dr. Noura Al-Sulaiti", next_weekday(today, TUE), "phone_agent")
    # Past appointments: booked "two weeks ago", then marked by staff
    two_weeks_ago = now - timedelta(days=14)
    past = book_first_free(pid[0], "Dr. Khalid Al-Mahmoud", next_weekday(two_weeks_ago.date(), SUN),
                           booked_at=two_weeks_ago)
    db.mark_outcome(past["appointment_id"], "completed", now=now)
    missed = book_first_free(pid[4], "Dr. Samir Nasser", next_weekday(two_weeks_ago.date(), TUE),
                             booked_at=two_weeks_ago)
    db.mark_outcome(missed["appointment_id"], "no_show", now=now)

    print(f"Created {DB_PATH}")
    print(f"  {len(DOCTORS)} doctors, {len(PATIENTS)} patients, 6 appointments (4 upcoming, 2 past)")
    print(f"  Dr. Benali on leave {leave_day:%a %d %b %Y}; clinic closed 18 Dec (National Day)")
    print("  Fake patients (file number, name, DOB):")
    for (full_name, dob, *_), p in zip(PATIENTS, patients):
        print(f"    {p['file_number']}  {full_name:20} {dob}")


if __name__ == "__main__":
    main()
