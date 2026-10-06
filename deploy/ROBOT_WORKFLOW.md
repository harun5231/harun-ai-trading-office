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
dari calon entry. Maksimal dua posisi dan dua entry bot terkonfirmasi per hari
Asia/Bangkok:

`available_slots = max(0, min(2 - running_positions, 2 - confirmed_bot_entries_today))`

Screening meminta literal satu atau dua coin sesuai slot. Hasil harus unique
USDT perpetual aktif dan tidak sedang terekspos. Kandidat valid yang sudah
antre tetap dianalisis selama kapasitas dan budget tersedia. Kandidat HOLD atau
dikecualikan dapat memakai replacement bounded; technical failure dan unknown
outcome tidak membuat loop riset tak terbatas.

## Analisis dan validasi

Market context terbaru mencakup candle1h/15m, mark price, waktu server, dan
filter kontrak. Model menghasilkan side serta harga entry/TP/SL. Quantity
ditentukan worker menggunakan floor Decimal berdasarkan jarak entry ke SL,
step size, batas quantity, dan min notional. Target default 5 USDT, RR aktual
minimal2; leverage tidak memperbesar budget loss. Data stale atau konfigurasi
tidak valid menghentikan alur sebelum gateway.

Pengaturan risiko positif sampai 100 USDT tetap tersedia di panel Robot Trading.
Target ditangkap sebelum analisis dan disimpan pada claim serta bukti intent.
Perubahan hanya dipakai analisis berikutnya; intent lama tidak diubah ukurannya.

Proof mengikat operation, request/output hash, level, quantity, risk target,
dan rules yang dipakai sizing. Claim persisten dibuat sebelum request berbayar.
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

OFF menghentikan claim baru. Request berjalan ditunggu saat shutdown;
outcome belum pasti memerlukan rekonsiliasi dan tetap fail-closed. Entry hanya
dihitung setelah konfirmasi exchange yang diverifikasi adapter; hasil screening,
analisis, dan intent bukan bukti entry. Proteksi, partial fill, recovery, serta
rekonsiliasi dipenuhi pada satu adapter order, bukan cabang runtime tersendiri.

Migrasi pertama menyimpan backup audit privat, menghapus tabel alur lama dari
database aktif, dan mengembalikan robot ke OFF. Journal request berbayar serta
entry nyata terkonfirmasi dipertahankan. Backup tidak dimuat oleh coordinator.
