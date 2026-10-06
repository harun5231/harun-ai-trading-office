# Operasi 24/7

Worker berjalan di VPS, terpisah dari browser Office. Menu Robot Trading
mengontrol ON/OFF satu coordinator. ON membangunkan riset; polling berikutnya
setiap 45 detik, maksimum satu operasi NeuroAPI per tick. Account Binance
dipoll terpisah sehingga request analisis tidak menahan pembaruan saldo/posisi.
OFF menghentikan riset, submission baru, dan pemanggilan rekonsiliasi gateway
berikutnya. OFF tidak menutup posisi atau membatalkan order entry/SL/TP.
Request eksternal yang telah dikirim tidak dapat ditarik kembali dan masih
dapat menyelesaikan penyimpanan hasil. Polling read-only saldo/posisi/riwayat
Office tetap berjalan, sementara seluruh karyawan AI tidak menunjukkan aktivitas
kerja saat OFF.

## Update dari Termius

Gunakan langkah di [DOCKER.md](DOCKER.md#update-instalasi-existing). Updater tidak
menghapus volume atau secret. Migrasi pertama mematikan ROBOT; upgrade berikutnya
mempertahankan pilihan ON/OFF. Aktifkan ON hanya ketika siap memakai kuota riset.

## Pemeriksaan tanpa request riset

```sh
cd /root/harun-ai-trading-office
docker compose ps
docker compose exec --user 10001:10001 -T worker python -m worker.robot_status
systemctl is-active docker
systemctl is-enabled docker
timedatectl show -p NTPSynchronized --value
```

Diagnostics memakai GET localhost existing dengan token privat. Output tidak
memuat secret atau raw exception. Periksa UID/GID10001, `health: ONLINE`, state
writable, serta lock regular owner10001 mode0600. Lock yang belum ada normal
sebelum tick pertama. Waktu coordinator dan pembacaan account memiliki arti
berbeda; saldo/posisi harus mengikuti observasi account terbaru.

Pemeriksaan key/koneksi provider tetap tersedia melalui GET tanpa request riset:

```sh
docker compose exec --user 10001:10001 -T worker python -m worker api-check
docker compose exec --user 10001:10001 -T worker python -m worker binance-check
```

Pengaturan risiko masih tersedia di panel Robot Trading; default net 5 USDT,
termasuk fee entry dan exit SL memakai taker commission akun per symbol.
Perubahan hanya dipakai analisis baru. Net RR minimal 2 setelah fee; funding,
slippage, perubahan fee, atau gap dapat membuat hasil nyata berbeda.
URL Office lama diarahkan ke halaman utama
yang sama, sehingga bookmark lama tetap menuju dashboard terbaru.

Jika hanya HYPEUSDT manual aktif, harapkan posisi1/2, slot1, dan HYPEUSDT berada
dalam manual exposure. Symbol manual tidak diambil alih. Laporan dan riwayat
akun berasal dari Binance; aktivitas riset tidak dicatat sebagai fill akun.
Maksimal dua entry bot baru per hari WIB (UTC+7) dan dua posisi bersamaan,
termasuk posisi manual serta posisi kemarin. Kapasitas mengikuti nilai terkecil
dari sisa kuota entry harian dan slot bersamaan. Semua `ENTRY_PENDING` belum fill
mencadangkan keduanya, termasuk order kemarin yang masih mungkin fill hari ini.
Partial fill pertama yang terverifikasi mencatat satu receipt pada hari WIB
fill pertama; tambahan fill entry yang sama tidak dihitung ulang. Journal dan
receipt persisten menjaga batas tersebut sesudah restart.

Pergantian hari pukul 00:00 WIB memperbarui kuota tanpa menutup posisi lama.
Satu posisi kemarin yang masih aktif menyisakan paling banyak satu slot saat
itu; dua posisi kemarin menyisakan nol sampai slot bebas. Pending lama yang
belum fill tetap mencadangkan kuota hari yang baru.
Posisi yang selesai membebaskan slot bersamaan, tanpa mengembalikan kuota entry
yang sudah dipakai hari itu.
Penggantian kandidat hanya berasal dari HOLD, maksimal
tiga screening tambahan setelah screening awal.

Upgrade namespace `robot-v9`/`analysis-v9` mempertahankan cycle dan journal lama,
order unknown, receipt, pengaturan, serta exposure. Schema version 2 menghapus
constraint hari/epoch yang menghalangi cycle baru; bukti lama tidak menjadi
order fee-inclusive secara otomatis.

Pada build ini ujung pipeline berstatus `EXECUTION_BLOCKED` dengan kode
`BINANCE_ORDER_GATEWAY_NOT_CONNECTED`; gateway menampilkan `NOT_CONNECTED`.
Worker dapat membaca akun, screening, analisis, dan validasi setup;
entry/TP/SL belum terkirim. Developer menyelesaikan satu adapter pada
[ORDER_INTEGRATION.md](ORDER_INTEGRATION.md). Restart tidak mengubah status
tersebut menjadi order dan tidak mereplay operasi dengan outcome belum pasti.
