# Harun AI Trading Office

Kantor virtual 3D dengan **worker DRY RUN terpisah**. GitHub Pages hanya menampilkan
kantor dan membaca snapshot; tidak ada password, cookie, API key, atau eksekutor
order Binance di frontend.

## Status implementasi

| Komponen | Kondisi |
|---|---|
| Scene 3D, Boss + 6 staf, menu lama | Dipertahankan |
| Prompt Neurobro | Literal persis dalam `worker/prompts.py`; tidak ada tambahan 75x |
| Screening dan parsing | Ketat/fail-closed; teks ambigu masuk ERROR / NEEDS_REVIEW |
| Screenshot | Adapter memeriksa URL/pair, label timeframe sebelum/sesudah capture, hash dan umur gambar |
| Risk Manager | Decimal, maksimum risiko harga 5 USDT, quantity dibulatkan turun ke step size |
| Leverage/Cross | Rencana paper wajib CROSS / 75x; belum diubah di akun Binance |
| Batas harian | Dua slot paper per hari Asia/Bangkok; transaksi SQLite atomik, tahan restart |
| Monitoring | PaperMonitor menerima event harga; demo memakai stream FIXTURE eksplisit |
| Laporan/dashboard | Import/export snapshot JSON, atau polling worker privat dengan token baca |
| Integrasi situs asli | Kode adapter tersedia, belum dikonfigurasi/login/diverifikasi pada Neurobro/Binance |
| Live trading | **Tidak diimplementasikan dan tidak dapat diaktifkan melalui setting** |
| Hosting worker / berjalan 24 jam | Belum dipasang; CLI berjalan satu siklus per pemanggilan |

**Tes fixture end-to-end bukan bukti browser asli end-to-end.** Angka fixture
(termasuk harga 100) adalah data sintetis. Tidak ada order, pengubahan leverage,
atau pengubahan margin mode pada Binance selama demo/browser-dry-run.

## Struktur

- `index.html`: scene kantor dan fungsi panel sebelumnya; hanya memuat dua asset workflow baru.
- `assets/workflow.js`, `assets/workflow.css`: panel Workflow DRY RUN, koneksi baca-saja.
- `worker/core.py`: parser, risk, preflight, SQLite ledger, laporan.
- `worker/workflow.py`: koordinator status, verifikasi capture, adapter fixture.
- `worker/browser.py`: adapter Playwright untuk chat Neurobro dan chart Binance.
- `worker/monitor.py`: fill LIMIT dan penutupan TP/SL **paper** dari event harga.
- `worker/__main__.py`: CLI dan endpoint snapshot lokal.
- `tests/test_worker.py`: unit dan integration tests termasuk race condition.

## Menjalankan simulasi pada mesin privat

Python 3.11+:

```sh
python -m unittest discover -s tests -v
python -m worker demo --data-dir "$HOME/.local/state/harun-office"
```

Snapshot: `~/.local/state/harun-office/fixture/snapshot.json`.
Di website buka **MENU → Workflow DRY RUN → Buka snapshot JSON**.
Laporan berlabel FIXTURE. Ledger demo terpisah dari ledger browser.
Menjalankan demo lagi pada hari yang sama akan LOCKED setelah dua slot tercatat;
program tidak menghapus ledger untuk melewati pembatasan. Uji otomatis memakai
database sementara yang terisolasi.

Risiko = `quantity × abs(ENTRY − SL)`. Quantity = nilai minimum dari batas risiko,
ukuran Neurobro (jika jelas), dan maxQty, kemudian dibulatkan **turun** ke stepSize.
75x tidak masuk rumus risiko. TP/SL harus berada pada sisi yang benar dan RR minimal
1:2; level harga tidak diganti. Level yang tidak cocok tickSize ditolak. Filters
pasar harus berumur paling lama 5 menit. Snapshot tidak berisi saldo akun nyata.

Slot dihitung sejak ORDER_READY, termasuk pending, untuk mencegah order ketiga.
Slot tidak dilepas saat posisi ditutup. Reset tanggal otomatis menggunakan
Asia/Bangkok; riwayat tidak dihapus. PNL paper dibukukan pada hari penutupan.
Fees, funding, liquidation dan eksekusi intrabar tidak dimodelkan; gap pada SL bisa
membuat kerugian aktual/simulasi lebih dari risiko harga rencana.

Parser sengaja menolak jawaban naratif/ambigu: screening menerima dua baris symbol
USDT eksplisit (boleh `1.` / `2.`); signal menerima field berlabel yang jelas atau
satu JSON object. Ukuran posisi harus memiliki unit base coin atau USDT. Bila
format chatbot berbeda, status NEEDS_REVIEW diperlukan; prompt asli tidak diubah
agar parser lebih mudah. Tidak ada tebakan angka, pemilihan TP alternatif, atau
penghapusan keterangan ambigu secara diam-diam.

## Menghubungkan browser privat (tahap berikutnya)

1. Siapkan mesin worker privat dengan Python, Chromium, dan desktop/display untuk
   login interaktif. Install `worker/requirements.txt`, lalu `python -m playwright install chromium`.
2. Salin `worker/browser-config.example.json` ke **luar repository**, misalnya
   `~/.config/harun-office/browser.json`. Contoh sengaja tidak berisi selector palsu.
3. Verifikasi URL chatbot Neurobro dan selector di sesi login sendiri. Isi penanda
   respons selesai yang berada di dalam respons baru, upload selesai (tepat dua
   attachment), percakapan baru, chart siap, pair serta timeframe aktif.
4. Area screenshot harus mencakup chart **beserta label pair dan timeframe**.
   Jika chart berada dalam iframe/komponen yang belum didukung selector ini,
   adapter harus disesuaikan dan diuji terlebih dahulu; jangan melewati pemeriksaan.
5. Login secara lokal pada profil worker, tangani 2FA/CAPTCHA sendiri. Profil tetap
   di direktori privat; jangan upload profil, screenshot privat, config, atau cookie.
6. Jalankan `python -m worker browser-dry-run --config /path/private/browser.json`.

Adapter hanya mengirim prompt Neurobro melalui browser; **tidak ada API Neurobro**.
Binance public `GET /fapi/v1/exchangeInfo` dipakai hanya untuk filter kontrak tanpa
kredensial, [sesuai dokumentasi Binance](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information).
Screenshots harus berumur ≤10 menit dan hash tidak boleh berubah sebelum upload.
Tidak ada endpoint order, selector tombol Buy/Sell, atau metode submit live.

Browser dry run berhenti di ORDER_READY untuk rencana paper; belum ada stream
harga Binance/monitor akun yang terhubung. POSITION_OPEN/MONITORING/CLOSED pada demo
berasal dari event harga fixture. Login expired, selector berubah, format ambigu,
atau capture gagal menghasilkan ERROR dan tidak ada order baru selanjutnya.
Order paper yang sudah tercatat sebelum error tetap tersimpan, tidak dihapus.

## Membaca snapshot secara berkala

Set `OFFICE_READ_TOKEN` sebagai secret lingkungan worker (acak, minimal 32 karakter).
`OFFICE_DASHBOARD_ORIGIN` default `https://harun5231.github.io`.

```sh
python -m worker serve --source browser
```

Server hanya bind ke `127.0.0.1:8787`, hanya `GET /snapshot` dengan Bearer token.
Untuk akses dari iPhone, diperlukan hosting worker privat dan reverse proxy HTTPS;
itu **belum dideploy oleh perubahan ini**. Jangan expose port lokal tanpa proteksi.
Masukkan URL `/snapshot` dan token baca di panel; token hanya di memori tab, tidak
masuk URL/localStorage/repository. Token baca ini bukan password atau API key Binance.
Snapshot lama ditandai DATA LAMA. Polling tidak menjalankan worker atau membuat order.
Data impor dapat dipalsukan dan hanya untuk display, tidak pernah menjadi input
order. Live activation memerlukan implementasi dan audit tersendiri, pengujian situs
asli end-to-end, rekonsiliasi akun/pending order, perlindungan posisi, dan instruksi
pengguna yang eksplisit.
