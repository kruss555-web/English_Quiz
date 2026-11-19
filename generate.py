#!/usr/bin/env python3
"""Пополняет questions.json новыми вопросами через Gemini API.

Как это работает:
  1. Выбирается уровень и тема, которых в банке меньше всего (банк растёт равномерно).
  2. Модель пишет пачку вопросов; варианты перемешиваются в коде.
  3. Вторым запросом модель независимо решает эти вопросы без подсказок.
     Остаются только те, где ответ совпал и нет неоднозначности.
  4. Дубликаты и почти-дубликаты отбрасываются.
  5. После каждой пачки банк сохраняется, поэтому при исчерпании лимита
     бесплатного тарифа прогресс не теряется, а на следующий день работа продолжится.
"""
import difflib
import os
import random
import re
import sys
import time
import json
import urllib.error
import urllib.request

from common import LEVELS, load_bank, save_bank, validate

API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"
EXPLANATION_LANG = os.environ.get("EXPLANATION_LANG") or "Russian"
BATCH_SIZE = int(os.environ.get("BATCH_SIZE") or 10)
PER_RUN = int(os.environ.get("PER_RUN") or 100)        # максимум новых за один запуск
TARGET_SIZE = int(os.environ.get("TARGET_SIZE") or 1000)  # до скольких пополнять банк
COUNT = (os.environ.get("COUNT") or "").strip()        # если задано: добавить ровно столько
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY") or 7)  # пауза между запросами, сек

TOPICS = {
    "B1": [
        "Grammar: Present Perfect", "Grammar: Past Simple vs Continuous",
        "Grammar: First Conditional", "Grammar: Second Conditional",
        "Grammar: Modal Verbs", "Grammar: Comparatives", "Grammar: Articles",
        "Grammar: Gerund or Infinitive", "Grammar: Passive Voice",
        "Grammar: Quantifiers", "Grammar: Future Forms", "Grammar: Relative Clauses",
        "Vocabulary: Prepositions", "Vocabulary: Phrasal Verbs",
        "Vocabulary: Everyday Collocations", "Vocabulary: Confusable Words",
        "Vocabulary: Word Formation",
    ],
    "B2": [
        "Grammar: Third Conditional", "Grammar: Wishes and Regrets",
        "Grammar: Reported Speech", "Grammar: Passive Constructions",
        "Grammar: Gerund or Infinitive", "Grammar: Linking Words",
        "Grammar: Modals of Deduction", "Grammar: Relative Clauses",
        "Grammar: Tenses in Context", "Grammar: Articles and Determiners",
        "Vocabulary: Phrasal Verbs", "Vocabulary: Verb Collocations",
        "Vocabulary: Idioms", "Vocabulary: Word Formation",
        "Vocabulary: Confusable Words", "Vocabulary: Prepositions",
    ],
    "C1": [
        "Grammar: Inversion", "Grammar: Mixed Conditionals", "Grammar: Subjunctive",
        "Grammar: Cleft Sentences", "Grammar: Advanced Passives",
        "Grammar: Participle Clauses", "Grammar: Perfect Modals",
        "Grammar: Emphasis and Ellipsis", "Vocabulary: Idioms",
        "Vocabulary: Advanced Collocations", "Vocabulary: Word Formation",
        "Vocabulary: Confusable Words", "Vocabulary: Phrasal Verbs",
        "Vocabulary: Formal Register", "Vocabulary: Prepositions",
    ],
}


class SoftStop(Exception):
    """Лимит или временный сбой API: сохраняем прогресс и спокойно завершаемся."""


class Fatal(Exception):
    """Ошибка настройки (неверный ключ, имя модели): запуск помечается как неудачный."""


def gemini(prompt, temperature):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": API_KEY}
    waits = [20, 45, 90]
    data = None
    for attempt in range(len(waits) + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504):
                if attempt < len(waits):
                    print(f"  Gemini вернул {e.code}, жду {waits[attempt]} с и повторяю...")
                    time.sleep(waits[attempt])
                    continue
                raise SoftStop(f"Gemini {e.code}: {detail}")
            raise Fatal(f"Gemini API {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < len(waits):
                time.sleep(waits[attempt])
                continue
            raise SoftStop(f"сеть недоступна: {e}")
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except (KeyError, IndexError, TypeError):
        return None  # например, ответ заблокирован фильтром


def parse_list(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    return data if isinstance(data, list) else None


def norm(s):
    return re.sub(r"[^a-z0-9а-яё]+", " ", s.lower()).strip()


def too_similar(text, others):
    n = norm(text)
    for o in others:
        m = difflib.SequenceMatcher(None, n, o)
        if m.real_quick_ratio() >= 0.88 and m.quick_ratio() >= 0.88 and m.ratio() >= 0.88:
            return True
    return False


def pick_cell(bank, rng):
    """Уровень и тему выбираем по принципу «где вопросов меньше всего»."""
    per_level = {lvl: 0 for lvl in LEVELS}
    per_topic = {}
    for q in bank:
        per_level[q["level"]] = per_level.get(q["level"], 0) + 1
        key = (q["level"], q["topic"])
        per_topic[key] = per_topic.get(key, 0) + 1
    low = min(per_level.values())
    level = rng.choice([lvl for lvl, c in per_level.items() if c == low])
    low_t = min(per_topic.get((level, t), 0) for t in TOPICS[level])
    topic = rng.choice([t for t in TOPICS[level] if per_topic.get((level, t), 0) == low_t])
    return level, topic


def generation_prompt(level, topic, recent, n):
    avoid = "\n".join(f"- {r}" for r in recent) or "(none yet)"
    return f"""You are an experienced English teacher and exam writer (Cambridge / CEFR).
Write {n} NEW multiple-choice questions for learners at CEFR level {level} ({LEVELS[level]}) on the topic: "{topic}".

Rules:
- Each question has exactly 4 options and exactly ONE correct option. Double-check that no other option is acceptable in standard English.
- Distractors must be plausible (typical learner mistakes), never silly.
- The question is written in English: a gap-fill sentence with "___" or a short direct question, at most 200 characters.
- Each option is at most 60 characters. Never use "all of the above" or "none of the above".
- "explanation" is written in {EXPLANATION_LANG}, at most 170 characters, plain text without markdown: it states the rule that makes the answer correct and, if space allows, why the main distractor is wrong.
- The difficulty must genuinely match {level}; do not make the questions too easy.
- Use varied contexts, vocabulary and sentence structures. Do NOT repeat or paraphrase these existing questions:
{avoid}

Return ONLY a JSON array of {n} objects, each of the form:
{{"question": "...", "options": ["...", "...", "...", "..."], "correct": 0, "explanation": "..."}}
where "correct" is the 0-based index of the correct option."""


def verification_prompt(cands):
    blocks = []
    for i, q in enumerate(cands):
        opts = "\n".join(f"   {'ABCD'[j]}) {o}" for j, o in enumerate(q["options"]))
        blocks.append(f"{i}. {q['question']}\n{opts}")
    return (
        "You are a strict English exam checker. For each question below choose the single best option. "
        "If more than one option is acceptable in standard English, or none is, set ambiguous to true.\n"
        'Return ONLY a JSON array like [{"i": 0, "answer": "B", "ambiguous": false}, ...].\n\n'
        + "\n\n".join(blocks)
    )


def clean_candidate(raw, level, topic):
    if not isinstance(raw, dict):
        return None
    try:
        correct = int(raw.get("correct"))
    except (TypeError, ValueError):
        return None
    opts = raw.get("options")
    if not isinstance(opts, list):
        return None
    q = {
        "level": level,
        "topic": topic,
        "question": str(raw.get("question", "")).strip(),
        "options": [str(o).strip() for o in opts],
        "correct": correct,
        "explanation": str(raw.get("explanation", "")).strip(),
    }
    return None if validate(q) else q


def shuffle_options(q, rng):
    """LLM любят ставить правильный ответ на B/C, поэтому перемешиваем сами."""
    right = q["options"][q["correct"]]
    rng.shuffle(q["options"])
    q["correct"] = q["options"].index(right)


def verified_indexes(cands):
    res = parse_list(gemini(verification_prompt(cands), 0))
    ok = set()
    for item in res or []:
        try:
            i = int(item["i"])
            ans = item["answer"]
            ans = "ABCD".index(ans.strip().upper()[:1]) if isinstance(ans, str) else int(ans)
            if not item.get("ambiguous") and 0 <= i < len(cands) and ans == cands[i]["correct"]:
                ok.add(i)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return ok


def main():
    if not API_KEY:
        print("Не задан GEMINI_API_KEY (секрет репозитория).")
        return 1
    bank = load_bank()
    if COUNT:
        to_add = int(COUNT)
    else:
        to_add = min(PER_RUN, TARGET_SIZE - len(bank))
    if to_add <= 0:
        print(f"В банке {len(bank)} вопросов, цель {TARGET_SIZE} достигнута. Ничего не делаю.")
        return 0
    print(f"Банк: {len(bank)}. Нужно добавить: {to_add}. Модель: {MODEL}")

    rng = random.Random()
    seen = {norm(q["question"]) for q in bank}
    next_id = max((int(q["id"][1:]) for q in bank if str(q.get("id", "")).startswith("q")), default=0) + 1
    added = 0
    failures = 0
    try:
        while added < to_add and failures < 5:
            level, topic = pick_cell(bank, rng)
            same_cell = [q["question"] for q in bank if q["level"] == level and q["topic"] == topic]
            same_cell_norm = [norm(t) for t in same_cell]
            print(f"Пачка: {level} / {topic}")

            raw = parse_list(gemini(generation_prompt(level, topic, same_cell[-25:], BATCH_SIZE), 1.0))
            time.sleep(REQUEST_DELAY)
            cands, batch_keys = [], set()
            for r in raw or []:
                q = clean_candidate(r, level, topic)
                if not q:
                    continue
                key = norm(q["question"])
                if key in seen or key in batch_keys or too_similar(q["question"], same_cell_norm):
                    continue
                shuffle_options(q, rng)
                cands.append(q)
                batch_keys.add(key)
            if not cands:
                failures += 1
                continue

            ok = verified_indexes(cands)
            time.sleep(REQUEST_DELAY)
            accepted = [q for i, q in enumerate(cands) if i in ok][: to_add - added]
            print(f"  сгенерировано {len(cands)}, прошло проверку {len(accepted)}")
            if not accepted:
                failures += 1
                continue

            failures = 0
            for q in accepted:
                bank.append({"id": f"q{next_id:04d}", **q})
                next_id += 1
                seen.add(norm(q["question"]))
            save_bank(bank)
            added += len(accepted)
    except SoftStop as e:
        print(f"Остановка (прогресс сохранён): {e}")
    except Fatal as e:
        print(f"ОШИБКА: {e}")
        return 1

    if failures >= 5:
        print("Пять неудачных пачек подряд, останавливаюсь, чтобы не тратить лимит.")
    print(f"Добавлено: {added}. Всего в банке: {len(bank)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
