# Бэкенд `tau` — автопубликация через TiktokAutoUploader

Это runbook для второго бэкенда `publish.py`. Разбор того, как устроен сам
[TiktokAutoUploader](https://github.com/makiisthenes/TiktokAutoUploader) (далее
TAU) и почему решения приняты именно такие — в [todo.md](../todo.md), раздел 11.

**Читать до всего остального.** TAU не использует официальный API: он логинится
браузером в веб-TikTok и повторяет запросами последовательность веб-загрузчика.
Это **нарушает ToS TikTok и может стоить аккаунта**. Бэкенд `api` остаётся
основным и по умолчанию; `tau` включается вручную, на канал, и осознанно.

Он **не поедет на GitHub Actions**, и это не недоработка: логин интерактивный,
куки — файл на диске, подпись требует node с playwright-chromium, а IP у
раннера датацентровый. Держать `sessionid` двух аккаунтов в секретах CI не
надо. Место этого бэкенда — постоянная машина: домашний ПК или VPS.

## Почему форк лежит рядом, а не внутри репозитория

TAU тянет `playwright`, `moviepy` и `undetected-chromedriver` прямо из git.
Ни одному из них нельзя жить в venv этого пайплайна. Поэтому чекаут отдельный,
со своим venv, а `publish.py` разговаривает с ним **подпроцессом** — не импортом.

## Установка

Патч снят с коммита `73475dbb67be5d8e5e7181af665fbf7f0db7fff4` (upstream,
2026-05-09). Если наверху что-то поменялось, `git apply` скажет об этом, и
патч надо будет пересобрать, а не подпихивать `--3way` вслепую.

```bash
git clone https://github.com/makiisthenes/TiktokAutoUploader.git ~/tau
cd ~/tau
git checkout 73475dbb67be5d8e5e7181af665fbf7f0db7fff4
git apply /path/to/reddit/tau/tau-synergy.patch
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv/Scripts/pip
.venv/bin/python -m playwright install chromium
(cd tiktok_uploader/tiktok-signature && npm install)
mkdir -p CookiesDir VideosDirPath output
cp .env.example .env
```

Нужен установленный Chrome — `undetected-chromedriver` поднимает системный.

## Логин

Один раз на аккаунт. Окно Chrome откроется, логинимся руками, куки сохранятся
в `CookiesDir/tiktok_session-<имя>.cookie`.

**Прокси задаётся до логина, а не после.** Патч учит браузер логина читать
`TIKTOK_PROXY`; смысл в том, чтобы `sessionid` был выпущен с того же IP, с
которого потом пойдут загрузки. Логин с домашнего адреса и загрузка через
прокси в другой стране — это сигнал сам по себе, а не маскировка.

```bash
cd ~/tau
TIKTOK_PROXY=http://user:pass@host:port .venv/bin/python cli.py login -n reddit_ru
```

Имя (`-n`) — это то, что потом уедет в `TIKTOK_TAU_USER`. На два канала —
два логина с разными именами **и разными прокси**.

Куки лежат в pickle. Читать чужой `.cookie` — исполнить чужой код; свои —
не проблема, но в git их класть нельзя ни в каком виде.

## Настройка нашей стороны

В `.env`, на канал:

```ini
TIKTOK_BACKEND=tau
TIKTOK_TAU_DIR=/home/me/tau
TIKTOK_TAU_USER=reddit_ru
TIKTOK_PROXY=http://user:pass@host:port
# TIKTOK_TAU_PYTHON=            # если venv не в <TIKTOK_TAU_DIR>/.venv
```

`TIKTOK_TAU_USER` и `TIKTOK_PROXY` — **без фолбэка между каналами**, по тому же
правилу, что и `TIKTOK_REFRESH_TOKEN`: молчаливый фолбэк отправил бы английский
ролик на русский аккаунт, а общий прокси свёл бы два аккаунта в один IP — ровно
то, против чего прокси и стоит. Для второго канала: `TIKTOK_TAU_USER_EN`,
`TIKTOK_PROXY_EN`, `TIKTOK_BACKEND_EN`.

Проверить, что конфиг сходится:

```bash
python config.py
```

## Что делает патч

| Файл | Правка | Зачем |
|---|---|---|
| `tiktok_uploader/tiktok.py` | `privacy_setting_info` берёт аргументы | Были захардкожены: публично, всё разрешено. Без этого `-vi 1` не работает и приватная обкатка невозможна |
| | добавлен `aigc_info` | Метку AI некуда было положить, а у нас есть `DECLARE_AI` |
| | успех возвращает `creation_id` | Возвращался `None` → `bool(None)` в их же адаптере → планировщик считал успех провалом и **публиковал повторно** |
| | `upload_to_tiktok` бросает исключение | Возвращал один `False` в распаковку на восемь переменных → `TypeError` вместо сообщения |
| | прокси уходит в подписчик | См. ниже |
| `tiktok_uploader/bot_utils.py` | `subprocess_jsvmp` принимает прокси | |
| `tiktok-signature/index.js`, `browser.js` | прокси в `chromium.launch()` | **Главное.** Подписчик открывал страницу на tiktok.com с реального IP на каждую публикацию, причём с `msToken` той же сессии в URL. Прямая склейка настоящего IP с аккаунтом |
| `tiktok_uploader/Browser.py` | прокси на логине из `TIKTOK_PROXY` | Было закомментировано с пометкой «Proxies not supported on login» |
| `cli.py` | ненулевой код возврата при провале | Провал завершался с кодом 0 — CLI нельзя было использовать как шаг чего-либо |

`aigc_info` — **единственная правка, положение которой не подтверждено живым
прогоном.** Точное место поля в payload `project/post/v1` по коду upstream не
восстанавливается (в закомментированной старой версии оно лежало в другой
структуре). Если TikTok не проставит метку — искать её надо в
`single_post_feature_info`, а не в `feature_common_info_list`.

## Обкатка перед первым настоящим постом

1. **Приватно, руками, один ролик.** По умолчанию `--next` ставит приватность —
   `--public` её снимает, и снимать её на этом шаге не надо.
   ```bash
   python publish.py out/<id>.mp4
   ```
   Смотрим: доехала ли подпись с тегами, встала ли приватность (это проверка
   первой правки патча), не прилетела ли капча.

2. **Замер утечки IP — не пропускать.** Это единственная проверка, которая
   отличает «прокси прописан» от «прокси работает». Пока идёт загрузка, смотрим
   исходящие соединения node-процесса:
   ```bash
   sudo lsof -i -a -c node -n | grep -v <ip-прокси>
   ```
   До правки подписчика там будет прямое соединение с tiktok.com; после —
   не должно быть ничего, кроме прокси.

3. **Только потом** `--next` в обычном режиме.

## Что теряется по сравнению с бэкендом `api`

`publish.py --status` для строк `tau:` не покажет статус: TAU отдаёт
`creation_id`, а `/post/publish/status/fetch/` про него ничего не знает. В
колонке `publish_id` лежит `tau:<creation_id>`, в колонке `backend` — `tau`.
`status()` на такой строке скажет об этом прямо, а не отдаст ошибку API.
