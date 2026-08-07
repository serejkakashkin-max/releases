from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

BASE = "4d41d42446d21b06b53e06c8f8e1e67131f87394"
TARGET = "e973887521f02373081bf487f880156503226007"
ZIP_NAME = "OPLOT_migration_4d41d424_to_e9738875.zip"

ROOT = Path.cwd()
OUT = ROOT / "migration_out"
STAGE = OUT / "stage"
OUT.mkdir(exist_ok=True)
STAGE.mkdir(exist_ok=True)


def git_list(*args: str) -> list[str]:
    data = subprocess.check_output(["git", *args])
    return [item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item]


changed = sorted(
    set(git_list("diff", "--name-only", "-z", "--diff-filter=ACMRTUXB", BASE, TARGET))
)
deleted = sorted(set(git_list("diff", "--name-only", "-z", "--diff-filter=D", BASE, TARGET)))

copied: list[str] = []
for rel in changed:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{TARGET}:{rel}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        continue
    data = subprocess.check_output(["git", "show", f"{TARGET}:{rel}"])
    dest = STAGE / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    copied.append(rel)

(STAGE / "DELETED_FILES.txt").write_text(
    "\n".join(deleted) + ("\n" if deleted else ""), encoding="utf-8"
)
(STAGE / "CHANGED_FILES.txt").write_text("\n".join(copied) + "\n", encoding="utf-8")

readme = f"""OPLOT — пакет переноса модернизации на рабочий стенд

Исходное состояние до модернизации:
{BASE}

Целевое состояние GitHub main:
{TARGET}

Файлов для добавления/замены: {len(copied)}
Файлов для удаления: {len(deleted)}

КАК ПРИМЕНЯТЬ
1. Сделать резервную копию рабочей локальной копии проекта или создать отдельную Git-ветку в Bitbucket-репозитории.
2. Распаковать ZIP в КОРЕНЬ проекта с сохранением структуры каталогов и заменой существующих файлов.
3. Открыть DELETED_FILES.txt и удалить перечисленные там файлы из рабочей копии, если они существуют.
4. Выполнить установку/синхронизацию Python-зависимостей из requirements.txt.
5. Проверить git status и убедиться, что config.json, .env, реальные DOCX и runtime-данные стенда не были заменены случайно.
6. Запустить тесты/приложение локально, затем commit/push в Bitbucket.

ВАЖНО
- Архив содержит NET-дельту между указанными commit SHA, а не весь репозиторий.
- Файлы внутри имеют финальное содержимое TARGET и исходные относительные пути проекта.
- DELETED_FILES.txt, CHANGED_FILES.txt и MIGRATION_README.txt — служебные файлы пакета, их не требуется коммитить в Bitbucket.
- config.json, .env и реальные production DOCX в дельту не входят.
"""
(STAGE / "MIGRATION_README.txt").write_text(readme, encoding="utf-8")

zip_path = OUT / ZIP_NAME
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(STAGE.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(STAGE).as_posix())

print(f"PACKAGE={zip_path}")
print(f"COPIED={len(copied)}")
print(f"DELETED={len(deleted)}")
for rel in deleted:
    print(f"DELETE={rel}")
