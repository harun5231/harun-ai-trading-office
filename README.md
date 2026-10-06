# HARUN AI TRADING OFFICE

Dashboard kantor 3D dan worker privat Binance USD-M Futures untuk satu alur:

`ROBOT ON → account Binance → screening NeuroAPI → analisis → validasi risiko → OrderGateway.submit(intent) → Binance Futures`

**Sambungan pengiriman order belum diimplementasikan.** Ujung alur saat ini
berhenti dengan status `EXECUTION_BLOCKED` dan kode
`BINANCE_ORDER_GATEWAY_NOT_CONNECTED`; gateway menampilkan `NOT_CONNECTED`.
Entry, TP, dan SL belum terkirim. Developer manusia menyambungkan satu adapter
[`worker/order_gateway.py`](worker/order_gateway.py), sesuai kontrak dalam
[panduan integrasi](deploy/ORDER_INTEGRATION.md). Tidak ada transport trading
alternatif atau tombol yang dapat melewati adapter tersebut.

Coordinator memeriksa implementasi `submit` dan `reconcile` sebelum menyimpan
claim `SUBMITTING`. Metode yang sudah diimplementasikan dipanggil setelah
validasi alur, dengan status koneksi sebagai metadata Office. Field `connected`
tidak mengaktifkan atau menonaktifkan dispatch, dan tidak ada sakelar environment
pengiriman order. Keberadaan metode maupun status tidak membuktikan autentikasi
atau kesehatan exchange; developer harus menyelesaikan dan memverifikasi adapter.

## Office

Menu hanya memuat Robot Trading ON/OFF, Karyawan AI, Laporan, Aktivitas, dan
Riwayat Posisi. Saldo, posisi, dan riwayat akun berasal dari pembacaan Binance.
Riset dan kejadian worker ditampilkan sebagai aktivitas, bukan transaksi akun.
Animasi karyawan mengikuti status worker; browser dapat ditutup tanpa
menghentikan worker VPS.

Default robot OFF dan target risiko net ke SL 5 USDT, termasuk fee entry dan
fee exit SL memakai taker commission per symbol dari signed GET Binance.
Maksimal dua posisi bersamaan; jumlah entry per hari tidak dibatasi. Posisi manual mengurangi
slot; HYPEUSDT selalu manual-only. Jika HYPEUSDT merupakan satu-satunya posisi
aktif, screening meminta satu coin lain. Robot tidak mengambil alih posisi
manual atau memakai hasil analisis sebagai bukti adanya order Binance.

Pengaturan risiko tetap tersedia di panel Robot Trading, dengan nilai positif
sampai 100 USDT seperti sebelumnya. Perubahan hanya berlaku untuk analisis baru;
level, quantity, dan risiko intent yang sudah dibuat tetap mengikuti buktinya.

Worker memakai NeuroAPI Starter `smart`, data Binance 15m/1h nyata, serta aturan
kontrak dan commission akun terbaru. Model menentukan side dan harga Entry/TP/SL;
quantity ditentukan worker menggunakan Decimal agar estimasi loss SL termasuk
fee tidak melebihi target. Net RR sesudah fee minimal 2. Funding, slippage,
perubahan fee, dan gap harga dapat membuat loss nyata melampaui estimasi tersebut.
Entry tetap LIMIT, margin CROSS, dan leverage 75; worker tidak menggeser level.
Hanya HOLD meminta kandidat pengganti, maksimal tiga screening tambahan per cycle.
Request berbayar
memiliki claim persisten. Outcome yang belum pasti dihentikan untuk rekonsiliasi,
tanpa replay otomatis setelah restart.

## Deploy dan pengembangan

- [Deployment Docker dan secret VPS](deploy/DOCKER.md)
- [Operasi dan pemeriksaan 24/7](deploy/ROBOT_24_7.md)
- [Alur worker dan API privat](deploy/ROBOT_WORKFLOW.md)
- [Satu adapter order untuk developer](deploy/ORDER_INTEGRATION.md)

Diagnostics koneksi `python -m worker api-check` dan
`python -m worker binance-check` tetap tersedia. Keduanya hanya membaca koneksi
provider/akun, tanpa screening berbayar atau pengiriman order.

Worker memakai Python standard library; frontend Three.js diterbitkan melalui
GitHub Pages. Secret Binance, NeuroAPI, dan token worker tetap berada di VPS,
di luar Git dan dashboard. Docker menjaga volume yang sama, menjalankan service
sebagai UID/GID 10001, dan memulihkan proses melalui restart policy serta watchdog.

Upgrade pertama membuat backup audit privat dari ledger lama, menghapus tabel
alur lama dari database aktif, dan mengembalikan robot ke OFF. Backup tidak dibaca
oleh jalur runtime. Journal request berbayar serta bukti entry Binance yang
terkonfirmasi dipertahankan. Tidak ada promosi setup lama menjadi order.

Upgrade alur fee-inclusive memakai namespace `robot-v9` dan `analysis-v9`.
Schema version 2 mengizinkan cycle baru pada hari/epoch yang sama dengan cycle
lama sambil mempertahankan semua baris lama, settings ON/OFF, journal, intent,
receipt, dan observasi exposure. Bukti risiko harga tanpa fee lama tidak dipromosikan;
order dengan outcome belum pasti tetap memblokir sampai direkonsiliasi.

Pengujian lokal menggunakan fixtures tanpa key atau uang nyata. Test lulus
tidak membuktikan adapter Binance sudah tersambung. Status default `NOT_CONNECTED`
mencerminkan adapter bawaan yang belum memiliki transport, rekonsiliasi, dan proteksi.
