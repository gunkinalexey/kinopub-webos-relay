## v0.9.60 — Full KinoPub audio metadata list

- The audio selector now uses the complete `audios` array returned by KinoPub.
- `media.tracks` no longer hides entries from the selector because production responses can contain only the active/default track there.
- Duplicate media nodes from `/v1/items/<id>` are merged instead of keeping the first occurrence. Streams, audio metadata, subtitles, and track numbers are combined.
- Playback logs now include `audio_count` and raw `tracks` for diagnostics.
- Cached HLS preparation from v0.9.59 remains unchanged.

## v0.9.59 — Resilient cached audio HLS

Исправлен обрыв подготовки дорожки на больших HTTP-файлах KinoPub: FFmpeg теперь повторно подключается к CDN после преждевременного TLS/HTTP EOF, повторяет временные сетевые ошибки и отбрасывает повреждённый неполный пакет. Видео по-прежнему копируется без перекодирования, аудио преобразуется в AAC, а готовый HLS используется для обычной перемотки.

- При выборе озвучки backend создаёт локальный HLS из исходного HTTP-файла KinoPub.
- Видео копируется без перекодирования, выбранный звук преобразуется в AAC 2.0.
- Текущий поток останавливается только на время подготовки первых сегментов.
- После подключения перемотка работает внутри уже созданного HLS; новый FFmpeg-процесс нужен только при переходе за пределы подготовленного диапазона.
- Подготовка выполняется отдельной задачей с опросом статуса и тайм-аутом 45 секунд, поэтому интерфейс не зависает на сообщении о подключении.
- Субтитры остаются внешними WebVTT; при HLS, начатом не с нулевой секунды, backend автоматически сдвигает таймкоды.
- Неактивные FFmpeg-задачи останавливаются, временные сегменты удаляются автоматически.

# KP TV — webOS-oriented KinoPub bridge

Лёгкий TV-веб-клиент и FastAPI-прослойка для браузеров LG webOS.

## Реализовано

- Device Flow с динамическим `verification_uri`, таймером и повторным получением кода;
- сохранение пользовательской сессии в SQLite;
- автоматическое обновление access token;
- mock API для разработки без реального каталога KinoPub;
- главная страница, поиск, карточка фильма/сериала и список серий;
- настройки качества, режима потока, языка, субтитров и анимаций;
- локальная серверная история просмотра и восстановление позиции;
- прямое воспроизведение, byte-range relay, HLS relay и локальный HLS для выбранной озвучки;
- переписывание URL в HLS playlist, включая аудио, ключи и субтитры;
- защита relay от localhost/private IP и проверка redirect;
- управление пультом, восстановление фокуса между экранами;
- диагностика кодеков и тест произвольного URL в трёх режимах;
- backend/frontend media-логи.

## Запуск

```bash
cp .env.example .env
# заполнить KINOPUB_CLIENT_ID и KINOPUB_CLIENT_SECRET
docker compose up --build
```

Откройте `http://IP_СЕРВЕРА:8080` в браузере телевизора.

## Что работает без KinoPub API

Главная, поиск, карточки, сериалы, настройки и история работают через `/mock/*`. В демонстрационных карточках нет видеоссылок. Для проверки плеера откройте **Диагностика** и вставьте доступный вам URL.

## Следующий этап

Снять реальные JSON-ответы каталога, карточки, сезонов и файлов KinoPub и реализовать один backend-adapter, преобразующий их во внутренние модели клиента.

## Production

Задайте `STREAM_HOST_SUFFIXES`, включите HTTPS и `COOKIE_SECURE=true`. Для публичного многопользовательского сервиса добавьте шифрование токенов at-rest, CSRF-защиту и rate limiting.


## API Explorer (v0.6.0)

После авторизации откройте кнопку **API Explorer** в верхнем меню. Explorer выполняет только GET-запросы через текущую backend-сессию.

- путь вводится без домена и без начального `/`, например `v1/items`;
- query-параметры вводятся отдельно: `page=0&perpage=20`;
- OAuth endpoint заблокирован;
- access/refresh token, cookie, secret и похожие поля маскируются;
- максимальный размер диагностического ответа — 2 MB;
- ответ можно скопировать или скачать как JSON.

Точные пути KinoPub API зависят от актуальной версии сервиса. Explorer нужен, чтобы безопасно снять структуру реальных ответов и затем написать нормализующий adapter. Не публикуйте скачанные ответы без просмотра: в данных аккаунта могут быть персональные сведения, даже если токены уже замаскированы.

## Первый реальный запуск видео (v0.9.0)

После авторизации главная страница загружает реальные списки KinoPub. При открытии карточки backend запрашивает `/v1/items/{id}`, строит список фильмов/серий и при нажатии «Смотреть» получает поток через `/v1/items/media-links?mid=...`. Приоритет: H.264, обычный HLS, около 1080p. Поток запускается через HLS relay.

Если воспроизведение не началось, откройте «Диагностика» и посмотрите последние события `Play option resolved`, `HLS playlist relayed` или ошибку media.


## 0.9.17
- Pagination remains available when KinoPub shortcut endpoints omit total page counts.
- Unknown totals show pages in blocks of ten; `>>` advances to the next block.


## v0.9.24
Standalone catalogue sections now use `/v1/items` (`feed=all`) instead of `/v1/items/fresh`. The New releases tabs continue to use popular/fresh/hot shortcuts.


## Новинки: фиксированные источники

- Популярные: `GET /v1/items/popular?type=movie`
- Свежие: `GET /v1/items/fresh?type=movie`



## v0.9.40
- Круглые иконки управления плеером вместо текстовых кнопок.
- Выбор Direct / Relay / HLS relay перенесён в выпадающий список справа.


## 0.9.48
- Более подробные подписи звуковых дорожек: язык, перевод/студия, тип озвучки, каналы, кодек и битрейт, когда эти данные доступны.


## 0.9.48
- Исправлен вывод `[object Object]` в названиях аудиодорожек.
- Добавлен безопасный разбор вложенных полей студии, перевода и типа озвучки.
- Нестроковые метаданные без понятного названия больше не выводятся.


## v0.9.48
- Подробные подписи субтитров: язык, название, переводчик/студия, тип, SDH/форсированные и формат.
- Вложенные объекты метаданных больше не отображаются как `[object Object]`.


## v0.9.48
- Сохраняет подробные названия аудиодорожек KinoPub после `loadedmetadata` на LG webOS.
- Нативные `audioTracks` используются для фактического переключения, а подписи объединяются с API-метаданными по индексу.
- Общие названия `Track 1` / `Дорожка 1` больше не затирают студию, тип перевода, каналы и кодек.


## v0.9.55
- Исправлена перемотка при серверной звуковой дорожке: целевая позиция передаётся напрямую в FFmpeg-поток.
- Перемотка больше не показывает сообщение о подключении дорожки.
- Таймлайн сразу отображает выбранную позицию во время короткого переподключения.


## 0.9.55 — HLS audio switching

Выбор звуковой дорожки переводит текущий вариант качества на соответствующий HLS-поток и переключает дорожку через hls.js. Поток меняется только при выборе аудио; последующая перемотка выполняется обычным seek без повторного FFmpeg-remux.


## 0.9.55
- Restored missing logicalCurrentTime/logicalDuration helpers.
- Restored timeline click seek handler.
- Fixes ReferenceError cascade that broke seeking, progress saving and audio switching.


## v0.9.55
- Убрано ошибочное переключение по HLS-аудиогруппам: KinoPub HLS часто содержит только одну дорожку.
- Альтернативная аудиодорожка подключается через серверный remux.
- Аудио перекодируется в AAC 2.0 для совместимости Chrome и LG webOS; видео копируется без перекодирования.
- Позиция сохраняется при смене дорожки и при перемотке.


## v0.9.56 — Reliable audio source selection

- Quality variants are grouped instead of discarding duplicate HTTP/HLS links.
- Normal playback uses the URL appropriate for Direct/Relay/HLS mode.
- Server-side audio switching always uses the original HTTP MP4 source, because KinoPub HLS variants often contain only the default audio track.
- FFprobe validates the requested absolute audio stream index before FFmpeg starts.
- AC3/EAC3 and other tracks are converted to browser-compatible AAC 2.0.
- The backend waits for the first playable MP4 fragment and returns a clear error instead of silently opening a stream without audio.


## v0.9.57 — Simplified native tracks

- Removed the unstable FFmpeg full-video remux endpoint and the FFmpeg Docker dependency.
- Audio switching now uses only mechanisms provided by the actual stream:
  - alternate HLS audio groups through hls.js, when KinoPub's manifest contains them;
  - native `HTMLMediaElement.audioTracks` on LG webOS Direct HTTP playback.
- Selecting audio automatically changes the current stream to Direct HTTP when needed, while preserving playback time.
- The KinoPub `tracks` field is used to filter audio choices that belong to the selected media.
- Desktop Chrome does not expose MP4 audio tracks, so the selector reports the limitation and returns to Auto instead of hanging.
- External KinoPub subtitles remain supported through the SRT/ASS-to-WebVTT relay and do not require FFmpeg.


## Как работает переключение звука в v0.9.60

KinoPub HLS обычно содержит только одну дорожку, а метаданные остальных озвучек относятся к исходному HTTP-файлу. Поэтому клиент не пытается переключать несуществующие HLS audio groups. При выборе дорожки backend:

1. проверяет абсолютный индекс через FFprobe;
2. запускает FFmpeg с `-c:v copy` и AAC 2.0 для звука;
3. создаёт EVENT HLS по 4 секунды на сегмент;
4. возвращает плееру URL после появления достаточного диапазона;
5. продолжает подготовку в фоне, расширяя доступный диапазон перемотки.

При закрытии плеера, возврате на «Авто», смене качества или режима активная задача останавливается. Временные данные находятся в `/data/audio_hls`.
