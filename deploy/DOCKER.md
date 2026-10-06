# Worker API pada VPS Ubuntu 24.04

Worker memakai `python:3.12-slim-bookworm`, standard library, dan UID/GID 10001
setelah bootstrap. Caddy menyediakan HTTPS. Port worker 8787 hanya berada di
jaringan internal Compose; port publik 80/443. Nama project `harun-office` dan
volume `harun-office_worker_data` dipertahankan.

## Update instalasi existing

Setelah perubahan digabungkan ke `main`, pilih ROBOT OFF di Office lalu jalankan
di Termius:

```sh
cd /root/harun-ai-trading-office
git status --short
git fetch origin &&
git switch main &&
git pull --ff-only origin main &&
git log -1 --oneline &&
bash deploy/update-api.sh
```

Jika Git menolak karena perubahan lokal, rekonsiliasi file tersebut dahulu.
Script build sebelum menghentikan service, menunggu request berjalan menyimpan
hasil, lalu mengganti worker dan proxy dengan volume serta secret existing.
Build Docker tidak menerbitkan frontend; GitHub Pages memakai source `main`
dan deployment Pages tersendiri.

Upgrade pertama membuat `ledger-pre-order.sqlite3` sebagai backup audit privat,
menghapus tabel alur lama dari database aktif, dan menyimpan ROBOT OFF. Backup
tidak menjadi mode alternatif. Journal request NeuroAPI dan entry nyata yang
terkonfirmasi dipertahankan. Setelah migrasi, jangan menjalankan writer dari
image lama terhadap state yang telah diupgrade. Volume dan secret tidak dihapus.

## Secret dan konfigurasi

Untuk instalasi baru, salin `.env.example` ke `.env`, isi domain worker serta
lokasi privat di luar checkout, lalu jalankan:

```sh
sudo python3 deploy/setup.py
bash deploy/update-api.sh
```

`setup.py` hanya meminta secret yang belum tersedia. Input key tidak ditampilkan
atau menjadi argumen shell. Read/control token dan key provider disimpan mode
0600 pada `${OFFICE_PRIVATE_DIR}/secrets`, dimount sebagai secret Compose, lalu
disalin ke tmpfs privat milik UID10001. Key tidak berada di `.env`, GitHub Pages,
log, atau output diagnostics. Dashboard memakai token worker, bukan key Binance.

`.env` hanya mengatur domain, origin, lokasi private, lookback candle, dan batas
umur context market. Tidak ada pilihan jadwal atau jalur pengiriman kedua.
Key kosong menghasilkan status tidak dikonfigurasi dan menghentikan riset.

## Health, recovery, dan izin state

```sh
docker compose ps
docker compose exec --user 10001:10001 -T worker python -m worker.health
docker compose exec --user 10001:10001 -T worker python -m worker.robot_status
```

Selalu gunakan `--user 10001:10001` untuk CLI worker. Bootstrap image dimulai
sebagai root lalu service turun ke UID10001; `docker compose exec` tanpa user
masih dapat memakai root dan membuat state yang tidak dapat dibuka service.
Bootstrap memperbaiki owner/mode hanya pada direktori trading, lock, SQLite
beserta sidecar bernama yang dikenali, dan backup audit. Inode/data dipertahankan;
symlink, hardlink, dan file bukan regular ditolak.

Compose memakai `restart: unless-stopped`. Docker harus aktif dan enabled agar
service kembali setelah reboot. Watchdog menghentikan proses ketika heartbeat
actor lebih dari 600 detik atau pembacaan account lebih dari 120 detik.
Status `unhealthy` sendiri tidak menjalankan restart; watchdog yang memulihkannya.
Saat SIGTERM, request baru dihentikan dan operasi berjalan diberi waktu hingga
600 detik; Compose serta updater menyediakan grace 660 detik.

API privat hanya memiliki GET `/health`, `/robot/status`, `/office/status`, dan
POST `/robot/settings`. Endpoint status tidak menjalankan riset atau order.
Pengaturan membutuhkan control token dan exact Origin; read token hanya dapat
membaca. Caddy menolak path lain.

Status `ONLINE` membuktikan proses tersedia. Coordinator `EXECUTION_BLOCKED`
dengan kode `BINANCE_ORDER_GATEWAY_NOT_CONNECTED` dan gateway `NOT_CONNECTED`
berarti adapter entry/TP/SL belum dihubungkan; order nyata belum dikirim.
Integrasi developer dijelaskan pada [ORDER_INTEGRATION.md](ORDER_INTEGRATION.md).
