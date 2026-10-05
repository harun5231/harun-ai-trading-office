#!/bin/bash
# Run from the existing repository checkout. Requires Docker owner/admin access.
set -euo pipefail
umask 077
cid="$(docker ps -aq --filter label=com.docker.compose.project=harun-office --filter label=com.docker.compose.service=worker | head -n 1)"
[ -n "$cid" ] || { echo 'Worker container tidak ditemukan.'; exit 1; }
image_id="$(docker inspect --format '{{.Image}}' "$cid")"
hostname="$(docker inspect --format '{{.Config.Hostname}}' "$cid")"
image_ref="$(docker inspect --format '{{.Config.Image}}' "$cid")"
private="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/private"}}{{.Source}}{{end}}{{end}}' "$cid")"
data="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}{{end}}' "$cid")"
[ -n "$data" ] && [ -f "$private/browser.json" ] || { echo 'Mount existing tidak sesuai; dihentikan tanpa mengubah profil.'; exit 1; }
case "$image_ref" in sha256:*|*@*) echo 'Image reference memerlukan review lokal; tidak ditimpa.'; exit 1;; esac
docker image inspect "$image_id" >/dev/null
docker tag "$image_id" harun-office-base:local
docker build --pull=false --network=none -f deploy/Dockerfile.local \
  --build-arg OFFICE_BASE_IMAGE=harun-office-base:local -t harun-office-worker:selectors-local .
docker tag harun-office-worker:selectors-local "$image_ref"
docker compose config --quiet
# Stop the owner normally before mounting its SAME profile into the discovery job.
restore() {
  docker compose up -d --no-build --pull never --no-deps --force-recreate --wait --wait-timeout 240 worker
  docker compose restart proxy
  docker compose ps
}
trap restore EXIT
docker compose stop -t 90 worker
set +e
docker run --rm --init --pull never --hostname "$hostname" --user 10001:10001 --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true --shm-size 1gb \
  --tmpfs /tmp:rw,mode=1777 --tmpfs /home/office:rw,uid=10001,gid=10001,mode=0700 \
  --mount "type=volume,source=$data,target=/data" \
  --mount "type=bind,source=$private,target=/private" \
  --env HOME=/home/office --entrypoint xvfb-run harun-office-worker:selectors-local \
  -a -s '-screen 0 430x932x24 -nolisten tcp' python -m worker.selector_discovery \
  --config /private/browser.json --data-dir /data
result=$?
set -e
if [ "$result" -eq 2 ]; then
  echo 'UNVERIFIED: konfigurasi aktif tidak diubah. Baca status selector di atas; jangan login ulang kecuali LOGIN_REQUIRED.'
elif [ "$result" -ne 0 ]; then
  echo 'Discovery gagal aman. Tidak ada prompt/trade dikirim oleh tool; konfigurasi tidak diperbarui.'
else
  echo 'Selector wajib terverifikasi dan config privat diperbarui. Setelah worker hidup, tekan CEK SESI.'
fi
exit "$result"
