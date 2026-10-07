# Pemeriksaan sebelum ROBOT ON pada VPS existing

Adapter order terbaru tersimpan hanya di VPS. Source GitHub dan status
`CONNECTED` atau `CONFIGURED` tidak membuktikan versi adapter yang berjalan,
entry yang diterima Binance, maupun proteksi SL/TP. Pemeriksaan ini mengambil
source tersamarkan dan fingerprint dari **container yang sedang berjalan**.
Tidak ada import adapter, panggilan API, pengiriman order, perubahan settings,
atau penghapusan file.

Adapter dengan fingerprint `74b1db1b51461af34d0d4f0ecc189d4876bc72a6e021a049f9dcd22c9dd3509c`
sudah ditinjau dan memerlukan perbaikan. Gunakan
[perbaikan VPS yang terarah](VPS_ORDER_FIX.md) untuk fingerprint tersebut;
pemeriksaan di bawah tetap berlaku untuk source lain atau setelah deployment.

## Ambil bukti melalui Termius

Pertahankan ROBOT OFF di Office selama pemeriksaan. Salin blok berikut:

```bash
cd /root/harun-ai-trading-office
git --no-optional-locks status --short
sha256sum worker/order_gateway.py
docker compose -f compose.yaml ps
(
  set -euo pipefail
  git fetch origin main
  git show origin/main:deploy/inspect_vps_gateway.py |
    docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S -
)
```

`git fetch` memperbarui referensi remote saja. Perintah ini tidak menjalankan
`git pull`, updater, rebuild, restart, atau replace gateway. Gateway lokal,
Dockerfile kustom, volume data, dan file untracked tetap dipertahankan.

Collector membaca `enabled` dari ledger dengan SQLite read-only dan menolak
melanjutkan jika OFF tidak dapat dibuktikan. Python memakai `-I -B -S` agar
inspeksi tidak memuat package startup atau menghasilkan bytecode. Source
gateway diparse sebagai AST; komentar dibuang dan literal selain token protokol
publik disamarkan. Tidak ada pembacaan `.env`, environment credential, atau file
secret. Jangan sertakan nilai API key atau secret saat mengirim hasil.

Output JSON memuat:

- `robot_on: false`: OFF teramati saat inspeksi; bukan perubahan settings.
- `gateway.sha256`: hash file adapter di container. Bandingkan dengan hash host
  yang dicetak oleh `sha256sum`. Perbedaan berarti kedua source belum identik.
- `gateway.sanitized_source`: source yang bisa ditinjau tanpa menjalankan SDK.
- `workflow_sha256` dan `workflow_status`: fingerprint file alur yang terpasang.
  `MISSING` pada modul baru dapat menunjukkan image masih memakai versi lama.

Output dibatasi 64 KiB. Jika muncul `ROBOT_OFF_REQUIRED`, matikan robot di Office
dan ulangi pemeriksaan. Error lain menghentikan collector tanpa mencetak source
mentah. Kirim output teks pemeriksaan untuk review versi yang sama dengan VPS.

## Aturan yang harus dibuktikan dari versi terpasang

| Bagian | Perilaku yang diperlukan |
| --- | --- |
| Kapasitas | Maksimum dua posisi bersamaan, termasuk manual dan carryover. Pending entry juga mencadangkan kapasitas. HYPEUSDT tidak boleh diambil alih. |
| Kuota harian | Maksimum dua entry bot pada hari WIB dari first fill terverifikasi. Partial fill dihitung sekali; restart mempertahankan receipt. Posisi close tidak mengembalikan kuota hari itu. |
| Screening | Minta sebanyak slot tersedia, satu atau dua coin. Analisis memakai chart Binance USD-M Futures 1h dan 15m. |
| Pengganti | HOLD dan penolakan lokal `REJECTED` dengan kode `NET_RISK_REWARD_BELOW_2` atau `NET_RISK_REWARD_NOT_TARGET_2` tanpa plan/intent order berbagi maksimum tiga screening tambahan. Hanya putaran terakhir, sesuai slot; status penolakan dan journal dipertahankan. Kegagalan lain/unknown tidak diganti. |
| Risiko | Target dapat diubah di Office. Quantity legal terbesar menghasilkan estimasi loss SL termasuk fee entry dan SL exit tidak melebihi target. Plan baru memakai `NET_1_TO_2_NEAREST_TICK`: TP tepat pada tick legal terdekat yang mencapai net RR 1:2; plan lama tanpa kebijakan ini tetap memerlukan net RR minimal dua. |
| Konfigurasi exchange | Adapter memastikan CROSS dan leverage 75 pada symbol terpilih sebelum entry. Menyimpan nilai tersebut dalam intent saja belum mengubah konfigurasi Binance. |
| Entry dan proteksi | LIMIT GTC LONG/SHORT memakai harga dan quantity intent; SL/TP lawan side, hanya mengurangi exposure bot. Plan baru memakai pemicu Terakhir (`CONTRACT_PRICE`); plan lama tanpa field tersebut tetap `MARK_PRICE`. Identitas order, basis pemicu, dan quantity terisi diverifikasi dari Binance. |
| Outcome belum pasti | Tidak mengirim ulang entry otomatis setelah timeout/restart. Kegagalan proteksi dilaporkan, bukan ditelan atau dianggap berhasil. |
| OFF | Tidak memulai request riset, submit, atau rekonsiliasi gateway berikutnya; tidak close/cancel posisi dan order. Request yang sudah terkirim boleh selesai dan dijournal. Polling saldo/posisi tetap tersedia. |

Collector tidak menghasilkan verdict kesiapan order nyata. Setelah static review,
bukti GET order entry, algo SL/TP, konfigurasi symbol, dan fill yang sudah ada
masih diperlukan untuk memastikan Binance menerima parameter yang benar. Tidak
perlu membuat order baru hanya untuk menjalankan collector.

## Perbaikan coordinator pada repository

Antrean hasil screening kini tetap tersimpan ketika kapasitas menyusut sementara.
Antrean v9 pada hari yang sama yang sebelumnya salah ditandai COMPLETE dapat
diteruskan sesuai anggaran awal dan batas bersama putaran pengganti; cycle yang benar-benar habis
tidak dibuka kembali. Setup siap dari seluruh cycle mencadangkan slot sebelum
analisis tambahan.

Coin yang dipastikan tidak lagi ada di katalog Futures ditolak dan dilewati
secara atomik, sehingga coin valid berikutnya tidak tertahan. Gangguan GET
katalog atau fee sementara tetap dapat dicoba kembali tanpa claim berbayar.

OFF diperiksa sebelum setiap GET riset berikutnya, termasuk chart dan fee,
dalam context coordinator. Polling akun pada thread lain tetap berjalan. Respons
analisis berbayar yang sudah diterima tetap dijournal memakai rules yang telah
dibaca jika OFF terjadi saat refresh. Pemeriksaan RR parser memakai perbandingan
rasional eksak agar hasil tidak bergantung pada presisi Decimal.

Guard riset tidak menginterupsi callback `submit`/`reconcile` yang sudah dimulai:
adapter mungkin sedang menyelesaikan proteksi sesudah entry. Callback tersebut
dapat menyelesaikan request-nya dan dijournal; OFF mencegah pemanggilan callback
berikutnya. Exception adapter tetap menghasilkan outcome belum pasti, tanpa
reset atau replay entry otomatis.

Pembaruan gabungan enam file pada VPS existing telah diverifikasi pada
7 Oktober 2026: package, patch gateway, dan fingerprint source host/container
cocok; worker healthy setelah restart 10:04 UTC. ETH lama tetap memakai
`MARK_PRICE`, SL 2585 / TP 2730, quantity 0.181, dan satu receipt. Snapshot bot
10:03:37 UTC mendahului restart, sehingga belum membuktikan putaran pengganti
baru atau order baru. Observasi cycle, kandidat, serta order setelah restart
tetap diperlukan.

Untuk instalasi lain atau perubahan berikutnya, bandingkan fingerprint dahulu
dan gunakan update terarah yang mempertahankan adapter lokal; jangan memakai
pull/reset atau updater standar untuk menggantinya dengan gateway bawaan.
Rincian kontrak adapter ada di
[ORDER_INTEGRATION.md](ORDER_INTEGRATION.md).
