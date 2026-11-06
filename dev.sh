#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="docker compose -p components-dev -f docker-compose.dev.yml"
[ -f .env.dev ] && set -a && . ./.env.dev && set +a
export PORT="${PORT:-8000}"

case "${1:-up}" in
  up)
    mkdir -p data-dev
    $COMPOSE up -d --build
    echo "Dev environment: http://localhost:$PORT (auto-login as admin, code reloads on save)"
    ;;
  down) $COMPOSE down ;;
  logs) $COMPOSE logs -f ;;
  shell) $COMPOSE exec components-dev sh ;;
  reset)
    $COMPOSE down
    rm -rf data-dev
    echo "Dev data removed."
    ;;
  *) echo "Usage: ./dev.sh [up|down|logs|shell|reset]"; exit 1 ;;
esac
