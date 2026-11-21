#!/usr/bin/env bash
set -euo pipefail
# Build and run the reusable KevenOS application in Docker.
#   ./run.sh                 start it (real event)
#   ./run.sh --rehearsal     practice run (REHEARSAL banner on every screen)
#   ./run.sh --mode desk     one shared clock run by the desk instead of judge-paced
#   ./run.sh stop            stop it      ./run.sh logs   show the container log
# Data and local credentials are kept in a Docker volume. Public deployments should
# inject secrets through their managed runtime rather than this helper.
cd "$(dirname "$0")"
container_name=kevenos-app
image_name="kevenos:$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' package.json 2>/dev/null | head -n 1 | sed 's/.*"\([^"]*\)"$/\1/' | grep . || echo local)"
port=${PORT:-8080}
if ! command -v docker >/dev/null; then
  echo "Install Docker before using this local helper." >&2
  exit 1
fi
case "${1:-}" in
  stop) docker stop "$container_name"; docker rm "$container_name" 2>/dev/null; exit 0;;
  logs) docker logs "$container_name"; exit 0;;
esac
docker stop "$container_name" >/dev/null 2>&1 || true
docker rm "$container_name" >/dev/null 2>&1 || true
docker build -t "$image_name" .
env_flags=()
for key in KEVENOS_PASSWORD KEVENOS_VIEW_KEY KEVENOS_DESK_PIN; do
  if [ -n "${!key:-}" ]; then env_flags+=(-e "$key"); fi
done
docker run -d --name "$container_name" --restart unless-stopped \
  -p "$port:8080" -v kevenos_data:/data "${env_flags[@]}" "$image_name" "$@"
sleep 2
docker logs "$container_name"
echo "Admin: http://localhost:$port/admin"
echo "Judge: http://localhost:$port/judge"
echo "Live: http://localhost:$port/live"
