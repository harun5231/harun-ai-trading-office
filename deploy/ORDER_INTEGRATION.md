# Sambungan tunggal order Binance Futures

Build ini menjalankan account reader, screening NeuroAPI, analisis, dan validasi
risiko. Pengiriman entry/TP/SL **belum terhubung**. Alur berhenti pada
`EXECUTION_BLOCKED`, dengan gateway `NOT_CONNECTED` dan kode
`BINANCE_ORDER_GATEWAY_NOT_CONNECTED`. ON dapat memakai kuota NeuroAPI;
status itu tidak menyatakan bahwa ada order di Binance.

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
  Quantity memakai Decimal dan net RR setelah fee minimal 2. Perubahan pengaturan hanya
  memengaruhi analisis baru, bukan intent yang telah dibuat dan diverifikasi.
  Estimasi ini tidak menjamin fee aktual, funding, slippage, atau gap harga.
- Maksimal dua posisi bersamaan; tidak ada batas entry harian.
  Pending entry ikut mencadangkan kapasitas. Counter entry hanya
  berasal dari observasi fill yang lolos kontrak, bukan dari ACK.
- HYPEUSDT selalu dikecualikan. Setiap symbol yang sudah memiliki posisi aktif
  dikecualikan, termasuk posisi manual. Label exposure tidak memberi hak
  mengambil alih posisi. Receipt historis tidak membuktikan kepemilikan posisi
  yang sekarang dibuka kembali secara manual.
- One-way, single-asset, CROSS, leverage 75, entry LIMIT GTC. Coordinator
  memeriksa ulang kontrak market sebelum submit. Harga entry/TP/SL tidak
  diubah untuk membuat setup yang ditolak menjadi lolos.
- Intent harus berasal dari bukti `analysis-v9` yang lengkap, hari yang sama,
  dan aturan yang diperiksa maksimal lima menit sebelumnya. Setup lama tidak
  otomatis menjadi order saat gateway disambungkan.
- Cycle `robot-v9` memakai candle Binance 1h/15m nyata, rules, dan fee akun
  terbaru. Hanya HOLD memicu replacement, maksimal tiga screening tambahan
  sesudah awal. Dua HOLD meminta dua pengganti; satu HOLD meminta satu.
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
| `fee_evidence` | `source`, `symbol`, `observed_at`, `taker_rate` dari signed GET Binance |
| `excluded_costs` | `SLIPPAGE`, `FUNDING`, `GAPS` yang tidak dijamin model biaya |
| `evidence_sha256` | Digest bukti analisis terverifikasi |

Payload journal dibandingkan ulang dengan intent dari bukti sebelum dipakai.
Preflight submission memakai target yang terikat pada bukti intent tersebut,
bukan mengganti quantity saat pengaturan risiko berikutnya berubah.
Taker commission sumber sizing berasal dari signed
`GET /fapi/v1/commissionRate?symbol=...`, bukan tarif tetap atau tebakan model.
Entry LIMIT dapat mendapat fee maker yang lebih rendah; sizing tetap konservatif
menggunakan taker untuk entry/exit. Worker memerlukan net RR minimal 2 sesudah
fee entry/TP dan tidak menggeser Entry/TP/SL agar setup yang gagal menjadi lolos.
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
baru. Order entry dan kedua proteksi bukan satu
transaksi atomik. Developer harus menyelesaikan penanganan kegagalan proteksi,
partial fill tambahan, cancel/expiry entry, serta pembatalan proteksi pasangan
sesudah exit tanpa menyentuh order manual. Jangan melaporkan sukses pada
exposure yang belum terlindungi.

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
tidak disimpan. Receipt entry ditulis satu kali berdasarkan waktu fill pertama.

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

OFF menghentikan riset dan submission baru; pemanggilan yang sudah berlangsung
dapat selesai. Polling saldo/posisi/riwayat tetap berjalan. Lifecycle gateway
saat ini direkonsiliasi saat ON, sehingga sebelum produksi developer juga harus
menyelesaikan monitoring pending/filled order selama OFF dan sesudah restart.
Proteksi Binance harus tetap berada di exchange ketika worker/browser berhenti.

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
