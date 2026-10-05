# Coordinator riset 24/7 + approval (tanpa execution)

Build ini **tidak dapat mengirim order Binance**. Transport production tetap
GET-only, binance-arm/execute tidak bisa diaktifkan, live scheduler OFF. ROBOT ON
hanya mengizinkan riset NeuroAPI berbayar dan pembuatan setup untuk review.

## Office

MENU → ROBOT TRADING memakai koneksi worker/token kontrol yang sama dengan panel
NeuroAPI. Token hanya berada di memori tab; secret provider tetap di VPS.

- Default instalasi: ROBOT OFF, risk target 5 USDT. ON/OFF dan risk disimpan di
  SQLite privat pada volume worker. Restart tidak mengubah pilihan.
- Risk menerima decimal positif sampai **100 USDT**; tidak menerima float/NaN,
  infinity, input eksponen atau nilai negatif. Perubahan berlaku untuk analisis
  yang baru dimulai; analisis in-flight memakai target yang sudah disnapshot.
- OFF menghentikan request riset baru/replacement. Request yang sudah dikirim
  tidak dapat dibatalkan pada provider dan boleh menyelesaikan penyimpanan hasil.
  OFF tidak menutup posisi, cancel order, mengubah TP/SL, atau menghapus state.
- SETUP_READY menampilkan side, LIMIT Entry, TP, SL, quantity, risk target,
  actual risk dan RR. OK menyimpan APPROVED. TIDAK menyimpan USER_REJECTED;
  saat ON, pengganti hanya dicari jika kapasitas masih tersedia.
- Approval idempotent dan final, tidak mengubah level/quantity, tidak memberi izin
  live. Setup APPROVED tidak otomatis expired/dieksekusi/dihapus. Karena tidak
  ada eksekusi pada build ini, setup unresolved menahan riset berikutnya.

## Kapasitas dan loop

Poll coordinator setiap 45 detik; maksimum satu operasi NeuroAPI per tick.
Pembacaan account USD-M Futures menggunakan private GET dengan autentikasi yang
sudah ada. Kegagalan autentikasi/data menghentikan riset. Hanya ONE_WAY dan
single-asset account yang diterima. Jumlah posisi adalah jumlah symbol unik
ber-positionAmt bukan nol, termasuk posisi manual. HYPEUSDT selalu manual-only,
tidak dapat menjadi target analysis/setup; tidak ada mutasi akun apa pun.

`slots_needed = max(0, min(2 - running_positions, 2 - bot_entries_today))`

Jika nol, tidak ada screening/analysis. Posisi yang terbawa dari kemarin mengurangi
concurrent capacity; hanya entry baru terkonfirmasi pada hari Asia/Bangkok yang
masuk counter harian. `robot_entry_receipts` sengaja terpisah dari setup/approval,
trades paper dan model lifecycle. **Tidak ada writer production untuk receipt
execution dalam build ini**, sehingga setup/OK tidak pernah dianggap entry nyata.
Tabel tersebut bukan backfill dari legacy paper/live-model records.

Cycle ID `YYYY-MM-DD:robot-v7:<executed-entry-count>` mengikat kesempatan riset
pada hari dan epoch entry nyata; polling/restart tidak membuat cycle baru untuk
kesempatan yang sama. Cycle complete/rejected tidak diulang otomatis. Cycle baru
memerlukan kapasitas, epoch/hari baru, dan tidak ada setup/lifecycle unresolved.
Tidak ada infinite quota loop ketika tidak ada order live yang dapat mengisi slot.

Screening memakai literal 1 coin untuk satu slot, literal lama 2 coin untuk dua
slot. Schema memaksa jumlah persis, unique uppercase USDT; catalog exchangeInfo
harus active USD-M perpetual. Tidak menambahkan suffix atau memperbaiki symbol.
HOLD/TIDAK memicu replacement sesuai slot yang tersisa, maksimal tiga screening
replacement per cycle. Symbol yang sudah dianalisis tidak diulang. Technical
REJECT bukan alasan replacement. Unknown/pending paid request menghentikan cycle
untuk review, bukan dikirim ulang setelah restart. Retry provider tetap 429/503
bounded sesuai implementasi lama.

## Risiko, bukti dan kompatibilitas

ANALYSIS literal tidak diubah. Risk target dan rules Binance dikirim lewat
structured market context bersama 1h/15m OHLCV, mark/time dan filter kontrak.
Model menentukan side/Entry/TP/SL. Worker menentukan quantity legal terbesar
melalui floor rational/Decimal: `target / abs(Entry-SL)`, dibatasi maxQty/stepSize,
minQty/minNotional dan aturan harga. Tidak menaikkan quantity, menggeser SL/TP,
atau memakai leverage sebagai pengali budget loss. RR aktual tetap minimal 2.

Setup baru di `robot_setups` memakai `analysis-v7` dan
`deterministic-configurable-max-risk-v1`. Proof mengikat risk target, operation,
request body/output hash, levels immutable, quantity, serta snapshot sizing rules.
Approval memverifikasi ulang proof/provider levels/derived RR/quantity maksimum.
Provider prose, headers, secret dan raw context tidak disimpan pada tabel baru.
`api_requests` mempertahankan output terstruktur tervalidasi sesuai sistem lama.

V6 tidak dimigrasikan atau restamp. CLI `analysis-once`, legacy paper workflow
library dan `binance-shadow` tetap bisa membaca kontrak v6 untuk audit/preflight.
V7 menggunakan approval store terpisah dan belum diteruskan ke executor; tidak
ada promosi otomatis v6 menjadi v7 atau approved execution. Command CLI riset lama
masih explicit/manual untuk kompatibilitas; coordinator Office tidak memakainya.
`/neuroapi/run` sekarang mengevaluasi coordinator dan tidak membypass ROBOT OFF.
Legacy `OFFICE_AUTO_DRY_RUN` tidak lagi menjadi trigger di service.

## API privat

- GET `/robot/status`: read/control token, status + reconstructed setup cards.
- POST `/robot/settings`: control token + exact dashboard Origin, JSON bounded
  1024 byte, hanya `robot_on` (boolean) / `risk_target_usdt` (string decimal).
- POST `/robot/approval`: control token + exact Origin, hanya `setup_id` dan
  `decision` (`APPROVED`/`USER_REJECTED`). Duplicate retry tidak menggandakan approval.

Tidak ada endpoint arm/order/execution. Caddy hanya meneruskan path eksplisit.

## Deployment (tanpa aktivasi atau request NeuroAPI)

Di direktori repository VPS:

```sh
git pull --ff-only origin main
bash deploy/update-api.sh
```

Script build worker lalu recreate worker/proxy, memakai volume dan secret yang
sama. Tidak menjalankan screening/analysis, tidak reset ledger dan tidak memakai
`down -v`. Pada upgrade pertama default ROBOT OFF. Pada upgrade selanjutnya pilihan
ON/OFF persisten: matikan lewat Office dahulu jika ingin menghentikan riset selama
maintenance. Jangan mengaktifkan ROBOT ON sebelum siap memakai kuota riset.

Tests memakai account/provider sintetis serta browser UI lokal. Tidak membuktikan
service VPS sudah terpasang atau meminta NeuroAPI nyata. Google/DOM/browser login
lama tidak digunakan atau dipulihkan.
