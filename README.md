# HARUN AI TRADING OFFICE

Dashboard kantor 3D dan worker privat Binance USD-M Futures untuk satu alur:

`ROBOT ON → account Binance → screening NeuroAPI → analisis → validasi risiko → OrderGateway.submit(intent) → Binance Futures`

**Gateway bawaan belum memiliki sambungan pengiriman order.** Ujung alur instalasi bawaan
berhenti dengan status `EXECUTION_BLOCKED` dan kode
`BINANCE_ORDER_GATEWAY_NOT_CONNECTED`; gateway menampilkan `NOT_CONNECTED`.
Entry, TP, dan SL belum terkirim. Developer manusia menyambungkan satu adapter
[`worker/order_gateway.py`](worker/order_gateway.py), sesuai kontrak dalam
[panduan integrasi](deploy/ORDER_INTEGRATION.md). Tidak ada transport trading
alternatif atau tombol yang dapat melewati adapter tersebut.

Untuk adapter existing yang hanya berada di VPS, tersedia
[perbaikan terarah berdasarkan source yang telah diaudit](deploy/VPS_ORDER_FIX.md).
Prosedur mempertahankan helper dan file lain, mengganti hanya class gateway
terverifikasi, dan memperbarui lima file coordinator tanpa pull/reset proyek.
Template deployment tidak dipilih sebagai executor runtime lain. Pengujian
offline belum membuktikan penerimaan entry atau SL/TP oleh Binance produksi.

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
Maksimal dua entry bot baru per hari WIB (UTC+7) dan dua posisi bersamaan,
termasuk posisi manual serta posisi yang terbawa dari hari sebelumnya.
Entry `ENTRY_PENDING` mencadangkan kuota harian dan kapasitas, termasuk yang
dibuat kemarin tetapi masih mungkin fill hari ini. Partial fill dihitung sebagai
satu entry pada hari WIB dari fill pertama yang terverifikasi; receipt tetap
tersimpan setelah restart. Posisi manual mengurangi slot; HYPEUSDT selalu
manual-only. Jika HYPEUSDT merupakan satu-satunya posisi
aktif, screening meminta satu coin lain. Robot tidak mengambil alih posisi
manual atau memakai hasil analisis sebagai bukti adanya order Binance.

Kapasitas adalah nilai terkecil dari sisa kuota harian dan sisa slot bersamaan.
Pergantian hari pada pukul 00:00 WIB memperbarui kuota entry, tanpa menutup posisi
lama. Satu posisi lama yang masih aktif menyisakan paling banyak satu slot saat
itu; dua posisi lama menyisakan nol sampai salah satunya selesai. Posisi yang
selesai membebaskan slot bersamaan, tanpa mengembalikan kuota entry hari itu.

ROBOT OFF menghentikan riset, submission, dan rekonsiliasi gateway berikutnya.
OFF tidak menutup posisi atau membatalkan order entry/SL/TP. Request
eksternal yang sudah dikirim tidak dapat ditarik kembali; hasilnya masih dapat
selesai dan dicatat ke journal. Callback adapter yang sudah dimulai juga dapat
menyelesaikan proteksinya; guard GET riset tidak menginterupsinya. Semua karyawan
AI berhenti menunjukkan aktivitas
kerja saat OFF, sementara pembacaan saldo, posisi, dan riwayat tetap tersedia
di Office.

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
- [Panduan Termius untuk VPS existing dan adapter Futures 24/7](deploy/TERMIUS_24_7.md)
- [Audit source container sebelum ROBOT ON tanpa mengubah adapter VPS](deploy/AUDIT_BEFORE_ON.md)
- [Perbaikan adapter VPS dan coordinator dengan backup, tanpa mengganti fungsi lain](deploy/VPS_ORDER_FIX.md)
- [Pemulihan journal riset lama dengan backup, tanpa replay atau perubahan adapter](deploy/LEGACY_RESEARCH_RECOVERY.md)
- [Pemulihan entry yang tidak ditemukan di Binance dan diagnostics callback](deploy/UNKNOWN_ORDER_RECOVERY.md)
- [Pemulihan satu slot setelah penolakan entry yang telah diverifikasi](deploy/ONE_SLOT_RECOVERY.md)
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
