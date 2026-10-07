# Operasi robot 24/7

Worker Python berjalan di VPS dengan Docker Compose, Caddy HTTPS, dan volume
state existing. Browser Office dapat ditutup. Docker memakai
`restart: unless-stopped`; watchdog menangani actor yang berhenti memperbarui
heartbeat. Jalankan Docker saat boot VPS. Detail ada di [DOCKER.md](DOCKER.md).

ON mengizinkan screening, analisis, entry, dan rekonsiliasi ketika slot/kuota
tersedia. OFF memblokir setiap provider request dan Binance mutation baru,
termasuk TP/SL/config/cancel/close. Hasil request yang sudah terkirim tetap dapat
dijournal. Account/status GET tetap berjalan. OFF tidak membatalkan pending
entry atau menutup posisi/order existing dan manual.

## Pemeriksaan Termius

Perintah berikut hanya membaca status/koneksi; selalu gunakan UID/GID worker:

```bash
cd /root/harun-ai-trading-office
docker compose -f compose.yaml ps
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker diagnostics
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker api-check
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker binance-check
```

`ONLINE` menunjukkan proses/actor tersedia, `CONFIGURED` konfigurasi gateway.
Keduanya bukan bukti entry/TP/SL diterima. Periksa timestamp coordinator dan
account; tick sekitar 45 detik sehingga pemeriksaan 30 detik setelah perubahan
belum selalu mencakup tick berikutnya. Laporan `PARTIAL` tetap belum lengkap.

## Aturan operasi

Maksimal dua first fill entry bot per hari WIB (UTC+7), dan dua occupancy
termasuk manual/carryover serta pending entry manual/bot. Symbol yang sama
pada posisi/open entry/reservasi intent dihitung sekali. HYPEUSDT selalu
manual-only. Receipt first fill tidak dihapus setelah close/restart.

Screening meminta satu/dua coin USD-M USDT perpetual aktif. Analisis memakai
candle fresh 15m/1h dan commission akun per symbol. HOLD/RR di bawah dua sebelum
plan/intent dapat mencari coin berbeda, berbagi maksimal tiga putaran pengganti.
Unknown outcome tidak mendapat replay. Cycle selesai tanpa setup valid menunggu
hari/epoch yang sesuai, bukan menjalankan riset berbayar ulang tanpa batas.

Risk target editable pada menu existing, default 5 USDT. Sizing V3 memasukkan
fee entry/exit dan reserve adverse exit 0,5%. Entry/SL tetap dari model; setup
minimal RR 2 mendapat TP eksekusi net 1:2 dengan fee/reserve dan tick legal.
CROSS 75× tidak memperbesar risk budget. Gap/slippage melampaui reserve, funding,
fee berubah, atau liquidation dapat membuat loss aktual di atas target.

Entry LIMIT yang belum fill masih pending dan memakai slot. Saat ON, actual
fill diproteksi SL market terlebih dahulu lalu TP market, pemicu Terakhir,
100% exposure bot yang masih terbuka. GET dua proteksi diperlukan sebelum
`POSITION_PROTECTED`; polling/latency berarti fill tidak atomik dengan proteksi.

## Update dan cleanup

Revisi dipasang/verifikasi di VPS saat OFF dahulu, lalu diterbitkan di GitHub.
Jangan menjalankan installer/seed/repair historis untuk membuka ulang cycle.
Operation tetap v9; response COMPLETE yang cocok hanya dapat diselesaikan
lokal setelah crash bila metadata waktu chart asli masih fresh. Metadata
hilang/stale ditolak lokal tanpa POST provider/replay. Intent lama tetap mengikuti
provenance-nya. Unknown order/request memerlukan pemeriksaan bukti, bukan reset.

Installer current harus memverifikasi source host/image, OFF, journal, dan
inventaris GET Binance. Source/image rollback tidak mengembalikan ledger ke
snapshot lama. Cleanup source/command robot historis hanya sesudah bukti akhir,
tanpa menghapus data, secret, backup, volume, fitur Office, dan order manual.
Untuk adapter privat, jangan menjalankan pull/reset yang menimpa SDK lokal.
Lihat [panduan Termius](TERMIUS_24_7.md) dan [alur lengkap](ROBOT_WORKFLOW.md).
