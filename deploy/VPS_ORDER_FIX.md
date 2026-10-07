# Perbaikan adapter VPS sebelum ROBOT ON

Pemeriksaan source container menemukan adapter
`74b1db1b51461af34d0d4f0ecc189d4876bc72a6e021a049f9dcd22c9dd3509c`.
Hash host dan container cocok; OFF terkonfirmasi. Seluruh 13 fingerprint worker
cocok dengan baseline sebelum perbaikan antrean/guard OFF; `research_guard.py`
belum ada. Pemeriksaan ini tidak mengirim order atau mengakses credential akun.

## Masalah pada adapter teramati

| Temuan | Dampak |
| --- | --- |
| CROSS dan 75 hanya diteruskan sebagai field intent | Tidak ada POST konfigurasi margin/leverage atau konfirmasi konfigurasi symbol. |
| TP/SL dikirim ke endpoint order reguler; exception ditelan | Entry dapat diterima sementara pemasangan proteksi gagal. Tidak ada bukti exchange menerima kedua proteksi. |
| Konfirmasi proteksi hanya melihat status | Symbol, exit side, trigger, quantity, working type, dan reduce-only belum diverifikasi. |
| Partial fill positif dilaporkan ENTRY_PENDING | Melanggar kontrak coordinator dan berhenti pada NEEDS_REVIEW. |
| first_fill_at berasal dari updateTime entry | Timestamp dapat berubah saat tambahan fill/cancel, sehingga receipt dan hari WIB tidak stabil. |
| CANCELED/EXPIRED dianggap CLOSED | Pembatalan entry bukan bukti exit posisi; tidak ada exit order dan closed_at. |
| GET gagal menghasilkan REJECTED dengan ID kosong | Query gagal tidak membuktikan entry tidak ada; coordinator memperlakukannya sebagai outcome belum pasti. |

Conditional TP/SL kini memakai `POST /fapi/v1/algoOrder`, `clientAlgoId`, dan
`triggerPrice` sesuai [dokumentasi resmi Binance](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).
Query algo menggunakan `orderType`, bukan field `type` pada request.
Margin symbol memakai `/fapi/v1/marginType` dengan `CROSSED`; leverage memakai
`/fapi/v1/leverage`. Respons konfigurasi dan bracket tetap harus dikonfirmasi.

Literal private pada snapshot disamarkan. Nilai base URL, status tertentu, atau
`closePosition` yang disamarkan tidak dijadikan bukti bahwa nilainya salah.

## Ruang perubahan

Utility repair mengganti **hanya class `OrderGateway`** pada file lokal yang
fingerprint-nya cocok. Ketujuh metode yang teramati tetap tersedia, dengan
implementasi yang diperbaiki. Import, `build_intent`, `require_implementation`,
dan semua byte di luar class tetap dipertahankan. Backup privat dibuat di luar
Git sebelum perubahan; source di VPS tidak dihapus.

Template deployment bukan executor runtime tambahan: class-nya ditempatkan
pada satu file `worker/order_gateway.py` yang sudah dipakai coordinator. Worker
tidak mengimpor template dari `deploy/`. Gateway bawaan repository tetap untuk
instalasi yang belum memiliki adapter; prosedur ini khusus adapter VPS existing
yang sudah diaudit, bukan pemasangan ke arbitrary source.

Updater worker hanya memperbarui empat modul yang berubah serta menambah guard
riset, dari commit tetap `79d3c51a6bdc7c80b98ac16092286c0f3e8b8f0f`:

```text
worker/robot.py
worker/binance_private.py
worker/market.py
worker/neuroapi.py
worker/research_guard.py
```

Fingerprint baseline/target dan sembilan modul terkait yang tidak berubah
diperiksa. Gateway lokal dan Dockerfile dipertahankan oleh updater tersebut;
Compose, secret, ledger, journal, posisi manual, file untracked, serta tampilan
3D tidak diganti. Tidak ada `git pull/reset`, penghapusan journal, atau ON otomatis.

## Jalankan sekali melalui Termius

Pertahankan ROBOT OFF pada semua tab. Source/container lama tetap berjalan
sampai image baru selesai dibangun. Semua tool memeriksa OFF sendiri, termasuk
tepat sebelum perubahan file. Prosedur berhenti jika fingerprint, hak akses,
status OFF, atau label Compose berbeda dari instalasi yang diperiksa.

```bash
(
  set -euo pipefail
  cd /root/harun-ai-trading-office
  git fetch origin main
  git show origin/main:deploy/apply_vps_order_fix.sh | bash
)
```

Script mengambil source tool/template dari satu revision remote, menjalankan
inspeksi kedua rencana sebelum menulis, membuat backup, menerapkan perubahan
terarah, build image, drain worker, lalu mengganti container dengan volume yang
sama. Dockerfile kustom tidak diubah; dependency yang sudah dipasang tetap ada.
Script tidak mengirim test order, tidak meminta screening, dan tidak memilih ON.
Worker baru tetap OFF. Status akhir dibaca melalui API lokal yang sudah ada.

Script ini untuk satu kali pemasangan pada fingerprint yang telah diaudit.
Jika tahap build/start gagal setelah source diperbaiki, jangan menjalankan repair
ulang atau memaksa fingerprint baru. Backup dan script staging tetap tersedia;
kirim kode error serta status container untuk menuntaskan tahap yang gagal.

Output yang diperlukan: `GATEWAY_REPAIRED`, updater `APPLIED`, pemeriksaan
`ROBOT_OFF_CONFIRMED`, `GATEWAY_SOURCE_MATCH`, `WORKER_SOURCE_MATCH`, container healthy, dan snapshot
dengan `robot_on:false`. Ringkasan `order_intent_counts` membaca journal tanpa
mengubahnya; status OFF saja tidak membuktikan journal bebas dari NEEDS_REVIEW.
Metadata gateway `CONFIGURED` berarti credential terbaca, bukan bukti order
atau proteksi diterima exchange. `can_trade:true` pada reader hanya membuktikan
permission yang dilaporkan akun.

## Koneksi Office dan bukti penerimaan Binance

Origin worker yang teramati adalah `https://103-147-33-13.sslip.io`. Akses HTTPS
tanpa token ke `/health` merespons 403, membuktikan endpoint terjangkau tetapi
bukan autentikasi. Konfigurasi frontend mengisi origin tersebut; token kontrol
tetap diisikan oleh pemilik akun dan hanya berada di memori tab. Jangan menaruh
token atau API key pada GitHub, URL, atau output yang dibagikan.

Sesudah deployment, kirim status akhir untuk review sebelum ON. Jika sudah ada
intent `NEEDS_REVIEW`/`SUBMITTING` dari percobaan sebelumnya, source baru tidak
menghapus atau mengirim ulang intent tersebut. Pemulihan membutuhkan bukti GET
berdasarkan client/order IDs yang ada, termasuk fill dan proteksi. Order/posisi
lama tidak diadopsi hanya karena mempunyai symbol atau prefix yang sama.

Jika gateway sudah `CONFIGURED` tetapi coordinator melaporkan
`ROBOT_REQUEST_NEEDS_REVIEW`, periksa journal NeuroAPI melalui `worker diagnostics`.
Request historis tetap tersimpan terpisah dari journal order. Untuk format
riset lama yang terverifikasi dan tidak terkait bukti order aktif, tersedia
[pemulihan journal riset lama](LEGACY_RESEARCH_RECOVERY.md) tanpa mengganti source
VPS atau mengirim ulang request.

Penerimaan entry nyata dibuktikan oleh GET entry dengan ID, side, price,
quantity, dan konfigurasi symbol yang cocok. Proteksi dibuktikan oleh GET algo
dan fill sebenarnya, termasuk trigger, reduce-only, quantity coverage, dan
working type. Belum ada bukti tersebut dari pengujian offline; jangan menyebut
kelulusan tes atau metadata koneksi sebagai transaksi berhasil.

Target risiko net termasuk fee tetap menggunakan rules akun dari coordinator;
default 5 USDT dan dapat diubah pada Office. Quantity tidak dikalikan 75.
Slippage, funding, gap harga, perubahan fee, dan likuidasi tidak dijamin oleh
estimasi loss pada harga SL. Riwayat Binance mempunyai batas window/retention;
data terpotong atau belum pasti berhenti untuk review, bukan dibentuk menjadi
receipt atau CLOSED tanpa bukti.

SL dipasang dan dibuktikan lebih dahulu, kemudian TP, untuk quantity yang benar
benar terisi. Tambahan partial fill diperiksa kembali dalam callback yang
dibatasi waktu. Entry LIMIT yang masih belum terisi dapat selesai sesudah
callback; proteksinya baru dipasang ketika reconcile berikutnya melihat fill
(interval coordinator saat ini 45 detik). Ini bukan proteksi atomik saat fill.
Jika kegagalan terjadi, adapter berusaha membatalkan hanya sisa entry milik
intent yang sudah dibuktikan, tanpa menutup posisi terisi atau menyentuh order
manual. Outcome yang tidak dapat dibuktikan diteruskan ke NEEDS_REVIEW.

OFF menghentikan callback berikutnya, tanpa close/cancel posisi atau order.
Callback yang sudah dimulai dapat menyelesaikan verifikasi/proteksi dan
pembatalan sisa entry miliknya bila diperlukan; OFF tidak dapat menarik kembali
request yang sudah terkirim. Journal hasilnya tetap dipertahankan.

## Restore terarah

Kedua utility mencetak lokasi backup privat yang berbeda. Restore hanya dapat
berjalan ketika OFF dan target masih cocok dengan manifest; perubahan lain
yang dibuat setelah pemasangan tidak ditimpa. Memulihkan source tidak mengubah
order atau posisi di exchange dan tidak otomatis mengganti image container.

```bash
python3 -I -B -S /root/harun-ai-trading-office-order-fix.XXXXXX/repair_existing_gateway.py --project /root/harun-ai-trading-office --restore /root/LOKASI_BACKUP_GATEWAY
python3 -I -B -S /root/harun-ai-trading-office-order-fix.XXXXXX/update_existing_worker.py --project /root/harun-ai-trading-office --restore-backup /root/LOKASI_BACKUP_WORKER
```

Ganti lokasi placeholder dengan path yang dicetak pada pemasangan. Pilih restore
file yang memang diperlukan setelah meninjau kegagalan; jangan menggunakan
backup sebagai cara menghapus status order yang outcome-nya belum diketahui.
