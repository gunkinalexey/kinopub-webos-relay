# kinopub-webos-client

Веб-клиент [KinoPub](https://kino.pub) для телевизоров на webOS и обычных
браузеров. Каталог, поиск, фильтры, закладки, история, подборки, выбор
качества/аудио/субтитров, плеер с Direct/Relay/HLS.

Ставится на свой сервер: бэкенд-мост к API KinoPub плюс статический
фронтенд, всё поднимается через Docker Compose.

История изменений — в [CHANGELOG.md](CHANGELOG.md).

**Ниже три шага, по нарастающей.** Можно остановиться на любом:

1. [Локальный запуск](#1-локальный-запуск) — попробовать за пять минут
2. [Развёртывание на Proxmox](#2-развёртывание-на-proxmox) — чтобы работало постоянно
3. [Красивое имя `kinopub.lan`](#3-имя-kinopublan-в-домашней-сети) — вместо адреса с портом

---

# 1. Локальный запуск

## Что понадобится

- **Docker** и **Docker Compose v2**
- Действующая подписка KinoPub
- ~1 ГБ под образы

**На Windows** нужен [Docker Desktop](https://www.docker.com/products/docker-desktop/)
— он приносит и Docker, и Compose. После установки его нужно **запустить**:
без работающего Docker Desktop команды `docker` не отвечают. В его настройках
должен быть включён бэкенд WSL 2 (по умолчанию так и есть).

На Linux и macOS достаточно самого Docker с плагином Compose.

## Запуск

```bash
git clone https://github.com/gunkinalexey/kinopub-webos-client.git kinopub
```

```bash
cd kinopub
```

```bash
cp .env.example .env
```

В `.env` нужно заполнить две строки — без них приложение не подключится к
API:

```
KINOPUB_CLIENT_ID=xbmc
KINOPUB_CLIENT_SECRET=<секрет этого клиента>
```

Это идентификатор **приложения**, а не вашего аккаунта: доступ к личной
библиотеке даёт отдельная привязка устройства по коду. Подходят публично
задокументированные учётные данные Kodi-плагина KinoPub — те же, что
используют другие сторонние клиенты. Остальные значения в `.env.example`
уже выставлены верно, трогать их не нужно.

```bash
docker compose up -d --build
```

Первая сборка займёт несколько минут: тянутся образы Python и nginx.

```bash
docker compose ps
```

Оба контейнера должны быть `running`. Открывайте **http://localhost:8080**.

## Привязка устройства

При первом открытии появится экран «Подключение устройства».

1. Нажать «Начать привязку устройства» — появится код
2. Ввести код на [kino.pub/device](https://kino.pub/device)
3. Приложение подхватит авторизацию само

Привязка нужна один раз: сессия лежит в SQLite внутри тома `kp_data` и
переживает перезапуск и пересборку контейнеров.

## Остановить

```bash
docker compose down
```

Данные при этом остаются — они в томе, а не в контейнере.

---

# 2. Развёртывание на Proxmox

Отдельный LXC-контейнер, работает постоянно, поднимается вместе с хостом.
Дальше — команды для Debian 13; адреса `192.168.0.x` замените на свои.

## 2.1. Контейнер

Шаблон, если ещё не скачан. Имя содержит точную версию и меняется от релиза
к релизу, поэтому берётся из списка, а не наизусть — и обязательно **под свою
архитектуру**:

```bash
pveam update
ARCH=$(dpkg --print-architecture)
TMPL=$(pveam available --section system | awk -v a="$ARCH" '$2 ~ /debian-13-standard/ && $2 ~ a {print $2}' | tail -1)
echo "Беру: $TMPL"
pveam download local "$TMPL"
```

> Фильтр по архитектуре обязателен: в списке лежат и `amd64`, и `arm64`.
> Возьмёте не ту — контейнер создастся без единой жалобы, а при запуске
> выдаст `__lxc_start: 2288 Failed to spawn container`, где про архитектуру
> нет ни слова.

```bash
pct create 120 local:vztmpl/$TMPL \
  --hostname kinopub \
  --cores 2 --memory 2048 --swap 1024 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.0.50/24,gw=192.168.0.1 \
  --nameserver 192.168.0.1 \
  --unprivileged 1 \
  --onboot 1 \
  --features nesting=1,keyctl=1 \
  --password
```

```bash
pct start 120
```

```bash
pct enter 120
```

Что здесь важно:

- **`--features nesting=1,keyctl=1`** — без них Docker внутри не заведётся.
  `nesting` нужен ещё и самому systemd 257 из Debian 13, Proxmox предупреждает
  об этом при создании.
- **Статический IP** — на него будет ссылаться прокси и DNS.
- **`--onboot 1`** — иначе после перезагрузки хоста сервис не поднимется.

## 2.2. Docker

Внутри контейнера:

```bash
apt update
```

```bash
apt install -y ca-certificates curl git
```

Добавить ключ и репозиторий Docker:

```bash
install -m 0755 -d /etc/apt/keyrings
```

```bash
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
```

```bash
chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
```

Установить:

```bash
apt update
```

```bash
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Проверить, что Docker в LXC действительно работает:

```bash
docker run --rm hello-world
```

Прошло — самое рискованное позади.

## 2.3. Код

### Если репозиторий публичный

```bash
cd /opt
```

```bash
git clone https://github.com/gunkinalexey/kinopub-webos-client.git kinopub
```

```bash
cd kinopub
```

### Если репозиторий приватный

Пароль от аккаунта GitHub не подойдёт — их отключили для git-операций.
Правильнее всего **deploy key**: он привязан к одному репозиторию и только на
чтение, в отличие от токена с правом `repo`, который открывает все ваши
репозитории на запись.

Создать ключ:

```bash
ssh-keygen -t ed25519 -C "kinopub-lxc" -f ~/.ssh/id_ed25519 -N ""
```

Показать публичную часть:

```bash
cat ~/.ssh/id_ed25519.pub
```

Вывод добавить в **репозиторий → Settings → Deploy keys → Add deploy key**,
галочку «Allow write access» **не** ставить.

Принять хост-ключ GitHub, иначе первый `git` спросит подтверждение:

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
```

Клонировать по SSH:

```bash
cd /opt
```

```bash
git clone git@github.com:<вы>/<репозиторий>.git kinopub
```

```bash
cd kinopub
```

## 2.4. Настройки и запуск

```bash
cp .env.example .env
```

Заполнить `KINOPUB_CLIENT_ID` и `KINOPUB_CLIENT_SECRET` так же, как в пункте
1. Если планируете пункт 3 — сразу поправьте origin:

```
CORS_ORIGINS=http://kinopub.lan
```

```bash
docker compose up -d --build
```

Проверить, что оба контейнера поднялись:

```bash
docker compose ps
```

Проверка:

```bash
curl -s http://localhost:8080/bridge/health
```

Ожидается `{"status":"ok",...}`. Снаружи — `http://192.168.0.50:8080`.

## Перенос существующей установки

Чтобы не привязывать устройство заново и сохранить настройки:

```bash
# на старой машине
docker compose cp backend:/data/kp.db ./kp.db
```

Скопировать файл на новую машину (`scp`), затем там — при уже запущенных
контейнерах:

```bash
docker compose cp ./kp.db backend:/data/kp.db
```

```bash
docker compose restart backend
```

> Файл содержит `access_token` и `refresh_token` KinoPub. **Не кладите его
> внутрь каталога репозитория** — оттуда он легко уезжает в коммит. `*.db`
> в `.gitignore` есть, но привычка держать такие файлы снаружи надёжнее.

Позиции просмотра переносить не обязательно: своей копии они не имеют,
читаются и пишутся прямо в историю KinoPub и потому одинаковы на всех
устройствах.

---

# 3. Имя `kinopub.lan` в домашней сети

Адрес с портом неудобно набирать на пульте телевизора, а при появлении других
сервисов порты начинают путаться. Решение — обратный прокси плюс запись в
DNS:

```
телевизор                     kinopub.lan
    │
    ▼
DNS роутера ──────────────►  192.168.0.40   (прокси)
    │
    ▼
Caddy :80  ──── по заголовку Host ────►  192.168.0.50:8080
                                              │
                                         nginx → backend
```

Прокси ставится **отдельным контейнером**, перед приложением. Само приложение
менять не нужно: порт оно публикует по-прежнему, прокси обращается к нему как
обычный клиент.

## 3.1. Контейнер с Caddy

Ещё один LXC, поменьше — 1 ядро, 512 МБ, 4 ГБ диска:

```bash
pct create 110 local:vztmpl/$TMPL \
  --hostname edge \
  --cores 1 --memory 512 --swap 512 \
  --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.0.40/24,gw=192.168.0.1 \
  --nameserver 192.168.0.1 \
  --unprivileged 1 \
  --onboot 1 \
  --features nesting=1 \
  --password
```

```bash
pct start 110
```

```bash
pct enter 110
```

Caddy ставится **нативно, без Docker**: это один статический бинарник со
своим systemd-юнитом, лишний слой ради него не нужен.

```bash
apt update
```

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
```

Добавить ключ и репозиторий Caddy:

```bash
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
```

```bash
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
```

Установить:

```bash
apt update
```

```bash
apt install -y caddy
```

## 3.2. Конфиг Caddy

```bash
cat > /etc/caddy/Caddyfile <<'EOF'
{
	# Зона .lan не делегирована никому, публичный сертификат на неё не
	# получить. Без этого Caddy будет бесконечно пытаться и писать в лог.
	auto_https off
}

http://kinopub.lan {
	reverse_proxy 192.168.0.50:8080 {
		# Отдавать ответ потоком, не накапливая в буфере.
		flush_interval -1
	}
}

# Незнакомое имя хоста Caddy по умолчанию отдаёт пустым 200 - в браузере это
# белый экран, неотличимый от упавшего сервиса. Явный 404 экономит полчаса
# поисков при опечатке в имени.
http:// {
	respond "Нет сервиса с таким именем. Проверьте адрес или добавьте блок в Caddyfile." 404
}
EOF
```

Применять только так — `reload` с битым конфигом просто не применится, а
`restart` уронил бы прокси:

```bash
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
```

> **Настройки для видеопотока обязательно дублируются на внешнем прокси.**
> Внутренний nginx проекта отключает буферизацию и пробрасывает
> `Range`/`If-Range`, но внешний прокси оказывается **перед** ним. Без этого
> симптом — не ошибка в логах, а плавающие подвисания при перемотке. У Caddy
> буферизации нет по умолчанию, `flush_interval -1` закрепляет это явно;
> nginx буферизует и требует `proxy_buffering off` и `proxy_read_timeout 24h`.

Проверить до настройки DNS, подставив имя заголовком:

```bash
curl -H "Host: kinopub.lan" http://localhost/bridge/health
```

## 3.3. DNS на Keenetic

Роутер должен отвечать на `kinopub.lan` адресом **прокси**, а не самого
приложения — разводит запросы по именам уже Caddy.

Подключитесь к роутеру по SSH или telnet и выполните:

```
ip host kinopub.lan 192.168.0.40
system configuration save
```

Wildcard Keenetic не поддерживает, поэтому каждое новое имя заводится
отдельной строкой. Появится второй сервис — добавите блок в `Caddyfile` и
ещё одну запись `ip host` на тот же адрес прокси.

> Если DNS умеет wildcard (AdGuard Home, Pi-hole, Unbound), достаточно одной
> записи `*.lan` → `192.168.0.40`, и роутер трогать больше не придётся.

## 3.4. Проверка

С компьютера:

```bash
nslookup kinopub.lan
```

Должен ответить адресом прокси. Затем откройте **http://kinopub.lan**.

> **Телевизор может не увидеть имя.** Многие модели игнорируют DNS,
> выданный по DHCP, и ходят во внешний резолвер. Тогда адрес роутера
> прописывается в сетевых настройках телевизора вручную.

---

# Справочник

## Зачем нужен мост

**Главная причина — воспроизведение.** Если отдать телевизору прямую ссылку
на файл в CDN, видео запускается, но потом встаёт, а перемотка не работает.
Особенно заметно при возобновлении с середины: запрос превращается в
byte-range внутрь многогигабайтного файла, CDN отдаёт его слишком медленно,
буфер не наполняется никогда. Хуже всего, что элемент `<video>` при этом **не
сообщает об ошибке** — просто висит с пустым буфером, и «Продолжить» зависает
молча. Проверено на 2160p HEVC: с 00:00 играет нормально, при старте с 31:11
мёртв и через полминуты. В приложении из-за этого есть отдельный сторож
зависания, а поток по умолчанию идёт через мост, который забирает файл сам и
отдаёт телевизору ровным потоком.

Вторая причина — доступ к API. Браузер телевизора не может ходить в KinoPub
напрямую: там нет CORS, а токены нельзя держать в коде страницы.

```
телевизор / браузер
        │
        ▼
    nginx  :80                     ← отдаёт статику фронтенда
        ├── /            → index.html, app.js, styles.css
        └── /bridge/*    → backend:8000
                              │
                              ├── хранит сессию KinoPub (httponly-кука)
                              ├── проксирует и сжимает постеры
                              ├── проксирует видеопоток (Relay/HLS)
                              └── ходит в api.service-kp.com
```

Токены KinoPub живут только на сервере, в браузер уходит лишь `httponly`-кука
сессии.

## Режимы потока

| Режим | Как идёт видео | Когда полезен |
|---|---|---|
| **Relay** | Файл целиком через мост | По умолчанию: ровное воспроизведение и рабочая перемотка |
| **HLS** | Мост нарезает поток | Когда нужен выбор дорожек на лету |
| **Direct** | Телевизор берёт файл из CDN сам | Единственный режим, доносящий HDR без потерь, но подвержен зависанию выше |

Переключается в Настройках. При зависании Direct приложение само уходит на
Relay, но один раз за сеанс — чтобы не дёргать туда-сюда рабочий поток.

## Настройки `.env`

| Переменная | Назначение |
|---|---|
| `KINOPUB_CLIENT_ID` / `_SECRET` | Учётные данные клиента API |
| `KINOPUB_API_BASE` | Адрес API, по умолчанию `https://api.service-kp.com` |
| `CORS_ORIGINS` | Origin'ы через запятую, с которых разрешены запросы |
| `COOKIE_SECURE` | `true` только если доступ по HTTPS |
| `STREAM_HOST_SUFFIXES` | **Белый список хостов** для проксирования видео и картинок |
| `MEDIA_REFERER` / `MEDIA_ORIGIN` | Заголовки к CDN, если он их требует |
| `IMAGE_REFERER` | Заголовок `Referer` для постеров |
| `IMAGE_MAX_BYTES` | Предел размера картинки |
| `AUDIO_HLS_*` | Временный HLS при пересборке звуковой дорожки |
| `WITH_FFMPEG` | `0` — образ без FFmpeg, меньше и быстрее собирается |

Переменные применяются через `docker compose up -d`, пересборка образа не
нужна. Исключение — `WITH_FFMPEG`: он решает, что попадёт в образ, и требует
`docker compose up -d --build backend`.

> **`STREAM_HOST_SUFFIXES` — источник самой обидной ошибки.** Список пуст —
> проксируется что угодно. Список заполнен — всё, чего в нём нет, получает
> `403`, включая постеры и видео. Значения должны быть **реальными** хостами:
> `staticpop.net` (постеры), `cdntogo.net` (видео). Вымышленный домен вроде
> `example-cdn.net`, оставленный из примера, даёт пустые обложки и
> неработающие Relay/HLS — при том что Direct продолжает играть, потому что
> идёт мимо прокси. Именно это и сбивает с толку. Совпадение по суффиксу,
> домена второго уровня достаточно.

### Учётные данные клиента API

`KINOPUB_CLIENT_ID` / `KINOPUB_CLIENT_SECRET` — идентификатор **приложения**,
а не вашего аккаунта. Доступ к личной библиотеке даёт отдельная привязка
устройства по коду. Подходят публично задокументированные учётные данные
Kodi-плагина (`xbmc`), которыми пользуются сторонние клиенты KinoPub.

## Обновление

```bash
git pull
```

```bash
docker compose up -d --build
```

Фронтенд смонтирован томом и подхватывается сразу после обновления страницы —
пересборка нужна только при изменениях в `backend/`.

## Тесты

Фронтенд — 312 проверок, обычные Node-скрипты без фреймворка. Четыре из
восьми файлов принимают путь к `app.js` **аргументом**; без него они падают
ещё до первой проверки и молча дают ноль строк:

```bash
cd frontend/tests
```

```bash
npm install
```

```bash
for f in harness actions episodes misc panel quality sections subs; do
  node $f.js ../app.js
done
```

Бэкенд — смоук-проверки. `smoke_test.py` не входит в образ (копируется только
`app/`), поэтому его нужно положить внутрь:

```bash
docker compose cp backend/smoke_test.py backend:/app/smoke_test.py
```

```bash
docker compose exec -T backend python smoke_test.py
```

## Частые проблемы

**Пустые обложки, не работает Relay/HLS, в логах `403`** — в
`STREAM_HOST_SUFFIXES` нет реальных хостов CDN. См. врезку выше.

**Долгая пауза и `KinoPub API timeout` / `502`** — API KinoPub периодически
недоступен, это внешняя проблема. Проверяется прямо:

```bash
docker compose exec backend python -c "import socket; s=socket.socket(); s.settimeout(5); s.connect(('api.service-kp.com',443)); print('ok')"
```

**`{"status":401,"error":"unauthorized"}` при живой сессии** — KinoPub отозвал
токен досрочно. Приложение обновляет его само при первом же 401; если и
обновление отклонено, устройство нужно привязать заново.

**Видео подвисает при перемотке за обратным прокси** — не проброшены
`Range`/`If-Range` или не отключена буферизация на внешнем прокси.

**Телевизор не открывает адрес по имени** — телевизор игнорирует DNS из DHCP.
Пропишите адрес роутера в его сетевых настройках вручную.

**`docker` не отвечает на Windows** — не запущен Docker Desktop.

## Структура

```
backend/          FastAPI-мост к API KinoPub (один модуль app/main.py)
  smoke_test.py   смоук-проверки эндпоинтов
frontend/         статика: index.html, app.js, api.js, styles.css
  assets/dog/     иконки режима «киноТёрк», подхватываются из папки
  tests/          312 регрессионных проверок
docker-compose.yml
nginx.conf        статика + прокси /bridge/ на бэкенд
```
