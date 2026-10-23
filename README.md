# Thai Gazette monitor

Ежедневный монитор Royal Thai Gazette (ratchakitcha.soc.go.th) для туристов:
визы, безвиз, сроки пребывания, бордерраны, границы, паспорта.
Находит новые объявления, вытаскивает текст PDF через OCR, переводит на русский
и публикует пост в Telegram-канал **@ratchakitcha**.

Работает целиком на GitHub Actions — бесплатно, без своего сервера и **без обхода
Cloudflare**: сайт закрыт CF Managed Challenge для HTML, но PDF-ы отдаёт напрямую.

## Ключевая идея

`https://ratchakitcha.soc.go.th/documents/<id>.pdf` → 200/404 без челленджа.
`curl`/браузер на HTML-страницы → 403 `cf-mitigated: challenge`.

Поэтому список новых документов получаем перебором id вперёд от последнего
известного, а не скачиванием Excel через браузер. Это снимает зависимость от
Chrome/Xvfb и от дата-центрового IP раннера.

## Как устроено

| Шаг | Файл | Что делает |
|---|---|---|
| 1 | `scripts/gazette_scan.py` | HEAD-проверка окна id (`last_id-ahead..last_id+behind`), скачивание новых PDF, дата из шапки PDF (`pdftotext`, тайская строка → CE), легаси-сканы отсекаются по дате без OCR, OCR первой страницы (`pdftoppm -r 400` + `tesseract -l tha+eng`) |
| 2 | `scripts/gazette_diff.py` | Фильтр «про туристов» по ключевикам (`is_tourist_relevant_page` — только шапка документа, иначе негативы из тела глушат визовые лоты) |
| 3 | `scripts/translate.py` | Перевод: DeepSeek (если есть `DEEPSEEK_API_KEY`) либо бесплатный Google gtx с ретраями на 429 |
| 4 | `scripts/gazette_post.py` | Публикация в канал через бота @socppostingbot, чанки ≤ 4000 символов |
| — | `scripts/run_pipeline.py` | Оркестратор + проверка годов (BE→CE, мусорные годы OCR вырезаются) |
| alt | `scripts/gazette_download.py` | Excel-путь через DrissionPage + Chrome под Xvfb (CF проходит только на residential IP) — `--source excel` |

## Запуск руками

```bash
# основной (CF-free) путь
python3 scripts/run_pipeline.py --dry-run

# боевой прогон
python3 scripts/run_pipeline.py

# Excel-путь (нужен Chrome + xvfb + DrissionPage, проходит CF только с резидентного IP)
xvfb-run -a python3 scripts/run_pipeline.py --source excel --dry-run
```

Зависимости: `openpyxl`; системно — `poppler-utils`, `tesseract-ocr`, `tesseract-ocr-tha`.
Для `--source excel` дополнительно `pip install DrissionPage` + `google-chrome` + `xvfb`.

## Секреты репозитория

| Secret | Обязателен | Назначение |
|---|---|---|
| `SOCPOST_BOT_TOKEN` | да | токен бота-публикатора |
| `SOCPOST_CHAT_ID` | да | chat_id канала |
| `DEEPSEEK_API_KEY` | нет | перевод + резюме вместо Google gtx (стоимость ~доли цента за прогон) |

Без `DEEPSEEK_API_KEY` работает на бесплатном gtx (перевод без резюме, качество ниже,
на всплесках возможен 429 — есть 4 попытки с бэкоффом).

## Расписание

`.github/workflows/daily-gazette.yml` — `0 1 * * *` UTC = 08:00 Asia/Bangkok.
GitHub может задержать запуск на 5–40 минут. Ручной прогон:
`gh workflow run daily-gazette.yml -f dry_run=true`.

После прогона `state/seen_ids.json` и `state/scan_state.json` коммитятся обратно
(`[skip ci]`), пост и лог уходят в артефакт `gazette-post`.

## Питфоллы

- **429 от сайта.** 6 параллельных запросов с IP Azure дали сплошные 429.
  Сейчас: 2 воркера, пауза 0.6 с между запросами, бэкофф 8→16→32→60 с, до 5 попыток.
  Прогон окна 370 id занимает ~6 минут.
- **ID не хронологичны.** Под свежими номерами встречаются документы 2004 года
  (массовая загрузка старых газет). Поэтому дата берётся из шапки PDF, а не из
  `CreationDate`, и всё старше `--max-age` (90 дней) отсекается без OCR.
- **Дата из `CreationDate` врёт** для старых сканов (это дата сканирования). Шапка
  новых TCPDF-документов набрана корректным шрифтом, поэтому `pdftotext` отдаёт
  «22 ตุลาคม 2569» чисто; тело документа — нет (битый ToUnicode), нужен OCR.
- **OCR путает тайские цифры** (๒๕๖๙ → ๒๕๒๐). Годы в посте проверяются:
  2400–2600 → CE (−543), неверные годы вырезаются вместе с предложением.
- **Cloudflare** на HTML: `curl` 403, Camoufox/Playwright/headless не проходят,
  DrissionPage + настоящий Chrome под Xvfb проходит — но только с residential IP,
  с GitHub-раннера нет.
- `seen_ids.json` тримится до последних 5000 id (`KEEP_IDS`).
