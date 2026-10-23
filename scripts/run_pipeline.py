#!/usr/bin/env python3
"""
Оркестратор пайплайна Royal Thai Gazette: download -> diff -> PDF/OCR ->
перевод -> пост в Telegram-канал.

Запуск (весь процесс под Xvfb, чтобы Chrome прошёл Cloudflare):
    xvfb-run -a python3 scripts/run_pipeline.py            # боевой прогон
    xvfb-run -a python3 scripts/run_pipeline.py --dry-run   # без публикации
    python3 scripts/run_pipeline.py --skip-download --dry-run  # только diff+OCR

Коды возврата: 0 — успех (в т.ч. «ничего нового»), 1 — сбой скачивания/публикации.
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

sys.path.insert(0, str(SCRIPTS))
from translate import translate_doc  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


def run(cmd, timeout=900, check_env=None):
    env = {**os.environ, **(check_env or {})}
    env["GAZETTE_DIR"] = str(GAZETTE_DIR)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def download():
    log("[1/4] Скачивание Excel (DrissionPage + Chrome)...")
    r = run([sys.executable, str(SCRIPTS / "gazette_download.py")], timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-12:]:
        log("   " + line)
    if "DONE" not in out:
        log("   FAIL: нет DONE в выводе скачивания")
        return False
    return True


def diff():
    log("[2/4] Diff со seen_ids.json...")
    r = run([sys.executable, str(SCRIPTS / "gazette_diff.py"), "diff"], timeout=300)
    if r.returncode != 0:
        log("   FAIL: " + (r.stderr or "")[-400:])
        return None
    try:
        return json.loads(r.stdout)
    except Exception as e:
        log(f"   FAIL: не разобрал JSON ({e}): {r.stdout[:300]}")
        return None


def fetch_pdf(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def ocr(pdf_bytes, max_pages=4):
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


def fmt_date(raw):
    """01/10/2026 -> 01.10.2026 (Excel уже в григорианском календаре)."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(raw).strip())
    if m:
        return f"{m.group(1).zfill(2)}.{m.group(2).zfill(2)}.{m.group(3)}"
    return str(raw).strip()


YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2}|2[45]\d{2})\b")


def fix_years(summary, doc_year):
    """OCR путает тайские цифры — вычищаем неверные годы.

    - 2400..2600 — буддийская эра, переводим в CE (-543);
    - год вне {doc_year-1 .. doc_year+1} и < 2020 — почти наверняка ошибка OCR,
      предложение с ним выбрасываем (дата документа берётся из Excel).
    """
    if not summary:
        return summary
    sentences = [s.strip() for s in summary.split(". ") if s.strip()]
    keep = []
    for s in sentences:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="не публиковать, только напечатать пост")
    ap.add_argument("--skip-download", action="store_true", help="использовать уже скачанные xlsx")
    ap.add_argument("--max-pages", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="обработать максимум N документов")
    args = ap.parse_args()

    if not args.skip_download:
        if not download():
            print("PIPELINE_FAILED: download")
            return 1
    else:
        log("[1/4] Скачивание пропущено (--skip-download)")

    d = diff()
    if d is None:
        print("PIPELINE_FAILED: diff")
        return 1

    relevant = d.get("relevant") or []
    log(f"   новых документов: {d.get('total_new')}, релевантных туристам: {len(relevant)}"
        f", в базе: {d.get('seen_total')}")
    if d.get("first_run"):
        log("   ВНИМАНИЕ: state был пуст — это первичная инициализация, постим только реально новые.")

    if not relevant:
        print("NOTHING_NEW")
        return 0

    if args.limit:
        relevant = relevant[: args.limit]

    log(f"[3/4] PDF + OCR + перевод ({len(relevant)} док.)...")
    blocks = []
    for doc in relevant:
        log(f"   - {doc['id']} {fmt_date(doc['date'])} {doc['title'][:70]}")
        body = ""
        try:
            body = ocr(fetch_pdf(doc["url"]), args.max_pages)
            log(f"     OCR: {len(body)} символов")
        except Exception as e:
            log(f"     OCR/PDF error: {e}")

        title_ru, summary_ru = translate_doc(doc["title"], body)
        if not title_ru:
            title_ru = doc["title"]
        m_year = re.search(r"(\d{4})$", fmt_date(doc["date"]))
        summary_ru = fix_years(summary_ru, int(m_year.group(1)) if m_year else None)
        if not summary_ru:
            summary_ru = "текст не извлечён, см. ссылку"

        blocks.append(
            f"📋 {title_ru} [{fmt_date(doc['date'])}]\n"
            f"📍 {summary_ru}\n"
            f"🔗 {doc['url']}"
        )

    post = build_post(blocks)
    POST_FILE.write_text(post, encoding="utf-8")
    log(f"[4/4] Пост собран: {len(post)} символов -> {POST_FILE}")

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

    print(f"PUBLISHED {len(blocks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
