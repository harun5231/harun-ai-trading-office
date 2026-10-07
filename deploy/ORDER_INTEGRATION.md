# Sambungan tunggal order Binance Futures

Kode gateway publik dalam repository tetap menyediakan stub. Tanpa adapter
produksi pada kelas tersebut, pengiriman berhenti pada `EXECUTION_BLOCKED`,
dengan gateway `NOT_CONNECTED` dan kode
`BINANCE_ORDER_GATEWAY_NOT_CONNECTED`. ON dapat memakai kuota NeuroAPI;
status itu tidak menyatakan bahwa ada order di Binance.

VPS pengguna memakai adapter privat yang telah mengirim entry ETH dan
membuktikan proteksi Binance. Pemasangan pembaruan pada 7 Oktober 2026 pukul
10:04 UTC menghasilkan `VPS_RR_TARGET_LAST_PRICE_VERIFIED`, source
host/container cocok, dan worker sehat. Akun tersambung, robot ON, serta
snapshot menunjukkan satu posisi, satu receipt entry, satu slot tersedia,
dan tidak ada failure code. Pemulihan closure ETH dan request SOL kemudian
terverifikasi pada 11:14 UTC. Pembacaan 11:19 UTC membuktikan ETH asli `CLOSED`
tanpa failure, receipt tetap satu, posisi aktif nol, dan request/job pending
atau needs-review kosong. Cycle epoch 1 `COMPLETE` setelah tiga putaran
pengganti; kandidat terakhir ETHUSDT baru ditolak
`NET_RISK_REWARD_NOT_TARGET_2` sebelum order. Robot ON menunggu
`INSUFFICIENT_ACTIONABLE_SETUPS / ROBOT_CYCLE_COMPLETE` tanpa failure robot
atau akun, dengan satu slot tersedia. Laporan tetap `PARTIAL`; belum ada order
pengganti diterima, sehingga penerimaan `CONTRACT_PRICE` serta partial fill
versi baru belum dibuktikan.

Developer melanjutkan di satu kelas: [`OrderGateway`](../worker/order_gateway.py).
Implementasikan `submit(intent)` dan `reconcile(intent)` beserta `status()` yang
menggambarkan adapter dengan benar. Kedua metode bawaan sekarang melempar error
dan belum memiliki transport produksi. Tidak ada engine kedua, jalur tiket,
approval browser, atau sakelar environment pengiriman order.

`require_implementation(gateway)` memeriksa secara lokal bahwa `submit` dan
`reconcile` tersedia, dapat dipanggil, dan bukan metode stub bawaan yang masih
diwarisi dari `_MissingOrderImplementation`. Tambahkan kedua implementasi pada
kelas publik `OrderGateway`; stub privat tetap menandai bagian yang belum diisi.
Pemeriksaan ini tidak menjalankan request,
membaca konfigurasi koneksi, atau menggunakan status sebagai sakelar. Adapter
yang belum diimplementasikan dihentikan sebelum claim `SUBMITTING`.

Coordinator meneruskan pemanggilan ke metode yang telah diimplementasikan setelah
validasi alur, terlepas dari metadata `connected` pada `status()`. Status dipakai
untuk laporan Office dan health, sedangkan adapter menangani autentikasi,
transport, dan hasil exchange. Kelulusan pemeriksaan metode maupun status
koneksi tidak membuktikan bahwa autentikasi atau kesehatan exchange sudah benar.

## Batas coordinator yang sudah tersedia

`ROBOT ON → Binance account → screening sesuai slot → analisis → validasi → intent → gateway`

- Estimasi risiko net ke SL tidak melampaui target tersimpan; default 5 USDT,
  termasuk fee entry dan SL memakai taker commission akun per symbol dari
  signed GET Binance. Model menghasilkan side dan Entry/TP/SL, tanpa quantity.
  Pengaturan menerima target positif sampai 100 USDT seperti sebelumnya.
  Quantity memakai Decimal. Setup baru mengikat target net RR 1:2 sesudah fee
  dengan `NET_1_TO_2_NEAREST_TICK`, pada tick legal pertama yang mencapai
  rasio itu; plan lama tanpa kebijakan ini mempertahankan validasi minimum 2.
  Perubahan pengaturan hanya memengaruhi analisis baru, bukan intent yang
  telah dibuat dan diverifikasi.
  Estimasi ini tidak menjamin fee aktual, funding, slippage, atau gap harga.
- Maksimal dua entry bot baru per hari WIB (UTC+7) dan dua posisi bersamaan,
  termasuk posisi manual serta posisi dari hari sebelumnya. Kapasitas adalah
  `min(day_remaining, concurrency_remaining)`. Seluruh `ENTRY_PENDING` belum fill
  mencadangkan kuota harian serta slot, termasuk order yang dibuat kemarin dan
  masih mungkin fill hari ini. Exposure yang sudah tercermin pada posisi akun
  tidak dihitung dua kali.
  Counter entry berasal dari fill pertama yang terverifikasi, bukan ACK.
  Partial fill dan tambahan fill entry yang sama tetap satu receipt pada hari
  WIB dari fill pertama; receipt dan reservasi tetap persisten setelah restart.
  Pergantian hari pukul 00:00 WIB memperbarui kuota tanpa menutup posisi lama.
  Satu posisi lama yang masih aktif menyisakan paling banyak satu slot saat itu;
  dua posisi lama menyisakan nol sampai slot bebas. Penutupan posisi membebaskan
  slot bersamaan, tanpa mengembalikan kuota entry hari itu.
- HYPEUSDT selalu dikecualikan. Setiap symbol yang sudah memiliki posisi aktif
  dikecualikan, termasuk posisi manual. Label exposure tidak memberi hak
  mengambil alih posisi. Receipt historis tidak membuktikan kepemilikan posisi
  yang sekarang dibuka kembali secara manual.
- One-way, single-asset, CROSS, leverage 75, entry LIMIT GTC. Coordinator
  memeriksa ulang kontrak market sebelum submit. Harga entry/TP/SL tidak
  diubah untuk membuat setup yang ditolak menjadi lolos. Plan baru mengikat
  pemicu TP/SL `CONTRACT_PRICE` ("Terakhir" pada Binance) sebelum bukti distamp.
  Plan lama tanpa field `protection_working_type` tetap memakai `MARK_PRICE`;
  pembaruan tidak mengganti pemicu intent atau order yang sudah ada.
- Intent harus berasal dari bukti `analysis-v9` yang lengkap, hari yang sama,
  dan aturan yang diperiksa maksimal lima menit sebelumnya. Setup lama tidak
  otomatis menjadi order saat gateway disambungkan.
- Cycle `robot-v9` memakai candle Binance 1h/15m nyata, rules, dan fee akun
  terbaru. HOLD dan penolakan lokal `NET_RISK_REWARD_BELOW_2` atau
  `NET_RISK_REWARD_NOT_TARGET_2` sebelum plan/intent order dapat memicu
  replacement; kandidat yang ditolak tetap `REJECTED`.
  Keduanya berbagi maksimum tiga screening tambahan, hanya untuk putaran terakhir:
  satu atau dua pengganti sesuai jumlah eligible dan slot tersedia. Penolakan
  lain, intent yang sudah ada, dan unknown tidak diganti atau dikirim ulang.
  Hanya screening pengganti memakai `message_history` existing untuk daftar
  seen/exposed/pending/HYPEUSDT; prompt dan schema tetap, awal tanpa history.
- Claim riset dan status `SUBMITTING` disimpan sebelum pemanggilan eksternal.
  Maksimal satu operasi NeuroAPI per tick; worker tidak bergantung pada tab web.

## Input adapter

`build_intent()` menghasilkan dict internal berikut. Ini bukan payload HTTP
Binance; adapter harus melakukan pemetaan parameter dan signing di VPS.
Semua nilai harga dan quantity tetap string Decimal.

| Field | Arti |
| --- | --- |
| `intent_id` | ID operasi analisis persisten |
| `client_order_id` | ID entry deterministik `hao-` + 28 karakter hash |
| `symbol`, `position_side`, `side` | Kontrak, `BOTH`, `LONG`/`SHORT` |
| `entry` | `order_type`, `side`, `price`, `quantity`, `time_in_force` |
| `protection` | `exit_side`, `stop_loss`, `take_profit`, `working_type` |
| `margin_mode`, `leverage` | Konfigurasi yang sudah divalidasi |
| `risk_target_usdt`, `risk_usdt` | Target dan estimasi risiko net SL, termasuk fee entry/SL |
| `gross_risk_usdt` | Estimasi loss harga ke SL sebelum fee |
| `entry_fee_usdt`, `sl_exit_fee_usdt`, `tp_exit_fee_usdt` | Fee entry, SL, dan TP pada taker rate akun |
| `net_reward_usdt`, `net_reward_risk` | Estimasi profit TP sesudah fee dan net RR |
| `reward_risk_policy`, `tp_tick_size` | Khusus intent baru dengan target `NET_1_TO_2_NEAREST_TICK`: kebijakan RR dan tick harga dari rules sizing terverifikasi |
| `fee_evidence` | `source`, `symbol`, `observed_at`, `taker_rate` dari signed GET Binance |
| `excluded_costs` | `SLIPPAGE`, `FUNDING`, `GAPS` yang tidak dijamin model biaya |
| `evidence_sha256` | Digest bukti analisis terverifikasi |

Payload journal dibandingkan ulang dengan intent dari bukti sebelum dipakai.
Preflight submission memakai target yang terikat pada bukti intent tersebut,
bukan mengganti quantity saat pengaturan risiko berikutnya berubah.
`protection.working_type` berasal dari `plan.protection_working_type` yang
terikat pada bukti. Adapter menerima `CONTRACT_PRICE` atau `MARK_PRICE` sesuai
intent dan memverifikasi nilai yang sama pada respons Binance. Fallback
`MARK_PRICE` hanya menjaga identitas plan lama yang belum memiliki field ini.
Taker commission sumber sizing berasal dari signed
`GET /fapi/v1/commissionRate?symbol=...`, bukan tarif tetap atau tebakan model.
Entry LIMIT dapat mendapat fee maker yang lebih rendah; sizing tetap konservatif
menggunakan taker untuk entry/exit. Worker memeriksa TP model pada setup baru dengan
target net RR 1:2, termasuk fee entry/SL pada risiko dan fee entry/TP pada
reward. Harga pembanding dibulatkan ke tick pertama yang mencapai net RR 2:
ke atas untuk LONG, ke bawah untuk SHORT. TP model harus sama dengan harga
legal tersebut. Nilai di bawah minimum tetap menghasilkan
`NET_RISK_REWARD_BELOW_2`; nilai yang tidak sesuai target tick menghasilkan
`NET_RISK_REWARD_NOT_TARGET_2`. Worker tidak menggeser Entry/TP/SL agar setup
yang gagal menjadi lolos. Plan dan intent lama tanpa field kebijakan tetap
memakai kontrak minimum net RR 2; pembaruan tidak mengubah level atau hash
bukti existing.
Adapter harus memeriksa posisi dan open orders terbaru tepat sebelum POST,
agar perubahan manual setelah account snapshot tidak melampaui kapasitas.
Jangan membatalkan order, mengganti konfigurasi akun, atau memasang proteksi
pada symbol yang tidak dapat dibuktikan berasal dari intent ini.

## Endpoint yang perlu dihubungkan

Dokumentasi resmi Binance diperiksa pada 6 Oktober 2026:

- [Entry dan query order](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade):
  `POST /fapi/v1/order` dan `GET /fapi/v1/order`.
- [Conditional TP/SL dan query algo](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order):
  `POST /fapi/v1/algoOrder` dan `GET /fapi/v1/algoOrder`.

Gunakan ID deterministik untuk entry serta kedua proteksi, signing, sinkronisasi
server time, timeout terbatas, dan rate limit sesuai dokumentasi terkini.
`BinanceReadOnly` tetap reader GET; transport order hanya berada di gateway.
Helper HTTP publik juga tetap melayani pembacaan market. Developer menyediakan
signed transport order pada gateway tunggal, dengan kontrak reader tersebut
tetap dipertahankan.
API key dan secret hanya dibaca di VPS, tidak masuk payload journal atau Office.

ACK entry belum membuktikan fill atau proteksi. Adapter harus mengonfirmasi
quantity yang benar-benar terisi, termasuk partial fill, serta SL dan TP di
exchange untuk exposure tersebut. Verifikasi symbol, sisi exit, trigger,
quantity proteksi, dan semantik reduce-only agar proteksi tidak membuka posisi
baru. Sesudah fill terverifikasi, adapter template memasang SL terlebih dahulu,
lalu TP, melalui `POST /fapi/v1/algoOrder`: `STOP_MARKET` untuk SL dan
`TAKE_PROFIT_MARKET` untuk TP, `reduceOnly=true`, `closePosition=false`, dan
`positionSide=BOTH`. Quantity melindungi 100% ukuran terisi yang dapat dibuktikan
milik intent, bukan saldo tersedia atau posisi manual. Kedua order diverifikasi
melalui GET sebelum status menjadi `POSITION_PROTECTED`.
Order entry dan kedua proteksi bukan satu transaksi atomik. Worker memeriksa
rekonsiliasi saat ON pada tick sekitar 45 detik; request Binance dapat menambah
waktu, sehingga proteksi tidak dijanjikan terpasang pada milidetik fill.
Developer harus menyelesaikan penanganan kegagalan proteksi,
partial fill tambahan, cancel/expiry entry, serta pembatalan proteksi pasangan
sesudah exit tanpa menyentuh order manual. Jangan melaporkan sukses pada
exposure yang belum terlindungi.

Pemetaan pilihan TP/SL pada aplikasi Binance dijelaskan di
[TP_SL_AUTOMATIC.md](TP_SL_AUTOMATIC.md). Pembaruan VPS mempertahankan adapter
privat dan bukti intent ETH historis, termasuk closure yang telah terverifikasi.
Pemasangan source dan pengujian lokal tidak
membuktikan penerimaan order baru; hasil produksi tetap memerlukan bukti Binance.

## Hasil `submit` dan `reconcile`

Kembalikan observasi Binance yang telah diverifikasi, bukan response karangan:

| Field | Ketentuan |
| --- | --- |
| `source` | `BINANCE_FUTURES` |
| `state` | `ENTRY_PENDING`, `POSITION_PROTECTED`, `CLOSED`, atau `REJECTED` |
| `symbol`, `client_order_id` | Sama persis dengan intent |
| `order_id` | ID entry Binance positif dalam string digit |
| `filled_quantity` | Quantity kumulatif string Decimal, 0 sampai quantity intent |
| `observed_at` | Waktu observasi ISO UTC bertimezone, maksimal 120 detik lalu |
| `first_fill_at` | Wajib jika terisi; waktu fill pertama sesudah intent dibuat |
| `sl_confirmed`, `tp_confirmed` | Harus boolean true untuk `POSITION_PROTECTED` |
| `sl_order_id`, `tp_order_id` | ID proteksi exchange positif, wajib untuk posisi terlindungi |
| `exit_order_id`, `closed_at` | Bukti exit dan waktu penutupan, wajib untuk `CLOSED` |

`ENTRY_PENDING` dan `REJECTED` hanya menerima quantity terisi nol.
`POSITION_PROTECTED` dan `CLOSED` harus memiliki fill positif. ID entry dan
waktu fill pertama tidak boleh berubah, quantity kumulatif tidak boleh turun,
observasi tidak boleh mundur, dan posisi terisi tidak boleh kembali menjadi
pending/rejected. ID proteksi dan exit harus sesuai validasi coordinator.
Field lain dibuang; raw response, secret, URL bertanda tangan, dan prose adapter
tidak disimpan. Receipt entry ditulis satu kali berdasarkan waktu fill pertama
Binance yang terverifikasi dan memakai hari WIB (UTC+7) dari waktu tersebut.
Tambahan partial fill tidak membuat entry baru, sekalipun terjadi pada hari
berikutnya. `ENTRY_PENDING` yang belum pernah fill tetap mencadangkan kuota
harian yang baru, walaupun order dibuat pada hari sebelumnya.

## Outcome tidak pasti dan operasi OFF

Timeout, restart ketika `SUBMITTING`, hasil adapter yang salah, atau kegagalan
proteksi menghasilkan `NEEDS_REVIEW`. Worker berhenti dan tidak mengirim ulang
entry dengan ID baru. Sebelum menjalankan produksi, developer harus menyediakan
prosedur rekonsiliasi berdasarkan client ID, order/fill Binance, dan proteksi,
kemudian memperbarui journal secara terverifikasi. Jangan menghapus journal
atau menganggap timeout berarti order gagal. Pemulihan outcome tidak pasti
sengaja belum diotomatisasi pada build sambungan yang belum terhubung ini.

Sesudah claim `SUBMITTING`, setiap exception dari metode adapter yang sudah
diimplementasikan diperlakukan sebagai outcome tidak pasti, termasuk
`GatewayUnavailable`. Worker tidak mengirim ulang claim tersebut otomatis.
Metadata koneksi tidak mengubah penanganan ini.

OFF menghentikan riset, submission baru, dan pemanggilan rekonsiliasi gateway
berikutnya. OFF tidak menutup posisi dan tidak membatalkan order entry/SL/TP.
Pemanggilan eksternal yang sudah dikirim tidak dapat ditarik kembali; hasilnya
masih dapat selesai dan dicatat ke journal. Guard GET riset tidak menginterupsi
callback adapter yang sudah dimulai, karena callback dapat sedang menyelesaikan
proteksi entry. OFF mencegah pemanggilan callback berikutnya. Exception callback
tetap diperlakukan sebagai outcome belum pasti, tanpa replay otomatis.
Polling read-only saldo/posisi/riwayat
Office tetap berjalan, sementara semua karyawan AI tidak menunjukkan aktivitas
kerja saat OFF. Rekonsiliasi gateway dijalankan saat ON; developer harus
menyediakan prosedur penanganan pending/filled order sesudah restart atau ketika
worker OFF, sesuai pilihan ini. Proteksi Binance tetap berada di exchange ketika
worker/browser berhenti; OFF tidak memicu close atau cancel otomatis.

## Migrasi dan pemeriksaan

Upgrade pertama membuat `ledger-pre-order.sqlite3` privat yang diverifikasi,
menghapus tabel runtime lama, membersihkan cache lama, dan mengembalikan robot
ke OFF. Archive hanya audit; runtime tidak membacanya. Journal NeuroAPI dan
receipt entry nyata dipertahankan. Request lama dengan outcome tidak pasti tetap
menghentikan riset sampai ditinjau. Setup dari namespace lama tidak dipromosikan.

Upgrade v8→v9 hanya mengganti constraint identitas cycle melalui transaksi
schema version 2. Semua cycle dan rowid, request berbayar, candidate lama,
intent termasuk `SUBMITTING`/`NEEDS_REVIEW`, receipt, ON/OFF, risiko tersimpan,
serta observasi exposure dipertahankan. Namespace baru dapat mulai pada
hari/epoch sama tanpa memakai request/proof gross-risk sebelumnya; bukti lama
tidak otomatis mendapat fee baru atau dipromosikan menjadi order.

Laporan Office memakai GET Binance account/income/userTrades. Riwayat berisi
fill, bukan inferensi posisi tertutup; scope parsial ditandai dan total transaksi
yang belum terbukti ditampilkan kosong. Animasi layar kantor bukan bukti order.

Jalankan verifikasi tanpa kredensial atau transaksi:

```sh
python -m unittest discover -s tests -p 'test_*.py' -q
```

Fixtures pada pengujian memeriksa kontrak adapter, claim persisten, slot manual,
receipt, dan restart; fixtures bukan executor runtime. Kelulusan pengujian
tidak menyatakan sambungan POST produksi atau proteksi Binance sudah bekerja.
