# VS Code Setup

Локальные настройки VS Code находятся в `.vscode/`.

Папка `.vscode/` уже указана в `.gitignore`, поэтому эти настройки не попадут в GitHub без отдельного решения.

## Что настроено

- Python interpreter: `.venv/bin/python`
- Pytest discovery: `tests`
- Задача `Run all tests`
- Задача `Run VA Schedule Manager tests`
- Задача `Start local app`
- Debug-конфигурация `Run parent app`
- Debug-конфигурация `Run current pytest file`
- Исключены из поиска и наблюдения `.venv`, `cache`, `logs`, `data`, `__pycache__`, `.pytest_cache`

## Рекомендуемые расширения

- `OpenAI.chatgpt` — Codex / ChatGPT Work with Apps для связи с VS Code.
- `ms-python.python` — Python support.
- `ms-python.vscode-pylance` — Python language server.
- `ms-python.debugpy` — Debugger.
- `charliermarsh.ruff` — быстрый linter/formatter.
- `GitHub.vscode-pull-request-github` — просмотр PR без выхода из VS Code.
- `eamodio.gitlens` — удобная история Git.
- `redhat.vscode-yaml`, `tamasfe.even-better-toml`, `yzhang.markdown-all-in-one` — поддержка конфигов и документации.

## Как открыть проект

Открывать нужно папку:

```text
/Users/antonvaskin/Documents/Codex/2026-07-11/new-chat/work/schedule-manager-github
```

Если команда `code` в терминале недоступна, в VS Code нужно выполнить:

```text
Command Palette -> Shell Command: Install 'code' command in PATH
```

После установки расширения `OpenAI.chatgpt` можно подключить ChatGPT/Codex к VS Code через Work with Apps.
