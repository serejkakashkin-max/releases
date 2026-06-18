import logging
import re
from pathlib import Path

from config import SMS_TEMPLATES_ROOT


SMS_PROFILE_WHITELIST = ("CLM", "EMRM", "AIST", "AI")


def normalize_sms_profile(profile):
    normalized = str(profile or "").strip().upper()
    if normalized not in SMS_PROFILE_WHITELIST:
        raise ValueError(
            f"Неизвестный профиль SMS: {profile or 'не указан'}. "
            f"Допустимые профили: {', '.join(SMS_PROFILE_WHITELIST)}."
        )
    return normalized


def resolve_sms_profile_template(profile):
    """Resolve a whitelisted logical profile to a CSV inside SMS_TEMPLATES_ROOT."""
    normalized = normalize_sms_profile(profile)
    root = Path(SMS_TEMPLATES_ROOT).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Папка SMS-шаблонов не найдена: {root}")

    for csv_file in sorted(root.glob("*.csv"), key=lambda path: path.name.lower()):
        resolved = csv_file.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        csv_key = str(extract_key_from_filename(csv_file.name) or "").strip().upper()
        if csv_key == normalized:
            return resolved

    raise FileNotFoundError(f"CSV-шаблон для профиля {normalized} не найден.")


def get_sms_profile_availability():
    availability = {}
    for profile in SMS_PROFILE_WHITELIST:
        try:
            resolve_sms_profile_template(profile)
            availability[profile] = True
        except (FileNotFoundError, ValueError):
            availability[profile] = False
    return availability


def process_csv(template_path, notification_text, output_path):
    """Fill the SMS CSV template with a prepared notification text."""
    try:
        with open(template_path, "r", encoding="cp1251") as f:
            lines = f.readlines()

        processed_lines = []
        for line in lines:
            clean_line = line.strip()
            if not clean_line:
                processed_lines.append(line)
                continue

            if ";" in clean_line:
                parts = clean_line.split(";", 1)
                phone = parts[0].strip()
                new_line = f"{phone};{notification_text}\n"
            else:
                new_line = f"{clean_line};{notification_text}\n"

            processed_lines.append(new_line)

        with open(output_path, "w", encoding="cp1251") as f:
            f.writelines(processed_lines)
        logging.info("SMS CSV saved: %s", output_path)
    except Exception as exc:
        logging.error("SMS CSV processing failed: %s", exc)
        raise


def extract_key_from_filename(filename):
    """Extract logical template key from a CSV filename."""
    match = re.search(r"\((.*?)\)", filename)
    if match:
        return match.group(1)
    match = re.search(r"([A-Z]+)", filename)
    return match.group(1) if match else None
