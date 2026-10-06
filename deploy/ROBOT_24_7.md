# Operasi 24/7

Worker berjalan di VPS, terpisah dari browser Office. Menu Robot Trading
mengontrol ON/OFF satu coordinator. ON membangunkan riset; polling berikutnya
setiap 45 detik, maksimum satu operasi NeuroAPI per tick. Account Binance
dipoll terpisah sehingga request analisis tidak menahan pembaruan saldo/posisi.
OFF menghentikan operasi baru; request yang telah dikirim masih dapat
menyelesaikan penyimpanan hasil.

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

Jika hanya HYPEUSDT manual aktif, harapkan posisi1/2, slot1, dan HYPEUSDT berada
dalam manual exposure. Symbol manual tidak diambil alih. Laporan dan riwayat
akun berasal dari Binance; aktivitas riset tidak dicatat sebagai fill akun.

Pada build ini ujung pipeline berstatus `EXECUTION_BLOCKED` dengan kode
`BINANCE_ORDER_GATEWAY_NOT_CONNECTED`; gateway menampilkan `NOT_CONNECTED`.
Worker dapat membaca akun, screening, analisis, dan validasi setup;
entry/TP/SL belum terkirim. Developer menyelesaikan satu adapter pada
[ORDER_INTEGRATION.md](ORDER_INTEGRATION.md). Restart tidak mengubah status
tersebut menjadi order dan tidak mereplay operasi dengan outcome belum pasti.
