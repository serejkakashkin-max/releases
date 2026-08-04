from flask import Flask
import ssl
import urllib3
from urllib3.exceptions import InsecureRequestWarning
from collections import defaultdict
import re
from config import DOC_TEMPLATES_ROOT
from services.sup_admin_auth_service import configure_sup_admin_session

# Отключаем предупреждения о небезопасных запросах
urllib3.disable_warnings(InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = 'super_secret_key'
app.config['SESSION_TYPE'] = 'filesystem'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
configure_sup_admin_session(app)

# Инициализация структуры релизов (глобально)
def get_release_structure():
    structure = {}
    id_map = defaultdict(list)
    id_pattern = re.compile(r'\((\d+)\)')  # ИСПРАВЛЕНО: ищем числа в скобках
    for category in DOC_TEMPLATES_ROOT.iterdir():
        if category.is_dir():
            releases = []
            for sub in category.iterdir():
                if sub.is_dir():
                    release_name = sub.name
                    match = id_pattern.search(release_name)
                    if match:
                        sm_id = match.group(1)
                        clean_name = release_name.replace(f' ({sm_id})', '').strip()
                        id_map[sm_id].append((category.name, clean_name))
                        releases.append((clean_name, release_name))
                    else:
                        releases.append((release_name, release_name))
            structure[category.name] = releases
    return structure, id_map

RELEASE_STRUCTURE, ID_MAP = get_release_structure()
