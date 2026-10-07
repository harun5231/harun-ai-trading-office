# Satu pipeline Binance Futures

`OFF → ON → account → kapasitas → screening → analisis → validasi risiko → order gateway`

Ujung pipeline hanya `OrderGateway.submit(intent)`. Kode gateway publik pada
repository menyediakan stub; tanpa adapter produksi, coordinator berhenti
dengan `EXECUTION_BLOCKED / BINANCE_ORDER_GATEWAY_NOT_CONNECTED`.
VPS pengguna mempertahankan adapter privat yang telah mengirim dan memverifikasi
entry serta proteksi ETH. Baca kontraknya di
[ORDER_INTEGRATION.md](ORDER_INTEGRATION.md).

Pemasangan VPS pada 7 Oktober 2026 pukul 10:04 UTC menghasilkan
`VPS_RR_TARGET_LAST_PRICE_VERIFIED`, source host/container cocok, dan worker
sehat. Snapshot menunjukkan robot ON, akun tersambung, satu posisi, satu
receipt entry, satu slot tersedia, dan tidak ada failure code. Bukti ini
memverifikasi pemasangan. Pemulihan closure ETH dan request SOL kemudian
terverifikasi pada 11:14 UTC. Pembacaan 11:19 UTC membuktikan request/job
pending atau needs-review kosong, ETH asli `CLOSED` tanpa failure, satu
receipt, dan posisi aktif nol. Cycle epoch 1 `COMPLETE` setelah tiga putaran
pengganti; kandidat terakhir ETHUSDT baru ditolak
`NET_RISK_REWARD_NOT_TARGET_2` sebelum order. Robot ON menunggu
`INSUFFICIENT_ACTIONABLE_SETUPS / ROBOT_CYCLE_COMPLETE`, tanpa failure robot
atau akun, dengan satu slot tersedia. Laporan tetap `PARTIAL` dan belum ada
order pengganti diterima; penerimaan `CONTRACT_PRICE` serta partial fill versi
baru belum dibuktikan.

## Account dan kapasitas

Private GET Binance memakai key VPS existing. Hanya akun ONE_WAY, single-asset,
dan konfigurasi yang lolos pemeriksaan diterima. Semua posisi nonzero mengurangi
concurrent capacity. Posisi manual tetap manual; HYPEUSDT selalu dikecualikan
dari calon entry. Ada dua batas yang dipenuhi bersamaan: maksimal dua entry bot
baru per hari WIB (UTC+7) dan maksimal dua posisi bersamaan. Posisi manual dan
posisi yang terbawa dari hari sebelumnya ikut memakai slot bersamaan.

`day_remaining = max(0, 2 - confirmed_bot_entries_today - unfilled_pending_entries)`

`concurrency_remaining = max(0, 2 - running_positions - unrepresented_intents)`

`available_slots = min(day_remaining, concurrency_remaining)`

`unfilled_pending_entries` mencakup seluruh `ENTRY_PENDING`, termasuk yang dibuat
kemarin karena fill pertama masih dapat terjadi hari ini. `unrepresented_intents`
adalah pending entry atau exposure bot terisi yang belum tercermin pada posisi
akun terbaru; exposure yang sudah tercermin tidak dihitung dua kali.
Counter harian berasal dari receipt fill pertama Binance yang terverifikasi,
bukan screening, analisis, submission, atau ACK. Partial fill dan tambahan fill
pada entry yang sama tetap satu entry, mengikuti hari WIB dari fill pertamanya.
Receipt dan reservasi tetap tersimpan setelah restart.

Pada pukul 00:00 WIB, kuota memakai receipt untuk hari yang baru. Posisi lama
tidak ditutup, dan pending entry lama masih mencadangkan kuota jika belum fill.
Jika satu posisi kemarin masih aktif, paling banyak satu slot bersamaan tersedia
saat itu. Jika dua masih aktif, tidak ada slot baru sampai suatu posisi selesai.
Posisi yang selesai membebaskan slot bersamaan; receipt entry hari itu tetap
mengurangi kuota harian.

Screening meminta literal satu atau dua coin sesuai slot. Hasil harus unique
USDT perpetual aktif dan tidak sedang terekspos. Kandidat valid yang sudah
antre tetap dianalisis selama kapasitas dan budget tersedia. Pengganti boleh
berasal dari HOLD atau penolakan lokal `REJECTED` dengan kode
`NET_RISK_REWARD_BELOW_2` atau `NET_RISK_REWARD_NOT_TARGET_2`,
hanya jika kandidat belum mempunyai plan atau intent order. Penolakan tetap
`REJECTED`; hasil model dan alasan validasi tidak diubah menjadi HOLD.

Keduanya berbagi maksimal tiga screening tambahan sesudah awal. Hanya kandidat
eligible dari putaran terakhir yang dihitung: satu meminta satu pengganti, dua
meminta dua, dibatasi slot dan budget yang tersedia. Putaran, request berbayar,
dan bukti lama tidak direset atau dikirim ulang. Symbol manual, kegagalan
teknis lain, penolakan yang sudah mempunyai intent, dan unknown outcome tetap
menghentikan atau menghabiskan budget sesuai aturan sebelumnya.

Screening pengganti menambahkan daftar coin yang telah dilihat, exposed/pending,
dan HYPEUSDT melalui `message_history` existing, agar provider memilih coin lain.
Prompt inti dan schema tetap sama; screening awal tetap tanpa history.

## Analisis dan validasi

Market context memakai candle 1h/15m nyata, mark price, waktu server, filter
kontrak, dan taker commission akun per symbol dari signed Binance GET.
Model hanya menghasilkan side serta harga Entry/TP/SL, tanpa provider quantity.
Worker menghitung quantity legal terbesar menggunakan floor Decimal:

`net_loss_sl_per_unit = abs(Entry - SL) + Entry * taker_rate + SL * taker_rate`

`net_reward_tp_per_unit = abs(TP - Entry) - Entry * taker_rate - TP * taker_rate`

Quantity mengikuti target default net 5 USDT, step size, batas quantity, dan
min notional. Target risiko tetap editable positif sampai 100 USDT. Setup baru
mengikat `reward_risk_policy=NET_1_TO_2_NEAREST_TICK`: TP dari model harus sama
dengan tick harga pertama yang mencapai net RR 1:2 sesudah fee. Worker menghitung
harga pembanding, membulatkan ke atas untuk LONG atau ke bawah untuk SHORT,
tanpa mengganti TP model. Net RR di bawah 2 ditolak dengan
`NET_RISK_REWARD_BELOW_2`; TP yang melampaui minimum tetapi berbeda dari target
tick ditolak dengan `NET_RISK_REWARD_NOT_TARGET_2`. Kedua penolakan sebelum
plan/intent dapat meminta coin pengganti seperti HOLD melalui batas tiga putaran
yang sama. Entry LIMIT, CROSS, dan leverage 75 tidak memperbesar budget
loss atau mengubah harga setup. Data/fee stale atau tidak tersedia menghentikan
alur sebelum gateway. Perhitungan memakai fee taker konservatif; funding,
slippage, perubahan fee, dan gap tidak dijamin oleh estimasi ini.

Pengaturan risiko positif sampai 100 USDT tetap tersedia di panel Robot Trading.
Target ditangkap sebelum analisis dan disimpan pada claim serta bukti intent.
Perubahan hanya dipakai analisis berikutnya; intent lama tidak diubah ukurannya.

Plan lama tanpa kebijakan target RR tetap memakai validasi minimum net RR 2.
Karena itu setup ETH existing dengan entry 2.610, SL 2.585, dan TP 2.730 tidak
diubah atau ditolak ulang oleh pembaruan. Setup baru memakai kebijakan target
sebelum bukti distamp; tick harga ikut terikat pada rules sizing dan intent.

Plan baru menyimpan `protection_working_type=CONTRACT_PRICE` sebelum bukti
distamp. Ini mengikuti pemicu "Terakhir" pada TP/SL Binance. Intent lama tanpa
field tersebut tetap menghasilkan `MARK_PRICE`, sehingga hash bukti dan pemicu
proteksi existing tidak diubah oleh pembaruan.

Proof `analysis-v9` mengikat operation, request/output hash, level, quantity,
risk target, commission, net risk/reward, serta rules yang dipakai sizing.
Cycle memakai namespace `robot-v9`. Claim persisten dibuat sebelum request berbayar.
Polling/restart tidak membuat replay claim yang outcome-nya belum pasti.
Journal account dan riset terpisah; hasil analisis terlambat tidak mengganti
saldo/posisi yang sudah diamati lebih baru.

## Entry terisi dan TP/SL

Entry LIMIT yang diterima tetapi belum fill tetap `ENTRY_PENDING` dan memakai
slot. Saat ON, worker memanggil rekonsiliasi pada tick sekitar 45 detik. Adapter
memverifikasi order, fill, serta kepemilikan exposure sebelum memasang SL
`STOP_MARKET` dan TP `TAKE_PROFIT_MARKET` melalui Algo API Binance.

Pemicu memakai `workingType` yang terikat pada intent: `CONTRACT_PRICE` untuk
plan baru atau `MARK_PRICE` untuk plan lama. Harga SL/TP berasal dari setup;
quantity mencakup 100% fill yang terverifikasi milik entry tersebut, termasuk
partial fill. Proteksi memakai `reduceOnly=true`, `closePosition=false`, dan
`positionSide=BOTH` agar tidak membuka posisi baru atau mengambil posisi manual.
Tambahan fill harus direkonsiliasi agar ukuran proteksi mengikuti exposure.

SL dikonfirmasi terlebih dahulu, kemudian TP. Status `POSITION_PROTECTED`
memerlukan bukti GET untuk keduanya; ACK bukan bukti proteksi lengkap. Ketiga
order dikirim terpisah, sehingga jeda polling dan waktu request tetap ada.
Kegagalan atau outcome tidak pasti menjadi `NEEDS_REVIEW` dan menghentikan
rekonsiliasi otomatis sampai dipulihkan berdasarkan bukti exchange. Pembaruan
kode tidak boleh menghapus status tersebut atau mengirim ulang entry.

Lihat [TP_SL_AUTOMATIC.md](TP_SL_AUTOMATIC.md) untuk pemetaan ke layar Binance
dan batas pemulihan posisi ETH yang sudah terisi.

## API dan lifecycle

| Endpoint | Fungsi |
| --- | --- |
| GET `/health` | Health thread/heartbeat worker. |
| GET `/robot/status` | Status coordinator dan konfigurasi ON/OFF. |
| GET `/office/status` | Data account, laporan, aktivitas, dan riwayat Binance. |
| POST `/robot/settings` | ON/OFF dan risiko dengan control token dan exact Origin. |

Read token hanya membaca; control token dapat mengubah pengaturan. Proxy tidak
meneruskan endpoint lain. Dashboard tidak menerima provider key.

Body pengaturan adalah subset tidak kosong dari `robot_on` (boolean) dan
`risk_target_usdt` (string Decimal positif sampai 100). Kedua nilai divalidasi
sebelum pembaruan atomik. Pemeriksaan koneksi CLI `api-check` dan `binance-check`
hanya menjalankan GET provider/akun, tanpa analisis atau order.

OFF menghentikan riset, claim/submission baru, dan pemanggilan rekonsiliasi gateway
berikutnya. OFF tidak menutup posisi atau membatalkan order entry maupun SL/TP.
Pemanggilan eksternal yang sudah dikirim tidak dapat ditarik kembali dan masih
dapat menyelesaikan penyimpanan hasil. Request berjalan ditunggu saat shutdown;
outcome belum pasti tetap fail-closed sampai ditinjau dan direkonsiliasi.
Pembacaan saldo/posisi/riwayat Office tetap berjalan sebagai monitoring read-only,
sementara semua karyawan AI tidak menunjukkan aktivitas kerja saat OFF.
Entry hanya dihitung setelah konfirmasi exchange yang diverifikasi adapter;
hasil screening, analisis, dan intent bukan bukti entry. Proteksi, partial fill,
recovery, serta rekonsiliasi dipenuhi pada satu adapter order.

Migrasi pertama menyimpan backup audit privat, menghapus tabel alur lama dari
database aktif, dan mengembalikan robot ke OFF. Journal request berbayar serta
entry nyata terkonfirmasi dipertahankan. Backup tidak dimuat oleh coordinator.
Upgrade namespace fee-inclusive memakai schema version 2; hanya constraint
`UNIQUE(day,entry_epoch)` yang dihapus. Cycle lama beserta seluruh journal,
candidate, intent, settings, receipt, dan exposure tetap tersimpan. Candidate
risiko harga tanpa fee lama tidak dipromosikan; unknown order tetap memblokir.
