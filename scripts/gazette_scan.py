#!/usr/bin/env python3
"""
Сканер Royal Thai Gazette БЕЗ Cloudflare.

Наблюдение: HTML-страницы сайта отдают CF Managed Challenge (403), но
документы по прямой ссылке /documents/<id>.pdf отдаются нормально (200/404).
Поэтому вместо браузера перебираем id документов вперёд от последнего
известного и берём только невиденные.

Дату берём из pdfinfo (CreationDate — надёжнее OCR), текст — OCR первой
страницы (tesseract -l tha+eng).

Запуск: python3 scripts/gazette_scan.py [--ahead N] [--behind N] [--limit N]
Вывод: JSON {"docs": [{id, date, url, text}], "scanned": N, "last_id": M}
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
GAZETTE_DIR = Path(os.environ.get("GAZETTE_DIR") or (REPO_ROOT / "state"))
SEEN_FILE = GAZETTE_DIR / "seen_ids.json"
SCAN_FILE = GAZETTE_DIR / "scan_state.json"

BASE = "https://ratchakitcha.soc.go.th/documents/%s.pdf"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def log(msg):
    print(msg, flush=True)


# Сайт режет всплески запросов (429). Ходим по одному с паузой и бэкоффом.
_pace_lock = threading.Lock()
_last_request = [0.0]
PACE = 0.6  # секунд между запросами ко всему хосту


def _pace():
    with _pace_lock:
        wait = PACE - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


def _request(url, method="GET", timeout=45):
    """GET/HEAD с бэкоффом на 429/503."""
    delay = 8
    for attempt in range(5):
        _pace()
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, (r.read() if method == "GET" else b"")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 4:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    pause = int(retry_after) if retry_after else delay
                except ValueError:
                    pause = delay
                log(f"  {e.code} rate-limit, пауза {pause}s (попытка {attempt + 1}/5)")
                time.sleep(pause)
                delay = min(delay * 2, 60)
                continue
            return e.code, b""
        except Exception:
            if attempt < 2:
                time.sleep(delay)
                continue
            return 0, b""
    return 0, b""


def head_ok(doc_id):
    """Есть ли документ (200) — без скачивания тела."""
    status, _ = _request(BASE % doc_id, method="HEAD", timeout=25)
    return status == 200


def fetch(doc_id):
    status, body = _request(BASE % doc_id, timeout=60)
    if status != 200 or not body:
        raise RuntimeError(f"HTTP {status}")
    return body


def pdf_date(pdf_path):
    """Дата создания PDF -> ДД.ММ.ГГГГ (григорианский)."""
    try:
        out = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""
    m = re.search(r"CreationDate:\s+\w+\s+(\w{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})", out)
    if m and m.group(1) in MONTHS:
        return f"{int(m.group(2)):02d}.{MONTHS[m.group(1)]:02d}.{m.group(6)}"
    return ""


TH_MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
             "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
TH_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def header_date(pdf_path):
    """Дата публикации из шапки PDF (тайская строка «22 ตุลาคม 2569»).

    Работает для «новых» TCPDF-документов: их шапка набрана шрифтом с
    корректными кодами, поэтому pdftotext (без OCR) отдаёт её чисто.
    Для старых сканов (Acrobat Distiller, шрифт-сдвиг) вернёт None.
    """
    try:
        out = subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(pdf_path), "-"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None
    m = re.search(r"(\d{1,2})\s+(" + "|".join(TH_MONTHS) + r")\s+(\d{4})",
                  out.translate(TH_DIGITS))
    if not m:
        return None
    d, mo, y = int(m.group(1)), TH_MONTHS.index(m.group(2)) + 1, int(m.group(3))
    ce = y - 543 if y > 2400 else y
    return f"{d:02d}.{mo:02d}.{ce}"


def is_recent(date_str, max_age_days):
    """Дата (ДД.ММ.ГГГГ) не старше max_age_days. Пустая дата -> False."""
    try:
        from datetime import date, datetime
        d = datetime.strptime(date_str, "%d.%m.%Y").date()
    except Exception:
        return False
    return (date.today() - d).days <= max_age_days


def ocr_first_page(pdf_path):
    """OCR первой страницы: tesseract tha+eng, 400 dpi."""
    from gazette_pdf import pdf_to_text
    return pdf_to_text(str(pdf_path), max_pages=1)


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def process(doc_id, max_age):
    """Скачать PDF -> дата -> (если свежий) OCR первой страницы.

    Легаси-сканы (ID не всегда хронологичны: под свежими номерами попадаются
    документы 2004 года) отсекаются по дате без OCR — это экономит минуты.
    """
    try:
        blob = fetch(doc_id)
    except Exception as e:
        log(f"  {doc_id}: fetch error {e}")
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(blob)
        path = fh.name
    try:
        date = header_date(path) or pdf_date(path)
        if not is_recent(date, max_age):
            return {"id": doc_id, "date": date, "old": True}
        text = ocr_first_page(path)
    except Exception as e:
        log(f"  {doc_id}: ocr error {e}")
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not text:
        log(f"  {doc_id}: пустой OCR")
        return {"id": doc_id, "date": date, "empty": True}
    return {"id": doc_id, "date": date, "url": BASE % doc_id, "text": text}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ahead", type=int, default=250, help="сколько id впереди проверять")
    ap.add_argument("--behind", type=int, default=120, help="окно назад (поздние публикации)")
    ap.add_argument("--limit", type=int, default=150, help="максимум документов на OCR за прогон")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-age", type=int, default=90, help="публикации старше N дней не берём")
    args = ap.parse_args()

    seen = set(load_json(SEEN_FILE, []))
    scan_state = load_json(SCAN_FILE, {})
    last_id = int(scan_state.get("last_id") or 0)
    if not last_id and seen:
        last_id = max(int(i) for i in seen if str(i).isdigit())

    start = max(1, last_id - args.behind) if last_id else 1
    end = last_id + args.ahead if last_id else 1
    if not last_id:
        print(json.dumps({"error": "нет состояния: сначала нужен last_id (запусти с --bootstrap)"}))
        return 1

    candidates = [i for i in range(start, end) if str(i) not in seen]
    log(f"window {start}..{end}, кандидатов {len(candidates)}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        exists = list(pool.map(head_ok, candidates))

    live = [str(i) for i, ok in zip(candidates, exists) if ok]
    log(f"живых PDF: {len(live)}")
    docs = []
    to_ocr = live[: args.limit]
    if len(live) > args.limit:
        log(f"  вним.: беру первые {args.limit} из {len(live)}")

    docs = []
    skipped = []
    empties = []
    for doc_id in to_ocr:
        d = process(doc_id, args.max_age)
        if not d:
            continue
        if d.get("old") or d.get("empty"):
            skipped.append(d["id"])
            log(f"  - {doc_id} {d['date']} стар/пуст, пропуск")
        else:
            docs.append(d)
            log(f"  + {doc_id} {d['date']} ({len(d['text'])} симв.)")

    max_seen = max([int(x) for x in [d["id"] for d in docs] + [int(s) for s in skipped]], default=last_id)
    # Раз мы прошли вперёд (пусть и с пустыми OCR) — двигаем указатель только
    # если дошли до конца окна без упора в limit.
    new_last = max(last_id, max_seen)
    if len(live) > args.limit:
        new_last = max_seen

    from datetime import datetime
    save_json(SCAN_FILE, {"last_id": new_last, "updated": datetime.now().strftime("%Y-%m-%d %H:%M")})
    print(json.dumps({"docs": docs, "skipped": skipped, "scanned": len(to_ocr),
                      "last_id": new_last}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
