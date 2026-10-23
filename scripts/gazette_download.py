#!/usr/bin/env python3
"""
Скачивание месячного Excel-отчёта Royal Thai Gazette.

DrissionPage + реальный Chrome под Xvfb проходят Cloudflare Managed Challenge,
затем через CDP fetch качаем /report_documents_monthly.php как base64.

Запуск: xvfb-run -a python3 scripts/gazette_download.py
Каталог состояния: $GAZETTE_DIR (по умолчанию ../state рядом со скриптом)
"""
import sys, os, io, json, time, base64
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from DrissionPage import ChromiumPage, ChromiumOptions

REPO_ROOT = Path(__file__).resolve().parent.parent
GAZETTE_DIR = Path(os.environ.get("GAZETTE_DIR") or (REPO_ROOT / "state"))
GAZETTE_DIR.mkdir(parents=True, exist_ok=True)

CHROME_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def log(msg):
    print(msg, flush=True)


def wait_for_cf(dp, max_wait=90):
    for i in range(max_wait):
        title = dp.title or ""
        if "Just a moment" not in title and "Один момент" not in title:
            return True
        if i > 0 and i % 5 == 0:
            dp.refresh()
        time.sleep(1)
    return False


def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def download_via_cdp(dp, month_val):
    js = """
    (async () => {
        const fd = new FormData();
        fd.append('month', '%s');
        const resp = await fetch('/report_documents_monthly.php', {
            method: 'POST',
            body: fd
        });
        if (!resp.ok) return JSON.stringify({error: 'HTTP ' + resp.status});
        const buf = await resp.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let binary = '';
        const chunk = 8192;
        for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        const b64 = btoa(binary);
        return JSON.stringify({ok: true, size: bytes.length, base64: b64});
    })()
    """ % month_val

    try:
        result = dp.run_cdp('Runtime.evaluate', expression=js, awaitPromise=True, returnByValue=True)
        if isinstance(result, dict):
            value = result.get('result', {}).get('value')
            if value:
                return value
            if result.get('exceptionDetails'):
                log("EXC: " + str(result['exceptionDetails'].get('exception', {}).get('description', ''))[:200])
        return None
    except Exception as e:
        log("CDP err: " + str(e))
        return None


def main():
    chrome = find_chrome()
    if not chrome:
        print("CHROME_NOT_FOUND", flush=True)
        sys.exit(1)

    targets = [('0', 'current.xlsx'), ('-1', 'previous.xlsx')]

    opts = ChromiumOptions()
    opts.set_browser_path(chrome)
    opts.headless(bool(os.environ.get("GAZETTE_HEADLESS")))
    opts.set_argument('--no-sandbox')
    opts.set_argument('--disable-dev-shm-usage')
    opts.set_argument('--disable-gpu')

    dp = ChromiumPage(addr_or_opts=opts)
    saved = 0

    try:
        dp.get("https://ratchakitcha.soc.go.th/")
        if not wait_for_cf(dp):
            print("CF_BLOCKED", flush=True)
            sys.exit(1)

        log(f"Страница загружена: {dp.title}")

        for month_val, out_name in targets:
            log(f"Скачиваю month={month_val} -> {out_name}...")
            result = download_via_cdp(dp, month_val)
            if not result:
                log(f"  FAILED для {month_val}")
                continue
            try:
                data = json.loads(result)
                if not data.get('ok'):
                    log(f"  Ошибка: {data}")
                    continue
                file_bytes = base64.b64decode(data['base64'])
                fpath = GAZETTE_DIR / out_name
                fpath.write_bytes(file_bytes)
                log(f"  СОХРАНЕНО: {out_name} ({data['size']} bytes)")
                saved += 1
            except Exception as e:
                log(f"  Ошибка парсинга: {e}")
    finally:
        dp.quit()

    if saved == 0:
        print("DOWNLOAD_FAILED", flush=True)
        sys.exit(1)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
