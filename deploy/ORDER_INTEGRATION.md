# Kontrak adapter Binance USD-M Futures

Runtime hanya mengirim order melalui `OrderGateway.submit(intent)` dan
`reconcile(intent)`. Gateway bawaan repository tetap stub; template deploy
bukan jalur eksekusi kedua. Adapter produksi VPS menggunakan class yang sama,
secret privat yang sudah ada, dan journal coordinator yang sama.

`CONFIGURED` berarti secret/configuration lokal tersedia. Source match, health,
ACK, tes `/order/test`, atau chart bukan bukti order terisi/terlindungi. Bukti
produksi harus berasal dari GET Binance untuk ID, field, fills, dan exposure.

## Intent, provenance, dan plan lama

`build_intent()` mengikat operation, client ID deterministik, symbol/side,
entry LIMIT GTC, SL/TP, quantity, margin CROSS, leverage 75, target risiko,
fee evidence, cost evidence, dan hash analisis. Operation tetap
`robot-v9`/`analysis-v9`; V3 bukan alasan memakai ID baru untuk replay.

Plan baru menyimpan `risk_model=FEE_SLIPPAGE_RISK_V3` dan
`reward_risk_policy=NET_1_TO_2_NORMALIZED_WITH_EXIT_RESERVE`, bersama TP model
asli, TP eksekusi, tick, dan cadangan. Worker/provenance/gateway memverifikasi
perhitungan itu kembali. Plan/intent lama tetap memakai model, level, trigger,
quantity, dan hash aslinya. Existing order tidak diubah otomatis menjadi V3.

LONG/SHORT model memerlukan declared RR dan jarak harga minimal 1:2. Entry dan
SL dipertahankan; TP eksekusi dinormalisasi ke net 1:2. RR model lebih besar
dari dua tidak ditolak hanya karena TP terlalu jauh. HOLD dan penolakan
RR di bawah dua sebelum plan/intent boleh mendapat pengganti sesuai slot,
hanya putaran terakhir dan maksimum tiga putaran gabungan.

## Model risiko dan size coin

Target editable pada menu existing, default 5 USDT, positif sampai 100 USDT.
Target ditangkap pada claim analisis, sehingga perubahan settings tidak
mengubah intent yang sudah dibuat. `quantity` memakai unit base asset.

Untuk entry E, trigger SL S, taker commission f, dan exit reserve r=0,005:

```text
SL adverse execution = S × (1-r) untuk LONG, S × (1+r) untuk SHORT
L = abs(E-S) + S×r + f×(E + SL adverse execution)
q = quantity legal terbesar, dibulatkan turun ke stepSize, sehingga q×L <= target
LONG TP ideal = (E×(1+f) + 2×L) / ((1-r)×(1-f))
SHORT TP ideal = (E×(1-f) - 2×L) / ((1+r)×(1+f))
```

LONG TP dibulatkan ke tick legal pertama di atas target; SHORT di bawahnya.
TP adverse execution memakai r yang sama. Net reward mengurangi taker fee
entry/TP dan cadangan TP; planned risk memasukkan taker fee entry/SL serta
cadangan SL. LIMIT entry memakai entry reserve nol. Fee exit dihitung pada
harga adverse execution yang diasumsikan, bukan fee rate tebakan model.

Signed GET `/fapi/v1/commissionRate` menyediakan commission per symbol.
Fee/filter evidence harus fresh dan cocok. Decimal/Fraction menjaga hitungan
tepat; min/max quantity, step, minimum notional, tick/range harga, margin, dan
leverage bracket harus lolos. Tidak ada quantity fallback ketika fee atau
rules tidak tersedia. Leverage 75× tidak mengalikan risk target.

Cadangan 0,5% adalah asumsi sizing. Funding, gap/slippage melampaui cadangan,
perubahan fee, dan liquidation dapat membuat loss aktual melampaui target.
Tidak ada jaminan loss aktual maksimal 5 USDT dari STOP_MARKET.

## Pemeriksaan sebelum entry

1. GET akun/config, posisi, seluruh regular/algo open entries. One-way,
   single-asset, izin trade valid, margin cukup, tanpa exposure/order lain
   pada symbol bot. HYPEUSDT tetap manual-only.
2. Hitung slot dari posisi manual/carryover, pending entry manual/bot, dan
   reservasi intent. Gabungan symbol dihitung sekali. Maksimal dua occupancy
   serta dua first fill entry bot per hari WIB; pending bot mencadangkan kuota.
3. GET `symbolConfig` dan `leverageBracket`. Bila perlu dan masih ON, POST
   `marginType=CROSSED` dan `leverage=75` hanya untuk symbol bot yang bebas
   exposure lain. Baca ulang config/bracket sebelum entry.
4. Verifikasi provenance, fee, rules, TP normalisasi dan risk target. Intent
   stale/exposed tidak dikirim. Payload yang dibekukan tidak diubah ukurannya.

Tidak ada perubahan global position mode atau multi-assets margin yang dapat
memengaruhi posisi manual. Jika akun tidak sesuai, hentikan submission.

## Pemetaan field Binance

| Operasi | Endpoint / field |
| --- | --- |
| CROSS | POST `/fapi/v1/marginType`, `marginType=CROSSED` |
| Leverage | POST `/fapi/v1/leverage`, `leverage=75` |
| Entry | POST `/fapi/v1/order`, `type=LIMIT`, `timeInForce=GTC`, `positionSide=BOTH` |
| Entry LONG / SHORT | `side=BUY` / `SELL` |
| Entry size / price | `quantity=intent.entry.quantity`, `price=intent.entry.price` |
| Identitas entry | `newClientOrderId=intent.client_order_id` |
| SL | POST `/fapi/v1/algoOrder`, `algoType=CONDITIONAL`, `type=STOP_MARKET` |
| TP | Endpoint algo sama, `type=TAKE_PROFIT_MARKET` |
| Harga trigger | `triggerPrice=intent.protection.stop_loss` / `take_profit` |
| Pemicu baru | `workingType=CONTRACT_PRICE` sesuai Terakhir |
| LONG / SHORT exit | `side=SELL` / `BUY`, `positionSide=BOTH` |
| Exit size | Exposure terisi bot yang masih terbuka, `reduceOnly=true`, `closePosition=false` |
| Identitas proteksi | `clientAlgoId` deterministik dan quantity-bound |
| Konfirmasi | GET entry/algo/history/trades/position sesuai ID dan ownership |

## Fill, TP/SL, dan exit

Entry diterima tetapi belum fill tetap `ENTRY_PENDING`. Untuk fill positif,
termasuk partial fill, gateway membuktikan order/fills dan ownership lalu
memasang SL terlebih dahulu, baru TP. Quantity exit mencakup 100% exposure
bot yang masih terbuka, tanpa mengambil posisi manual.

GET kedua proteksi harus cocok dengan symbol, ID, sisi, trigger, quantity,
workingType, reduceOnly/closePosition, dan active status sebelum
`POSITION_PROTECTED`. ACK tidak cukup. Additional fills mengikuti exposure;
receipt first fill tetap satu. Saat exit terbukti, cleanup pasangan hanya
mengenai order bot yang dibuktikan milik intent ini.

Tick sekitar 45 detik dan waktu request menambah jeda; entry/SL/TP tidak
atomik. [Pemetaan layar Binance](TP_SL_AUTOMATIC.md) menjelaskan pilihannya.

## OFF dan batas mutation

Setiap provider POST serta Binance POST/DELETE memeriksa ON tepat sebelum
transport. OFF memblokir entry, config, proteksi, cancel dan close baru,
termasuk mutation berikutnya di callback yang sudah dimulai. Standalone
adapter tanpa konteks coordinator tidak mendapat izin mutation implisit.

Request yang sudah terkirim masih dapat selesai; GET/hasil journal boleh
menyelesaikan pembuktiannya tanpa mutation berikutnya. Polling akun read-only
berjalan. OFF tidak close/cancel posisi atau order existing/manual. Pending
entry dapat fill di Binance saat OFF tanpa pemasangan proteksi baru oleh bot.

## Observasi yang diterima coordinator

Observasi memakai `source=BINANCE_FUTURES`, symbol/client ID/order ID yang
sesuai, `filled_quantity`, `observed_at`, serta state yang dibuktikan.
Fill positif memerlukan `first_fill_at`; `POSITION_PROTECTED` memerlukan
`sl_confirmed=true`, `tp_confirmed=true`, dan dua order ID. `CLOSED` memerlukan
exit serta waktu closure yang dapat dibuktikan. Coordinator menyimpan receipt
sekali menurut hari WIB first fill; close tidak mengembalikan kuota hari itu.

## Unknown outcome dan diagnostics

`ORDER_OUTCOME_UNKNOWN` berarti hasil callback belum dapat dibuktikan, bukan
Binance pasti menolak. Posisi nol belum membuktikan tidak ada LIMIT pending.
Timeout setelah POST tidak boleh diubah menjadi rejection atau submission ulang.
Jangan menghapus claim, mereset attempts/counter, atau memakai ID baru untuk
mengulang order. Rekonsiliasi manual memerlukan exact client lookup, riwayat
order/fills lengkap, open regular/algo orders, posisi, dan ownership saat ini.
Kode absence `-2013` sendirian belum menyelesaikan kemungkinan order/fill lain.

Exception TP/SL tidak ditelan. Unknown order/protection/provider menjadi
`NEEDS_REVIEW` dan memblokir pekerjaan baru sampai bukti diperiksa. Diagnostics
menyimpan kode aman dan tahap callback, misalnya `ORDER_UNKNOWN_PROTECTION`;
raw exception, signed query, credential, atau respons provider tidak dicetak.
Pemasangan source/tes tidak menandai unknown sebagai sukses.

## Deployment

Gunakan [audit source](AUDIT_BEFORE_ON.md), [Termius](TERMIUS_24_7.md), dan
[persiapan build existing](EXISTING_GATEWAY_BUILD.md). Revisi dibuktikan di VPS
saat OFF sebelum GitHub. Backup source/image/journal privat dipertahankan.
Cleanup source/command historis sesudah bukti akhir tidak menghapus volume,
financial journal, receipt, secret, manual orders atau pending entries.
