# Worker API pada VPS Ubuntu 24.04

Worker memakai `python:3.12-slim-bookworm`, standard library saja, UID 10001 setelah
bootstrap. Caddy tetap HTTPS; API hanya jaringan internal Compose. Volume
`harun-office_worker_data` dan volume sertifikat dipertahankan. Tidak ada dependency
image MCR. Docker Hub masih harus dapat diakses untuk base Python saat build pertama.

## Update VPS existing

Untuk perubahan tiket manual/simulasi pada branch yang sudah dipakai VPS:

```sh
cd /root/harun-ai-trading-office
git status --short
git fetch origin &&
git switch codex/fix-robot-coordinator-24-7 &&
git pull --ff-only origin codex/fix-robot-coordinator-24-7 &&
bash deploy/update-api.sh &&
docker compose ps &&
docker compose exec -T worker python -m worker.health
```

Jangan reset paksa bila Git menolak perubahan lokal. Tidak perlu key baru untuk
simulasi lokal. Frontend GitHub Pages harus mendapat perubahan `assets/` yang sama
melalui PR merge ke configured source branch; update Docker tidak mempublish UI.
Source Pages aktual belum diverifikasi. Panduan lengkap:
[MANUAL_SIMULATION.md](MANUAL_SIMULATION.md).

Setelah perubahan sudah digabungkan ke `main`, update rutin memakai:

1. `git pull --ff-only origin main` dari folder instalasi existing.
2. `sudo python3 deploy/setup.py`: hanya meminta API key bila belum tersimpan;
   input getpass tidak tampil, tidak berada di argumen command/history.
3. `bash deploy/update-api.sh`: build sebelum stop, lalu recreate worker/proxy.
   Jangan mengganti nama Compose project/volume dan jangan menghapus volume.
4. Hubungkan kantor ke endpoint HTTPS worker yang sama, token kontrol lama.
   CEK API → NEUROAPI CONNECTED membuktikan authenticated health, bukan hasil riset.
   Jalankan DRY RUN untuk siklus berbayar; periksa laporan/REJECTED bila gagal.

Saat stop/update, worker menghentikan request riset baru dan menunggu operasi
yang sedang berjalan hingga 600 detik untuk menyimpan hasil. `stop_grace_period`
dan script `docker compose stop -t` memakai 660 detik; update bisa menunggu selama
drain ini. Jangan memaksa kill agar journal berbayar sempat diselesaikan. Bila
outcome masih belum pasti, request tetap fail-closed untuk review setelah restart.
Pilihan ROBOT ON/OFF persisten. Script update tidak mengaktifkan ROBOT, tetapi
worker baru dapat melanjutkan riset bila pilihan sebelumnya ON. Matikan ROBOT lewat
Office sebelum maintenance bila riset harus tetap berhenti.

Key disimpan mode 0600 di `${OFFICE_PRIVATE_DIR}/secrets/neuroapi_key`, dimount
sebagai Docker secret, disalin ke tmpfs 0600 milik UID10001. Environment
NEUROBRO_API_KEY_FILE menunjuk file; mode noncontainer juga mendukung
NEUROBRO_API_KEY. Jangan taruh key dalam .env, dashboard, argumen command, log,
atau kirim ke ChatGPT. Read/control token lama tetap digunakan. Tidak ada file
config privat lama yang dimount ke runtime baru.

Jika key kosong/tidak tersedia: NEUROAPI_NOT_CONFIGURED, tidak menjalankan siklus.
`.env.example` memuat setting nonsecret saja. Lookback/freshness terpusat di
Market dan environment Compose. Port publik hanya 80/443.

Migrasi ledger: boot mem-backup SQLite lama dari `/data/browser/ledger.sqlite3`
ke `/data/trading/ledger.sqlite3` sekali, termasuk trades/events/audit; file asal
TIDAK dihapus. Claim jadwal lama juga diimpor agar daily limit/duplicate protection
tidak direset. Migrasi tidak memindahkan atau menghapus kredensial lama. Rollback
image lama setelah siklus baru memerlukan rekonsiliasi ledger (jangan menjalankan
writer lama terhadap salinan state lama).

## Cleanup terpisah, setelah API benar-benar bekerja

Tidak ada penghapusan secret/state otomatis. Untuk menonaktifkan akses secret
lama, arsipkan folder config lama dan secret autentikasi desktop lama secara privat
(setelah deployment baru healthy):

```sh
sudo python3 deploy/archive-retired.py
```

Ini memindahkan file lama ke direktori arsip privat, bukan menghapus audit log atau
volume trading. State lama di volume dibiarkan untuk audit/rekonsiliasi.

## Health / restart

`docker compose ps`; `docker compose exec -T worker python -m worker.health`.
API menyediakan /health, /snapshot, /neuroapi/status (GET), /neuroapi/check dan
/neuroapi/run (POST), dengan bearer authorization, origin allowlist untuk mutasi,
no-store, tanpa log headers/body. Tidak ada endpoint untuk menerima provider key.
Restart Compose tidak menghapus counter, claim cycle atau request journal.
`restart: unless-stopped` memulihkan worker yang keluar dan setelah reboot VPS
ketika Docker berjalan; worker yang sengaja dihentikan memerlukan `compose up`.
Health memakai umur heartbeat dan thread hidup. Watchdog menghentikan proses bila
heartbeat actor lebih dari 600 detik atau pembacaan account lebih dari 120 detik,
sehingga restart policy dapat memulihkannya. Status Docker unhealthy sendiri
tidak menjalankan restart; recovery dilakukan watchdog. Unknown paid request
tetap membutuhkan review, tanpa replay otomatis.

ROBOT ON membangunkan coordinator segera, lalu polling riset setiap 45 detik
dengan maksimum satu operasi NeuroAPI per tick. Private GET account berjalan
terpisah dan memasok posisi serta saldo USDT nyata. `/robot/status` memakai
`wait_reason` untuk menjelaskan WAITING normal dan blocker lifecycle. Lihat
[ROBOT_WORKFLOW.md](ROBOT_WORKFLOW.md) untuk kapasitas, pengecualian HYPEUSDT dan
recovery screening yang dibatasi tiga replacement. Live order tetap OFF.

POST `/robot/simulation` memakai control token + exact Origin dan menyimpan hasil
lokal idempotent untuk setup terverifikasi. Tidak ada provider calls/order, key
baru, mutasi akun atau counter entry. GET `/robot/status` menyertakan tiket manual
dan hasil simulasi terbaru. Screening tetap memakai key NeuroAPI existing dan
approval unresolved tetap membatasi riset.

Spesifikasi awal: 1 vCPU, RAM 1 GB (2 GB disarankan), storage 10 GB, Ubuntu 24.04,
Docker Engine + Compose plugin. Ini perkiraan operasional, bukan benchmark VPS.
Belum dinyatakan terpasang/terhubung sampai pengguna menjalankan deployment.
