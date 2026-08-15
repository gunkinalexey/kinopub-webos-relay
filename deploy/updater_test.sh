#!/usr/bin/env bash
# Запуск: docker run --rm -v "$PWD:/app" debian:stable-slim bash /app/deploy/updater_test.sh
# Проверка решения об автообновлении в отрыве от git и docker: функция
# извлекается из updater.sh как есть, окружение подставляется.
set -uo pipefail
fails=0
check() { # имя ожидание факт
  if [ "$2" = "$3" ]; then echo "PASS  $1"; else echo "FAIL  $1: ждали '$2', получили '$3'"; fails=$((fails+1)); fi
}

SRC="${SRC:-$(dirname "$0")/updater.sh}"
UPDATE_DIR=$(mktemp -d)
AUTO_FILE="$UPDATE_DIR/auto.json"
AUTO_DATE_FILE="$UPDATE_DIR/last_auto_date"
log() { :; }

# Вытащить только функцию, ничего больше из скрипта не выполняя.
eval "$(awk '/^should_auto_update\(\) \{/,/^\}/' "$SRC")"

reset() { rm -f "$AUTO_FILE" "$AUTO_DATE_FILE"; available=true; }
say()   { printf '{"enabled": %s, "at": "%s", "written_at": 1}\n' "$1" "$2" > "$AUTO_FILE"; }
run()   { if should_auto_update; then echo yes; else echo no; fi; }

TODAY=$(date +%F)
YESTERDAY=$(date -d 'yesterday' +%F 2>/dev/null || date -v-1d +%F)
NOW=$(date +%H:%M)
EARLIER=$(date -d '2 hours ago' +%H:%M 2>/dev/null || date -v-2H +%H:%M)
LATER=$(date -d '2 hours' +%H:%M 2>/dev/null || date -v+2H +%H:%M)

reset
check "без файла настройки не обновляемся" no "$(run)"

reset; say false "$EARLIER"; echo "$YESTERDAY" > "$AUTO_DATE_FILE"
check "с выключенной галочкой не обновляемся" no "$(run)"

reset; say true "$EARLIER"; echo "$YESTERDAY" > "$AUTO_DATE_FILE"; available=false
check "без доступного обновления не обновляемся" no "$(run)"

reset; say true "$EARLIER"
check "первое включение не обновляет сразу" no "$(run)"
check "  и помечает сегодняшний день" "$TODAY" "$(cat "$AUTO_DATE_FILE")"

reset; say true "$EARLIER"; echo "$TODAY" > "$AUTO_DATE_FILE"
check "второй раз за сутки не обновляемся" no "$(run)"

reset; say true "$LATER"; echo "$YESTERDAY" > "$AUTO_DATE_FILE"
check "до наступления часа не обновляемся" no "$(run)"

reset; say true "$EARLIER"; echo "$YESTERDAY" > "$AUTO_DATE_FILE"
check "после наступления часа, раз в сутки - обновляемся" yes "$(run)"
check "  и записываем дату, чтобы не повторить" "$TODAY" "$(cat "$AUTO_DATE_FILE")"

reset; say true "$EARLIER"; echo "$YESTERDAY" > "$AUTO_DATE_FILE"
should_auto_update >/dev/null
check "повтор сразу после срабатывания не проходит" no "$(run)"

reset; printf '{"enabled": true, "written_at": 1}\n' > "$AUTO_FILE"; echo "$YESTERDAY" > "$AUTO_DATE_FILE"
result=$(run)
check "без поля времени берётся 04:00 по умолчанию" "$([ "$NOW" \> "04:00" ] && echo yes || echo no)" "$result"

rm -rf "$UPDATE_DIR"
[ "$fails" = 0 ] && echo "" && echo "All checks passed" || { echo ""; echo "$fails FAILURE(S)"; exit 1; }
