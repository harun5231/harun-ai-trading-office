# Pemeriksaan sebelum ROBOT ON pada VPS existing

Adapter produksi tersimpan di VPS. Source GitHub, health, dan label
`CONNECTED`/`CONFIGURED` tidak membuktikan entry diterima, fill, atau proteksi
SL/TP. Cocokkan source host dan container, journal, settings, serta bukti GET
Binance sebelum menggunakan revisi alur.

Pertahankan ROBOT OFF melalui menu Office. Perbaikan harus diverifikasi di VPS
lebih dahulu; publikasi GitHub dan cleanup source historis mengikuti bukti
akhir. Adapter privat, financial journal, receipt, secret, backup, volume, dan
order/posisi existing maupun manual harus dipertahankan.

## Inspeksi source melalui Termius

Collector yang sudah ada di checkout membaca source secara statis. Ia tidak
mengimpor adapter, mengirim API request, mengubah settings, atau menghapus file.
Jalankan dari Termius:

```bash
cd /root/harun-ai-trading-office
git --no-optional-locks status --short
sha256sum worker/order_gateway.py
docker compose -f compose.yaml ps
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - < deploy/inspect_vps_gateway.py
```

Blok ini tidak mengambil atau memasang source baru. Collector membaca `enabled`
dengan SQLite read-only dan berhenti bila OFF tidak terbukti. Python memakai
`-I -B -S`; gateway diparse sebagai AST, komentar dibuang, dan literal selain
protokol publik disamarkan. Tidak ada pembacaan `.env`, credential environment,
atau file secret. Jangan sertakan API key atau token saat membagikan output.

Output dibatasi 64 KiB dan memuat `robot_on:false`, `gateway.sha256`, source
tersamarkan, serta `workflow_sha256`/`workflow_status`. Bandingkan hash gateway
dengan host. Perbedaan menunjukkan source host/container belum sama; `MISSING`
dapat menunjukkan image lama. Hash atau static inspection belum membuktikan
perilaku exchange. `ROBOT_OFF_REQUIRED` berarti OFF belum teramati oleh collector.

## Aturan versi yang dipasang

| Bagian | Perilaku yang harus dibuktikan |
| --- | --- |
| Kapasitas | Maksimal dua occupancy; gabungan symbol posisi manual/carryover, pending entry manual/bot, dan reservasi intent dihitung sekali. Reducing exits bukan entry. HYPEUSDT manual-only; semua order manual tetap utuh. |
| Kuota WIB | Maksimal dua first fill bot per hari UTC+7; pending bot mencadangkan kuota. Partial/additional fill pada entry sama tetap satu receipt. Close tidak mengembalikan kuota hari itu. |
| Screening | Satu atau dua coin sesuai slot, USD-M USDT perpetual aktif, tanpa seen/exposed/pending/manual-only symbols. Analisis memakai candle Futures fresh 15m/1h serta commission per symbol. |
| Pengganti | HOLD dan RR di bawah dua sebelum plan/intent berbagi maksimal tiga putaran pengganti. Hanya putaran terakhir eligible. Terminal HTTP 422 satu attempts, tanpa output/plan/intent dapat mengikuti batas sama. Unknown tidak memberi izin replay/reset. |
| TP V3 | Entry/SL model dipertahankan; declared RR dan jarak harga minimal 1:2. TP di atas dua dinormalisasi ke net 1:2 pada tick legal terdekat setelah fee/reserve, dengan TP model asli dan bukti normalisasi tersimpan. |
| Risk V3 | Target editable pada menu existing, default 5 USDT, positif sampai 100. Quantity legal terbesar memenuhi planned loss yang memasukkan taker fee entry/SL dan adverse exit reserve 0,5%. Cadangan TP juga masuk net reward. Gap/slippage lebih besar, funding, fee berubah, atau liquidation dapat melampaui loss aktual target. |
| Config/entry | Akun one-way/single-asset, margin/filters/bracket valid; CROSS dan leverage 75 dibaca ulang. Entry LIMIT GTC memakai harga, quantity dan client ID intent, tanpa mengubah global config/order manual. |
| TP/SL | Actual fill termasuk partial mendapat SL `STOP_MARKET` lalu TP `TAKE_PROFIT_MARKET`, Terakhir (`CONTRACT_PRICE`) untuk plan baru. Exit `BOTH`, reduce-only, 100% exposure bot yang masih terbuka. Kedua active algos harus dibuktikan GET sebelum `POSITION_PROTECTED`. |
| Intent lama | Payload, provenance, harga, quantity, trigger dan cost model historis tetap utuh; tidak dinormalisasi ulang atau diberi ID baru untuk replay. |
| Restart | Operation tetap v9. Cached COMPLETE hanya dapat diselesaikan tanpa POST bila request/job/hash/target cocok dan metadata waktu chart asli masih fresh; missing/stale context ditolak lokal tanpa provider replay. |
| OFF | Setiap provider POST dan Binance mutation baru diblokir tepat pada transport, termasuk mutation lanjutan callback. Request yang sudah terkirim boleh selesai/dijournal; GET akun/status berjalan. OFF tidak cancel/close existing/manual orders. |

Saat OFF, pending entry masih dapat fill di Binance tanpa pemasangan TP/SL baru
oleh bot. Entry/SL/TP adalah request terpisah; polling sekitar 45 detik dan
latency request berarti proteksi tidak atomik dengan fill.

## Status dan bukti exchange

```bash
cd /root/harun-ai-trading-office
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker diagnostics
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker binance-check
```

CLI ini hanya membaca. Cocokkan status fresh, OFF, account/config, occupancy,
receipt harian, pending entries, dan kode aman. Jangan memulai order nyata untuk
membuktikan collector. Untuk order existing, review GET exact client/order ID,
regular/algo open orders, complete order/trade histories, positions, ownership,
trigger, working type, quantity dan protection status.

`ORDER_OUTCOME_UNKNOWN` bukan bukti rejection Binance. Posisi nol belum
membuktikan tidak ada LIMIT pending; ACK bukan bukti fill/proteksi. Lookup
`-2013` sendirian belum membuktikan order tidak pernah ada. Jangan menghapus
claim, mereset attempts/counter, memakai operation baru, atau memaksa submit
ulang. Unknown request/order/protection memblokir pekerjaan baru sampai bukti
terverifikasi; source patch dan build lulus tidak menjadikannya sukses.

Diagnostics tidak mencetak raw provider output, signed query, exception mentah,
atau secret. Metadata `CONFIGURED` tetap hanya konfigurasi lokal. Lihat
[kontrak adapter](ORDER_INTEGRATION.md), [alur lengkap](ROBOT_WORKFLOW.md), dan
[panduan Termius](TERMIUS_24_7.md).
