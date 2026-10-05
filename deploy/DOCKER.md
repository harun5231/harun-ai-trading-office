# Worker API pada VPS Ubuntu 24.04

Worker memakai `python:3.12-slim-bookworm`, standard library saja, UID 10001 setelah
bootstrap. Caddy tetap HTTPS; API hanya jaringan internal Compose. Volume
`harun-office_worker_data` dan volume sertifikat dipertahankan. Tidak ada dependency
image MCR. Docker Hub masih harus dapat diakses untuk base Python saat build pertama.

## Update VPS existing

1. `git pull --ff-only origin main` dari folder instalasi existing.
2. `sudo python3 deploy/setup.py`: hanya meminta API key bila belum tersimpan;
   input getpass tidak tampil, tidak berada di argumen command/history.
3. `bash deploy/update-api.sh`: build sebelum stop, lalu recreate worker/proxy.
   Jangan mengganti nama Compose project/volume dan jangan menghapus volume.
4. Hubungkan kantor ke endpoint HTTPS worker yang sama, token kontrol lama.
   CEK API → NEUROAPI CONNECTED membuktikan authenticated health, bukan hasil riset.
   Jalankan DRY RUN untuk siklus berbayar; periksa laporan/REJECTED bila gagal.

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

Spesifikasi awal: 1 vCPU, RAM 1 GB (2 GB disarankan), storage 10 GB, Ubuntu 24.04,
Docker Engine + Compose plugin. Ini perkiraan operasional, bukan benchmark VPS.
Belum dinyatakan terpasang/terhubung sampai pengguna menjalankan deployment.
