"""Общие функции: уровни, проверка вопроса, чтение/запись JSON."""
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.environ.get("QUESTIONS_FILE") or os.path.join(BASE, "questions.json")
STATE_PATH = os.environ.get("STATE_FILE") or os.path.join(BASE, "state.json")

# Подпись уровня в заголовке опроса
LEVELS = {
    "B1": "B1 (Intermediate)",
    "B2": "B2 (Upper-Intermediate)",
    "C1": "C1 (Advanced)",
}

# Разделитель между заголовком и вопросом. Если в вашем Telegram-клиенте
# перенос строки в опросе выглядит плохо, замените на " | "
HEADER_SEP = "\n\n"

# Лимиты Telegram для опросов-викторин
MAX_POLL_QUESTION = 300
MAX_OPTION = 100
MAX_EXPLANATION = 200


def poll_text(q):
    """Текст вопроса в опросе: заголовок с уровнем + сам вопрос."""
    return f"🎯 {LEVELS[q['level']]} · {q['topic']}{HEADER_SEP}{q['question']}"


def validate(q):
    """Возвращает текст ошибки или None, если вопрос корректный."""
    if not isinstance(q, dict):
        return "не объект"
    if q.get("level") not in LEVELS:
        return "неизвестный уровень"
    for key in ("topic", "question", "explanation"):
        if not isinstance(q.get(key), str) or not q[key].strip():
            return f"пустое поле {key}"
    opts = q.get("options")
    if not isinstance(opts, list) or len(opts) != 4:
        return "нужно ровно 4 варианта"
    if not all(isinstance(o, str) and 1 <= len(o) <= MAX_OPTION for o in opts):
        return f"вариант пустой или длиннее {MAX_OPTION} символов"
    if len({o.strip().lower() for o in opts}) != 4:
        return "варианты повторяются"
    c = q.get("correct")
    if not isinstance(c, int) or isinstance(c, bool) or not 0 <= c <= 3:
        return "correct должен быть числом 0-3"
    if len(q["explanation"]) > MAX_EXPLANATION or q["explanation"].count("\n") > 2:
        return f"пояснение длиннее {MAX_EXPLANATION} символов или более 2 переносов"
    if len(poll_text(q)) > MAX_POLL_QUESTION:
        return f"вопрос с заголовком длиннее {MAX_POLL_QUESTION} символов"
    return None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    """Атомарная запись, чтобы файл не повредился при сбое."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_bank():
    return load_json(BANK_PATH, [])


def save_bank(bank):
    """Один вопрос на строку: файл компактный, а изменения в git читаются легко."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(BANK_PATH) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("[\n")
        f.write(",\n".join(json.dumps(q, ensure_ascii=False) for q in bank))
        f.write("\n]\n")
    os.replace(tmp, BANK_PATH)
