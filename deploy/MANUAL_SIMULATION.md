# Tiket manual dan simulasi lokal

Build ini membuat tiket review dari setup NeuroAPI v7 terverifikasi dan menjalankan
contoh lifecycle lokal. Tidak ada pengiriman entry, TP atau SL ke Binance.
Tidak perlu API key baru untuk simulasi; screening/analysis NeuroAPI tetap
memerlukan key provider yang sudah dipasang dan dapat memakai kuota berbayar.

## Penggunaan di Office

1. Hubungkan Office ke worker HTTPS memakai token kontrol existing.
2. SETUP_READY atau APPROVED yang proof-nya valid menampilkan tiket entry LIMIT,
   side, quantity, TP, SL, CROSS/75x target, risk target, actual risk dan RR.
   Default risk target adalah 5 USDT. Quantity dan level berasal dari proof yang
   diverifikasi ulang; simulasi dan approval tidak mengubah parameter tersebut.
3. Periksa tanggal setup, account review, slot tersisa dan exposure simbol.
   **SALIN TIKET** hanya menyalin teks. Tinjau ulang kondisi pasar, rules kontrak,
   margin/leverage, quantity, TP/SL dan posisi di Binance sebelum mengirim manual.
   Posisi manual termasuk HYPEUSDT tetap memakai kapasitas dan tidak dimutasi.
4. Pilih skenario lalu **UJI SIMULASI**. Jejak hasil ditandai SIMULASI SINTETIS.
   Menjalankan simulasi tidak memerlukan ROBOT ON dan tidak meminta riset baru.

| Skenario | Contoh hasil lokal |
| --- | --- |
| `FULL_TP` | Entry penuh, proteksi TP/SL sintetis, keluar pada TP. |
| `FULL_SL` | Entry penuh, proteksi TP/SL sintetis, keluar pada SL. |
| `PARTIAL_TP` | Sebagian entry terisi, sisa entry dibatalkan, keluar pada TP. |
| `PROTECTION_FAILURE` | Entry terisi tetapi proteksi gagal; model memerlukan review. |
| `UNCERTAIN_ENTRY` | Outcome entry tidak pasti; model berhenti untuk review. |

`PARTIAL_TP` memakai kelipatan lot legal dan ditolak jika quantity tidak bisa
dibagi menjadi partial fill legal; simulator tidak membuat quantity fiktif.

Fill, acknowledgment SL/TP dan sibling cancellation pada jejak adalah bukti
**sintetis**, bukan receipt exchange. PnL model tidak mencakup biaya, funding,
slippage atau gap harga dan tidak membuktikan profit/proteksi nyata.
Simulasi tidak mengubah saldo/posisi/PnL akun, ledger paper, approval ataupun
`robot_entry_receipts`/counter harian. Hasil gagal/tidak pasti hanya status model.
Simulasi tidak menyelesaikan approval: setup unresolved/APPROVED tetap menahan
riset berbayar sesuai coordinator existing.

## API dan persistensi

POST `/robot/simulation` memakai bearer control token, exact dashboard Origin,
JSON maksimum 1024 byte, hanya `setup_id` dan `scenario`. Server menerima hanya
SETUP_READY/APPROVED terverifikasi lalu membangun ulang tiket dari evidence privat;
client tidak dapat memasukkan quantity, level atau proof pengganti. Route tidak
memanggil Binance/NeuroAPI.

Hasil tersimpan pada `robot_simulations`, unik per `(setup_id, scenario,
ticket_sha256)`. Mengulang skenario yang sama memakai hasil deterministik yang
sama dan memperbarui waktu permintaan terakhir, tanpa record/receipt ganda.
GET `/robot/status` memuat tiket review, account review dan simulasi terbaru yang
diverifikasi ulang. Restart mempertahankan hasil pada volume existing.

## Update worker VPS dan frontend

Matikan ROBOT lewat Office terlebih dahulu jika riset tidak boleh berlanjut selama
maintenance. Di Termius, setelah perubahan branch tersedia di GitHub:

```sh
cd /root/harun-ai-trading-office
git status --short
git fetch origin &&
git switch main &&
git pull --ff-only origin main &&
bash deploy/update-api.sh &&
docker compose ps &&
docker compose exec --user 10001:10001 -T worker python -m worker.health
```

Jika Git menolak karena perubahan lokal atau history berbeda, berhenti dan
rekonsiliasi perubahan tersebut. Jangan memakai reset paksa atau menghapus volume.
Worker/proxy harus healthy; itu belum membuktikan riset provider berhasil.

Dashboard adalah file statis GitHub Pages (`index.html` dan `assets/`); update
worker Docker tidak menerbitkan JavaScript baru. Checkout ini tidak mempunyai
file workflow Pages; deployment Pages yang dikelola GitHub telah diperiksa
berhasil untuk frontend kantor di main. Perubahan frontend berikutnya masih
memerlukan deployment Pages berhasil setelah merge.
Refresh penuh/reopen Office setelah publish dan pastikan SALIN TIKET serta
UJI SIMULASI muncul pada setup terverifikasi. Token tetap hanya di memori tab.

Pengujian lokal tidak mengklaim bahwa versi ini sudah dipasang di VPS, frontend
sudah dipublikasikan, atau screening NeuroAPI akun pengguna sudah diuji.

Untuk worker 24/7, perintah status singkat, dan arti WAITING, gunakan
[ROBOT_24_7.md](ROBOT_24_7.md).
