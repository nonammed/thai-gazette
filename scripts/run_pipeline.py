#!/usr/bin/env python3
"""
Оркестратор пайплайна Royal Thai Gazette.

Два источника списка документов:

  enum  (по умолчанию) — БЕЗ Cloudflare: перебор /documents/<id>.pdf вперёд
        от последнего известного id. PDF-ы отдаются без челленджа, дата из
        pdfinfo, текст из OCR первой страницы.
  excel — «классический» путь: Chrome под Xvfb проходит CF Managed Challenge
        и качает месячный Excel. Нужен только если понадобились настоящие
        заголовки из реестра (на GitHub Actions CF не проходит).

Запуск:
    python3 scripts/run_pipeline.py                 # enum, публикация
    python3 scripts/run_pipeline.py --dry-run       # без публикации
    python3 scripts/run_pipeline.py --source excel  # через Excel (нужен xvfb)

Коды возврата: 0 — успех (в т.ч. «ничего нового»), 1 — сбой.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
GAZETTE_DIR = Path(os.environ.get("GAZETTE_DIR") or (REPO_ROOT / "state"))
POST_FILE = Path(os.environ.get("POST_FILE") or (REPO_ROOT / "post.txt"))
SEEN_FILE = GAZETTE_DIR / "seen_ids.json"

sys.path.insert(0, str(SCRIPTS))
from translate import translate_doc  # noqa: E402
from gazette_diff import is_tourist_relevant_page  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
KEEP_IDS = 5000  # сколько последних id держим в seen_ids.json


def log(msg):
    print(msg, flush=True)


def run(cmd, timeout=900):
    env = {**os.environ, "GAZETTE_DIR": str(GAZETTE_DIR)}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


# ---------------------------------------------------------------- источники

def collect_excel():
    log("[1/4] Скачивание Excel (DrissionPage + Chrome под Xvfb)...")
    r = run([sys.executable, str(SCRIPTS / "gazette_download.py")], timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-10:]:
        log("   " + line)
    if "DONE" not in out:
        log("   FAIL: скачать Excel не удалось")
        return None

    log("[2/4] Diff со seen_ids.json...")
    r = run([sys.executable, str(SCRIPTS / "gazette_diff.py"), "diff"], timeout=300)
    if r.returncode != 0:
        log("   FAIL: " + (r.stderr or "")[-400:])
        return None
    try:
        data = json.loads(r.stdout)
    except Exception as e:
        log(f"   FAIL: не разобрал JSON ({e})")
        return None
    relevant = data.get("relevant") or []
    log(f"   новых: {data.get('total_new')}, релевантных: {len(relevant)}, в базе: {data.get('seen_total')}")
    return [{"id": d["id"], "date": fmt_date(d["date"]), "url": d["url"],
             "title_th": d["title"], "text": None} for d in relevant]


def collect_enum(args):
    log("[1/4] Сканирую /documents/ (без Cloudflare)...")
    cmd = [sys.executable, str(SCRIPTS / "gazette_scan.py"),
           "--ahead", str(args.ahead), "--behind", str(args.behind), "--limit", str(args.limit)]
    r = run(cmd, timeout=1800)
    out = (r.stdout or "")
    lines = out.strip().splitlines()[:-1]
    for line in lines[:4]:
        log("   " + line)
    if len(lines) > 14:
        log(f"   ... ({len(lines) - 14} строк лога сканера опущено)")
    for line in lines[-10:]:
        log("   " + line)
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        log(f"   FAIL: не разобрал JSON сканера ({e}): {out[-300:]}")
        return None
    docs = data.get("docs") or []
    skipped = data.get("skipped") or []
    relevant = [d for d in docs if is_tourist_relevant_page(d["text"])]
    log(f"[2/4] свежих PDF: {len(docs)}, старых/пустых пропущено: {len(skipped)},"
        f" релевантных туристам: {len(relevant)}")
    for d in relevant:
        log(f"   * {d['id']} {d['date']}")
    out_docs = [{"id": d["id"], "date": fmt_date(d["date"]), "url": d["url"],
                 "title_th": title_from_ocr(d["text"]), "text": d["text"]} for d in relevant]
    return out_docs, [d["id"] for d in docs] + skipped


# ---------------------------------------------------------------- обработка

def fetch_pdf(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def ocr(pdf_bytes, max_pages=1):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(pdf_bytes)
        path = fh.name
    try:
        r = run([sys.executable, str(SCRIPTS / "gazette_pdf.py"), path, str(max_pages)], timeout=600)
        return (r.stdout or "").strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def title_from_ocr(text):
    """Заголовок из OCR-шапки: всё после «เรื่อง», иначе первая строка."""
    head = (text or "")[:900]
    m = re.search(r"(?:เรื่อง|เรือง|เรื\s*อง)\s*(.{20,300})", head, re.S)
    if m:
        first_line = m.group(1).split("\n")[0]
        return " ".join(first_line.split())[:300]
    lines = [l.strip() for l in head.splitlines() if len(l.strip()) > 25]
    return lines[0][:300] if lines else ""


def fmt_date(raw):
    """01/10/2026 -> 01.10.2026 (Excel уже в григорианском календаре)."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(raw).strip())
    if m:
        return f"{m.group(1).zfill(2)}.{m.group(2).zfill(2)}.{m.group(3)}"
    return str(raw).strip()


YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2}|2[45]\d{2})\b")


def fix_years(summary, doc_year):
    """OCR путает тайские цифры — вычищаем неверные годы."""
    if not summary:
        return summary
    keep = []
    for s in [x.strip() for x in summary.split(". ") if x.strip()]:
        bad = False
        for m in YEAR.finditer(s):
            n = int(m.group(1))
            if 2400 <= n <= 2600:
                s = s.replace(m.group(1), str(n - 543))
                n -= 543
            if doc_year and abs(n - doc_year) > 1 and n < 2020:
                bad = True
        if not bad:
            keep.append(s)
    out = ". ".join(keep).strip()
    if out and not out.endswith((".", "!", "?")):
        out += "."
    return out


def build_post(blocks):
    head = "🇹🇭 КОРОЛЕВСКАЯ ГАЗЕТА ТАИЛАНДА\n━━━━━━━━━━━━━━━━━━\n\n"
    foot = "\n\n━━━━━━━━━━━━━━━━━━\n@ratchakitcha · следите за обновлениями"
    return head + "\n\n".join(blocks) + foot


def mark_seen(ids):
    seen = []
    if SEEN_FILE.exists():
        try:
            seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            seen = []
    merged = {str(x) for x in seen} | {str(i) for i in ids}
    numeric = sorted(int(x) for x in merged if str(x).isdigit())
    keep = numeric[-KEEP_IDS:]
    GAZETTE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    return len(keep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="не публиковать, только показать пост")
    ap.add_argument("--source", choices=["enum", "excel"], default="enum")
    ap.add_argument("--skip-download", action="store_true", help="(excel) использовать уже скачанные xlsx")
    ap.add_argument("--max-pages", type=int, default=1, help="страниц на OCR")
    ap.add_argument("--ahead", type=int, default=250)
    ap.add_argument("--behind", type=int, default=120)
    ap.add_argument("--limit", type=int, default=150, help="макс. PDF на OCR за прогон")
    args = ap.parse_args()

    scanned_ids = []
    if args.source == "excel":
        if args.skip_download:
            log("[1/4] Скачивание пропущено")
            r = run([sys.executable, str(SCRIPTS / "gazette_diff.py"), "diff"], timeout=300)
            try:
                data = json.loads(r.stdout)
                relevant = [{"id": d["id"], "date": fmt_date(d["date"]), "url": d["url"],
                             "title_th": d["title"], "text": None} for d in data.get("relevant", [])]
            except Exception as e:
                print(f"PIPELINE_FAILED: diff ({e})")
                return 1
        else:
            relevant = collect_excel()
            if relevant is None:
                print("PIPELINE_FAILED: excel source")
                return 1
    else:
        res = collect_enum(args)
        if res is None:
            print("PIPELINE_FAILED: enum source")
            return 1
        relevant, scanned_ids = res

    if not relevant:
        if scanned_ids:
            mark_seen(scanned_ids)
        print("NOTHING_NEW")
        return 0

    log(f"[3/4] OCR + перевод ({len(relevant)} док.)...")
    blocks = []
    for doc in relevant:
        text = doc.get("text")
        if not text:
            try:
                text = ocr(fetch_pdf(doc["url"]), args.max_pages)
                log(f"   {doc['id']}: OCR {len(text)} символов")
            except Exception as e:
                log(f"   {doc['id']}: OCR/PDF error {e}")
                text = ""
        if not doc.get("title_th"):
            doc["title_th"] = title_from_ocr(text)
        title_ru, summary_ru = translate_doc(doc["title_th"], text)
        if not title_ru:
            title_ru = doc["title_th"] or "Документ Королевской газеты"
        m_year = re.search(r"(\d{4})$", doc["date"])
        summary_ru = fix_years(summary_ru, int(m_year.group(1)) if m_year else None)
        if not summary_ru:
            summary_ru = "текст не извлечён, см. ссылку"
        blocks.append(f"📋 {title_ru} [{doc['date']}]\n📍 {summary_ru}\n🔗 {doc['url']}")

    post = build_post(blocks)
    POST_FILE.write_text(post, encoding="utf-8")
    log(f"[4/4] Пост: {len(post)} символов -> {POST_FILE}")

    if args.dry_run:
        print("---- POST ----")
        print(post)
        print("---- END POST ----")
        print("DRY_RUN_OK")
        return 0

    r = run([sys.executable, str(SCRIPTS / "gazette_post.py"), str(POST_FILE)], timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines():
        log("   " + line)
    if "DONE" not in out:
        print("PIPELINE_FAILED: publish")
        return 1

    mark_seen(scanned_ids + [d["id"] for d in relevant])
    print(f"PUBLISHED {len(blocks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
