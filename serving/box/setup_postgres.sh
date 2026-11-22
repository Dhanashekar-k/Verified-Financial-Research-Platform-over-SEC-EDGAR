#!/usr/bin/env bash
# Start Postgres 17 + pgvector in Docker on the serving box (run on the box, as root, via SSM).
# Data lives on the root volume; the password is generated once and never leaves /opt/edgarqa.
set -euo pipefail
CODE=/opt/edgarqa/code
PGDATA=/var/lib/edgarqa/pgdata
IMAGE=pgvector/pgvector:pg17
mkdir -p "$PGDATA" /opt/edgarqa
[ -f /opt/edgarqa/bootstrap.done ] || { echo "bootstrap.done missing: the instance's user_data failed (docker/uv not installed)"; exit 1; }
[ -d "$CODE/serving" ] || { echo "$CODE/serving missing: run serving/box/pull_bundle.sh first"; exit 1; }
[ -s /opt/edgarqa/pgpass ] || (umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > /opt/edgarqa/pgpass)
# the password reaches the container through a root-only env file, never on a command line
(umask 077; printf 'POSTGRES_PASSWORD=%s\n' "$(cat /opt/edgarqa/pgpass)" > /opt/edgarqa/pg.env)
docker pull "$IMAGE" >/dev/null
docker rm -f edgarqa-pg >/dev/null 2>&1 || true
docker run -d --name edgarqa-pg --restart unless-stopped --shm-size=4g \
  -p 127.0.0.1:5432:5432 \
  --env-file /opt/edgarqa/pg.env \
  -v "$PGDATA":/var/lib/postgresql/data \
  -v "$CODE/serving/postgres.conf":/etc/postgresql/postgresql.conf:ro \
  "$IMAGE" postgres -c config_file=/etc/postgresql/postgresql.conf >/dev/null
for _ in $(seq 1 60); do docker exec edgarqa-pg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 2; done
docker exec edgarqa-pg pg_isready -U postgres
printf 'export EDGARQA_PG_DSN=postgresql://postgres:%s@127.0.0.1:5432/postgres\n' "$(cat /opt/edgarqa/pgpass)" > /opt/edgarqa/env.sh
chmod 600 /opt/edgarqa/env.sh
echo "image digest: $(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE")"
docker exec edgarqa-pg psql -U postgres -Atc "CREATE EXTENSION IF NOT EXISTS vector; SELECT 'server ' || current_setting('server_version') || ', pgvector ' || extversion FROM pg_extension WHERE extname = 'vector'"
