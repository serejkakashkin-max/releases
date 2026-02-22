import os
import tempfile
import json
import logging
from pathlib import Path
from flask import Blueprint, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename
from zipfile import ZipFile
from io import BytesIO

from config import sms_logger
from services.sms_service import find_matching_csv, extract_notification_text, process_csv, extract_key_from_filename
from utils.common import extract_date_from_text, replace_date_in_text, validate_date
from services.counter_service import increment_counter  # НОВОЕ: импорт счетчика

sms_bp = Blueprint('sms', __name__)

BASE_PATH = os.getenv("BASE_PATH", "")

@sms_bp.route('/sms', methods=['GET'])
def sms():
    return render_template('sms.html', basepath=BASE_PATH, status="", errors=[])

@sms_bp.route('/sms/extract', methods=['POST'])
def sms_extract():
    """Извлекает оба текста (успешный и неуспешный) и дату из загруженного DOCX файла"""
    file = request.files.get('file')
    if not file:
        sms_logger.error('extract: No file provided')
        return jsonify({'error': 'No file provided'}), 400
    
    tmp_path = None
    try:
        # Создаем временный файл
        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tmp:
            file.save(tmp.name)
            tmp_path = Path(tmp.name)
            sms_logger.info(f"extract: Processing file {file.filename}, saved to {tmp_path}")
        
        # Извлекаем оба текста
        sms_logger.info(f"extract: Extracting success text...")
        success_text = extract_notification_text(tmp_path, is_failure=False)
        
        sms_logger.info(f"extract: Extracting failure text...")
        failure_text = extract_notification_text(tmp_path, is_failure=True)
        
        # Определяем дату из успешного текста (там она всегда есть)
        date = ''
        if success_text:
            date = extract_date_from_text(success_text)
            sms_logger.info(f"extract: Date extracted from success text: {date}")
        elif failure_text:
            date = extract_date_from_text(failure_text)
            sms_logger.info(f"extract: Date extracted from failure text: {date}")
        else:
            sms_logger.warning("extract: No texts found, using current date")
            from datetime import datetime
            date = datetime.now().strftime('%d.%m.%Y')
        
        # Удаляем временный файл
        os.unlink(tmp_path)
        
        response_data = {
            'success_text': success_text or '',
            'failure_text': failure_text or '',
            'date': date
        }
        sms_logger.info(f"extract: Returning data for {file.filename} with date={date}")
        return jsonify(response_data)
        
    except Exception as e:
        sms_logger.error(f"extract: Error processing file: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return jsonify({'error': str(e)}), 500

@sms_bp.route('/sms/generate', methods=['POST'])
def sms_generate():
    """Генерирует CSV файлы на основе загруженных DOCX и метаданных"""
    files = request.files.getlist('files')
    metadata_str = request.form.get('metadata', '{}')
    
    sms_logger.info(f"generate: Received {len(files)} files")
    sms_logger.info(f"generate: Raw metadata string: {metadata_str}")
    
    try:
        metadata = json.loads(metadata_str)
        sms_logger.info(f"generate: Parsed metadata keys: {list(metadata.keys())}")
    except json.JSONDecodeError as e:
        sms_logger.error(f"generate: JSON decode error: {e}")
        metadata = {}
    
    errors = []
    
    if not files:
        return jsonify({'errors': ['Нет выбранных файлов']}), 400
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        generated_csvs = []
        
        for file in files:
            if not file or not file.filename.endswith('.docx'):
                errors.append(f"Неверный формат файла: {file.filename}")
                continue
            
            # !!! КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ !!!
            # Оригинальное имя файла для поиска в метаданных (как прислал фронтенд)
            original_filename = file.filename
            # Санитайзим только для сохранения на диск
            safe_filename = secure_filename(file.filename)
            
            sms_logger.info(f"generate: Processing original='{original_filename}', safe='{safe_filename}'")
            
            # Ищем метаданные по ОРИГИНАЛЬНОМУ имени файла
            file_meta = metadata.get(original_filename, {})
            
            if not file_meta:
                sms_logger.warning(f"generate: No metadata found for '{original_filename}', available keys: {list(metadata.keys())}")
            
            is_failure = file_meta.get('is_failure', False)
            custom_date = file_meta.get('date', '')
            edited_text = file_meta.get('edited_text', None)
            
            sms_logger.info(f"generate: Metadata for '{original_filename}': is_failure={is_failure}, date={custom_date}, has_edited_text={edited_text is not None}")
            
            # Сохраняем файл с безопасным именем
            doc_path = temp_path / safe_filename
            file.save(doc_path)
            
            # Получаем ключ из имени файла (используем оригинальное имя для извлечения ключа)
            key = extract_key_from_filename(original_filename)
            if not key:
                errors.append(f"Ключ не найден в имени файла: {original_filename}")
                continue
            
            # Ищем CSV шаблон
            csv_template = find_matching_csv(key)
            if not csv_template:
                errors.append(f"Шаблон CSV не найден для ключа ({key}) в файле: {original_filename}")
                continue
            
            # Определяем итоговый текст
            notification_text = edited_text
            
            if not notification_text:
                # Если нет отредактированного текста, извлекаем из документа
                sms_logger.info(f"generate: No edited text provided, extracting from document (is_failure={is_failure})")
                notification_text = extract_notification_text(doc_path, is_failure=is_failure)
                # Если не нашли нужный текст, пробуем другой
                if not notification_text:
                    notification_text = extract_notification_text(doc_path, is_failure=not is_failure)
            else:
                sms_logger.info(f"generate: Using edited text: {notification_text[:50]}...")
            
            if not notification_text:
                errors.append(f"Текст оповещения не найден в: {original_filename}")
                continue
            
            # !!! ИСПРАВЛЕНИЕ ДАТЫ !!!
            # Если пользователь указал дату в метаданных, обязательно используем её
            # Иначе берем из текста документа
            if custom_date and validate_date(custom_date):
                date_to_use = custom_date
                sms_logger.info(f"generate: Using user-provided date: {date_to_use}")
                # Заменяем дату в тексте на пользовательскую
                notification_text = replace_date_in_text(notification_text, date_to_use)
            else:
                # Если даты нет в метаданных, извлекаем из текущего текста
                date_to_use = extract_date_from_text(notification_text)
                sms_logger.info(f"generate: Using date from text: {date_to_use}")
            
            # Генерируем CSV
            output_csv_path = temp_path / f"{safe_filename}.csv"
            process_csv(csv_template, notification_text, output_csv_path)
            generated_csvs.append(output_csv_path)
            sms_logger.info(f"generate: CSV created: {output_csv_path}")
        
        # Если есть ошибки и нет успешных файлов - возвращаем ошибки
        if errors and not generated_csvs:
            return jsonify({'errors': errors}), 400
        
        # НОВОЕ: Инкремент счетчика при успешной генерации (если есть хотя бы один CSV)
        if generated_csvs:
            increment_counter('sms')
        
        # Формируем ZIP архив
        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, 'w') as zip_file:
            for csv_path in generated_csvs:
                zip_file.write(csv_path, csv_path.name)
        
        zip_buffer.seek(0)
        
        # Если есть ошибки но есть и успешные файлы - возвращаем архив с заголовком ошибок
        if errors:
            response = send_file(zip_buffer, as_attachment=True, download_name="sms.zip")
            response.headers['X-Errors'] = json.dumps(errors)
            return response
        
        return send_file(zip_buffer, as_attachment=True, download_name="sms.zip")