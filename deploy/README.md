# Hosting privat untuk LOGIN NEUROBRO dari iPhone

Implementasi ini memerlukan **server Linux privat dengan desktop virtual**. GitHub
Pages tidak menjalankan Python/Chromium. Tidak ada server, domain, sertifikat, atau
akun hosting yang dibuat otomatis oleh commit ini.

Komponen pada satu server/akun OS khusus tanpa hak root:

- Python 3.11+, `worker/requirements.txt`, Chromium Playwright dengan dependency OS.
- Xvfb, xauth, xdpyinfo, OpenSSL, x11vnc, websockify, noVNC.
- Caddy 2.8+ untuk HTTPS dan autentikasi akses desktop. Hanya port HTTPS/HTTP Caddy
  yang dibuka; 8787, 5900, dan 6080 tetap loopback/firewall privat.
- Penyimpanan persisten untuk profil dan ledger di luar checkout/public web root.

Dokumentasi upstream: [noVNC](https://novnc.com/noVNC/docs/EMBEDDING.html),
[Caddy basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth),
[Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

## Konfigurasi pada server

1. Instal dependency di atas. Jalankan `python -m playwright install chromium` di
   akun OS worker. Salin `worker/browser-config.example.json` ke direktori privat,
   misalnya `~/.config/harun-office/browser.json`. Jangan gunakan config di checkout.
2. Audit selector Neurobro pada UI aktual. Untuk status CONNECTED wajib ada tanggal
   `selectors_verified_on` dan selector `authenticated`, `login_required`, `captcha`,
   `loading`, `composer`. Indikator authenticated harus khusus akun yang sudah login.
   Field kosong/tidak sesuai tidak pernah dianggap login berhasil. Tombol LOGIN
   tetap dapat membuka situs resmi dan browser manual untuk menyelesaikan audit.
3. Simpan variabel berikut dalam environment service privat (file mode 0600 di luar
   repository). **Jangan memasukkan nilai nyata ke file contoh atau GitHub**:

   - `OFFICE_WORKER_HOST`: domain server, tanpa scheme.
   - `OFFICE_WORKER_ORIGIN`: `https://` diikuti domain yang sama.
   - `OFFICE_DASHBOARD_ORIGIN`: `https://harun5231.github.io`.
   - `OFFICE_READ_TOKEN`: token acak minimal 32 karakter, untuk snapshot/status.
   - `OFFICE_CONTROL_TOKEN`: token acak berbeda minimal 32 karakter, hanya kontrol
     login/check; bukan token Neurobro dan tidak dapat membuat order.
   - `OFFICE_DESKTOP_USER` dan `OFFICE_DESKTOP_PASSWORD_HASH`: akun akses desktop,
     hash dibuat dengan `caddy hash-password` secara interaktif.
   - `OFFICE_VNC_PASSWORD_FILE`: path file password VNC privat. Buat menggunakan
     `x11vnc -storepasswd` interaktif, bukan password pada argumen command line.
   - `OFFICE_DESKTOP_STATE`: direktori runtime privat, misalnya
     `~/.local/state/harun-desktop` dengan path absolut.
   - `OFFICE_NOVNC_WEB`: direktori instalasi noVNC, default `/usr/share/novnc`.

   Token worker dapat dibuat dengan `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   secara lokal privat. Token tidak otomatis tersimpan di repository.
4. Jalankan dari root repository, dengan environment di atas sudah dimuat:

   ```sh
   ./deploy/start-desktop.sh --config /path/private/browser.json --data-dir /path/private/harun-office
   ```

   Script memakai display `:99`, direktori Xauthority privat, VNC berpassword di
   loopback dan websockify loopback. Chromium dan API berada di desktop yang sama.
   Gunakan supervisor service OS agar proses tetap berjalan setelah SSH ditutup.
5. Salin `deploy/Caddyfile.example` ke config Caddy server privat. Muat environment
   yang sesuai pada service Caddy, validasi dengan `caddy validate --config ...`,
   lalu jalankan. Caddy melindungi seluruh `/desktop/*`, termasuk WebSocket, dengan
   autentikasi desktop terpisah; koneksi WebSocket dari origin lain ditolak.
   Tidak ada pemetaan filesystem profil/config ke HTTP. Jangan menambah file_server
   untuk direktori worker-data. Deployment TLS/noVNC ini perlu diuji pada server
   tujuan; lingkungan pengembangan belum memiliki stack tersebut.

## Pemakaian dari kantor trading

1. Buka menu **NEUROBRO**. Isi origin HTTPS worker dan **token kontrol worker**.
   Dashboard tidak meminta atau menerima login Neurobro. Token kontrol hanya di
   memori tab, tidak localStorage/sessionStorage, query URL, snapshot, atau GitHub.
2. Tekan **LOGIN NEUROBRO**. API menjadwalkan pembukaan profil Chromium nyata,
   mengamati halaman, lalu menyediakan **BUKA BROWSER SERVER**. Tautan hanya menuju
   origin worker yang dikonfigurasi, tanpa bearer token. Login desktop/VNC berlangsung
   pada origin server, di luar GitHub Pages.
3. Di tampilan desktop server, masukkan kredensial hanya pada halaman Neurobro.
   Cloudflare diselesaikan sendiri secara manual. Jangan mengirim prompt sendiri.
4. Kembali ke kantor dan tekan **CEK SESI**. Worker tidak mengirim prompt: ia memuat ulang halaman aplikasi sekali untuk memeriksa
   challenge, origin, indikator akun, loading, dan editor. Jika valid, status menjadi
   **NEUROBRO CONNECTED**, browser ditutup secara teratur untuk flush profil dan
   melepas lock agar screening CLI dapat memakai profil yang sama. Ini tidak logout.
5. Menutup kantor/iPhone/tautan desktop tidak menghapus profil worker. LOGIN tanpa
   CEK SESI membiarkan browser terbuka untuk takeover. Selesaikan CEK SESI sebelum
   memulai screening. Jika lock dipakai screening, login ditolak WORKER_BUSY, tanpa
   menjalankan Chromium kedua pada profil yang sama.

Screening selanjutnya tetap menggunakan command `python -m worker screen-neurobro`
atau `browser-dry-run` dengan **data-dir, config, akun OS, dan DISPLAY yang sama**.
Guard workflow yang sudah ada memeriksa login lagi dan PAUSED_NEEDS_LOGIN jika
verifikasi diperlukan. Tombol login/check tidak menjadwalkan trading maupun
mengirim prompt. Scheduler trading 24 jam tidak ditambahkan pada tahap ini.

## API

| Method/path | Hak | Perilaku |
|---|---|---|
| GET `/neurobro/status` | Read/control bearer | Status hasil pemeriksaan terakhir; tidak membuka browser |
| POST `/neurobro/login` | Control bearer | Buka/reuse profil dan tab untuk takeover manual |
| POST `/neurobro/check` | Control bearer | Periksa sesi tanpa prompt; simpan/release profil jika valid |
| GET `/snapshot` | Read/control bearer | Snapshot workflow lama, bila tersedia |

POST wajib tanpa body; tidak ada input credential, URL tujuan, selector, shell,
file upload, cookie, atau prompt. Hanya satu aksi browser berjalan pada satu waktu;
permintaan paralel mendapat 409. API bind `127.0.0.1:8787`, dipublikasi hanya melalui
HTTPS Caddy. CORS hanya menerima origin kantor; bearer selalu diperlukan, tidak
mengandalkan CORS saja. Tidak memakai cookie untuk otorisasi API. Response no-store;
log HTTP/Playwright dan isi halaman tidak dikirim ke dashboard.

Status: DISCONNECTED, LOGIN_REQUIRED, LOGIN_IN_PROGRESS, CONNECTED, SESSION_EXPIRED,
CLOUDFLARE_REQUIRED. SESSION_EXPIRED digunakan ketika login hilang setelah pernah
CONNECTED pada proses service yang sama; setelah restart tanpa riwayat positif,
login yang hilang ditampilkan LOGIN_REQUIRED. Status CONNECTED lebih tua dari 60
detik tidak dipercaya dan menjadi DISCONNECTED sampai diperiksa kembali.
Konfigurasi belum lengkap / halaman tidak dikenal menghasilkan DISCONNECTED,
bukan login palsu. LOGIN_REQUIRED / SESSION_EXPIRED ditampilkan sebagai LOGIN REQUIRED.
CLOUDFLARE_REQUIRED tidak pernah diterjemahkan menjadi sukses.

## Bukti dan batas pengujian

59 tes Python/Chromium offline lolos (43 lama + 16 baru), termasuk API auth/CORS,
pemisahan token, penolakan body/endpoint lain, lock profil, status browser, expiry,
Cloudflare, dan tombol frontend. Tes menggunakan stub/halaman offline berlabel;
**bukan bukti login Neurobro produksi berhasil**. Scene iPhone, 7 avatar, 6 menu lama,
dan laporan tetap diperiksa dengan smoke test. NoVNC/TLS, login Neurobro asli,
latensi dari iPhone dan deployment server belum diuji; perlu hosting privat dan
konfigurasi selector sesungguhnya. Tidak ada janji sesi tidak akan kedaluwarsa:
Neurobro/Cloudflare tetap dapat meminta verifikasi ulang.
