# Satu pipeline Binance Futures

`OFF → ON → account → kapasitas → screening → analisis → validasi risiko → order gateway`

Ujung pipeline hanya `OrderGateway.submit(intent)`. Implementasi transport
Binance belum tersambung; coordinator berhenti dengan `EXECUTION_BLOCKED` dan
kode `BINANCE_ORDER_GATEWAY_NOT_CONNECTED`, sedangkan gateway menampilkan
`NOT_CONNECTED`. Baca kontrak adapter di
[ORDER_INTEGRATION.md](ORDER_INTEGRATION.md) sebelum implementasi.

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
antre tetap dianalisis selama kapasitas dan budget tersedia. Hanya hasil HOLD
meminta screening pengganti, maksimal tiga screening tambahan sesudah awal.
Dua HOLD meminta dua pengganti; satu HOLD meminta satu pengganti. Symbol manual,
technical failure, risiko/net RR yang ditolak, dan unknown outcome tidak
memberi izin mengulang screening.

## Analisis dan validasi

Market context memakai candle 1h/15m nyata, mark price, waktu server, filter
kontrak, dan taker commission akun per symbol dari signed Binance GET.
Model hanya menghasilkan side serta harga Entry/TP/SL, tanpa provider quantity.
Worker menghitung quantity legal terbesar menggunakan floor Decimal:

`net_loss_sl_per_unit = abs(Entry - SL) + Entry * taker_rate + SL * taker_rate`

`net_reward_tp_per_unit = abs(TP - Entry) - Entry * taker_rate - TP * taker_rate`

Quantity mengikuti target default net 5 USDT, step size, batas quantity, dan
min notional. Net RR harus minimal 2 sesudah fee; target tetap editable positif
sampai 100 USDT. Entry LIMIT, CROSS, dan leverage 75 tidak memperbesar budget
loss atau mengubah harga setup. Data/fee stale atau tidak tersedia menghentikan
alur sebelum gateway. Perhitungan memakai fee taker konservatif; funding,
slippage, perubahan fee, dan gap tidak dijamin oleh estimasi ini.

Pengaturan risiko positif sampai 100 USDT tetap tersedia di panel Robot Trading.
Target ditangkap sebelum analisis dan disimpan pada claim serta bukti intent.
Perubahan hanya dipakai analisis berikutnya; intent lama tidak diubah ukurannya.

Proof `analysis-v9` mengikat operation, request/output hash, level, quantity,
risk target, commission, net risk/reward, serta rules yang dipakai sizing.
Cycle memakai namespace `robot-v9`. Claim persisten dibuat sebelum request berbayar.
Polling/restart tidak membuat replay claim yang outcome-nya belum pasti.
Journal account dan riset terpisah; hasil analisis terlambat tidak mengganti
saldo/posisi yang sudah diamati lebih baru.

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
