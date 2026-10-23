# Thai Gazette monitor

Ежедневный монитор Royal Thai Gazette (ratchakitcha.soc.go.th) для туристов:
визы, безвиз, сроки пребывания, бордерраны, границы, паспорта.
Находит новые объявления, вытаскивает текст PDF через OCR, переводит на русский
и публикует пост в Telegram-канал **@ratchakitcha**.

Работает целиком на GitHub Actions (бесплатно для public-репо), без своего сервера.

## Как устроено

| Шаг | Файл | Что делает |
|---|---|---|
| 1 | `scripts/gazette_download.py` | DrissionPage + настоящий Chrome под Xvfb проходит Cloudflare Managed Challenge, через CDP `fetch` качает месячные Excel (`month=0` — текущий, `month=-1` — предыдущий) в `state/` |
| 2 | `scripts/gazette_diff.py` | Сравнивает id документов с `state/seen_ids.json`, фильтрует туристически релевантные (списки ключевиков внутри) |
| 3 | `scripts/gazette_pdf.py` | PDF → `pdftoppm -r 400` → `tesseract -l tha+eng`. Без OCR текст не извлекается: PDF собраны TCPDF-шрифтами с битым ToUnicode |
| 4 | `scripts/translate.py` | Перевод на русский: DeepSeek (если задан `DEEPSEEK_API_KEY`) либо бесплатный Google gtx без ключа |
| 5 | `scripts/gazette_post.py` | Публикация в канал через бота @socppostingbot, чанки ≤ 4000 символов |
| — | `scripts/run_pipeline.py` | Оркестратор: download → diff → PDF/OCR → перевод → пост |

## Запуск руками

```bash
# локально (нужны xvfb, google-chrome, tesseract-ocr-tha, poppler-utils)
xvfb-run -a python3 scripts/run_pipeline.py --dry-run

# только diff + OCR + сборка поста, без скачивания Excel
python3 scripts/run_pipeline.py --skip-download --dry-run
```

Под `xvfb-run` запускать обязательно: Chrome стартует headed, так челлендж
Cloudflare проходится стабильно (headless детектится).

## Секреты репозитория

| Secret | Обязателен | Назначение |
|---|---|---|
| `SOCPOST_BOT_TOKEN` | да | токен бота-публикатора |
| `SOCPOST_CHAT_ID` | да | chat_id канала |
| `DEEPSEEK_API_KEY` | нет | качественный перевод + резюме вместо gtx |

Локально те же значения можно держать в `~/.hermes/scripts/gazette_post.env`
(переменные `SOCPOST_BOT_TOKEN`, `SOCPOST_CHAT_ID`).

## Расписание

`.github/workflows/daily-gazette.yml` — `0 1 * * *` UTC = 08:00 Asia/Bangkok.
GitHub может задержать запуск на 5–40 минут. Ручной прогон:
`gh workflow run daily-gazette.yml -f dry_run=true`.

После каждого прогона состояние `state/seen_ids.json` коммитится обратно в репозиторий
(коммит с `[skip ci]`), а собранный пост и лог уходят в артефакт `gazette-post`.

## Питфоллы

- **OCR путает тайские цифры.** Даты берутся только из Excel-поля `date`,
  а годы в тексте поста проверяются: 2400–2600 → CE (−543), неверные годы
  (< 2020 и не совпадают с датой документа) вырезаются вместе с предложением.
- **`curl`/`requests` получают 403** (`cf-mitigated: challenge`) — TLS-отпечаток.
  Помогает только реальный Chrome.
- **Camoufox, Playwright Chromium, headless Chrome** челлендж не проходят.
- PDF по прямой ссылке `/documents/<id>.pdf` отдаётся без Cloudflare.
- Если перевод недоступен, пост собирается с оригинальным тайским названием.
