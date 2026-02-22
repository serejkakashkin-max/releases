import json
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from config import COUNTERS_FILE

# Лок для потокобезопасности
_counters_lock = threading.Lock()

def _ensure_counters_file():
    """Создает файл счетчиков если он не существует"""
    if not COUNTERS_FILE.exists():
        COUNTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        default_data = {
            "release": {"total": 0, "daily": {}},
            "sms": {"total": 0, "daily": {}}
        }
        try:
            with open(COUNTERS_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_data, f, ensure_ascii=False, indent=2)
            logging.info(f"Создан файл счетчиков: {COUNTERS_FILE}")
            return default_data
        except Exception as e:
            logging.error(f"Ошибка создания файла счетчиков: {e}")
            return default_data
    return None

def _load_counters():
    """Загружает счетчики из файла или создает новые"""
    # Сначала пробуем создать файл если его нет
    default_data = _ensure_counters_file()
    if default_data is not None:
        return default_data
    
    try:
        with open(COUNTERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Ошибка загрузки счетчиков: {e}")
        # Создаем резервную копию поврежденного файла
        if COUNTERS_FILE.exists():
            backup_path = COUNTERS_FILE.with_suffix('.json.backup')
            try:
                COUNTERS_FILE.rename(backup_path)
                logging.info(f"Создана резервная копия: {backup_path}")
            except:
                pass
        return {
            "release": {"total": 0, "daily": {}},
            "sms": {"total": 0, "daily": {}}
        }

def _save_counters(counters):
    """Сохраняет счетчики в файл"""
    try:
        COUNTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COUNTERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(counters, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения счетчиков: {e}")
        return False

def _cleanup_old_entries(daily_data):
    """Удаляет записи старше 30 дней"""
    today = datetime.now().date()
    cutoff_date = today - timedelta(days=30)
    
    cleaned = {}
    for date_str, count in daily_data.items():
        try:
            entry_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            if entry_date >= cutoff_date:
                cleaned[date_str] = count
        except ValueError:
            continue
    return cleaned

def _calculate_last_30_days(daily_data):
    """Вычисляет сумму за последние 30 дней"""
    today = datetime.now().date()
    cutoff_date = today - timedelta(days=30)
    total = 0
    
    for date_str, count in daily_data.items():
        try:
            entry_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            if entry_date >= cutoff_date:
                total += count
        except ValueError:
            continue
    return total

def increment_counter(counter_type):
    """
    Увеличивает счетчик на 1
    counter_type: 'release' или 'sms'
    """
    if counter_type not in ('release', 'sms'):
        logging.error(f"Неизвестный тип счетчика: {counter_type}")
        return False
    
    with _counters_lock:
        counters = _load_counters()
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # Увеличиваем общий счетчик
        counters[counter_type]['total'] = counters[counter_type].get('total', 0) + 1
        
        # Увеличиваем дневной счетчик
        daily = counters[counter_type].get('daily', {})
        daily[today_str] = daily.get(today_str, 0) + 1
        counters[counter_type]['daily'] = daily
        
        # Очищаем старые записи
        counters[counter_type]['daily'] = _cleanup_old_entries(daily)
        
        return _save_counters(counters)

def get_stats():
    """
    Возвращает статистику для отображения на главной странице
    """
    with _counters_lock:
        counters = _load_counters()
        
        release_total = counters['release'].get('total', 0)
        sms_total = counters['sms'].get('total', 0)
        release_30d = _calculate_last_30_days(counters['release'].get('daily', {}))
        sms_30d = _calculate_last_30_days(counters['sms'].get('daily', {}))
        
        # Вычисляем проценты для прогресс-баров (относительно максимума)
        max_total = max(release_total, sms_total, 1)  # минимум 1 чтобы избежать деления на 0
        release_pct = (release_total / max_total) * 100 if max_total > 0 else 0
        sms_pct = (sms_total / max_total) * 100 if max_total > 0 else 0
        
        return {
            'release': {
                'total': release_total,
                'last_30_days': release_30d,
                'percentage': release_pct
            },
            'sms': {
                'total': sms_total,
                'last_30_days': sms_30d,
                'percentage': sms_pct
            },
            'total_combined': release_total + sms_total
        }