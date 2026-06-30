import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from config import SMS_TEMPLATES_ROOT


SMS_PROFILE_WHITELIST = ("CLM", "EMRM", "AIST", "AI")
SMS_PHONE_PATTERN = re.compile(r"^\+?\d{5,20}$")
SMS_TEMPLATE_BACKUP_LIMIT = 10
SMS_TEMPLATE_BACKUP_ROOT = Path(__file__).resolve().parent.parent / "cache" / "sms_template_backups"


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


def normalize_sms_phone(phone):
    value = str(phone or "").strip()
    if not value:
        return ""
    normalized = re.sub(r"[\s()\-\u00a0]+", "", value)
    if not SMS_PHONE_PATTERN.fullmatch(normalized):
        raise ValueError(f"Некорректный номер телефона: {value}.")
    return normalized


def normalize_sms_phone_list(numbers):
    if not isinstance(numbers, list):
        raise ValueError("Ожидается список номеров телефонов.")

    normalized_numbers = []
    seen = set()
    for number in numbers:
        phone = normalize_sms_phone(number)
        if not phone:
            continue
        key = phone.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized_numbers.append(phone)

    if not normalized_numbers:
        raise ValueError("Список получателей не может быть пустым.")
    return normalized_numbers


def read_sms_template_numbers(profile):
    normalized_profile = normalize_sms_profile(profile)
    template_path = resolve_sms_profile_template(normalized_profile)
    numbers = []
    seen = set()

    with open(template_path, "r", encoding="cp1251") as file:
        for line_number, line in enumerate(file, start=1):
            phone_part = line.strip().split(";", 1)[0].strip()
            if not phone_part:
                continue
            try:
                phone = normalize_sms_phone(phone_part)
            except ValueError as exc:
                raise ValueError(f"{normalized_profile}, строка {line_number}: {exc}") from exc
            key = phone.casefold()
            if key in seen:
                continue
            seen.add(key)
            numbers.append(phone)

    return {
        "profile": normalized_profile,
        "filename": template_path.name,
        "numbers": numbers,
        "count": len(numbers),
    }


def _backup_sms_template(profile, template_path):
    if not template_path.exists():
        return None

    SMS_TEMPLATE_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{profile}_{timestamp}_{template_path.name}"
    backup_path = SMS_TEMPLATE_BACKUP_ROOT / backup_name
    shutil.copy2(template_path, backup_path)

    backups = sorted(
        SMS_TEMPLATE_BACKUP_ROOT.glob(f"{profile}_*_{template_path.name}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[SMS_TEMPLATE_BACKUP_LIMIT:]:
        try:
            old_backup.unlink()
        except OSError:
            logging.warning("Failed to remove old SMS template backup: %s", old_backup)
    return backup_path


def save_sms_template_numbers(profile, numbers):
    normalized_profile = normalize_sms_profile(profile)
    template_path = resolve_sms_profile_template(normalized_profile)
    normalized_numbers = normalize_sms_phone_list(numbers)

    _backup_sms_template(normalized_profile, template_path)

    temp_path = template_path.with_name(f".{template_path.name}.{uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="cp1251", newline="") as file:
            for phone in normalized_numbers:
                file.write(f"{phone}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, template_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logging.warning("Failed to remove temporary SMS template file: %s", temp_path)

    return read_sms_template_numbers(normalized_profile)


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
