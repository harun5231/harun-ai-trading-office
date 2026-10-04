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
| Integrasi situs asli | Screening browser + guard siap diuji; situs Neurobro menampilkan CAPTCHA pada pemeriksaan 4 Oktober 2026, session/selector login belum diverifikasi |
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
- `worker/screening.py`: kesiapan login/chat, single-send, completion guard, parser dua kontrak dinamis.
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

Parser sengaja menolak jawaban ambigu: fixture screening tetap menerima dua baris
symbol USDT eksplisit. Browser screening menerima dua pilihan eksplisit bernomor,
bullet, heading Markdown, atau dua baris pair. Ticker dicocokkan ke katalog kontrak
USDT perpetual aktif terbaru, tanpa fuzzy matching nama atau tebakan multiplier.
Narasi alasan diperbolehkan, tetapi coin tambahan, penolakan, contoh, atau alternatif
ditolak. Format daftar yang tidak dikenali berhenti NEEDS_REVIEW. Signal menerima field berlabel yang jelas atau
satu JSON object. Ukuran posisi harus memiliki unit base coin atau USDT. Bila
format chatbot berbeda, status NEEDS_REVIEW diperlukan; prompt asli tidak diubah
agar parser lebih mudah. Tidak ada tebakan angka, pemilihan TP alternatif, atau
penghapusan keterangan ambigu secara diam-diam.

## Menghubungkan browser privat

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

## Screening Neurobro saja (tanpa membuka Binance)

```sh
python -m worker screen-neurobro --config /path/private/browser.json
```

Perintah ini berhenti pada COINS_SELECTED, PAUSED_NEEDS_LOGIN, atau ERROR, tanpa capture, analisis setup,
reserve slot, maupun order paper/live. `browser-dry-run` memakai screening yang sama
lalu melanjutkan alur lama hanya jika screening lolos. Prompt tetap diambil dari
`worker/prompts.py`, tidak ditambah petunjuk format, leverage, atau instruksi lain.
Tidak ada retry pengiriman otomatis ketika hasil klik kirim tidak pasti.

Profil Chromium persisten berada di `browser/browser-profile` di dalam data-dir
privat, di luar repository, dengan direktori mode 0700 dan umask CLI 077. Worker
menolak profil/config di repository. Profil menyimpan sesi **lokal saja**, bukan di
GitHub Pages. Jangan gunakan profil browser pribadi sehari-hari atau direktori
public web root sebagai data-dir. Untuk login manual pada profil worker yang sama,
tutup worker terlebih dahulu, lalu pada mesin privat ber-desktop:

```sh
umask 077
mkdir -p "$HOME/.local/state/harun-office/browser/browser-profile"
chmod 700 "$HOME/.local/state/harun-office/browser/browser-profile"
python -m playwright open --user-data-dir "$HOME/.local/state/harun-office/browser/browser-profile" https://app.neurobro.ai/
```

Login/2FA dan CAPTCHA hanya ditangani pengguna secara interaktif sesuai mekanisme
situs. Tutup browser manual sebelum menjalankan worker. Tidak ada ekstraksi cookie,
penyimpanan password, pemecah CAPTCHA, stealth plugin, atau rotasi proxy. Session
browser pemeriksaan cloud berbeda dengan profil worker; tidak disalin di antara keduanya.

Konfigurasi selector harus diaudit pada UI login aktual; isian kosong sengaja
menghentikan worker. `selectors_verified_on` mencatat tanggal audit, bukan bukti
otomatis bahwa selector masih benar. Makna field screening:

| Field | Bukti yang wajib ditunjuk selector |
|---|---|
| `authenticated` | Indikator sesi akun terautentikasi, bukan sekadar textarea terlihat |
| `login_required`, `captcha`, `loading` | Indikator login wajib, tantangan keamanan, dan halaman belum siap |
| `composer`, `send`, `new_chat` | Satu editor, tombol kirim, dan kontrol percakapan baru |
| `user_messages` | Isi teks pesan pengguna, tanpa label/tombol tambahan |
| `assistant_messages` | Container pesan asisten pada percakapan aktif |
| `response_text` | Isi jawaban relatif terhadap container, tanpa tombol/sumber UI |
| `completed_response` | Penanda final di dalam pesan yang baru selesai, bukan indikator typing |
| `streaming` | Indikator respons masih ditulis pada percakapan aktif |

Driver memastikan chat baru kosong, prompt identik sebelum kirim dan pada echo
pesan pengguna, tepat satu jawaban baru, penanda selesai eksplisit, tidak streaming,
dan teks stabil satu detik. Stabilitas teks saja tidak dianggap selesai. Guard login,
CAPTCHA termasuk iframe Cloudflare yang terlihat, dan origin diperiksa selama polling.
Timeout kesiapan 90 detik; respons 180 detik. Perubahan DOM, jawaban belum selesai,
metadata kontrak gagal, atau hasil selain dua pilihan menghentikan alur.

Log/snapshot menampilkan OPENING_NEUROBRO → WAITING_NEUROBRO → SCREENING_SENT →
WAITING_RESPONSE → COINS_SELECTED, atau PAUSED_NEEDS_LOGIN untuk login/verifikasi manual; ERROR untuk kesalahan lainnya.
Snapshot progres tidak memuat respons mentah, URL session, cookie, atau trace browser.
Status dashboard membutuhkan impor snapshot terbaru atau koneksi server baca yang
sudah tersedia; worker tidak otomatis di-host oleh GitHub Pages.

### Verifikasi perubahan screening

```sh
python -m unittest discover -s tests -v
HARUN_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

Perintah pertama menjalankan 25 tes Python dan melewati 18 tes browser opsional.
Perintah kedua menjalankan seluruh **43 tes** setelah Playwright/Chromium terpasang:
15 tes lama tetap utuh, 10 tes parser/integrasi/keamanan profil, dan 18 tes Chromium
pada UI mock offline. Semua request UI mock dicegat lokal, tanpa session atau pesan
ke layanan asli. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` hanya opsi executable untuk tes.

Hasil pengembangan: 43/43 lolos, serta smoke test dashboard ukuran iPhone (7 avatar,
6 menu, impor laporan, penolakan live snapshot, tanpa error JavaScript). Desain
`index.html`, CSS, prompt literal, Risk Manager, batas harian, dan monitor tidak diubah;
`core.py` hanya mendapat nama status baru.

**Batas bukti:** situs resmi Neurobro berhasil dibuka sampai halaman aplikasi,
tetapi menampilkan CAPTCHA Cloudflare “Verifikasi bahwa Anda adalah manusia”.
Tidak ada prompt yang dikirim ke Neurobro asli. Login, selector UI terautentikasi,
completion marker layanan asli, dan parsing respons nyata **belum terverifikasi**.
Konfigurasi contoh tetap belum siap jalan sampai audit selector dan login privat
selesai. Tes Chromium offline bukan klaim screening Neurobro produksi berhasil.
Binance tetap DRY RUN dan tidak menerima order nyata.

## Jeda verifikasi manual dan sesi persisten

Login/CAPTCHA saat screening sekarang menghasilkan **PAUSED_NEEDS_LOGIN**. Pada
terminal interaktif, CLI mempertahankan proses dan jendela Chromium yang sama.
Pemilik mengambil alih desktop worker (langsung atau melalui akses desktop privat
yang sudah diamankan), menyelesaikan login/verifikasi sendiri, lalu mengetik
`LANJUT` di terminal. Jangan mengirim prompt sendiri atau mengganti percakapan.
Worker tidak mengklik, memecahkan, atau melewati CAPTCHA. `LANJUT` hanya meminta
pemeriksaan ulang: bila challenge masih ada, status tetap PAUSED_NEEDS_LOGIN.

Setelah indikator akun login terverifikasi dan loading selesai, alur melanjutkan
langkah yang terhenti pada tab yang sama. Jika prompt sudah dikirim, hanya jawaban
lama yang ditunggu, bukan membuat chat atau mengirim prompt baru. Waktu jeda manual
tidak memakan timeout respons. Sesi valid tidak memunculkan permintaan login lagi.
Cookie/token tetap di profil Chromium privat di worker, tidak diekspor ke snapshot
atau repository. Profil yang sama digunakan pada pemanggilan berikutnya.

Tanpa terminal interaktif, atau jika pengguna membatalkan, CLI menyimpan snapshot
PAUSED_NEEDS_LOGIN dan keluar dengan kode 3, menutup browser secara teratur tanpa
menghapus profil. Untuk lanjut sebelum pengiriman, jalankan kembali pada terminal
interaktif dengan data-dir yang sama. Tidak ada endpoint resume publik atau layanan
remote desktop yang otomatis dipasang oleh commit ini. Akses desktop worker dari
iPhone memerlukan server/remote desktop privat tersendiri; GitHub Pages saja tidak
menjalankan Chromium.

Sebelum klik kirim, worker menulis `screening-pending.json` (mode 0600) di profil
privat. File hanya berisi penanda pending, tanpa prompt, respons, atau kredensial.
Jika proses mati atau hasil pengiriman belum pasti, pemanggilan berikutnya ditolak
NEEDS_REVIEW agar tidak mengirim ulang. File dibersihkan hanya setelah jawaban
lengkap dan dua coin lolos parser. Pemulihan setelah crash pascapengiriman perlu
rekonsiliasi manual pada percakapan asli; jangan menghapus penanda tanpa pemeriksaan.
Lanjut setelah login pada proses yang masih berjalan tidak mengulang pengiriman.

Verifikasi revisi ini: **43 tes lolos**, termasuk semua 35 tes sebelumnya, tes jeda
sebelum/sesudah kirim, verifikasi belum selesai, sesi valid, interaksi manual yang
mengirim pesan, crash guard, status workflow, dan persistensi profil Chromium dengan
cookie sintetis pada domain uji offline. Tes sintetis tidak dihitung sebagai sukses
Neurobro asli. Smoke test dashboard iPhone tetap lolos.

Pemeriksaan langsung pada 4 Oktober 2026 masih melihat CAPTCHA Cloudflare di browser
cloud: **PAUSED_NEEDS_LOGIN**, belum ada prompt screening terkirim dan belum ada dua
coin asli diperoleh. Pengambilalihan browser cloud tidak otomatis menghubungkan sesi
tersebut ke worker/server; keduanya profil terpisah. Konfigurasi selector worker
setelah login masih perlu diverifikasi pada profil worker aktual.

## LOGIN NEUROBRO melalui dashboard kantor

Menu **NEUROBRO** kini menyediakan LOGIN NEUROBRO, CEK SESI, status hasil pemeriksaan
worker, dan BUKA BROWSER SERVER untuk takeover manual dari iPhone. Implementasi API
ada di `worker/session_service.py`; panel di `assets/neurobro.js`. `index.html`
hanya ditambah pemuatan script panel, tanpa perubahan scene atau desain 3D.

Worker menggunakan profil dan lock yang sama dengan screening sebelumnya. Sesi
tersimpan privat; dashboard tidak menerima kredensial/cookie/token Neurobro. API
memerlukan token kontrol worker tersendiri, terpisah dari token baca. Token kontrol
sementara ini bukan token Neurobro. Login/check tidak mengirim prompt atau order.

**Fitur belum aktif di GitHub Pages tanpa server:** ikuti [panduan hosting privat](deploy/README.md).
Disediakan launcher desktop Linux/noVNC dan contoh reverse proxy HTTPS terlindungi.
Server, domain, kredensial akses desktop, selector login dan sesi Neurobro nyata
belum tersedia/deploy otomatis. Pengujian revisi: **59 tes lolos**, termasuk semua
43 tes lama dan 16 tes baru; hasil offline tidak dinyatakan sebagai login nyata.
