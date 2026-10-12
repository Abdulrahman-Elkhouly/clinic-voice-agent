r"""Checks that the knowledge base's doctor descriptions agree with the booking database.

    python agent\check_schedules.py

The database is the source of truth for booking; the knowledge base describes the same doctors
in words for general questions. This reports any doctor, gender, language, day or hour that differs.
Exit code 1 if anything differs (so it can run in CI).
"""
import re
import sys
from pathlib import Path

from db import ClinicDB, now_qatar

KB_FILE = Path(__file__).resolve().parent.parent / "knowledge_base" / "doctors_and_specialties.md"

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]   # Monday = 0
CLINIC_WEEK = ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
HEADING_TO_SPECIALTY = {
    "Family medicine": "family medicine", "Pediatrics": "pediatrics", "Internal medicine": "internal medicine",
    "Obstetrics and gynecology": "obstetrics and gynecology", "Orthopedics": "orthopedics",
    "Dermatology": "dermatology", "ENT (ear, nose and throat)": "ENT",
}
DOCTOR_LINE = re.compile(r"^- (Dr\. [^(]+?) \((male|female)\), speaks (.+?)\. (.+?)\.?$")
TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2}) (in the morning|in the afternoon|at night) to "
                        r"(\d{1,2}):(\d{2}) (in the morning|in the afternoon|at night)")


def to_hhmm(h: str, m: str, part: str) -> str:
    hour = int(h)
    if part != "in the morning" and hour < 12:
        hour += 12
    return f"{hour:02d}:{m}"


def parse_days(phrase: str) -> list[int]:
    """'Saturday to Wednesday' -> a range in the clinic week; 'Sunday, Tuesday and Thursday' -> a list."""
    phrase = phrase.strip(" ,").removeprefix("and ").strip(" ,")
    m = re.fullmatch(r"(\w+) to (\w+)", phrase)
    if m:
        a, b = CLINIC_WEEK.index(m.group(1)), CLINIC_WEEK.index(m.group(2))
        return [DAYS.index(d) for d in CLINIC_WEEK[a:b + 1]]
    return [DAYS.index(d) for d in re.findall("|".join(DAYS), phrase)]


def parse_schedule(text: str) -> set[tuple[int, str, str]]:
    """'Saturday and Monday 3:00 in the afternoon to 10:00 at night' -> {(weekday, '15:00', '22:00'), ...}"""
    result, prev_end = set(), 0
    for m in TIME_RANGE.finditer(text):
        start, end = to_hhmm(*m.group(1, 2, 3)), to_hhmm(*m.group(4, 5, 6))
        for wd in parse_days(text[prev_end:m.start()]):
            result.add((wd, start, end))
        prev_end = m.end()
    return result


def kb_doctors() -> dict[str, dict]:
    doctors, specialty = {}, None
    for line in KB_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            specialty = HEADING_TO_SPECIALTY.get(line[3:].strip())
        elif specialty and (m := DOCTOR_LINE.match(line.strip())):
            name, gender, langs, schedule = m.groups()
            doctors[name] = {
                "specialty": specialty, "gender": gender,
                "languages": set(re.split(r", | and ", langs)),
                "schedule": parse_schedule(schedule),
            }
    return doctors


def db_doctors(db: ClinicDB) -> dict[str, dict]:
    today = now_qatar().date()
    rows = db.schedules_on(today)
    doctors = {}
    for d in db.list_doctors():
        doctors[d["name"]] = {
            "specialty": d["specialty"], "gender": d["gender"],
            "languages": set(d["languages"].split(", ")),
            "schedule": {(r["weekday"], r["start_time"], r["end_time"]) for r in rows
                         if r["doctor_id"] == d["doctor_id"]},
        }
    return doctors


def fmt(schedule: set) -> str:
    return ", ".join(f"{DAYS[wd][:3]} {s}-{e}" for wd, s, e in sorted(schedule)) or "nothing"


def main() -> int:
    kb, db = kb_doctors(), db_doctors(ClinicDB())
    problems = []
    for name in sorted(kb.keys() - db.keys()):
        problems.append(f"{name}: in the knowledge base but not in the database")
    for name in sorted(db.keys() - kb.keys()):
        problems.append(f"{name}: in the database but not in the knowledge base")
    for name in sorted(kb.keys() & db.keys()):
        for field in ("specialty", "gender", "languages"):
            if kb[name][field] != db[name][field]:
                problems.append(f"{name}: {field} differs (KB {kb[name][field]}, DB {db[name][field]})")
        if kb[name]["schedule"] != db[name]["schedule"]:
            problems.append(f"{name}: schedule differs\n    KB {fmt(kb[name]['schedule'])}"
                            f"\n    DB {fmt(db[name]['schedule'])}")

    if problems:
        print("Knowledge base and database disagree:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK: {len(kb)} doctors agree (names, specialties, genders, languages, days and hours).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
