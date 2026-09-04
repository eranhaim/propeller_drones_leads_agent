#!/usr/bin/env bash
#
# Deploy the Propeller Drones bot to production.
#
# Runs from this machine but uploads nothing: the server pulls from origin/main,
# so what runs in production is always an exact pushed commit. The server's .env
# is never read, written, or overwritten - it holds the production secrets and
# this repo's .env holds local ones.
#
# Usage:
#   ./deploy/deploy.sh            rebuild and restart the bot
#   ./deploy/deploy.sh --ingest   also re-ingest the knowledge base afterwards
#
# Pass --ingest whenever knowledge/**.md or the website content changed. The
# retriever reads Chroma, not the files, so a knowledge change that skips the
# ingest ships silently stale answers.
#
# Only the ``bot`` service is rebuilt. Postgres and Chroma hold the leads and
# the embeddings; they are never touched by a deploy.
#
# The server is read from deploy/deploy.env, which is gitignored. Copy
# deploy.env.example to deploy.env and fill it in once.

set -euo pipefail

die() { printf '\ndeploy failed: %s\n' "$*" >&2; exit 1; }

# Host and key are deliberately not in this file - it is committed, and the
# repo is on GitHub. deploy.env holds them, or the environment does.
CONFIG="$(cd "$(dirname "$0")" && pwd)/deploy.env"
# shellcheck source=/dev/null
[ -f "$CONFIG" ] && . "$CONFIG"

HOST="${PROPELLER_HOST:-}"
KEY="${PROPELLER_KEY:-}"
REMOTE_DIR="${PROPELLER_DIR:-/home/ubuntu/propeller_drones_leads_agent}"
BRANCH=main
INGEST=""

case "${1:-}" in
  "") ;;
  --ingest) INGEST=1 ;;
  *) die "unknown argument '$1' (expected --ingest or nothing)" ;;
esac

[ -n "$HOST" ] || die "PROPELLER_HOST is not set - copy deploy.env.example to deploy.env and fill it in"
[ -n "$KEY" ] || die "PROPELLER_KEY is not set - copy deploy.env.example to deploy.env and fill it in"

# --- preflight: the server can only pull what has actually been pushed --------

[ -f "$KEY" ] || die "ssh key not found: $KEY"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside a git repository"

current_branch=$(git branch --show-current)
[ "$current_branch" = "$BRANCH" ] || die "on branch '$current_branch', deploy expects '$BRANCH'"

git diff-index --quiet HEAD -- || die "uncommitted changes - commit and push before deploying"

git fetch --quiet origin "$BRANCH"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ] \
  || die "HEAD does not match origin/$BRANCH - push before deploying"

echo "deploying $(git rev-parse --short HEAD) to $HOST${INGEST:+ (with knowledge re-ingest)}"

# --- remote ------------------------------------------------------------------

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" \
  REMOTE_DIR="$REMOTE_DIR" BRANCH="$BRANCH" INGEST="$INGEST" bash -s <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"

previous=$(git rev-parse --short HEAD)

# --ff-only so a dirty or diverged server tree stops the deploy loudly instead
# of having its state silently discarded.
git fetch --quiet origin "$BRANCH"
git pull --ff-only --quiet origin "$BRANCH"
deployed=$(git rev-parse --short HEAD)

if [ "$previous" = "$deployed" ]; then
  echo "already at $deployed, rebuilding anyway"
else
  echo "$previous -> $deployed"
  git --no-pager log --oneline "$previous..$deployed"
fi

# Only the bot. Restarting it is safe: GreenAPI keeps queued notifications
# (delete_notifications_at_startup=False), so no inbound message is lost, and
# app.main runs ``alembic upgrade head`` before the threads start.
docker compose build bot
docker compose up -d bot
docker compose ps

# The health route is served by the FastAPI thread inside the bot container.
# A 200 on the published port proves the container is up AND the port
# publishing the host nginx proxies to still works.
published=$(docker compose port bot 8080 | tail -1)
for _ in $(seq 1 20); do
  code=$(curl -fsS -o /dev/null -w '%{http_code}' "http://${published}/health" 2>/dev/null || true)
  if [ "$code" = "200" ]; then
    echo "health check: 200 on ${published} - deployed $deployed"
    if [ -n "$INGEST" ]; then
      echo "re-ingesting the knowledge base..."
      docker compose exec -T bot python -m scripts.ingest_knowledge --reset
    fi
    exit 0
  fi
  sleep 3
done

echo "health check failed after 60s (last response: ${code:-none})" >&2
docker compose logs --tail 40 bot >&2
echo "roll back with: cd $REMOTE_DIR && git reset --hard $previous && docker compose up -d --build bot" >&2
exit 1
REMOTE
