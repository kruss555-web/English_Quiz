#!/usr/bin/env python3
"""Публикует ОДИН вопрос-викторину в Telegram-канал.

Порядок: все вопросы перемешиваются и выдаются по одному. Когда очередь
кончилась, банк перемешивается заново (новый круг). Новые вопросы, которые
появились в банке, автоматически встают в случайные места очереди.
"""
import datetime
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

from common import STATE_PATH, load_bank, load_json, poll_text, save_json, validate

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FORCE = os.environ.get("FORCE") == "1"      # публиковать, даже если сегодня уже было
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # только показать, ничего не отправлять


def choose_next(bank, state, rng):
    """Выбирает следующий вопрос. Возвращает (question, новое_состояние)."""
    by_id = {q["id"]: q for q in bank}
    queue = [i for i in state.get("queue", []) if i in by_id]
    done = [i for i in state.get("done", []) if i in by_id]
    cycle = state.get("cycle", 1)

    # Новые вопросы вставляем в случайные места очереди
    known = set(queue) | set(done)
    for qid in by_id:
        if qid not in known:
            queue.insert(rng.randint(0, len(queue)), qid)

    # Очередь закончилась: начинаем новый круг с новой перемешкой
    if not queue:
        queue = list(by_id)
        rng.shuffle(queue)
        if done:
            cycle += 1
        done = []
        # чтобы последний вопрос прошлого круга не стал первым в новом
        if len(queue) > 1 and queue[0] == state.get("last_id"):
            j = rng.randint(1, len(queue) - 1)
            queue[0], queue[j] = queue[j], queue[0]

    qid = queue.pop(0)
    done.append(qid)
    new_state = {
        "cycle": cycle,
        "queue": queue,
        "done": done,
        "last_id": qid,
        "last_date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
    }
    return by_id[qid], new_state


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error_code": e.code, "description": "HTTP error"}


def send_quiz(q):
    payload = {
        "chat_id": CHAT_ID,
        "question": poll_text(q),
        "options": [{"text": o} for o in q["options"]],
        "type": "quiz",
        "is_anonymous": True,  # в каналах викторины всегда анонимные
        "explanation": q["explanation"],
    }
    # Новый параметр (Bot API 9.6+) и старый как запасной вариант
    variants = [{"correct_option_ids": [q["correct"]]}, {"correct_option_id": q["correct"]}]
    last = None
    for extra in variants:
        for _ in range(2):
            r = tg_call("sendPoll", {**payload, **extra})
            if r.get("ok"):
                return r
            last = r
            if r.get("error_code") == 429:  # слишком часто — подождать и повторить
                time.sleep(min(r.get("parameters", {}).get("retry_after", 10), 60))
                continue
            break
    raise RuntimeError(f"Telegram отклонил запрос: {last}")


def main():
    bank = load_bank()
    good = []
    for q in bank:
        err = validate(q)
        if err:
            print(f"Пропускаю {q.get('id', '?')}: {err}")
        else:
            good.append(q)
    if not good:
        print("В questions.json нет корректных вопросов.")
        return 1

    state = load_json(STATE_PATH, {})
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if not FORCE and not DRY_RUN and state.get("last_date") == today:
        print("Сегодня вопрос уже опубликован, пропускаю.")
        return 0

    q, new_state = choose_next(good, state, random.Random())
    print(f"Вопрос {q['id']} (круг {new_state['cycle']}, "
          f"осталось в круге: {len(new_state['queue'])})")
    print(poll_text(q))

    if DRY_RUN:
        print("DRY_RUN: ничего не отправлено.")
        return 0
    if not TOKEN or not CHAT_ID:
        print("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        return 1

    send_quiz(q)
    save_json(STATE_PATH, new_state)  # состояние сохраняем только после успешной отправки
    print("Опубликовано.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
