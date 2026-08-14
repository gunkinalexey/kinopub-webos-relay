#!/usr/bin/env bash
# Обновлятор. Живёт на хосте, вне Docker, запускается systemd-таймером.
#
# Почему снаружи: пересборка убивает контейнер, из которого её запустили, -
# изнутри себя приложение обновиться не может. Альтернатива, пробросить в
# контейнер docker.sock, равносильна выдаче root на хосте сервису, который
# проксирует контент из интернета, и отвергнута.
#
# Обмен с приложением - два файла в каталоге update/:
#   status.json  пишет этот скрипт, читает GET /update/status
#   requested    создаёт POST /update/apply, удаляет этот скрипт
#
# Ключевая идея: применять только то, что действительно нужно. frontend/
# смонтирован в nginx томом и подхватывается сам, поэтому правки интерфейса
# не требуют вообще ничего - ни пересборки, ни перезапуска, ни секунды
# простоя. Пересобирается только backend/ и только когда он менялся.

set -uo pipefail

REPO="${REPO:-/opt/kinopub}"
BRANCH="${BRANCH:-main}"
UPDATE_DIR="$REPO/update"
STATUS="$UPDATE_DIR/status.json"
REQUEST="$UPDATE_DIR/requested"
# Когда этот экземпляр в последний раз реально сменил версию - отдельный
# файл, не commit-дата: деплой обычно отстаёт от коммита на время до
# следующего тика таймера, и это два разных факта. Переживает перезапуски
# скрипта, потому что status.json перезаписывается целиком при каждом
# прогоне, а этот файл трогается только при фактическом обновлении.
APPLIED_AT_FILE="$UPDATE_DIR/last_applied_at"
HEALTH="${HEALTH:-http://localhost:8080/bridge/health}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"

cd "$REPO" || exit 1
mkdir -p "$UPDATE_DIR"

log() { echo "[updater] $*"; }

# jq в образе LXC может не быть, а тащить его ради трёх полей незачем.
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/ /g' | tr -d '\r\n'; }

write_status() {
  local state="$1" message="$2"
  local cur_sha cur_subject cur_date rem_sha rem_subject rem_date behind available applied_at
  cur_sha=$(git rev-parse --short HEAD 2>/dev/null || echo '')
  cur_subject=$(git log -1 --format=%s 2>/dev/null || echo '')
  # Unix-время коммита (%ct), не автора (%at): при ребейзах/каналах доставки
  # это то, что реально совпадает с порядком в git log --reverse, который
  # плечо plan_actions() использует для diff между before/after.
  cur_date=$(git log -1 --format=%ct 2>/dev/null || echo '')
  rem_sha=$(git rev-parse --short "origin/$BRANCH" 2>/dev/null || echo '')
  rem_subject=$(git log -1 --format=%s "origin/$BRANCH" 2>/dev/null || echo '')
  rem_date=$(git log -1 --format=%ct "origin/$BRANCH" 2>/dev/null || echo '')
  behind=$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)
  if [ "${behind:-0}" -gt 0 ]; then available=true; else available=false; fi
  applied_at=$(cat "$APPLIED_AT_FILE" 2>/dev/null || echo '')

  cat > "$STATUS" <<EOF
{
  "checked_at": $(date +%s),
  "state": "$(json_escape "$state")",
  "message": "$(json_escape "$message")",
  "available": $available,
  "behind": ${behind:-0},
  "branch": "$(json_escape "$BRANCH")",
  "current_sha": "$(json_escape "$cur_sha")",
  "current_subject": "$(json_escape "$cur_subject")",
  "current_committed_at": ${cur_date:-null},
  "remote_sha": "$(json_escape "$rem_sha")",
  "remote_subject": "$(json_escape "$rem_subject")",
  "remote_committed_at": ${rem_date:-null},
  "last_applied_at": ${applied_at:-null}
}
EOF
}

wait_healthy() {
  local i=0
  while [ "$i" -lt "$HEALTH_TRIES" ]; do
    if curl -fsS --max-time 3 "$HEALTH" >/dev/null 2>&1; then return 0; fi
    i=$((i + 1))
    sleep 2
  done
  return 1
}

# Что менять, решает список изменившихся файлов, а не факт обновления как
# таковой. Возвращает через глобальные переменные, потому что sh-функция
# умеет отдать только код возврата.
plan_actions() {
  local from="$1" to="$2" changed
  changed=$(git diff --name-only "$from" "$to" 2>/dev/null)
  NEED_BUILD=false
  NEED_RECREATE=false
  CHANGED_SUMMARY=$(printf '%s' "$changed" | sed 's#/.*##' | sort -u | paste -sd, -)

  # backend/ попадает в образ - без пересборки изменения не применятся.
  printf '%s\n' "$changed" | grep -q '^backend/' && NEED_BUILD=true
  # Эти читаются при старте контейнера, достаточно пересоздать.
  printf '%s\n' "$changed" | grep -qE '^(docker-compose\.yml|nginx\.conf)$' && NEED_RECREATE=true
  # frontend/ намеренно не упомянут: он смонтирован read-only томом и
  # подхватывается nginx сам, действий не требует.
}

do_update() {
  local before after
  before=$(git rev-parse HEAD)

  write_status updating "Забираем изменения"
  if ! git pull --ff-only origin "$BRANCH" >/dev/null 2>&1; then
    write_status failed "Не удалось забрать изменения (git pull)"
    log "git pull failed"
    return 1
  fi
  after=$(git rev-parse HEAD)

  if [ "$before" = "$after" ]; then
    write_status idle "Обновлений не было"
    return 0
  fi

  plan_actions "$before" "$after"
  log "changed: ${CHANGED_SUMMARY:-none}; build=$NEED_BUILD recreate=$NEED_RECREATE"

  if [ "$NEED_BUILD" = true ]; then
    write_status updating "Пересобираем бэкенд"
    if ! docker compose up -d --build backend >/dev/null 2>&1; then
      log "build failed, rolling back to $before"
      git reset --hard "$before" >/dev/null 2>&1
      docker compose up -d --build backend >/dev/null 2>&1
      write_status failed "Сборка не удалась, откатились на прежнюю версию"
      return 1
    fi
  elif [ "$NEED_RECREATE" = true ]; then
    write_status updating "Перезапускаем контейнеры"
    docker compose up -d >/dev/null 2>&1
  else
    # Менялся только фронтенд или документация - применять нечего, но
    # версия всё равно сменилась, так что last_applied_at обновляется и
    # здесь, а не только на пути с пересборкой.
    date +%s > "$APPLIED_AT_FILE"
    write_status idle "Обновлено без перезапуска"
    log "frontend/docs only, nothing to restart"
    return 0
  fi

  write_status updating "Ждём, пока поднимется"
  if ! wait_healthy; then
    log "health check failed, rolling back to $before"
    git reset --hard "$before" >/dev/null 2>&1
    docker compose up -d --build backend >/dev/null 2>&1
    if wait_healthy; then
      write_status failed "Новая версия не поднялась, откатились на прежнюю"
    else
      write_status failed "Новая версия не поднялась, и откат тоже не помог"
    fi
    return 1
  fi

  date +%s > "$APPLIED_AT_FILE"
  write_status idle "Обновлено"
  log "updated $before -> $after"
}

git fetch --quiet origin "$BRANCH" 2>/dev/null

if [ -f "$REQUEST" ]; then
  rm -f "$REQUEST"
  log "update requested"
  do_update
else
  write_status idle ""
fi
