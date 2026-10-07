# Alur robot Binance USD-M Futures

`OFF → ON → account/open orders → kapasitas → screening → analisis → sizing/TP → gateway → entry → SL/TP → rekonsiliasi`

Coordinator berjalan di VPS. Browser Office dapat ditutup tanpa menghentikan
worker. Gateway bawaan repository tetap stub; adapter produksi VPS harus cocok
dengan source yang ditinjau dan bukti Binance. `CONFIGURED` adalah metadata
konfigurasi lokal, bukan bukti order diterima.

## ON dan OFF

ON mengizinkan pekerjaan ketika slot/kuota tersedia. Setiap request provider
baru dan setiap mutation Binance memeriksa ON kembali pada batas pengiriman.
OFF menghentikan screening/analisis baru, entry, perubahan margin/leverage,
pemasangan TP/SL, cancel, dan close baru, walaupun callback telah dimulai.
Request yang sudah terkirim dapat selesai dan dijournal tanpa mutation lanjutan.

GET akun dan Office tetap tersedia. OFF tidak membatalkan entry/TP/SL existing
atau menutup posisi. Pending entry masih dapat fill di Binance saat OFF;
worker tidak memasang proteksi baru selama OFF. Semua order/posisi manual
termasuk pending manual tetap dipertahankan.

## Account, slot, dan kuota

Worker menerima one-way, single-asset, dengan izin Futures valid. GET membaca
posisi serta seluruh regular/algo open orders. Symbol posisi nonzero dan
pending entry manual/bot memakai occupancy; reducing exit TP/SL tidak menjadi
entry baru. HYPEUSDT selalu manual-only. Coin dengan exposure/pending lain
juga dikecualikan dari screening dan submission.

Ada dua batas bersamaan: maksimal dua first fill entry bot per hari WIB
(UTC+7), dan maksimal dua occupancy. Gabungan symbol posisi, pending entry
Binance, dan reservasi intent bot dihitung sekali untuk concurrency. Pending
manual memakai slot concurrency, tetapi bukan receipt first fill bot.

Kuota harian menyisihkan receipt hari ini serta entry bot belum fill yang masih
mungkin fill. Partial fill dan tambahan fill pada entry sama tetap satu
receipt pada hari WIB dari first fill terverifikasi. Receipt, counter, dan
reservasi persisten setelah restart. Posisi closed membebaskan occupancy tetapi
tidak mengembalikan kuota entry hari itu.

Pada 00:00 WIB, kuota memakai receipt hari baru. Posisi carryover dan pending
entry tetap dicadangkan; pergantian hari tidak close/cancel order.

## Screening, analisis, dan pengganti

Screening meminta satu atau dua coin sesuai kapasitas bebas. Symbol harus
unique, aktif, Binance USD-M USDT perpetual. Analisis memakai candle Futures
fresh 15m/1h, contract filters, harga pasar, commission akun per symbol, dan
target risiko tersimpan. Data kedaluwarsa/tidak tersedia memblokir order.

NeuroAPI mengembalikan LONG, SHORT, atau HOLD. HOLD memerlukan level numerik
null dan tidak membuat entry. LONG/SHORT memerlukan harga positif, urutan
entry/TP/SL benar, declared RR minimal 2, serta jarak TP model minimal dua
kali jarak SL. Worker memeriksa level, bukan mempercayai angka RR model saja.

HOLD dan penolakan RR sebelum plan/intent dapat meminta coin berbeda, maksimum
tiga screening pengganti setelah awal. RR di bawah dua memakai kode seperti
`RISK_REWARD_BELOW_2` atau `NET_RISK_REWARD_BELOW_2`. Penolakan RR historis yang
eligible tetap dikenali. Setup baru dengan RR di atas dua dinormalisasi ke
net 1:2, bukan ditolak hanya karena TP model terlalu jauh.

Pengganti berasal dari hasil putaran terakhir, sesuai slot/budget, dan
mengecualikan coin yang sudah dilihat, exposed/pending, serta HYPEUSDT. Respons
HTTP 422 yang terbukti diterima dapat mengikuti batas sama hanya dengan request dan
job terminal, satu percobaan, tanpa output, plan, atau intent. Unknown outcome,
intent existing, dan kegagalan teknis lain tidak memberi izin replay/reset.
Setelah tiga putaran habis, cycle selesai; polling tidak membuka riset berbayar
ulang tanpa batas pada hari/epoch yang sama.

## Risiko V3 dan TP net 1:2

Risk target tetap editable pada menu Robot Trading existing: default 5 USDT,
nilai positif sampai 100 USDT. Target ditangkap pada claim analisis; perubahan
menu berlaku untuk analisis baru. Intent existing memakai level, target,
quantity, dan cost model yang telah terikat pada provenance-nya.

Plan baru memakai `FEE_SLIPPAGE_RISK_V3`: planned loss mencakup jarak entry ke
SL, taker fee entry/SL, dan cadangan adverse exit sebesar 0,5% dari trigger SL.
Commission berasal dari GET akun per symbol yang masih fresh. LIMIT entry
tidak diberi cadangan slippage entry. Quantity legal terbesar tetap memenuhi
target, lot step/min/max, minimum notional, aturan harga, dan margin Binance.

Entry dan SL model dipertahankan. Setup dengan RR model minimal 2 mendapat TP
eksekusi ke net reward dua kali planned net loss, setelah fee dan cadangan
adverse exit TP 0,5%. LONG memakai tick legal pertama di atas target; SHORT di
bawahnya. TP model asli, TP eksekusi, fee, cadangan, dan tick disimpan dalam
bukti normalisasi. TP eksekusi harus legal dan profitable.

Decimal/Fraction menjaga hitungan tepat. Leverage 75× menentukan margin, bukan
pengali risk budget. Cadangan 0,5% adalah asumsi sizing, bukan jaminan slippage
Binance. Gap/slippage melebihi cadangan, funding, perubahan fee, dan liquidation
dapat membuat loss aktual melampaui target, termasuk 5 USDT.

## Entry dan proteksi

Gateway memverifikasi akun, slot, ownership, margin, CROSS/75×, filters, fee,
dan intent fresh sebelum entry. LONG memakai BUY LIMIT GTC; SHORT SELL LIMIT
GTC, harga/quantity intent dan client ID deterministik.

ACK entry yang belum fill adalah `ENTRY_PENDING`. Saat ON, actual fills dan
ownership dibuktikan; fill positif, termasuk partial fill, mendapat SL
`STOP_MARKET` terlebih dahulu lalu TP `TAKE_PROFIT_MARKET` melalui Algo API.
Plan baru memakai pemicu Terakhir, `CONTRACT_PRICE`; intent historis memakai
pemicu aslinya.

Sisi exit berlawanan, `positionSide=BOTH`, `reduceOnly=true`,
`closePosition=false`, quantity 100% exposure entry bot yang masih terbuka.
GET harus membuktikan dua proteksi aktif sebelum `POSITION_PROTECTED`.
Tambahan fill dan exit direkonsiliasi; cleanup pasangan saat ON hanya mengenai
order milik intent bot. Order manual tidak diubah.

Tick sekitar 45 detik dan latency Binance menambah jeda. Entry/SL/TP adalah
request terpisah; fill dan proteksi tidak atomik. OFF memblokir setiap mutation
berikutnya. Lihat [pemetaan TP/SL](TP_SL_AUTOMATIC.md) dan
[kontrak adapter](ORDER_INTEGRATION.md).

## Journal dan restart

Operation tetap `robot-v9`/`analysis-v9`; risk model V3 tidak membuka namespace
baru untuk replay. Claim, attempts, body/output hash, receipt, replacement
counter, payload, dan provenance dipertahankan. Respons `COMPLETE` yang cocok
hanya dapat diselesaikan lokal setelah crash bila metadata waktu chart asli
masih fresh. Metadata waktu fetch/claim/server dan close candle 15m/1h disimpan
sebelum claim. Metadata hilang/stale ditolak lokal dengan
`STALE_MARKET_CONTEXT`, tanpa POST baru atau replay provider.

`ORDER_OUTCOME_UNKNOWN` bukan bukti rejection Binance. Posisi nol belum
membuktikan tidak ada LIMIT pending; ACK bukan bukti fill/proteksi. Unknown
provider/order/protection memblokir sampai bukti request/order, fills, algo,
dan exposure diperiksa. Jangan menghapus claim, mereset attempts/counter,
atau memaksa submit ulang. Diagnostics hanya mengeluarkan kode aman; exception
mentah, signed query, secret, dan output provider tidak ditampilkan.
Lihat [semantik request](NEUROAPI_422_RECOVERY.md).

## Deployment dan status

Revisi dibuktikan di VPS saat OFF sebelum publikasi GitHub. Source host/image,
settings, journal, posisi, dan open orders harus sesuai audit. Cleanup source dan
command historis hanya setelah bukti akhir; financial data, secret, backup,
volume, dan order existing tidak dihapus.

GET `/health`, `/robot/status`, `/office/status` hanya membaca.
POST `/robot/settings` menyimpan ON/OFF/risk target dengan control token dan
exact Origin. `ONLINE`, `CONFIGURED`, dan tes offline tidak membuktikan order
baru diterima.

| Status | Makna |
| --- | --- |
| `ENTRY_PENDING` | Entry pending; actual fills masih direkonsiliasi |
| `POSITION_PROTECTED` | Exposure bot dan TP/SL aktif terverifikasi |
| `CLOSED` | Exit dan penutupan exposure telah dibuktikan |
| `INSUFFICIENT_ACTIONABLE_SETUPS` | Batas pencarian selesai tanpa setup valid |
| `NEEDS_REVIEW` | Outcome belum terbukti; pekerjaan baru diblokir |
| `EXECUTION_BLOCKED` | Adapter pengiriman belum tersedia |
