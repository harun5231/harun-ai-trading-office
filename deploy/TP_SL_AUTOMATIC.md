# TP/SL otomatis sesudah entry terisi

Untuk order baru, pilihan pengguna mengikuti layar TP/SL Binance: pemicu
**Terakhir**, kedua opsi **Limit Order** tidak dicentang, dan jumlah **100%**
ukuran entry yang benar-benar terisi dan masih terbuka. Adapter menerapkan pilihan tersebut
melalui API, tanpa mengoperasikan tombol aplikasi Binance.

| Pilihan pada layar Binance | Parameter API |
| --- | --- |
| Take Profit, Limit Order tidak dicentang | `type=TAKE_PROFIT_MARKET` |
| Stop Loss, Limit Order tidak dicentang | `type=STOP_MARKET` |
| Pemicu Terakhir | `workingType=CONTRACT_PRICE` |
| Pemicu TP/SL | `triggerPrice` dari harga TP/SL setup |
| Jumlah 100% | `quantity` sesuai exposure terisi yang masih terbuka milik entry bot |
| Menutup exposure tanpa membuka posisi baru | `reduceOnly=true`, `closePosition=false` |
| Akun one-way | `positionSide=BOTH` |
| Posisi LONG / SHORT | Sisi proteksi `SELL` / `BUY` |

Entry tetap LIMIT GTC, margin CROSS, leverage 75×. Harga entry, TP, SL, dan
quantity berasal dari setup serta perhitungan risiko. Setup baru harus memenuhi
target net RR 1:2 pada tick harga Binance yang tepat; pemicu Terakhir tidak
mengubah budget risiko atau membuat RR yang ditolak menjadi lolos. TP/SL market
dapat mengalami slippage setelah harga pemicu tercapai.

## Target RR setup baru

Setup baru mengikat kebijakan `reward_risk_policy=NET_1_TO_2_NEAREST_TICK`.
Worker memeriksa TP yang diberikan Neurobro terhadap harga legal pertama yang
memberi net reward minimal dua kali net risk sesudah fee entry dan exit.
Untuk LONG, harga target dibulatkan ke tick di atasnya; untuk SHORT, ke tick
di bawahnya. Selisih kecil akibat tick diperbolehkan melalui harga target ini,
bukan dengan menerima TP yang memberi reward jauh di atas rasio yang diminta.
Worker menghitung harga pembanding, tetapi tidak mengganti harga model.

Net risk memakai jarak entry ke SL beserta taker fee entry dan SL. Net reward
memakai jarak entry ke TP dikurangi taker fee entry dan TP. Fee berasal dari
commission akun Binance untuk symbol tersebut, bukan angka yang dipilih model.
Quantity legal terbesar tetap mengikuti target risiko tersimpan, default
5 USDT. Funding, slippage, gap, dan perubahan fee tidak dijamin estimasi ini.

Sebagai contoh perhitungan, entry LONG 2.610, SL 2.585, taker fee 0,0005,
dan tick 0,01 menghasilkan target TP ideal sekitar 2.667,833916958,
sehingga TP legalnya 2.667,84. TP 2.660 memberi rasio harga 1:2 sebelum fee;
ia belum memenuhi net 1:2 pada asumsi fee tersebut. Dengan quantity 0,181,
TP 2.730 memberi net RR sekitar 1:4,25 dan lolos aturan lama yang hanya
mensyaratkan minimum 1:2. Contoh ini menjelaskan validasi; harga order ETH
yang sudah aktif tidak diubah.

TP di bawah net 1:2 ditolak dengan `NET_RISK_REWARD_BELOW_2`; TP yang
memenuhi minimum tetapi tidak sama dengan target tick ditolak dengan
`NET_RISK_REWARD_NOT_TARGET_2`. Jika belum ada plan atau intent order,
penolakan ini dapat meminta coin pengganti seperti HOLD. Keduanya berbagi
batas tiga putaran pengganti dan tetap dibatasi slot; tidak ada entry dikirim
untuk setup yang ditolak.

## Urutan dan konfirmasi

1. Binance menerima entry LIMIT. ACK belum berarti entry terisi.
2. Saat ON, worker memeriksa order dan fill pada tick sekitar 45 detik. Adapter
   membuktikan bahwa exposure berasal dari intent bot ini.
3. Untuk fill positif, adapter memasang dan memverifikasi SL terlebih dahulu,
   kemudian TP melalui `POST /fapi/v1/algoOrder`. Partial fill juga memerlukan
   proteksi untuk ukuran yang sudah terisi.
4. GET Binance harus membuktikan trigger, quantity, sisi exit, pemicu, dan kedua
   proteksi aktif sebelum journal menyatakan `POSITION_PROTECTED`. ID
   deterministik memungkinkan pemeriksaan order yang sudah ada tanpa duplikat.
5. Tambahan fill direkonsiliasi agar proteksi mengikuti exposure. Satu entry
   tetap dihitung sekali berdasarkan fill pertama yang terverifikasi.

Entry, SL, dan TP merupakan request terpisah. Jeda polling dan waktu Binance
merespons berarti proteksi tidak atomik dengan fill. Status `NEEDS_REVIEW`
berarti outcome belum dapat dibuktikan; worker menghentikan rekonsiliasi
otomatis dan tidak mengirim ulang entry. Pemulihan memerlukan pemeriksaan
order/fill/proteksi Binance dan pembaruan journal berdasarkan hasil terverifikasi.

OFF menghentikan callback berikutnya, termasuk pemasangan atau rekonsiliasi
proteksi baru. Callback yang sudah dimulai dapat selesai. OFF tidak menutup
posisi dan tidak membatalkan entry, SL, atau TP yang sudah berada di Binance.

## Pemicu intent lama dan posisi ETH existing

Plan baru menyimpan `protection_working_type=CONTRACT_PRICE` sebelum bukti
analisis distamp. `build_intent()` mengambil nilai tersebut; plan lama yang
tidak memiliki field ini tetap memakai `MARK_PRICE`. Adapter membaca pemicu
dari intent dan memverifikasi pemicu yang sama pada Binance. Intent dan order
lama tidak dipindahkan otomatis ke pemicu Terakhir.

Audit dan pencatatan VPS pada 7 Oktober 2026 telah membuktikan entry ETHUSDT
LONG 0,181 ETH terisi pada 2.610, SL 2.585 dan TP 2.730 aktif dengan
`MARK_PRICE`. Output `ETH_PROTECTION_JOURNAL_VERIFIED` membuktikan journal
`POSITION_PROTECTED`, kedua proteksi terkonfirmasi, dan satu receipt fill.
Harga, pemicu, serta bukti intent ETH ini tetap mengikuti setup existing.
Pada 7 Oktober 2026 pukul 10:04 UTC, update VPS menghasilkan
`VPS_RR_TARGET_LAST_PRICE_VERIFIED`: source cocok dan worker sehat. Kebijakan
target net RR dan pemicu Terakhir kini terpasang untuk setup baru. Marker ini
tidak membuktikan bahwa Binance telah menerima TP/SL dengan pemicu baru;
audit berikutnya membuktikan pencarian pengganti berjalan, tetapi analisis SOL
ditolak HTTP 422 sebelum order baru.

Audit GET pada 7 Oktober 2026 pukul 10:45 UTC kemudian membuktikan SL telah
menutup ETH pada 10:07:39.052 UTC. Exit SELL MARKET terisi 0,181 ETH pada average
price 2.575,81; SL `FINISHED`, TP pasangan `CANCELED`, dan tidak ada posisi atau
order/algo ETH terbuka. Journal sempat `NEEDS_REVIEW / BINANCE_ORDER_EXIT_RACE`
setelah pemeriksaan cancel. Pada 11:14 UTC, pencatatan berdasarkan bukti GET
menghasilkan `ETH_CLOSED_JOURNAL_VERIFIED`: state `CLOSED` tercatat,
receipt pertama tetap satu, dan tidak ada entry ETH ulang.

Realisasi harga exit -6,18839 USDT dan kedua fee total 0,3275928 USDT memberi
hasil trade net -6,5159828 USDT sebelum funding. Pemicu SL 2.585 berbeda dari
harga fill market 2.575,81; estimasi risiko sizing 5 USDT tidak menjamin loss
aktual dibatasi 5 USDT. Lihat
[pemulihan closure dan request SOL](NEUROAPI_422_RECOVERY.md) untuk bukti,
helper GET-only, serta batas perubahan cancel pasangan yang telah terpasang.
Source pembaruan dan worker healthy telah diverifikasi pada 11:14 UTC.
Pemeriksaan coordinator 11:19 UTC kemudian membuktikan order ETH asli
`CLOSED` tanpa failure, receipt tetap satu, posisi aktif nol, dan tidak ada
request/job pending atau needs-review. Putaran pengganti ketiga selesai;
setup ETHUSDT baru ditolak `NET_RISK_REWARD_NOT_TARGET_2` sebelum order.
Robot ON menunggu `INSUFFICIENT_ACTIONABLE_SETUPS / ROBOT_CYCLE_COMPLETE`
tanpa failure teknis tersisa pada pemeriksaan itu. Belum ada order pengganti
diterima, sehingga pemicu `CONTRACT_PRICE` dan partial fill versi baru tetap
memerlukan bukti produksi tersendiri. Laporan akun masih `PARTIAL`.

Helper kasus historis [`recover_eth_take_profit.py`](recover_eth_take_profit.py)
memverifikasi entry dan SL existing lalu hanya mengizinkan satu POST TP dengan
pemicu `MARK_PRICE` yang sudah terikat pada intent. Helper tidak mengirim ulang
entry, membatalkan SL, atau mengganti trigger existing. Jangan mengubah pin
helper agar dipakai pada posisi atau versi kode lain. Percobaan helper tersebut
menghasilkan outcome tidak pasti; audit GET berikutnya membuktikan TP sudah
ada. Pencatatan GET-only melalui
[`record_eth_protection.py`](record_eth_protection.py) kemudian memverifikasi
kedua order dan mencatat hasil melalui coordinator. Kedua helper terikat pada
kasus dan versi lama tersebut; jangan mengulang POST TP yang sudah terbukti ada.
