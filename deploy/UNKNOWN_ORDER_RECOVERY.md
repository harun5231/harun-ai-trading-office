# Pemulihan entry yang tidak ditemukan di Binance

`ORDER_OUTCOME_UNKNOWN` berarti callback adapter gagal tanpa hasil yang dapat
diverifikasi. Ini bukan bukti penolakan Binance. Jumlah posisi nol juga belum
membuktikan bahwa entry LIMIT tidak sedang menunggu. Coordinator mempertahankan
`NEEDS_REVIEW` dan tidak mengirim ulang intent tersebut.

## Perbaikan yang telah diverifikasi pada VPS

Pemulihan dilakukan saat robot OFF, sesudah pemeriksaan konfigurasi Cross 75×
dan endpoint validasi `/fapi/v1/order/test`. Respons sukses endpoint validasi
dapat berupa object berisi field placeholder; keberhasilan tidak ditentukan
hanya oleh respons `{}`. Validasi ini tidak membuka posisi dan tidak membuktikan
penerimaan entry/TP/SL nyata.

Dua pemeriksaan authenticated GET menemukan client order lama tidak ada,
riwayat order dan fill BTC kosong, serta tidak ada order entry, algo, atau posisi
BTC aktif. Backup ledger dan bukti pemeriksaan disimpan secara privat sebelum
transaksi lokal menyelesaikan intent dan candidate sebagai
`REJECTED / NO_ACCEPTED_ORDER_OBSERVED`. Status ini menyatakan hasil pemeriksaan
pemulihan, bukan respons rejection asli dari exchange.

Fingerprint SHA-256 adapter yang dipertahankan:

```text
7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422
```

Fingerprint `worker/robot.py` sesudah pemasangan diagnostics:

```text
4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef
```

Host dan container cocok; worker sehat, gateway `CONFIGURED`, robot OFF, dan
tidak ada intent `NEEDS_REVIEW` atau `SUBMITTING` tersisa pada verifikasi akhir.
Penyebab exception asli sebelumnya tidak tersimpan dan tidak dapat disimpulkan
dari keberhasilan pemeriksaan berikutnya. Peringatan Docker mengenai
`COMPOSE_BAKE=false` bukan kegagalan build. Laporan atau riwayat `PARTIAL` tetap
berarti datanya belum lengkap dan tidak diselesaikan oleh pemulihan ini.

## Utility pemulihan terbatas

[`recover_absent_entry.py`](recover_absent_entry.py) adalah helper pemeliharaan
sekali jalan untuk kasus BTCUSDT yang sudah diperiksa. Helper dijalankan sebagai
UID/GID worker di container dengan adapter terverifikasi. Syaratnya:

- Robot OFF dan exclusive lock `cycle.lock` berhasil diperoleh.
- Source SDK cocok dengan fingerprint sebelum dimuat.
- Satu intent dengan client ID deterministik yang cocok, candidate yang sama,
  status `NEEDS_REVIEW`, hasil NULL, dan tidak ada receipt entry miliknya.
- Tidak ada intent `SUBMITTING`, `ENTRY_PENDING`, atau `POSITION_PROTECTED` lain.
- Waktu Binance cocok dengan jam lokal dalam 30 detik. Intent berumur paling
  sedikit 10 menit, kurang dari 48 jam, dan dibuat pada hari WIB yang sama.
- Dua pemeriksaan GET memastikan lookup client ID menghasilkan kode `-2013`,
  riwayat rolling 72 jam lengkap dan kurang dari 1.000 baris, tidak ada matching
  entry, tidak ada fill BTC, dan tidak ada order/algo/posisi BTC aktif.
- Snapshot ledger dan setting tetap sama sebelum perubahan atomik.

Pemeriksaan BTC mengizinkan posisi atau order manual pada coin lain. Helper
tidak memanggil POST Binance, `submit`, `reconcile`, close, cancel, atau perubahan
margin/leverage. Journal request, receipt, payload intent, hasil NULL, cycle,
kuota, risiko, dan source SDK dipertahankan. Entry lama tidak dipromosikan menjadi
READY atau dikirim ulang.

Backup ledger 0600 dan `proof.json` berada di direktori privat 0700 di
`/data/trading/maintenance/absent-entry-*`. Bukti arsip memakai status
`ABSENCE_VERIFIED`; output `ABSENT_ENTRY_REJECTED` hanya dicetak setelah commit.
Bila syarat tidak terpenuhi, output `RECOVERY_REFUSED` menyertakan tahap. Jangan
mengubah guard atau memakai helper untuk membersihkan order yang masih
berpotensi diterima Binance.

Setelah peninjauan kasus yang sesuai, ganti placeholder client ID dengan ID
intent yang telah diperiksa. Perintah berikut tidak diperlukan lagi pada VPS
yang sudah mencetak `VPS_ORDER_RECOVERY_VERIFIED`.

```bash
(
  set -euo pipefail
  cd /root/harun-ai-trading-office
  git fetch origin main
  git show origin/main:deploy/recover_absent_entry.py |
    docker compose -f compose.yaml exec -T --user 10001:10001 worker \
    python -B - \
    --sdk-sha 7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422 \
    --client-id '<CLIENT_ORDER_ID_DARI_JOURNAL>'
)
```

## Diagnostics callback berikutnya

`Coordinator.gateway_failure_code()` menyimpan hanya kode yang dikenali dan
tahap dari frame adapter pada path source yang dipercaya. Contoh:
`ORDER_UNKNOWN_CONFIGURE_C2015`, `BINANCE_ORDER_FEE_CHANGED`, atau
`BINANCE_ORDER_OUTCOME_UNKNOWN_CONFIGURE`. Pesan exception mentah, query bertanda
tangan, key, dan respons provider tidak disimpan sebagai failure code.
Exception tetap menghasilkan `NEEDS_REVIEW`; diagnostics tidak memberi izin
retry atau menganggap order pasti ditolak.

[`patch_gateway_diagnostics.py`](patch_gateway_diagnostics.py) mereproduksi
perubahan source yang dipasang di VPS. Default hanya inspeksi; `--apply` mengubah
dua catch callback dan menambah helper, dengan backup privat di luar repository,
pemeriksaan anchor, serta pemeliharaan mode/owner/newline. Utility ini tidak
mengimpor adapter atau mengakses Binance. Pertahankan robot OFF dan periksa
kecocokan host/container sebelum instalasi serta setelah rebuild.

VPS yang sudah terverifikasi tidak memerlukan pull, reset, atau rebuild ulang
setelah publikasi ini. Adapter lokal dan Dockerfile kustom tetap dipertahankan.
Aturan dua entry per hari, dua posisi bersamaan, risiko net termasuk fee,
Cross 75×, HOLD replacement, dan perilaku OFF tetap sama.

Sesudah robot dinyalakan oleh pemilik, penerimaan nyata harus dibuktikan dengan
ID dan field order Binance yang sesuai serta proteksi SL/TP yang terverifikasi.
Status `CONFIGURED`, tes offline, dan pemulihan intent lama tidak menggantikan
bukti tersebut.
