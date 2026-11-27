"""Rewrite text so Kokoro says numbers the way people do.

Kokoro's phonemizer reads many number formats literally: "5.5" as "five five", "$5" as
"dollar five", "2026" as "two thousand twenty six", "3:30" with a pause for the colon,
"20°C" as "twenty degrees C", "3-4" as "three dash four". `speakable` rewrites them into
words it reads right. It only changes what is spoken; the chat shows the original text.
Every rewrite was checked against Kokoro's phonemes (see tests/test_speech_text.py).

The rules run in order: money, temperatures and units keep their decimals for the
final decimal rule ("$5.5 million" -> "5.5 million dollars" -> "5 point 5 ...").
"""
import re

# Words Kokoro's phonemizer guesses wrong, respelled so they come out right. Matched as
# whole words, case-insensitive, with an optional plural "s" ("drivetrains"); a
# possessive "'s" is untouched. Check a new entry with
# kokoro.tokenizer.phonemize(word, "en-us") before adding it.
PRONUNCIATIONS = {
    "drivetrain": "drive-train",  # was "driv-i-train"
    "ESP32": "E S P 32",          # was "esp thirty-two"
    "ONNX": "onyx",               # was "ongnks"
    "Sibiu": "Seebeeoo",          # SEE-bee-oo, not SIB-ee-oo
    "Tudor": "Toodor",            # TOO-dor, not the English TYOO-der
    "Fifi": "Feefee",             # FEE-fee, not FIFF-ee
}
PRONUNCIATION = re.compile(
    r"\b(" + "|".join(map(re.escape, PRONUNCIATIONS)) + r")(s?)\b", re.IGNORECASE
)
_RESPELL = {word.lower(): spoken for word, spoken in PRONUNCIATIONS.items()}

NUMBER = r"\d+(?:\.\d+)?"
# Not glued to another number, word or dotted/hyphenated run (2.0.1, 2026-11-27, 4x4)
START = r"(?<![\w.\-–/:])"
END = r"(?![\w\-–/:]|\.\d)"

THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")

# symbol: (unit, units, subunit, subunits)
CURRENCIES = {
    "$": ("dollar", "dollars", "cent", "cents"),
    "€": ("euro", "euros", "cent", "cents"),
    "£": ("pound", "pounds", "penny", "pence"),
}
SCALE = r"(?:thousand|million|billion|trillion)"
MONEY_RANGE = re.compile(rf"([$€£])({NUMBER})\s?[-–]\s?[$€£]?({NUMBER})")
MONEY_BEFORE = re.compile(rf"([$€£])\s?(\d+)(?:\.(\d+))?(?:\s+({SCALE})\b)?")
MONEY_AFTER = re.compile(r"(?<![\d.])(\d+)(?:\.(\d+))?\s?(€)")

TEMPERATURE = re.compile(rf"(-?{NUMBER})\s?°\s?([CF])\b")
TEMPERATURE_UNITS = {"C": "Celsius", "F": "Fahrenheit"}

UNITS = {
    "km/h": ("kilometer per hour", "kilometers per hour"),
    "m/s": ("meter per second", "meters per second"),
    "mph": ("mile per hour", "miles per hour"),
    "km": ("kilometer", "kilometers"),
    "cm": ("centimeter", "centimeters"),
    "mm": ("millimeter", "millimeters"),
    "kg": ("kilogram", "kilograms"),
}
UNIT = re.compile(rf"(?<![\w.])(-?{NUMBER})\s?(km/h|m/s|mph|km|cm|mm|kg)(?![\w/])")

# 3:30, 10:05 pm, 7:00 a.m. (the a.m.'s last dot is left alone: it may end the sentence)
TIME = re.compile(
    r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])(?:\s?([ap])(?:\.m|m)(?![a-z]))?", re.I
)
HOUR_AMPM = re.compile(r"(?<![\d:.])(1[0-2]|0?[1-9])\s?([ap])(?:\.m|m)(?![a-z])", re.I)

YEAR = r"(?:1[1-9]\d\d|20\d\d)"
YEAR_RANGE = re.compile(rf"{START}({YEAR})\s?[-–]\s?({YEAR}){END}")
YEAR_CONTEXT = (
    r"in|since|by|until|till|from|before|after|of|to|through|year|circa|early|late|mid|"
    r"spring|summer|autumn|fall|winter|January|February|March|April|May|June|July|"
    r"August|September|October|November|December"
)
YEAR_AFTER_WORD = re.compile(rf"\b({YEAR_CONTEXT})(\s+)({YEAR}){END}", re.I)
# A bare 19xx/20xx that ends a sentence: "It's 2026."
YEAR_AT_END = re.compile(rf"{START}((?:19|20)\d\d)(?=[.!?](?:\s|$))")

TENS = {
    "1": "tens", "2": "twenties", "3": "thirties", "4": "forties", "5": "fifties",
    "6": "sixties", "7": "seventies", "8": "eighties", "9": "nineties",
}
CENTURY_DECADE = re.compile(r"(?<![\w.'])(1[1-9]|20)(\d)0s\b")
SHORT_DECADE = re.compile(r"(?<![\w.])'?([1-9])0s\b")

RANGE = re.compile(rf"{START}({NUMBER})\s?[-–]\s?({NUMBER}){END}")

FRACTIONS = {
    "1/2": "one half", "1/3": "one third", "2/3": "two thirds",
    "1/4": "one quarter", "3/4": "three quarters",
}
FRACTION = re.compile(r"(?<![\d/])([1-3]/[2-4])(?![\d/])")
HASH_NUMBER = re.compile(r"#(\d+)")
TIMES = re.compile(rf"(?<![\w.])({NUMBER})x(?!\w)")

DECIMAL = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?![\d]|\.\d)")


def say_year(year: int) -> str:
    """2026 -> "20 26", 1905 -> "19 oh 5", 1900 -> "19 hundred"; 2000-2009 as is."""
    if 2000 <= year <= 2009:
        return str(year)
    hi, lo = divmod(year, 100)
    if lo == 0:
        return f"{hi} hundred"
    if lo < 10:
        return f"{hi} oh {lo}"
    return f"{hi} {lo}"


def _is_one(number: str) -> bool:
    return number.lstrip("-") == "1"


def _money(symbol: str, whole: str, fraction: str | None, scale: str | None = None) -> str:
    unit, units, sub, subs = CURRENCIES[symbol]
    if scale:
        amount = f"{whole}.{fraction}" if fraction else whole
        return f"{amount} {scale} {units}"
    if fraction is not None and len(fraction) == 2 and fraction != "00":
        cents = str(int(fraction))
        cents_words = f"{cents} {sub if cents == '1' else subs}"
        if int(whole) == 0:
            return cents_words
        return f"{whole} {unit if _is_one(whole) else units} and {cents_words}"
    if fraction and set(fraction) != {"0"}:
        return f"{whole}.{fraction} {units}"  # "$5.5" -> the decimal rule finishes it
    return f"{whole} {unit if _is_one(whole) else units}"


def _time(m: re.Match) -> str:
    hour, minutes, ampm = m.group(1).lstrip("0") or "0", m.group(2), m.group(3)
    suffix = f" {ampm.lower()}m" if ampm else ""
    if minutes == "00":
        return f"{hour}{suffix}" if ampm else f"{hour} o'clock"
    if minutes[0] == "0":
        return f"{hour} oh {minutes[1]}{suffix}"
    return f"{hour} {minutes}{suffix}"


def _century_decade(m: re.Match) -> str:
    century, tens = m.group(1), m.group(2)
    if tens == "0":
        return "two thousands" if century == "20" else f"{century} hundreds"
    return f"{century} {TENS[tens]}"


def speakable(text: str) -> str:
    """Rewrite text so Kokoro says it right: known words first, then numbers."""
    text = PRONUNCIATION.sub(lambda m: _RESPELL[m.group(1).lower()] + m.group(2), text)
    text = text.replace("−", "-")  # Unicode minus
    text = THOUSANDS_COMMA.sub("", text)

    text = MONEY_RANGE.sub(
        lambda m: f"{m.group(2)} to {m.group(3)} {CURRENCIES[m.group(1)][1]}", text
    )
    text = MONEY_BEFORE.sub(lambda m: _money(m.group(1), m.group(2), m.group(3), m.group(4)), text)
    text = MONEY_AFTER.sub(lambda m: _money(m.group(3), m.group(1), m.group(2)), text)

    text = TEMPERATURE.sub(
        lambda m: f"{m.group(1)} {'degree' if _is_one(m.group(1)) else 'degrees'} "
                  f"{TEMPERATURE_UNITS[m.group(2)]}",
        text,
    )
    text = UNIT.sub(
        lambda m: f"{m.group(1)} {UNITS[m.group(2)][0 if _is_one(m.group(1)) else 1]}", text
    )

    text = TIME.sub(_time, text)
    text = HOUR_AMPM.sub(lambda m: f"{m.group(1)} {m.group(2).lower()}m", text)

    text = YEAR_RANGE.sub(
        lambda m: f"{say_year(int(m.group(1)))} to {say_year(int(m.group(2)))}", text
    )
    text = CENTURY_DECADE.sub(_century_decade, text)
    text = SHORT_DECADE.sub(lambda m: TENS[m.group(1)], text)
    text = YEAR_AFTER_WORD.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{say_year(int(m.group(3)))}", text
    )
    text = YEAR_AT_END.sub(lambda m: say_year(int(m.group(1))), text)

    text = RANGE.sub(r"\1 to \2", text)
    text = FRACTION.sub(lambda m: FRACTIONS.get(m.group(1), m.group(1)), text)
    text = HASH_NUMBER.sub(r"number \1", text)
    text = TIMES.sub(r"\1 times", text)

    return DECIMAL.sub(lambda m: f"{m.group(1)} point {' '.join(m.group(2))}", text)
