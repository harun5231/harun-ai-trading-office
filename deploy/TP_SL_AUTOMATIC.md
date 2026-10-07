# Entry dan TP/SL otomatis Binance

Plan baru mengikuti layar Binance: pemicu **Terakhir**, opsi **Limit Order**
TP/SL tidak dicentang, dan jumlah **100%** exposure bot yang sudah terisi serta
masih terbuka. Worker mengisi parameter melalui API.

| Pilihan / field | Parameter Binance |
| --- | --- |
| Entry LONG / SHORT | `side=BUY` / `SELL`, `type=LIMIT`, `timeInForce=GTC` |
| Harga masuk | `price=intent.entry.price` |
| Size coin | `quantity=intent.entry.quantity`, unit base asset |
| CROSS | `marginType=CROSSED`, diverifikasi sebelum entry |
| 75× | `leverage=75`, sesuai bracket/margin tersedia |
| TP tanpa Limit Order | Algo `type=TAKE_PROFIT_MARKET` |
| SL tanpa Limit Order | Algo `type=STOP_MARKET` |
| Pemicu Terakhir | `workingType=CONTRACT_PRICE` |
| Harga pemicu | `triggerPrice` TP eksekusi / SL intent |
| Jumlah 100% | Quantity exposure terisi bot yang masih terbuka |
| Mengurangi posisi | `reduceOnly=true`, `closePosition=false` |
| One-way LONG / SHORT exit | `positionSide=BOTH`, `side=SELL` / `BUY` |

## Harga dan risiko

Setup model memerlukan declared RR dan jarak harga minimal 1:2. HOLD atau RR
di bawah dua sebelum plan/intent meminta coin berbeda melalui maksimum tiga
putaran pengganti. Entry dan SL model tetap dipakai setelah validasi.

Worker menormalisasi TP eksekusi ke net 1:2 memakai fee akun fresh dan cadangan
adverse exit 0,5%. TP model yang lebih jauh dinormalisasi, bukan ditolak hanya
karena RR di atas dua. LONG memakai tick legal pertama di atas target, SHORT
di bawahnya. TP model asli, TP eksekusi, fee, cadangan, dan tick disimpan untuk
audit. Intent existing tidak dinormalisasi ulang saat update.

Model `FEE_SLIPPAGE_RISK_V3` memilih quantity legal terbesar agar planned loss
SL termasuk fee entry/SL dan cadangan 0,5% memenuhi target. Default 5 USDT tetap
dapat diubah pada menu existing. Cadangan bukan jaminan fill: gap, slippage
melebihi cadangan, funding, liquidation, dan perubahan fee dapat membuat loss
aktual melampaui target.

## Urutan pemasangan

1. Gateway memverifikasi akun, ownership, slot, margin, CROSS/75, filters,
   fee, dan intent fresh sebelum mengirim entry LIMIT GTC.
2. Entry diterima belum berarti fill; `ENTRY_PENDING` tetap memakai slot.
3. Saat ON, actual fills dan exposure bot dibuktikan. Fill positif, termasuk
   partial fill, mendapat SL terlebih dahulu kemudian TP melalui
   `POST /fapi/v1/algoOrder`.
4. GET membuktikan ID, sisi, quantity, trigger, `workingType`, dan kedua
   proteksi aktif sebelum status `POSITION_PROTECTED`.
5. Tambahan fill mengikuti exposure tersisa. First fill tetap dihitung satu
   kali meskipun entry terisi dalam beberapa bagian.
6. Exit/cleanup pasangan saat ON hanya memakai order milik intent tersebut;
   order manual tidak diubah.

Coordinator memeriksa sekitar 45 detik; respons Binance menambah waktu.
Entry/SL/TP merupakan request terpisah, bukan satu transaksi atomik. ID
deterministik memungkinkan pemeriksaan order existing tanpa duplikasi.

OFF menghentikan setiap mutation baru, termasuk TP/SL dan cancel, walaupun
callback telah dimulai. Existing orders tetap berada di Binance; OFF tidak
close posisi atau cancel pending entry, yang masih dapat fill di Binance.
GET account/status tetap tersedia dan request yang sudah terkirim masih
bisa dijournal tanpa mutation berikutnya.

Intent lama mempertahankan level/quantity/model biaya dan pemicu aslinya.
Plan historis tanpa field pemicu tetap `MARK_PRICE`; V3 baru memakai
`CONTRACT_PRICE`. Jangan mengubah order historis agar tampak mengikuti V3.

`NEEDS_REVIEW` memerlukan bukti exchange sebelum alur dilanjutkan, tanpa replay
entry otomatis. Posisi nol bukan bukti tidak ada LIMIT pending; ACK/chart saja
bukan bukti proteksi sesuai. Lihat [kontrak adapter](ORDER_INTEGRATION.md) dan
[panduan Termius](TERMIUS_24_7.md) untuk pemeriksaan read-only.
