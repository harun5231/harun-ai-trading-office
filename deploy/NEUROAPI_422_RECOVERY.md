# Pemulihan request NeuroAPI HTTP 422 tanpa replay

Pemasangan VPS pada 7 Oktober 2026 pukul 11:14 UTC telah menghasilkan
`ETH_CLOSED_JOURNAL_VERIFIED`, `SOL_HTTP_422_SETTLED`,
`NEUROAPI_422_RECOVERY_VERIFIED`, dan
`ETH_CLOSED_AND_NEUROAPI_FIX_INSTALLED`. Source host/container cocok dan worker
healthy. Closure ETH telah dicatat, receipt tetap satu, request SOL mendapat
state terminal yang benar, dan pemeriksaan saat settlement tidak menemukan
unresolved order.

Pemeriksaan terbaru pada 11:19 UTC telah membuktikan coordinator memperbarui
status sesudah settlement: robot ON, `INSUFFICIENT_ACTIONABLE_SETUPS`,
`wait_reason=ROBOT_CYCLE_COMPLETE`, tanpa failure robot atau akun. Timestamp
coordinator `2026-10-07T11:19:25.463839+00:00` dan account snapshot
`2026-10-07T11:19:52.454Z` lebih baru dari pemasangan. Daftar request dan job
`PENDING`/`NEEDS_REVIEW` kosong; order ETH asli `CLOSED` tanpa failure, receipt
tetap satu, dan tidak ada posisi aktif.

Putaran pengganti ketiga telah berjalan dan menghasilkan kandidat ETHUSDT baru
pada epoch 1, ditolak `NET_RISK_REWARD_NOT_TARGET_2` sebelum order. Cycle
selesai setelah batas tiga putaran, sehingga slot tersedia tidak memicu riset
ulang tanpa batas. Ini membuktikan pemulihan blok teknis dan alur pengganti;
belum ada order pengganti yang diterima Binance. Penerimaan order masa depan
dengan `CONTRACT_PRICE` maupun penanganan partial fill pada versi baru belum
dibuktikan oleh kasus ini. Laporan akun masih `PARTIAL`.

Installer sebelumnya berhenti pada `UNRESOLVED_ORDER` setelah `SOURCE_MATCH`,
sebelum perubahan source karena journal ETH memerlukan bukti closure.
Marker pembaruan enam file
`VPS_RR_TARGET_LAST_PRICE_VERIFIED` sebelumnya tetap membuktikan source yang
sudah terpasang; ia tidak membuktikan kelulusan request NeuroAPI berikutnya.
Jangan mengulang `apply_rr_replacement.sh`: script tersebut dipin untuk
pemasangan sekali dan telah dijalankan.

## Kasus yang teramati

Pada cycle `2026-10-07:robot-v9:1`, BTCUSDT ditolak dengan
`NET_RISK_REWARD_BELOW_2`, lalu coin penggantinya BNBUSDT ditolak dengan
`NET_RISK_REWARD_NOT_TARGET_2`. Putaran pengganti berikutnya memilih SOLUSDT.
Request analisis SOL menerima respons HTTP 422 dari NeuroAPI sebelum ada plan
atau intent order SOL. Kode lama menyimpan request/job dan cycle sebagai
`NEEDS_REVIEW`, sehingga pencarian selanjutnya berhenti.

ETH sebelumnya terverifikasi `POSITION_PROTECTED`: entry LONG 0,181 ETH pada
2.610, SL 2.585, TP 2.730, kedua proteksi `MARK_PRICE`, dan satu receipt fill.
Audit GET 10:45 UTC membuktikan SL `FINISHED` telah menghasilkan exit SELL
MARKET `FILLED`, order `8389766291712624741`, average price 2.575,81 pada
7 Oktober 2026 pukul 10:07:39.052 UTC. TP `CANCELED` dengan
`actualOrderId` kosong; tidak ada posisi, open order, atau open algo ETH.
Journal sempat `NEEDS_REVIEW / BINANCE_ORDER_EXIT_RACE`, tercatat pada
10:07:57 UTC. Pencatatan GET-only pada 11:14 UTC telah memverifikasi `CLOSED`
untuk exit yang sudah terjadi, tanpa entry ulang dan tanpa mengganti receipt.

Realisasi trade adalah -6,18839 USDT, fee entry 0,094482 USDT, dan fee exit
0,2331108 USDT; hasil trade sesudah kedua fee -6,5159828 USDT, belum termasuk
funding. Pemicu SL 2.585 tidak menjamin fill pada harga itu: exit market
mengalami slippage. Budget terencana 5 USDT adalah estimasi sizing, bukan
jaminan batas loss aktual. Payload, level, trigger, quantity entry, dan
receipt historis tetap dipertahankan.

## Batas input dan bentuk request baru

[Changelog resmi NeuroAPI](https://neuroapi.neurobro.ai/docs/changelog)
mencatat batas v0.1.1: `prompt` maksimal 32.000 karakter,
`message_history` maksimal 50 pesan, dan `content` setiap pesan maksimal
32.000 karakter. [Panduan error resmi](https://neuroapi.neurobro.ai/docs/guides/errors)
menempatkan HTTP 422 sebagai kegagalan validasi request; mengirim ulang body
yang sama tanpa memperbaikinya bukan pemulihan.

Journal menyimpan hash body SOL, tetapi tidak menyimpan seluruh body request
historisnya. Pemeriksaan ukuran context yang dibentuk sekarang dapat menguji
kepatuhan kode lama terhadap batas tersebut; ia tidak membuktikan bahwa batas
karakter adalah satu-satunya penyebab respons 422 historis. Hasil angka ukuran
request masih menunggu audit VPS saat catatan ini dibuat.

Module [`worker/neuroapi_request.py`](../worker/neuroapi_request.py) menjaga
request kecil identik dengan bentuk sebelumnya. Context analisis yang terlalu
besar dibagi menjadi pesan bernomor dalam satu `message_history`: metadata
dan bagian candle tiap timeframe 1h/15m tetap utuh, berurutan, dengan seluruh
harga, timestamp, filter, fee, dan batas risiko. Instruksi meminta model
menggabungkan semua bagian menjadi satu context. Bentuk JSON lain dapat
dibagi menjadi potongan teks JSON yang harus digabungkan kembali.

Partisi tidak membuang candle, memotong data, atau mengganti data asli dengan
ringkasan. Semua bagian tetap berada dalam satu request NeuroAPI dan satu
claim persisten, bukan rangkaian request ulang. Prompt, jumlah pesan, dan
panjang masing-masing content diperiksa sebelum pengiriman. Data yang tetap
tidak dapat dimuat dalam batas ditolak lokal dengan
`NEUROAPI_REQUEST_LIMIT_EXCEEDED`, tanpa request provider baru.

## Respons diterima dan outcome tidak pasti

Respons HTTP 422 yang benar-benar diterima dicatat sebagai
`api_requests.state=REJECTED_REQUEST_VALIDATION`, dengan alasan `HTTP_422`;
job analisis menjadi `REQUEST_REJECTED`. Kandidat tetap `REJECTED / HTTP_422`
tanpa plan atau intent order. Status ini bukan `COMPLETE` dan tidak menciptakan
hasil model.

Kandidat tersebut boleh meminta coin berbeda melalui putaran pengganti yang
sama dengan HOLD dan penolakan RR. Batas gabungan tetap tiga putaran, sesuai
slot dan budget. Pada saat settlement SOL sudah terpakai dua putaran, sehingga
tersisa satu putaran pengganti terakhir; pembacaan 11:19 UTC membuktikan putaran
itu kemudian dijalankan. Request SOL tidak diputar ulang;
operation, body hash, jumlah percobaan, dan alasan kegagalan tetap menjadi
bukti audit. Billing outcome tetap `UNKNOWN`: respons 422 tidak membuktikan
apakah provider menagih operasi itu.

Timeout atau kegagalan jaringan tanpa respons yang dapat dibuktikan tetap
`NEEDS_REVIEW` dan memblokir pekerjaan. Status terminal 422 tidak menjadi
alasan untuk menghapus request, mereset counter, mengirim ulang operasi yang
outcome-nya belum pasti, atau membuka order Binance.

## Helper untuk satu kasus SOL

[`settle_sol_request_422.py`](settle_sol_request_422.py) hanya menangani
operation SOL pada cycle tersebut. Mode default adalah inspeksi; `--apply`
membuat backup privat dan memperbarui tiga state secara atomik: request ke
`REJECTED_REQUEST_VALIDATION`, job ke `REQUEST_REJECTED`, dan cycle ke
`ACTIVE`. Candidate SOL, data cycle, replacement counter, hash request,
attempt count, dan seluruh tabel finansial dipertahankan.

Helper memeriksa UID 10001, lock dan journal privat, schema, kasus SOL tepat,
tidak adanya order SOL, tidak adanya unresolved request lain, serta bukti ETH
dan receipt existing. Versi lanjutan juga menerima closure SL ETH yang telah
dicatat dan cocok tepat dengan bukti exit di atas. Dua replacement yang sudah
terpakai tetap dipertahankan. Perubahan kasus membuatnya menolak, bukan menebak.
Ia tidak memanggil NeuroAPI atau Binance. Inspeksi mencetak `INSPECTION_ONLY`;
apply yang terverifikasi mencetak `SOL_HTTP_422_SETTLED`, lokasi backup, dan
`billing_outcome:UNKNOWN`. Kasus yang sudah dicatat menghasilkan
`ALREADY_SETTLED`; jangan mengubah pin atau guard untuk menangani kasus lain.

## Pencatatan closure ETH dan bukti cancel pasangan

[Helper `record_eth_closed.py`](record_eth_closed.py) menyiapkan pencatatan
closure khusus ETH tersebut. Mode default adalah inspeksi; apply memegang
lock, membuat backup privat, membuktikan kembali entry, SL, exit, pembatalan
TP, dan tidak adanya exposure dengan GET Binance saja. Ia memakai recorder
observasi coordinator produksi untuk menyimpan `CLOSED`, tanpa mengirim,
membatalkan, atau menutup order baru. Receipt fill pertama tetap satu dan
payload intent historis tidak diubah. Keberhasilan pencatatan harus dibuktikan
oleh `ETH_CLOSED_JOURNAL_VERIFIED`; marker itu telah diterima dari VPS pada
11:14 UTC, dengan backup
`/data/trading/maintenance/eth-closed-7i09d4gw/ledger.before.sqlite3`.

`BINANCE_ORDER_EXIT_RACE` berasal dari pembuktian keadaan sesudah cancel.
Audit belakangan membuktikan pasangan sudah `CANCELED`, tetapi tidak membuktikan
bahwa lag cancel adalah penyebab historis satu-satunya. Perubahan SDK yang
telah terpasang membatasi `_cancel_algo` pada satu DELETE yang diterima,
kemudian maksimal tiga GET untuk membuktikan hasil. Hanya status `NEW` dengan
`actualOrderId` kosong yang boleh menunggu 0,5 detik lalu 1 detik, tetap dalam
budget callback. Tidak ada DELETE ulang atau POST tambahan. Error, pemicu
yang sudah berjalan, atau keadaan ambigu tetap memerlukan review.

## Pemasangan VPS

Paket lanjutan [`apply_neuroapi_422_fix.sh`](apply_neuroapi_422_fix.sh) telah
dijalankan melalui Termius setelah pencatatan GET-only closure ETH.
Paket memuat source yang diperlukan tanpa mengambil file dari GitHub.
Script terikat pada source awal dan kasus tersebut untuk satu pemasangan;
VPS yang sudah mendapat versi target tidak perlu menjalankannya ulang.

Installer tidak memerlukan flag `--apply`; flag tersebut milik helper
pencatatan. Script terikat pada source awal dan kasus SOL yang dijelaskan di
atas, bukan updater umum. Jangan mengubah fingerprint untuk memasangnya pada
versi atau kasus lain. Observasi coordinator terbaru sesudah settlement sudah
terverifikasi.

Paket membandingkan source host/container, mencadangkan source dan image lama,
memperbarui `worker/neuroapi.py`, `worker/robot.py`, `worker/diagnostics.py`,
serta menambahkan `worker/neuroapi_request.py`. Perubahan terarah SDK hanya
memperbaiki pembuktian cancel pasangan; entry, konfigurasi/kredensial privat,
harga dan pemicu intent lama tetap dipertahankan.
Image baru dibangun dan diperiksa sebelum restart; volume, secret, settings
ON/OFF, journal, dan receipt tetap dipakai. Helper inspeksi/apply dijalankan
dengan batas kasus yang sama, tanpa menandai request sebagai sukses. Inspeksi
berjalan sebelum patch dan sekali lagi sebelum restart; apply baru dilakukan
setelah worker baru sehat dan source container cocok.

Kegagalan patch, build, pemeriksaan image, atau startup memulihkan source dan
tag image lama yang telah dicadangkan. Sesudah worker baru sehat, kegagalan
pencatatan atau diagnostic tidak otomatis mengganti kembali image. Script
tidak memulihkan database ke backup sehingga transaksi atau receipt yang
terjadi sesudah backup tidak dihapus.

Hasil VPS telah membuktikan `SOL_HTTP_422_SETTLED` dan
`NEUROAPI_422_RECOVERY_VERIFIED`: request SOL tetap satu percobaan `HTTP_422`
tanpa output, menjadi `REJECTED_REQUEST_VALIDATION`; job menjadi
`REQUEST_REJECTED`; cycle kembali `ACTIVE`. Replacement counter tetap dua,
dan tidak ada order SOL dari operasi tersebut. Billing tetap `UNKNOWN`.

| Bukti pemulihan | Nilai terverifikasi |
| --- | --- |
| Backup source | `/root/harun-neuroapi-422.1n3GeX` |
| Backup image | `harun-office-worker:before-neuroapi-422-20261007T111434Z` |
| Backup journal SOL | `/data/trading/maintenance/sol-422-o7a1ck15/ledger.before.sqlite3` |
| SDK awal | `df6705bbdda9f2a34374fde186244d400e2fc84ecb8f27ce05ad15f35a9bbfc4` |
| SDK sesudah patch | `4cd0be4f03659ef6eb75193f261465fb33519b0602d51a4a9c7f3865d0153049` |
| Receipt entry ETH | Satu, tetap mengikuti fill pertama |
| Replacement yang telah terpakai saat settlement | Dua dari batas tiga |

Sesudah pemulihan dan startup sehat, worker yang sudah ON dapat melanjutkan
putaran terakhir secara normal untuk coin berbeda. Script tidak menjalankan
tick manual, mengulang SOL, mereset cycle, atau memaksa order. Pembacaan terbaru
telah membuktikan putaran tersebut berjalan dengan hasil berikut:

| Cycle | State | Target | Replacement terpakai | Queue |
| --- | --- | --- | --- | --- |
| Epoch 0 | `COMPLETE` | 2 | 0 | 0 |
| Epoch 1 | `COMPLETE` | 1 | 3 | 0 |

Kandidat terakhir epoch 1 adalah ETHUSDT baru, `REJECTED /
NET_RISK_REWARD_NOT_TARGET_2`; ia berbeda dari intent ETH epoch 0 yang sudah
`CLOSED`. Tidak ada order pengganti diterima. Satu fill bot hari itu tetap
mengurangi kuota harian dua entry, sehingga tersedia satu slot meskipun posisi
aktif nol. `INSUFFICIENT_ACTIONABLE_SETUPS / ROBOT_CYCLE_COMPLETE` adalah hasil
cycle yang tidak memperoleh setup valid setelah tiga putaran, tanpa failure
teknis tersisa pada pemeriksaan tersebut. Laporan dan riwayat akun tetap
`PARTIAL`; verifikasi ini tidak menjadikannya lengkap.
