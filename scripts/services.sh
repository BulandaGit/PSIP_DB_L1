#!/usr/bin/env bash
set -euo pipefail

operation="${1:?Podaj start, stop, destroy lub status}"
environment="${2:?Podaj env-test, env-qa lub main}"
case "$environment" in
  env-test|env-qa|main) ;;
  *) echo "Nieobslugiwane srodowisko: $environment" >&2; exit 2 ;;
esac

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="remote-db-$environment"
compose=(docker compose -f "$root_dir/docker_compose.yml" -p "$project")

case "$operation" in
  start)
    "${compose[@]}" up -d --build --wait --wait-timeout 180 postgis
    "${compose[@]}" exec -T postgis sh -c \
      'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT current_database(), postgis_version();"'
    "${compose[@]}" ps
    ;;
  stop)
    "${compose[@]}" stop
    ;;
  destroy)
    if [[ "${CONFIRM_DESTROY:-}" != "$environment" ]]; then
      echo "Usuwanie danych wymaga CONFIRM_DESTROY=$environment" >&2
      exit 2
    fi
    "${compose[@]}" down --volumes --remove-orphans
    ;;
  status)
    "${compose[@]}" ps -a
    ;;
  *) echo "Nieobslugiwana operacja: $operation" >&2; exit 2 ;;
esac
