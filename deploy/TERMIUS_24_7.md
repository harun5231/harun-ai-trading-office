# Panduan Termius untuk worker robot

Termius hanya klien SSH. VPS menjalankan worker, account reader, journal,
NeuroAPI, serta adapter Binance. GitHub Pages menjalankan tampilan Office;
menutup browser/Termius tidak menghentikan container.

Gunakan [alur robot](ROBOT_WORKFLOW.md), [TP/SL](TP_SL_AUTOMATIC.md), dan
[kontrak adapter](ORDER_INTEGRATION.md) sebagai aturan current. Source gateway
produksi berada di VPS; gateway publik bawaan adalah stub.

## Sebelum update

Pilih Robot Trading OFF di Office. OFF memblokir provider request serta Binance
mutation baru, tetapi tidak close/cancel posisi maupun order existing/manual.
Pending entry masih bisa fill di Binance. Hasil request yang sudah terkirim
boleh dijournal; jangan mengganti ledger, kuota, atau receipt.

Periksa instalasi existing melalui satu blok read-only:

```bash
cd /root/harun-ai-trading-office
git --no-optional-locks status --short
docker compose -f compose.yaml ps
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker diagnostics
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker binance-check
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker api-check
```

Gunakan UID/GID 10001 agar command tidak membuat state milik root. Jangan
menampilkan `.env`, file secret, signed query, atau API key. Token dashboard
adalah token worker, bukan key Binance.

## Pemasangan revisi alur

Pasang paket current yang source/pin-nya telah ditinjau untuk versi VPS yang
sama. Revisi diverifikasi di VPS dahulu; GitHub menyusul setelah bukti akhir.
Jangan mengambil atau menjalankan installer/seed/settlement historis agar status
menjadi ON atau slot terbuka. Jangan memakai `git reset --hard`, menghapus
volume/ledger, atau menimpa adapter lokal untuk mengatasi source mismatch.

Installer memeriksa source host/container, OFF, journal, request/job, dan
inventaris Binance dengan GET, termasuk pending entry manual. Backup source,
image, dan journal privat dibuat sebelum perubahan. SDK diperbarui terarah,
fungsi/config/kredensial privat lain dipertahankan. Image dibuild dan diperiksa
sebelum worker diganti; settings tetap OFF setelah restart/verifikasi.

Source/image rollback tidak mengembalikan financial journal ke backup lama.
Cleanup source/command historis sesudah bukti akhir memakai allowlist dan
fingerprint; file asing/berubah dipertahankan. Financial state, secret, receipt,
backup, manual orders, pending entries, serta fitur Office tidak dihapus.

Untuk kebutuhan build/dependency adapter existing, lihat
[EXISTING_GATEWAY_BUILD.md](EXISTING_GATEWAY_BUILD.md). Panduan Docker standar
ada di [DOCKER.md](DOCKER.md); jangan menjalankan update Git standar terhadap
checkout dengan SDK privat yang belum direkonsiliasi.

## Sesudah pemasangan

Jalankan kembali `worker.robot_status`. Pastikan health ONLINE, robot OFF,
account tersambung, source sesuai, dan tidak ada failure baru. `CONFIGURED`,
source match, serta build/test lulus belum membuktikan penerimaan order.
Jika ingin ON, pemilik menyalakannya dari menu existing setelah memeriksa
open orders/posisi Binance dan aturan di bawah.

- Dua first fill bot per hari WIB dan dua occupancy, termasuk posisi/pending
  manual/bot. Posisi carryover/pending lama tetap memakai kapasitas.
- Risk default 5 USDT editable di menu existing, fee fresh + reserve exit 0,5%.
  Reserve tidak menjamin loss aktual maksimal 5 USDT ketika gap/slippage lebih
  besar, funding, perubahan fee, atau liquidation terjadi.
- HOLD/RR di bawah dua meminta coin berbeda, maksimum tiga putaran pengganti.
  Setup minimal RR 2 memakai TP normalisasi net 1:2; entry/SL tetap dari model.
- Entry LIMIT GTC CROSS 75×. Actual fill mendapat SL kemudian TP market,
  pemicu Terakhir, 100% exposure bot, dengan bukti GET kedua proteksi.
- Unknown outcome memblokir; posisi nol bukan bukti tidak ada LIMIT pending.
  Jangan mengirim ulang, menghapus claim, atau mereset counters.

Tick coordinator sekitar 45 detik. Bedakan status fresh dengan snapshot lama;
polling 30 detik belum selalu mencakup tick berikutnya. Laporan `PARTIAL`
berarti history belum lengkap. Browser membaca status cache dan tidak menjadi
mesin trading atau jalur submission kedua.

## Koneksi dashboard

Origin HTTPS worker adalah domain VPS HTTPS yang dilayani Caddy, bukan URL
GitHub Pages. Origin dashboard adalah `https://harun5231.github.io`; path Office
berada pada `/harun-ai-trading-office/`. Isi control token worker pada panel
koneksi existing. Token hanya berada dalam memori tab; key provider/Binance
tetap di VPS. API hanya menerima GET health/status dan POST settings dengan
control token serta exact Origin. Tidak ada endpoint manual submit order.
