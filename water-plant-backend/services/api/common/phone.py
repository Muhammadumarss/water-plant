"""Phone normalization for searching. Matching numbers never merge customers."""

import re


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    digits = digits.removeprefix("00")
    if digits.startswith("0") and len(digits) == 11:  # 03XXXXXXXXX (Pakistan mobile)
        digits = "92" + digits[1:]
    elif len(digits) == 10 and digits.startswith("3"):
        digits = "92" + digits
    return f"+{digits}" if digits else ""
