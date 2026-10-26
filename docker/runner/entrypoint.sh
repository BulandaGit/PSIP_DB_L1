#!/usr/bin/env bash
set -euo pipefail

cd /runner
if [[ ! -f run.sh ]]; then
  cp -a /home/runner/. /runner/
fi

case "${1:-run}" in
  register)
    if [[ -f .runner ]]; then
      echo "Runner jest juz zarejestrowany."
      exit 0
    fi
    IFS= read -r registration_token
    registration_token="${registration_token%$'\r'}"
    if [[ -z "$registration_token" ]]; then
      echo "Brak tokena rejestracyjnego na standardowym wejsciu." >&2
      exit 2
    fi
    ./config.sh --unattended --url "$RUNNER_REPO_URL" \
      --token "$registration_token" --name "$RUNNER_NAME" \
      --labels psip-db --work _work
    unset registration_token
    ;;
  registered)
    test -f .runner
    ;;
  run)
    if [[ ! -f .runner ]]; then
      echo "Najpierw uruchom scripts/Initialize-Lab.ps1." >&2
      exit 2
    fi
    exec ./run.sh
    ;;
  *) exec "$@" ;;
esac
