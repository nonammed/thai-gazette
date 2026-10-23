#!/usr/bin/env python3
"""
Извлечение текста из PDF Royal Thai Gazette.

PDF генерирует TCPDF с подмножествами THSarabunPSK и битым ToUnicode CMap:
pdftotext / pymupdf / pdfminer дают мусор. Работает только рендер + OCR:
pdftoppm -png -r 400 | tesseract -l tha

Запуск: python3 scripts/gazette_pdf.py <pdf_path> [max_pages]
Вывод: текст в stdout (пусто = текст не извлечён).
"""
import sys, os, io, subprocess, tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

TESSDATA_PREFIX = Path.home() / ".local" / "share" / "tessdata"
if (TESSDATA_PREFIX / "tha.traineddata").exists():
    os.environ.setdefault("TESSDATA_PREFIX", str(TESSDATA_PREFIX))


def pdf_to_text(pdf_path, max_pages=4):
    pdf_path = str(pdf_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, "page")
        # Рендер страниц в PNG (400 dpi — качество для OCR)
        r = subprocess.run(
            ["pdftoppm", "-png", "-r", "400", "-f", "1", "-l", str(max_pages), pdf_path, prefix],
            capture_output=True, timeout=180,
        )
        if r.returncode != 0:
            return ""
        pages = sorted(Path(tmpdir).glob("page*.png"))
        texts = []
        for p in pages:
            t = subprocess.run(
                ["tesseract", str(p), "stdout", "-l", "tha+eng", "--psm", "3"],
                capture_output=True, timeout=240,
            )
            if t.returncode == 0:
                txt = t.stdout.decode("utf-8", "replace").strip()
                if txt:
                    texts.append(txt)
        return "\n\n".join(texts)


def main():
    if len(sys.argv) < 2:
        print("usage: gazette_pdf.py <pdf_path> [max_pages]", file=sys.stderr)
        sys.exit(2)
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    text = pdf_to_text(sys.argv[1], max_pages)
    # Схлопываем пустые строки
    out = "\n".join(l for l in text.splitlines() if l.strip())
    sys.stdout.write(out)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
