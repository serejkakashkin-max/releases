from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List


def normalize_person_name(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    return " ".join(text.strip().split())


def _comparison_parts(value: object):
    text = normalize_person_name(value)
    tokens = [token for token in re.split(r"\s+", text) if token]
    if not tokens:
        return "", ()
    surname = tokens[0].casefold()
    initials: List[str] = []
    for token in tokens[1:]:
        cleaned = token.replace(".", "").strip()
        if not cleaned:
            continue
        if len(cleaned) <= 3 and cleaned.isalpha():
            initials.extend(char.casefold() for char in cleaned)
        elif cleaned[0].isalpha():
            initials.append(cleaned[0].casefold())
    return surname, tuple(initials)


def match_release_employee_name(raw_name: object, release_names: Iterable[str]) -> str:
    return match_release_employee_name_diagnostic(raw_name, release_names)["name"]


def match_release_employee_name_diagnostic(raw_name: object, release_names: Iterable[str]) -> dict:
    raw_display = normalize_person_name(raw_name)
    if not raw_display:
        return {"name": "", "status": "unmapped"}

    options = [normalize_person_name(value) for value in release_names if normalize_person_name(value)]
    exact_key = raw_display.replace(".", "").casefold()
    exact_candidates = [
        value for value in options
        if value.replace(".", "").casefold() == exact_key
    ]
    if len(exact_candidates) == 1:
        return {"name": exact_candidates[0], "status": "matched"}
    if len(exact_candidates) > 1:
        return {"name": "", "status": "ambiguous"}

    surname, initials = _comparison_parts(raw_display)
    if not surname or not initials:
        return {"name": "", "status": "unmapped"}

    candidates = []
    for option in options:
        option_surname, option_initials = _comparison_parts(option)
        if option_surname != surname:
            continue
        if len(initials) >= 2:
            matches = option_initials[: len(initials)] == initials
        else:
            matches = bool(option_initials and option_initials[0] == initials[0])
        if matches:
            candidates.append(option)
    if len(candidates) == 1:
        return {"name": candidates[0], "status": "matched"}
    return {"name": "", "status": "ambiguous" if candidates else "unmapped"}
