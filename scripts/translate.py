#!/usr/bin/env python3
"""
Перевод тайского текста на русский. Два бэкенда:

1. DeepSeek (если задан DEEPSEEK_API_KEY) — перевод + краткое резюме.
2. Google gtx (по умолчанию, без ключа и бесплатно) — построчный перевод.

Оба необязательны для запуска пайплайна: при полной ошибке возвращаем
пустую строку, и пост собирается с оригинальным тайским названием.
"""
import os
import re
import json
import time
import urllib.error
import urllib.request
import urllib.parse

GTX = "https://translate.googleapis.com/translate_a/single"
DEEPSEEK = "https://api.deepseek.com/chat/completions"


def _post_json(url, payload, headers, timeout=90):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _chunks(text, size=1800):
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut])
        text = text[cut:]
    if text.strip():
        parts.append(text)
    return parts


def google_translate(text, src="th", dst="ru"):
    """Бесплатный endpoint Google Translate (client=gtx). Без ключа, лимит на длину."""
    if not text or not text.strip():
        return ""
    out = []
    for part in _chunks(text):
        params = urllib.parse.urlencode({
            "client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": part,
        })
        data = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(f"{GTX}?{params}", timeout=45) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(3 * (2 ** attempt))  # 3s, 6s, 12s
                    continue
                print(f"  translate(google) error: {e}", flush=True)
                return ""
            except Exception as e:
                print(f"  translate(google) error: {e}", flush=True)
                return ""
        if data is None:
            return ""
        out.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
        time.sleep(0.6)  # не долбим endpoint
    return "".join(out).strip()


def deepseek_summary(title_th, body_th):
    """Резюме+перевод через DeepSeek. Возвращает (перевод названия, суть) или None."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    body = (body_th or "")[:6000]
    prompt = (
        "Ты переводишь тайские объявления Royal Gazette для русскоязычных туристов.\n"
        "Верни СТРОГО JSON: {\"title_ru\": \"...\", \"summary_ru\": \"...\"}\n"
        "title_ru — краткий перевод названия документа (до 120 символов).\n"
        "summary_ru — 2-5 предложений по-русски: что меняется, сроки, какие страны/категории,"
        " есть ли упоминание России (รัสเซีย). Только факты из текста, без домыслов."
        " Годы в документах указаны в буддийской эре (พ.ศ.): переводи их в CE (вычти 543)."
        " Если цифры OCR противоречивы — не приводи их вообще, ограничься сутью."
        " Если текст пустой или нечитаемый — summary_ru = \"текст не извлечён, см. ссылку\".\n\n"
        f"НАЗВАНИЕ: {title_th}\n\nТЕКСТ (OCR, может содержать ошибки распознавания):\n{body}"
    )
    try:
        resp = _post_json(
            DEEPSEEK,
            {
                "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            {"Authorization": f"Bearer {key}"},
        )
        content = resp["choices"][0]["message"]["content"]
        data = json.loads(content)
        return (data.get("title_ru", "").strip(), data.get("summary_ru", "").strip())
    except Exception as e:
        print(f"  translate(deepseek) error: {e}", flush=True)
        return None


NOISE_LINE = re.compile(
    r"^(หน้า|เล่ม|ตอนที่|ตอน|ราชกิจจานุเบกษา|ฉบับที่|ประกาศ|เลขที่)\s|"
    r"^(?:หน้า|เล่ม|ตอน)\s*\d|ราชกิจจานุเบกษา\s*เล่ม",
)
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def clean_body(text):
    """Убираем OCR-шапку документа и служебные строки."""
    keep = []
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) < 12:
            continue
        if NOISE_LINE.search(line):
            continue
        if sum(ch.isdigit() for ch in line) > len(line) * 0.5:
            continue
        keep.append(line.translate(THAI_DIGITS))
    return "\n".join(keep)


def _trim(text, limit=150):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit * 0.6 else limit].rstrip(" ,;:") + "…"


def _dedupe(title_ru, summary):
    """Если «суть» начинается с того же, что и заголовок — срезаем дубль."""
    if not summary:
        return summary
    head = title_ru.rstrip("…").strip()
    for size in (len(head), len(head) * 3 // 4, 50):
        if size >= 30 and summary[:size].lower().strip(" .,;:") == head[:size].lower().strip(" .,;:"):
            summary = summary[size:].lstrip(" .,;:—-")
            break
    return summary.strip()


def translate_doc(title_th, body_th):
    """-> (title_ru, summary_ru). Пустые строки, если перевести не удалось."""
    llm = deepseek_summary(title_th, body_th)
    if llm and (llm[0] or llm[1]):
        return _trim(llm[0] or google_translate(title_th)), llm[1]

    title_ru = _trim(google_translate(title_th))
    body = clean_body(body_th)
    if not body.strip():
        return title_ru, ""
    body_ru = google_translate(body[:2500])
    flat = " ".join(body_ru.split())
    sentences = [s.strip() for s in flat.split(". ") if len(s.strip()) > 15]
    summary = _dedupe(title_ru, ". ".join(sentences[:3]).strip())
    if summary and not summary.endswith((".", "!", "?")):
        summary += "."
    return title_ru, summary[:800]


if __name__ == "__main__":
    import sys
    print(translate_doc(sys.argv[1] if len(sys.argv) > 1 else "", ""))
