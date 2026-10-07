# HARUN TRADING OFFICE

Dashboard kantor 3D dan worker privat Binance USD-M Futures:

`ON → account/open orders → kapasitas → screening → analisis 15m/1h → sizing/TP → entry → SL/TP → rekonsiliasi`

Worker berjalan di VPS 24/7; browser dapat ditutup. Menu menyediakan Robot
Trading ON/OFF dengan pengaturan risiko dan koneksi worker, serta Kalender PNL.
Key Binance/NeuroAPI tetap di VPS. Dashboard
memakai token worker dan data akun yang dibaca dari Binance.

Kalender menampilkan riwayat posisi USD-M Futures sejak 1 Oktober 2026 dalam
WIB, dikelompokkan pada tanggal posisi selesai ditutup. Hari profit berwarna
hijau dan hari rugi merah; memilih tanggal menampilkan posisi LONG/SHORT,
harga masuk/keluar rata-rata, volume, waktu buka/tutup, durasi, serta komponen
PNL yang dapat dibuktikan. Worker merekonstruksi siklus posisi dari transaksi
Binance karena API publik tidak menyediakan riwayat posisi seperti layar app.
Komisi, funding, dan biaya asuransi hanya dihitung bila atribusinya dapat
dipastikan. Data parsial ditandai `*`, jumlah yang diketahui dipisahkan dari
total yang belum lengkap, dan hari tanpa bukti ditampilkan `—`. Kalender tidak
menebak ROI, leverage historis, atau angka nol untuk data yang belum tersedia.

Gateway publik bawaan merupakan stub dengan
`EXECUTION_BLOCKED / BINANCE_ORDER_GATEWAY_NOT_CONNECTED`. VPS existing
memakai adapter produksi lokal pada `worker/order_gateway.py`. Status
`CONFIGURED` hanya membuktikan konfigurasi lokal; penerimaan entry, fill,
TP/SL, dan exit memerlukan bukti Binance. Update mempertahankan adapter privat,
financial journal, receipt, settings, secret, dan order existing.

## Aturan robot

- Maksimal dua first fill entry bot per hari WIB, UTC+7, dan dua occupancy
  bersamaan. Posisi manual/carryover serta pending entry manual dan bot memakai
  kapasitas; symbol yang sama dihitung sekali. HYPEUSDT manual-only.
- Screening meminta satu atau dua coin sesuai slot. Analisis memakai USDT
  perpetual aktif, candle Futures fresh 15m/1h, filter kontrak, dan commission
  akun per symbol. NeuroAPI mengembalikan LONG, SHORT, atau HOLD.
- HOLD dan RR di bawah 1:2 sebelum plan/intent meminta coin berbeda, maksimal
  tiga putaran pengganti. Unknown outcome tidak diulang. Polling/restart tidak
  membuka cycle berbayar tanpa batas setelah putaran habis.
- Risk target tetap editable pada menu lama: default 5 USDT, positif sampai
  100 USDT. Quantity legal terbesar mencakup taker fee entry/SL dan cadangan
  exit SL merugikan 0,5%. Leverage 75× tidak memperbesar budget risiko.
- Setup model minimal RR 1:2 dapat dipakai. Entry dan SL dipertahankan; worker
  menormalisasi TP eksekusi ke net 1:2 setelah fee dan cadangan exit TP 0,5%,
  pada tick legal terdekat. TP model asli dan bukti normalisasi disimpan.
- Entry LIMIT GTC, CROSS 75×. Setelah actual fill, SL `STOP_MARKET` dipasang
  terlebih dahulu lalu TP `TAKE_PROFIT_MARKET`, pemicu Terakhir
  (`CONTRACT_PRICE`), untuk 100% exposure bot yang masih terbuka. GET harus
  membuktikan keduanya sebelum `POSITION_PROTECTED`.

Model biaya baru adalah `FEE_SLIPPAGE_RISK_V3`. Planned loss dengan cadangan
harus memenuhi target; gap, slippage melampaui cadangan, funding, perubahan fee,
atau liquidation dapat membuat loss aktual lebih besar dari 5 USDT. Entry dan
proteksi terpisah; fill tidak atomik dengan pemasangan TP/SL.

OFF menghentikan setiap request provider dan mutation Binance baru, termasuk
entry, TP/SL, perubahan config, cancel, dan close. Request yang sudah terkirim
dapat selesai dan dicatat. Polling akun tetap tersedia. OFF tidak membatalkan
entry pending atau menutup posisi/order existing maupun manual; entry pending
masih dapat fill di Binance saat OFF.

## Journal dan update

Namespace operation tetap `robot-v9`/`analysis-v9`, agar update tidak membuat
replay riset/order. Receipt first fill, pending reservations, kuota, replacement
counter, payload dan provenance lama dipertahankan. Plan lama memakai model
biaya serta pemicu yang terikat pada buktinya; update tidak mengganti level
atau quantity order historis.

Respons provider `COMPLETE` yang cocok dapat diselesaikan dari journal setelah
restart tanpa POST baru hanya jika metadata waktu chart asli masih fresh.
Metadata hilang/stale ditolak lokal tanpa replay provider. HTTP 422 yang diterima
bersifat terminal, tanpa output model; timeout/unknown tetap memblokir.
`ORDER_OUTCOME_UNKNOWN` bukan bukti rejection Binance. Posisi nol belum
membuktikan tidak ada entry pending. Jangan menghapus claim, mereset counter,
atau memaksa submission ulang.

Revisi alur diverifikasi di VPS saat OFF sebelum publikasi GitHub. Cleanup
hanya menyangkut source/command robot historis setelah verifikasi; volume,
journal, secret, backup, order manual, dan fitur Office tetap dipertahankan.

## Panduan aktif

- [Alur dan aturan robot](deploy/ROBOT_WORKFLOW.md)
- [Operasi melalui Termius](deploy/TERMIUS_24_7.md)
- [Worker 24/7 dan status](deploy/ROBOT_24_7.md)
- [Audit source sebelum ON](deploy/AUDIT_BEFORE_ON.md)
- [Entry dan TP/SL Binance](deploy/TP_SL_AUTOMATIC.md)
- [Kontrak adapter dan model risiko](deploy/ORDER_INTEGRATION.md)
- [Batas request NeuroAPI dan unknown outcome](deploy/NEUROAPI_422_RECOVERY.md)
- [Docker, HTTPS, secret, dan migrasi](deploy/DOCKER.md)
- [Persiapan build adapter existing](deploy/EXISTING_GATEWAY_BUILD.md)

Diagnostics `python -m worker api-check`, `binance-check`, `diagnostics`, dan
`status` hanya membaca. Tidak ada command manual untuk memulai screening atau
entry. Tes memakai fixtures tanpa uang/key nyata; tes lulus tidak membuktikan
order produksi diterima Binance.
