"""Arabic input handled in code, so verification never depends on the model getting it right:
Eastern Arabic digits and spoken digit words -> ASCII digits, and Hijri dates of birth -> Gregorian.
"""
import re
from datetime import date

from hijridate import Hijri

# Eastern Arabic (٠-٩) and Persian (۰-۹) digits
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DIACRITICS = re.compile("[ؗ-ًؚ-ْـ]")   # harakat and tatweel

# One digit per word, as callers read file and phone numbers. Spellings are normalized first (alef forms -> ا,
# ة -> ه), so each entry covers its variants. Gulf and Egyptian forms included (ثنين, تلاته).
_DIGIT_WORDS = {
    "صفر": "0", "زيرو": "0",
    "واحد": "1", "وحده": "1",
    "اثنين": "2", "اثنان": "2", "ثنين": "2", "اتنين": "2",
    "ثلاث": "3", "ثلاثه": "3", "تلاته": "3", "تلات": "3",
    "اربع": "4", "اربعه": "4",
    "خمس": "5", "خمسه": "5",
    "ست": "6", "سته": "6",
    "سبع": "7", "سبعه": "7",
    "ثمان": "8", "ثماني": "8", "ثمانيه": "8", "ثمنيه": "8", "تمانيه": "8",
    "تسع": "9", "تسعه": "9",
}
_ARABIC_WORD = re.compile("[ء-يٱ]+")

# A birth year in this range can only be Hijri: no living person was born in Gregorian 1300-1500,
# and Hijri 1300-1500 covers 1882-2077. The model passes the date as the caller said it.
HIJRI_YEARS = range(1300, 1501)


def _normalize(word: str) -> str:
    w = _DIACRITICS.sub("", word)
    return re.sub("[إأآٱ]", "ا", w).replace("ة", "ه").replace("ى", "ي")


def _digit_word(match: re.Match) -> str:
    w = _normalize(match.group())
    if w in _DIGIT_WORDS:
        return _DIGIT_WORDS[w]
    if w.startswith("و") and w[1:] in _DIGIT_WORDS:   # "وخمسة": "and five"
        return _DIGIT_WORDS[w[1:]]
    return match.group()


def to_ascii_digits(text: str) -> str:
    """'٥٥١٢' -> '5512'; 'خمسة خمسة واحد اثنين' -> '5 5 1 2'. Other words are left as they are."""
    return _ARABIC_WORD.sub(_digit_word, (text or "").translate(_DIGITS))


def digits_only(text: str) -> str:
    return re.sub(r"\D", "", to_ascii_digits(text))


def parse_iso_date(text: str) -> tuple[date, tuple[int, int, int] | None] | None:
    """'YYYY-MM-DD' in either calendar -> (Gregorian date, the Hijri (y, m, d) if it was Hijri), or None if invalid.
    Hijri dates use the Umm al-Qura calendar (valid 1343-1500 AH)."""
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", to_ascii_digits(text))
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        if y in HIJRI_YEARS:
            g = Hijri(y, mo, d).to_gregorian()
            return date(g.year, g.month, g.day), (y, mo, d)
        return date(y, mo, d), None
    except (ValueError, OverflowError):
        return None


def hijri_text(y: int, m: int, d: int) -> str:
    """(1410, 8, 17) -> '17 Sha'ban 1410 AH'"""
    return f"{d} {Hijri(y, m, d).month_name()} {y} AH"
