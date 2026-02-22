import re
import unicodedata
from datetime import datetime, timedelta

def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def validate_date(date_str):
    """Проверяет корректность даты в формате dd.mm.yyyy"""
    try:
        datetime.strptime(date_str, '%d.%m.%Y')
        return True
    except ValueError:
        return False

def extract_date_from_text(text):
    """Извлекает дату из текста в формате Дата: dd.mm.yyyy"""
    if not text:
        return datetime.now().strftime('%d.%m.%Y')
    
    match = re.search(r'Дата:\s*(\d{2}\.\d{2}\.\d{4})', text)
    if match:
        return match.group(1)
    return datetime.now().strftime('%d.%m.%Y')

def replace_date_in_text(text, new_date):
    """Заменяет дату в тексте на новую"""
    return re.sub(r'Дата:\s*\d{2}\.\d{2}\.\d{4}', f'Дата: {new_date}', text)

def extract_key_from_filename(filename):
    """Извлекает ключ из имени файла (например, (BH))"""
    match = re.search(r'\((.*?)\)', filename)
    return match.group(1) if match else None