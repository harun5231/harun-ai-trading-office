# Operasi 24/7: simulasi lokal + tiket manual

Worker VPS melakukan polling riset saat ROBOT ON, membaca account Binance melalui
GET, dan menyimpan setup serta simulasi pada volume Docker. Pengiriman entry/TP/SL
tetap dilakukan pengguna di Binance. GitHub Pages hanya dashboard; browser boleh
ditutup tanpa menghentikan worker VPS.

## Update di Termius

Perubahan harus sudah digabungkan ke `main`. Buka direktori proyek, periksa
`git status --short`, dan rekonsiliasi perubahan lokal bila ada. Jalankan:

```sh
cd /root/harun-ai-trading-office
git status --short
git fetch origin &&
git switch main &&
git pull --ff-only origin main &&
git log -1 --oneline &&
bash deploy/update-api.sh &&
docker compose exec --user 10001:10001 -T worker python -m worker.health
```

Jangan reset Git paksa, menghapus lock, atau menghapus volume. Update melakukan
build sebelum menghentikan worker, menunggu operasi berjalan menyimpan hasil,
lalu menjalankan container dengan volume dan secret existing. Pilihan ON/OFF
tersimpan: jika sebelumnya ON, riset dapat berlanjut setelah start. Pilih OFF di
Office sebelum maintenance bila riset berbayar harus berhenti selama update.
Tidak ada rollback image otomatis karena ledger persistent dapat sudah berubah.

Bootstrap memperbaiki izin direktori trading serta file state bernama yang
dikenali, termasuk lock dan SQLite, tanpa mengganti inode atau menghapus data.
Symlink, hardlink, dan file bukan regular ditolak. Gunakan UID10001 untuk semua
perintah pemeriksaan; Docker exec default dapat memakai root.

## Pemeriksaan singkat tanpa riset berbayar

```sh
docker compose ps
docker compose exec --user 10001:10001 -T worker python -m worker.robot_status
systemctl is-active docker
systemctl is-enabled docker
timedatectl show -p NTPSynchronized --value
```

`robot_status` hanya memakai token baca privat untuk GET localhost existing.
Output tidak memuat token, key provider, raw response, atau teks exception.
Perintah tidak menjalankan screening, simulasi, approval, maupun order.
Jalur GET status membuka SQLite dalam mode baca, tanpa inisialisasi schema.

Periksa UID/GID10001, `health: ONLINE`, `state_writable: true`, dan lock existing
`REGULAR`, owner10001, mode0600, `writable: true`. `cycle.lock: MISSING` normal
sebelum tick pertama. Provider `NEUROAPI_UNCHECKED` tidak berarti worker mati.
`checked_at` adalah waktu coordinator, sedangkan `account_checked_at` adalah
waktu pembacaan account; keduanya mempunyai arti berbeda.

Jika Docker tidak enabled, aktifkan sesuai kebijakan VPS agar container kembali
setelah reboot. Worker dan proxy memakai `restart: unless-stopped`; container
yang sengaja dihentikan membutuhkan `docker compose up -d` untuk dijalankan lagi.
Watchdog menghentikan worker bila thread/heartbeat macet sehingga restart policy
dapat memulihkan proses. Docker `healthy` membuktikan proses berjalan, belum
membuktikan screening provider berhasil.

## Verifikasi dari Office

1. Buka https://harun5231.github.io/harun-ai-trading-office/ dan hubungkan worker
   HTTPS dengan token kontrol existing. Secret Binance/NeuroAPI tetap di VPS.
2. Pastikan risk target **5 USDT**. Pilih ROBOT ON saat siap memakai kuota riset
   NeuroAPI existing. ON membangunkan coordinator; riset selanjutnya dipoll setiap
   45 detik, maksimum satu operasi provider per tick. Account dipoll sekitar
   15 detik saat ON.
3. Jika hanya HYPEUSDT manual yang terbuka, harapkan posisi1/2, slots1,
   `manual_exposure: [HYPEUSDT]`, lalu screening **satu coin**, ANALYZING, dan
   SETUP_READY. HYPEUSDT tidak boleh menjadi target tiket. Waktu provider/retry
   dapat membuat satu operasi berjalan beberapa menit.
4. Periksa tiket immutable: Entry LIMIT, TP, SL, quantity, actual risk≤5,
   RR≥2, `execution_mode: MANUAL_ONLY`, `submission_enabled: false`.
   Account review harus menampilkan snapshot account terbaru yang tersedia,
   termasuk bila posisi manual baru dibuka selama analisis.
5. Pilih **FULL_SL → UJI SIMULASI**. Hasil lokal harus SIMULATION, order nyata
   false, PnL negatif sebesar actual risk. Mengulang skenario memakai hasil
   persisten yang sama; saldo, posisi nyata, dan counter entry tidak berubah.
6. **SALIN TIKET** menyalin parameter review. OK menyimpan APPROVED. Tinjau
   ulang harga, rules, exposure, margin/leverage dan proteksi di Binance sebelum
   mengirim manual. Simulasi bukan bukti fill/proteksi exchange.

## WAITING dan batas mode ini

| Alasan/status | Tindakan |
| --- | --- |
| ROBOT_OFF | Pilih ON bila ingin mengizinkan riset berbayar. |
| ROBOT_CAPACITY_FULL | Tunggu kapasitas; posisi manual tetap dihitung. |
| SCREENING_COMPLETE_ANALYSIS_PENDING | Tunggu tick analisis berikutnya. |
| SCREENING_REPLACEMENT_REQUIRED | Tunggu replacement yang dibatasi kuota. |
| SETUP_READY / APPROVED / ROBOT_CYCLE_COMPLETE | Review tiket existing; ini dapat menjadi penantian normal. |
| ROBOT_CYCLE_LOCK_UNAVAILABLE | Periksa izin lock/bootstrap dan versi yang terpasang. |
| ROBOT_COORDINATOR_UNAVAILABLE | Periksa diagnostics/log worker; actor melaporkan error tetap tanpa payload rahasia. |
| ROBOT_LIFECYCLE_NEEDS_REVIEW / ROBOT_REQUEST_NEEDS_REVIEW | Review journal/lifecycle; outcome belum pasti tidak dikirim ulang otomatis. |
| Binance/account failure | Perbaiki koneksi, izin account, IP atau waktu sesuai kode yang tampil. |

Worker tetap berjalan 24/7, tetapi setup unresolved/APPROVED menahan riset baru
sesuai batas coordinator. Menjalankan simulasi atau mengirim manual tidak
menghasilkan receipt execution yang ditulis robot. Setup tidak expired otomatis,
approval final, dan tidak ada reset harian terhadap blocker tersebut. Ini
mencegah penambahan paid request hanya karena polling/restart. Mode ini belum
merupakan executor trading otomatis.

Jika status gagal, pemeriksaan journal tidak mengirim request baru:

```sh
docker compose exec --user 10001:10001 -T worker python -m worker diagnostics
docker compose logs --since 10m --tail 80 worker proxy
```

Jangan gunakan `api-dry-run`, `screening-once`, atau `analysis-once` sebagai health
check: command tersebut dapat memakai kuota dan mengubah state. Jangan membagikan
file secret atau token. Hasil tes lokal/deployment Pages tidak membuktikan worker
VPS sudah diperbarui; verifikasi output di Termius setelah update.
