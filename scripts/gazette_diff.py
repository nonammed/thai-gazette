#!/usr/bin/env python3
"""
Gazette diff — сравнивает скачанные Excel с прошлым состоянием.

Выводит JSON: total_new + relevant (документы про туристов: визы, безвиз,
пребывание, бордерраны, граница, паспорт). Отсеивает технические стандарты,
назначения чиновников, бизнес иностранцев, импорт товаров.

Режимы: init (сбросить состояние), diff (по умолчанию).
Каталог состояния: $GAZETTE_DIR (по умолчанию ../state рядом со скриптом)
"""
import sys, os, io, json
from pathlib import Path

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
GAZETTE_DIR = Path(os.environ.get("GAZETTE_DIR") or (REPO_ROOT / "state"))
STATE_FILE = GAZETTE_DIR / "seen_ids.json"

# Сильные сигналы — точно про туристов/визы/пребывание/границу
STRONG_KEYWORDS = [
    # Визы
    "ตรวจลงตรา", "วีซ่า", "ฟรีวีซ่า", "visa", "VOA",
    "ยกเว้นการตรวจลงตรา", "ยกเว้นวีซ่า",
    # Пребывание / въезд
    "อยู่ในราชอาณาจักร", "ระยะเวลาพำนัก", "การพำนัก", "อยู่เกิน",
    # Граница / бордерран
    "ผ่านแดน", "ด่านพรมแดน", "ช่องทางอนุญาต", "ด่านตรวจคนเข้าเมือง",
    "ตรวจคนเข้าเมือง", "การเข้าเมือง", "ข้ามแดน",
    # Паспорт / документы
    "หนังสือเดินทาง", "เอกสารใช้แทนหนังสือเดินทาง",
    # Туристы
    "นักท่องเที่ยว",
]

# "ท่องเที่ยว" (tourism) — слишком общее (ловит ассоциации/турбизнес),
# поэтому только в сочетании с визовым контекстом
VISA_CONTEXT = [
    "ตรวจลงตรา", "วีซ่า", "visa", "พำนัก",
    "อยู่ในราชอาณาจักร", "ผ่านแดน", "หนังสือเดินทาง", "ยกเว้น",
]

# Исключения (компании с "วีซ่า" в названии и т.п.)
NEGATIVE = ["บริษัท", "จำกัด"]


def extract_docs(xlsx_path):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    docs = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 8 or not row[1]:
            continue
        date = str(row[0] or "").strip()
        title = str(row[1] or "").strip()
        url = str(row[7] or "").strip()
        if not url:
            continue
        doc_id = url.split("/")[-1].replace(".pdf", "")
        docs.append({"id": doc_id, "date": date, "title": title, "url": url})
    wb.close()
    return docs


def load_state():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding='utf-8')))
        except Exception:
            return set()
    return set()


def save_state(ids):
    GAZETTE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding='utf-8')


def is_tourist_relevant(title):
    """Документ касается туристов (визы/пребывание/граница/бордерран)?"""
    t = title.lower()
    for n in NEGATIVE:
        if n in t:
            return False
    for s in STRONG_KEYWORDS:
        if s.lower() in t:
            return True
    # комбинированное: "ท่องเที่ยว" + визовый контекст
    if "ท่องเที่ยว" in t:
        for c in VISA_CONTEXT:
            if c.lower() in t:
                return True
    return False


def is_tourist_relevant_page(text, window=600):
    """Тот же фильтр, но по OCR-тексту страницы: смотрим только «шапку»
    (заголовок документа), иначе негативы (บริษัท в теле) глушат визовые лоты."""
    return is_tourist_relevant((text or "")[:window])


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "diff"
    files = ["current.xlsx", "previous.xlsx"]

    if mode == "init":
        all_ids = set()
        for f in files:
            p = GAZETTE_DIR / f
            if p.exists():
                for d in extract_docs(p):
                    all_ids.add(d["id"])
        save_state(all_ids)
        print(json.dumps({"status": "init", "seen": len(all_ids)}, ensure_ascii=False))
        return

    if mode == "diff":
        seen = load_state()
        new_docs = []
        all_ids = set()

        for f in files:
            p = GAZETTE_DIR / f
            if not p.exists():
                continue
            for d in extract_docs(p):
                all_ids.add(d["id"])
                if d["id"] not in seen:
                    new_docs.append(d)

        # Дедуп по id (один документ может быть в обоих файлах месяца)
        uniq = {}
        for d in new_docs:
            uniq[d["id"]] = d
        new_docs = list(uniq.values())

        # Сохраняем состояние: first-run (пустой state) не должен вываливать
        # тысячи старых документов — только новые id помечаются seen.
        save_state(all_ids)

        relevant = [d for d in new_docs if is_tourist_relevant(d["title"])]

        print(json.dumps({
            "status": "diff",
            "first_run": len(seen) == 0,
            "total_new": len(new_docs),
            "relevant": relevant,
            "seen_total": len(all_ids),
        }, ensure_ascii=False, indent=2))
        return

    print(json.dumps({"error": f"unknown mode {mode}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
