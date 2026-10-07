# Kalender PNL Futures

Kalender dimulai pada **1 Oktober 2026, pukul 00.00 WIB**
(`2026-09-30T17:00:00Z`). Hari kalender mengikuti `Asia/Jakarta`, dan posisi
dimasukkan pada tanggal penutupan terakhir, bukan tanggal entry atau tiap fill.
Dashboard membaca `pnl_calendar` dari endpoint worker `/office/status` yang
memerlukan read token. Tidak ada kredensial Binance di halaman atau responsnya.

## Sumber dan perhitungan

API REST resmi USD-M menyediakan `GET /fapi/v1/income`,
`GET /fapi/v1/userTrades`, dan `GET /fapi/v3/positionRisk`. API publik tersebut
tidak menyediakan record Riwayat Posisi aplikasi Binance. Worker merekonstruksi
siklus posisi dari fill pembukaan sampai saldo quantity kembali nol. Kalender
menandai data sebagai `CLOSED_POSITIONS_FROM_FILLS`; tidak memakai endpoint
internal aplikasi, harga spot, simulasi, atau hasil analisis robot.

PNL posisi yang lengkap adalah realized PNL dikurangi seluruh commission entry
dan exit, ditambah funding dan insurance clearance yang dapat dihubungkan
secara pasti dengan satu siklus posisi. Fee rebate bertanda negatif menambah
PNL. Pada fill reversal, realized PNL masuk seluruhnya ke bagian penutupan,
sedangkan commission dibagi menurut quantity bagian penutupan/pembukaan. Untuk
pecahan berulang, bagian fee penutupan dibulatkan menuju nol dengan minimal
16 tempat desimal dan bagian pembukaan mendapat sisa persis, sehingga jumlah
fee tetap sama dengan catatan exchange. Harga rata-rata ditampilkan dengan
hingga 16 tempat desimal tanpa mengubah komponen PNL dari fill.
Penutupan sebagian belum dihitung sebagai posisi selesai sampai quantity nol.

Contoh ETH dari bukti exchange: `-6.18839000 - 0.09448200 - 0.23311080 =
-6.51598280 USDT`; tampilan dua desimal adalah **-6,52 USDT**. Satu fill tidak
dihitung sebagai satu posisi. Historical leverage, margin mode dan ROI tidak
diisi dari konfigurasi saat ini karena API fill tidak membuktikannya.

Funding/insurance dengan attribution ambigu, commission non-USDT, pembukaan
sebelum jendela yang berhasil dibaca, atau coverage history yang belum lengkap
membuat `pnl_usdt` bernilai `null`. `known_pnl_usdt` menyimpan subtotal komponen
yang terbukti dan ditampilkan dengan penanda belum lengkap. Penyesuaian income
yang tidak dapat dipasangkan, termasuk posting setelah posisi ditutup, tidak
dianggap sebagai biaya nol dan tidak dialokasikan sembarang. Income agregat
funding/insurance kalender ditampilkan terpisah jika coverage income lengkap.

## Coverage dan cache

`worker/pnl_calendar.py` menyimpan cache khusus `pnl-history-v1.json` dan lock
`pnl-history.lock` dalam direktori private `/data/trading` (direktori `0700`, file
`0600`, owner worker). Cache diikat ke hash internal API key; perubahan key
memulai scope baru agar data dua akun tidak tercampur. Hash tersebut tidak
dikirim ke dashboard. Pembaruan file atomic; file symlink, hardlink, permission
terbuka, JSON rusak, atau ukuran di atas batas tidak dibaca atau diganti diam-diam.

Cache tidak menambah tabel/schema ledger dan tidak mengubah order intents,
receipt, daily quota, paid request, risk atau pilihan ON/OFF. Reader berjalan
saat robot OFF dan hanya memiliki operasi GET. Service memakai satu history
GET per refresh dengan budget 10 detik; income dan trade bergantian serta
backfill lama dan tail baru mendapat giliran. Pagination/resume tersimpan agar
restart tidak mengulang seluruh sejarah. Pembacaan posisi sebelum dan sesudah
history yang berubah, termasuk zero-position update setelah cutoff, mencegah
anchor replay baru; fakta cache sebelumnya tetap dapat ditampilkan sebagai
data terbatas. Snapshot REST bukan transaksi exchange yang atomik.

Window trade maksimal tujuh hari dan page maksimal 1.000 fill. Page penuh
dibagi menjadi dua window waktu yang tidak bertumpuk; tidak melewatkan fill
dengan timestamp sama. Lebih dari satu page pada timestamp yang sama tetap
ditandai belum terbukti. Pagination income menolak page yang mengulang row
sebelumnya, termasuk page pendek yang berisi subset page lama. Batas cache
100.000 row/32 MB dan maksimal 100 simbol mencegah pembacaan tanpa batas.

Bootstrap membuka tujuh hari sebelum awal kalender. Carry-in yang dibuka
lebih awal tetap ditandai tidak lengkap. Dokumentasi resmi menetapkan retention
income/fill tiga bulan; fakta yang sudah tersimpan bertahan di cache, sedangkan
periode yang sudah kedaluwarsa sebelum sempat dibaca tetap unknown.

Discovery simbol berasal dari income account-wide dan exposure saat ini. Fill
zero fee/zero realized dapat tidak muncul di discovery income; karena itu
calendar global/day `complete=false` dan nilai account-wide `pnl_usdt` hari
bernilai `null`. Subtotal posisi yang diketahui tetap terlihat. Hari tanpa
posisi yang terbukti menampilkan tanda kosong, bukan keuntungan nol palsu.

## Kontrak respons

`pnl_calendar` berisi `status`, `kind`, `checked_at`, `start_date`, `end_date`,
`timezone`, `complete`, `incomplete_reasons`, `days` dan `positions`.
Setiap hari mempunyai `date`, `known_pnl_usdt`, `pnl_usdt`, `closed_positions`
dan `complete`. Setiap posisi mempunyai waktu buka/tutup UTC, `close_date` WIB,
arah, quantity tertutup, harga rata-rata entry/exit, komponen realized/commission/
funding/insurance, `pnl_usdt`, `known_pnl_usdt`, serta alasan data belum lengkap.
Jumlah uang tetap string decimal sampai proses tampilan; tidak memakai float
untuk penjumlahan keuangan.

Sumber resmi:

- [Account Trade List](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#account-trade-list).
- [Get Income History](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#get-income-history).
- [Penjelasan Riwayat Posisi Binance](https://www.binance.com/en/support/faq/detail/7a2887232b144523b3ba9bde5f8c641d).
- [Perhitungan PNL posisi Binance](https://www.binance.com/en/support/faq/detail/3a55a23768cb416fb404f06ffedde4b2).
