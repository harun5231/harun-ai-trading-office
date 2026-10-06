# Coordinator riset 24/7 + approval (tanpa execution)

Build ini **tidak dapat mengirim order Binance**. Transport production tetap
GET-only, binance-arm/execute tidak bisa diaktifkan, live scheduler OFF. ROBOT ON
hanya mengizinkan riset NeuroAPI berbayar dan pembuatan setup untuk review.

## Office

MENU → ROBOT TRADING memakai koneksi worker/token kontrol yang sama dengan panel
NeuroAPI. Token hanya berada di memori tab; secret provider tetap di VPS.

- Default instalasi: ROBOT OFF, risk target 5 USDT. ON/OFF dan risk disimpan di
  SQLite privat pada volume worker. Restart tidak mengubah pilihan.
- Perubahan OFF → ON segera membangunkan coordinator. Pembacaan account
  terpisah mulai mengikuti pilihan ON pada poll berikutnya.
  Operasi yang sudah berjalan diselesaikan dahulu; ON tidak menambah actor atau
  melewati claim persistent.
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

Setelah wake saat ON, poll coordinator setiap 45 detik; maksimum satu operasi
NeuroAPI per tick. Ini jadwal riset dan approval, bukan jadwal eksekusi live.
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
Pengecualian recovery: cycle lama ACTIVE/COMPLETE tanpa setup, tanpa antrean dan dengan
screening tercatat boleh mencari replacement selama batas tiga replacement belum
tercapai. Claim/request lama tetap dipertahankan dan tidak dikirim ulang.

Screening memakai literal 1 coin untuk satu slot, literal lama 2 coin untuk dua
slot. Schema memaksa jumlah persis, unique uppercase USDT; catalog exchangeInfo
harus active USD-M perpetual. Tidak menambahkan suffix atau memperbaiki symbol.
HOLD/TIDAK memicu replacement sesuai slot yang tersisa, maksimal tiga screening
replacement per cycle. HYPEUSDT, symbol yang sedang terbuka dan kandidat yang
sudah dipakai dikecualikan. Jika screening dua coin mengandung HYPEUSDT dan satu
kandidat valid, kandidat valid tetap dianalisis dan slot tersisa dapat mencari
replacement dengan batas yang sama. Jika tidak ada kandidat eligible, replacement
juga dibatasi tiga; setelah habis status INSUFFICIENT_ACTIONABLE_SETUPS.
Symbol yang sudah dianalisis tidak diulang. Technical REJECT bukan alasan
replacement. Unknown/pending paid request menghentikan cycle
untuk review, bukan dikirim ulang setelah restart. Retry provider tetap 429/503
bounded sesuai implementasi lama.

GET `/robot/status` menyertakan `wait_reason` agar WAITING dapat dibedakan:
ROBOT OFF, kapasitas penuh, menunggu analisis/replacement pada tick berikutnya,
atau lifecycle lama yang perlu review. Menunggu tick berikutnya adalah alur normal.
Blocker lifecycle tidak menghapus model/proof lama dan tidak mengirim order.
Kegagalan account/request tetap dilaporkan dengan kode kegagalan yang aman.

| `wait_reason` | Arti |
| --- | --- |
| `ROBOT_OFF` | Riset dinonaktifkan. |
| `ROBOT_CAPACITY_FULL` | Tidak ada slot posisi atau entry harian tersisa. |
| `SCREENING_COMPLETE_ANALYSIS_PENDING` | Screening berhasil; analisis pada tick berikutnya. |
| `SCREENING_REPLACEMENT_REQUIRED` | Kandidat dikecualikan; replacement pada tick berikutnya selama batas tersedia. |
| `ROBOT_CYCLE_COMPLETE` | Kesempatan riset ini selesai atau menunggu setup/approval tersimpan. |
| `ROBOT_LIFECYCLE_NEEDS_REVIEW` | Model lifecycle lama unresolved; blocker memerlukan review. |
| `ROBOT_STOPPING` | Worker berhenti; tidak menerima request riset baru. |
| `ROBOT_DAY_CHANGED` | Hari bisnis berubah sebelum operasi baru diklaim. |

Panel robot membaca wallet dan available balance USDT nyata dari private GET.
Pembacaan account berjalan terpisah dari actor riset, sekitar setiap 15 detik
saat ON, sehingga hasil saldo tidak menunggu request NeuroAPI selesai. Kegagalan
account mengosongkan saldo dan melaporkan alasan; snapshot paper tetap terpisah.

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

## Deployment dan maintenance

Di direktori repository VPS:

```sh
git pull --ff-only origin main
bash deploy/update-api.sh
```

Script build worker lalu recreate worker/proxy, memakai volume dan secret yang
sama. Pada SIGTERM worker menghentikan claim/request riset baru dan memberi operasi
yang sedang berjalan hingga 600 detik untuk menyimpan hasil. Compose dan script
stop memakai grace 660 detik. Bila proses tetap terputus sebelum hasil pasti,
journal PENDING/NEEDS_REVIEW tetap memblokir riset untuk review setelah restart.
Script tidak mengaktifkan ROBOT atau reset ledger dan tidak memakai `down -v`.
Pada upgrade pertama default ROBOT OFF. Pada upgrade selanjutnya pilihan
ON/OFF persisten: matikan lewat Office dahulu jika ingin menghentikan riset selama
maintenance; jika tetap ON, riset otomatis dapat berlanjut setelah worker baru
boot. Jangan mengaktifkan ROBOT ON sebelum siap memakai kuota riset.

Health memeriksa thread dan umur heartbeat actor/account. Watchdog menghentikan
proses bila actor tidak memberi heartbeat selama 600 detik atau pembacaan account
selama 120 detik; `restart: unless-stopped` kemudian menjalankan worker kembali.
Heartbeat memakai jam monotonic agar koreksi jam sistem tidak menyebabkan restart
palsu. Restart tidak mereplay request dengan outcome belum pasti. Gangguan monitor
paper tidak mematikan actor riset.

Tests memakai account/provider sintetis serta browser UI lokal. Tidak membuktikan
service VPS sudah terpasang atau meminta NeuroAPI nyata. Google/DOM/browser login
lama tidak digunakan atau dipulihkan.
