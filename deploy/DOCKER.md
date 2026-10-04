# Pemasangan Docker sekali dari iPhone

**Paket siap untuk deployment, bukan layanan yang sudah aktif.** Diperlukan VPS
Linux milik pengguna, domain yang menunjuk IP VPS, dan console web VPS yang dapat
dibuka dari iPhone. GitHub Pages tetap hanya dashboard. Tidak perlu laptop.

## Kapasitas yang direncanakan

Minimum praktis: **2 vCPU, RAM 4 GB, SSD 40 GB, Ubuntu Server 24.04 LTS x86_64**.
Untuk browser lebih nyaman gunakan 4 vCPU / RAM 8 GB. Ini estimasi engineering untuk
satu worker dan satu profil Chromium, belum hasil benchmark server produksi.
Gunakan Docker Engine + Compose v2 plugin yang aktif saat boot. Buka 80/tcp,
443/tcp dan opsional 443/udp; jangan membuka 5900, 6080, 8787 atau 2019.

## Instalasi awal

Pilih VPS yang sudah menyediakan Docker + Compose dan Python 3, atau instal dari
[dokumentasi Docker Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
Pada console web VPS (di iPhone), jalankan:

```sh
git clone https://github.com/harun5231/harun-ai-trading-office.git
cd harun-ai-trading-office
sudo python3 deploy/setup.py --host worker.domain-anda.com
```

Ganti domain dengan domain yang sudah memiliki DNS A ke IP VPS. Jika ada AAAA,
pastikan IPv6 juga mengarah ke VPS; Caddy memerlukan DNS benar untuk sertifikat.

Setup menghasilkan token read/control dan password desktop acak **sekali**,
menyimpannya di `/opt/harun-office-private`, menyalin konfigurasi awal ke direktori
privat, menulis `.env` tanpa secret, memvalidasi Compose dan menjalankan build/up.
Menjalankan setup lagi tidak mengganti password, token, selector atau volume.
`.env.example` hanya berisi konfigurasi non-secret; tidak ada credential Neurobro
atau Binance di dalamnya. Secret Docker dimuat dari file host privat saat runtime,
bukan environment build, GitHub, atau layer image.

Periksa layanan:

```sh
docker compose ps
sudo cat /opt/harun-office-private/ACCESS.txt
```

ACCESS.txt berisi origin, token worker, dan login desktop. Baca hanya pada console
privat; jangan kirim ke chat, GitHub atau GitHub Pages. Simpan akses desktop dalam
password manager pribadi. Tutup console tidak menghentikan layanan Compose.

Pada kantor trading → NEUROBRO, masukkan origin dan token kontrol worker. Tombol
LOGIN NEUROBRO membuka Chromium asli di server; BUKA BROWSER SERVER membuka noVNC
melalui HTTPS dan login desktop. Masukkan kredensial Neurobro hanya pada situs
Neurobro dalam browser itu, selesaikan verifikasi sendiri, lalu CEK SESI.

Endpoint bukan URL server hard-coded. Ia berasal dari input origin dashboard dan
`OFFICE_WORKER_HOST` server. Opsional isi **hanya origin HTTPS publik** pada
`assets/worker-config.json` satu kali agar field dashboard otomatis terisi. File
tersebut tidak boleh berisi token/password/cookie. Token tetap hanya di memori tab;
menutup kantor tidak menghapus sesi Chromium di server.

## Hal yang belum bisa otomatis tanpa verifikasi situs asli

Konfigurasi awal `/opt/harun-office-private/config/browser.json` sengaja memiliki
selector kosong. Login manual dapat dibuka, tetapi status CONNECTED dan screening
memerlukan audit indikator login dan selector pada UI Neurobro aktual. Selector
chart Binance juga diperlukan untuk kelanjutan browser-dry-run. Jangan menandai
selector terverifikasi sebelum pemeriksaan nyata. Pemasangan Docker tidak melewati
Cloudflare, login, atau audit ini dan tidak menjamin sesi tidak akan kedaluwarsa.

## Proses yang berjalan

| Komponen | Perilaku |
|---|---|
| Worker container | Supervisor, runner DRY RUN, session API, Xvfb, x11vnc, websockify/noVNC |
| Chromium | Diluncurkan sesuai permintaan pada display server; menggunakan profil `/data/browser/browser-profile` |
| Runner | Proses hidup 24/7; jadwal otomatis default **DISABLED** sampai diaktifkan setelah validasi |
| Proxy | Caddy HTTPS + autentikasi semua `/desktop/*` termasuk WebSocket |
| Volume `worker_data` | Profil, cookie sesi, ledger, snapshot, artefak dan penanda percobaan |
| Volume `caddy_data`/`caddy_config` | Sertifikat dan state HTTPS |
| `/run/office`, `/tmp`, home | tmpfs sementara, bukan tempat penyimpanan login |

Profil memiliki hostname container tetap dan UID tetap. Browser/worker berjalan
sebagai pengguna `office` UID 10001. Root bootstrap hanya mengatur izin volume dan
membaca file secret, lalu turun hak akses. Root filesystem worker read-only, tidak
privileged, tidak memasang socket Docker. VNC RFB hanya loopback dalam worker;
websockify/API hanya jaringan internal Compose tanpa port host. Caddy adalah satu-
satunya pintu publik dan melindungi noVNC dengan password hash; koneksi WebSocket
lintas origin ditolak. Pada paket Docker tidak perlu login VNC kedua karena akses
HTTPS desktop sudah diautentikasi. Konfigurasi desktop non-Docker lama tetap terpisah.

`restart: unless-stopped` memulihkan proses saat crash/reboot, selama Docker aktif
dan layanan tidak sengaja dihentikan manual. Supervisor keluar jika subprocess
mati atau pemeriksaan liveness gagal berulang, sehingga restart Compose benar-benar
terpicu: label Docker `unhealthy` saja tidak dianggap mekanisme restart. Watchdog
memeriksa local API/heartbeat, bukan mengulang request situs Neurobro. CAPTCHA,
LOGIN_REQUIRED, atau jadwal DISABLED bukan alasan mencoba bypass/relogin otomatis.

Penutupan SIGTERM memberi kesempatan worker menutup Chromium dan menyimpan profil.
Jika mesin mati mendadak, hanya data yang sudah ditulis browser yang dapat bertahan;
Neurobro tetap dapat mencabut sesi dari sisi layanan. Jangan memakai `docker compose
down -v` atau menghapus volume jika ingin mempertahankan sesi dan ledger. Volume
bukan backup untuk kerusakan disk VPS; simpan backup privat saat worker dihentikan.

## Health dan status kantor

`GET /health` wajib bearer token worker. Docker probe membacanya secara lokal,
tidak memakai password/cookie Neurobro. Response menampilkan:

- API dan actor browser masih hidup; startup menguji Chromium offline pada halaman
  kosong. Browser berstatus IDLE ketika tidak sedang dibuka, ALIVE ketika terbuka,
  ERROR bila crash/tidak merespons. Tidak membuka Neurobro hanya untuk health check.
- Heartbeat runner dan supervisor, termasuk DISABLED/RUNNING_DRY_RUN.
- Uji tulis/baca/fsync pada direktori persistent; tidak mengekspor isinya.
- Status sesi Neurobro terpisah: worker ONLINE tidak berarti Neurobro CONNECTED.

Dashboard memperoleh ONLINE/OFFLINE dari API ini; kegagalan jaringan atau probe
berarti OFFLINE. Login/token kedaluwarsa tetap ditangani guard lama dan tidak
melanjutkan order. API read/control, origin CORS dan larangan input credentials
Neurobro tetap berlaku. Tidak ada endpoint order live.

## Jadwal DRY RUN setelah audit berhasil

Default `.env`: `OFFICE_AUTO_DRY_RUN=false`. Seluruh service/health tetap berjalan,
tetapi tidak mengirim prompt tanpa aktivasi jadwal. Setelah login, selector, dan
browser-dry-run nyata diverifikasi, set `OFFICE_AUTO_DRY_RUN=true`, pilih
`OFFICE_RUN_AT=08:00` (Asia/Bangkok), lalu `docker compose up -d`.

Runner memanggil **workflow browser-dry-run yang sama**, satu percobaan per hari.
Ia tidak mengubah prompt, Risk Manager, CROSS 75x, batas 2 trade/hari atau ledger.
Penanda percobaan ditulis sebelum memulai untuk mencegah pengiriman ulang setelah
crash. Jika gagal/PAUSED, tidak ada retry buta. Setelah verifikasi manual, pemulihan
percobaan perlu diperiksa; pending-send lama tidak dihapus otomatis. Jadwal ini
bukan aktivasi live trading dan tidak menghasilkan fill pasar nyata.

Untuk menguji satu screening tanpa scheduler atau Binance, setelah CEK SESI valid:

```sh
docker compose exec --user office worker python -m worker screen-neurobro --data-dir /data --config /private/browser.json
```

Jika login diperlukan, gunakan LOGIN NEUROBRO dari dashboard; jangan menjalankan
CLI interaktif lain sambil browser login masih memegang lock. Gunakan data-dir
sama agar ledger dan profil tidak terduplikasi.

## Update dan verifikasi

```sh
git pull --ff-only
docker compose up -d --build
docker compose ps
```

`.env`, secret privat, konfigurasi selector dan named volumes dipertahankan.
Dependency browser dipatok Playwright 1.63.0 agar paket dan image Chromium cocok.
Perbarui versi tersebut bersama-sama setelah pengujian keamanan/regresi.

Pengujian pengembangan:

```sh
python -m pip install -r worker/requirements.txt -r tests/requirements.txt
python -m playwright install chromium
HARUN_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

Hasil: **71 tes**, seluruh 59 lama ditambah 12 deployment/health/status UI, serta
smoke test scene/dashboard iPhone. YAML Compose, Python dan JavaScript tervalidasi.
**Docker CLI/daemon tidak tersedia pada lingkungan pengerjaan**, sehingga build
image, startup Compose, HTTPS/noVNC publik dan reboot VPS belum diuji langsung.
Ini keterbatasan verifikasi yang harus diselesaikan pada server sebelum menyatakan
layanan 24/7 aktif. Login Neurobro nyata tetap belum terbukti berhasil.

Rujukan: [Docker Compose](https://docs.docker.com/reference/compose-file/services/),
[Docker secrets](https://docs.docker.com/reference/compose-file/secrets/),
[Playwright Docker](https://playwright.dev/python/docs/docker),
[Caddy password hashing](https://caddyserver.com/docs/command-line#caddy-hash-password).

## Pembaruan tampilan portrait iPhone

Desktop default sekarang **430×932**, dapat diatur melalui `OFFICE_DESKTOP_WIDTH`
dan `OFFICE_DESKTOP_HEIGHT` pada `.env` (integer 320–4096). Compose juga memberi
nilai default itu bila `.env` lama belum memiliki kedua variabel. Launcher Xvfb
Docker/non-Docker menggunakan geometry yang sama. Kedua adapter Chromium memakai
ukuran window yang sama dan `no_viewport=True`, sehingga tidak lagi memaksakan
viewport Playwright 1280px. Ini perubahan ukuran tampilan, bukan emulasi identitas
browser atau perubahan profil/login.

Tautan noVNC memakai `resize=scale`: desktop portrait tetap, gambar menyesuaikan
ukuran layar/rotasi iPhone di sisi noVNC. Mode ini tidak bergantung dukungan resize
resolusi remote dari Xvfb/x11vnc. Path WebSocket tetap **desktop/websockify**. Buka
ulang BUKA BROWSER SERVER dari dashboard yang sudah direfresh agar memakai parameter
scaling baru; bookmark noVNC lama dapat tetap membawa `resize=remote`.

Sesudah `git pull --ff-only origin main` pada checkout VPS existing, jalankan
`sh deploy/update-portrait.sh`. Script hanya mengatur dua dimensi dalam `.env`,
memvalidasi Compose, rebuild/recreate worker, menunggu health maksimal 240 detik,
dan restart proxy. Volume `worker_data`, profil, ledger dan sertifikat tidak dihapus;
config privat dan token tidak diubah. Login tetap memakai profil lama, tetapi
Neurobro tetap dapat meminta verifikasi ulang dari sisi layanannya.

Perubahan ini diuji melalui suite lokal dan pemeriksaan konfigurasi. Tidak ada
akses ke VPS pengguna dalam sesi pengembangan ini, jadi tampilan desktop aktual
pada VPS perlu dilihat setelah perintah update dijalankan.

## Login manual tanpa Playwright

LOGIN NEUROBRO sekarang menjalankan Chromium headed sebagai proses biasa pada
Xvfb/noVNC. Tidak ada Playwright, CDP, remote-debugging port, penggantian user agent,
stealth plugin, ekspor cookie, atau interaksi otomatis dengan challenge dalam
proses login tersebut. Binary Chromium lengkap menggunakan versi yang sama dengan
image worker. Konfigurasi sandbox container mengikuti browser worker yang sudah
ada (`--no-sandbox` hanya pada OFFICE_MANAGED=1); container tetap non-root,
read-only dan memakai pembatasan capability yang ada. Ini bukan solusi bypass
Cloudflare dan tidak menjamin Neurobro menerima browser/IP VPS.

1. Tekan LOGIN NEUROBRO lalu BUKA BROWSER SERVER.
2. Masukkan kredensial hanya langsung di halaman Neurobro melalui noVNC.
3. Setelah selesai, kembali ke kantor dan tekan SELESAI LOGIN / CEK SESI.
4. Worker mengirim SIGTERM untuk penutupan Chromium yang bersih, menunggu proses
   keluar serta singleton profile dilepas, baru membuka profil yang sama dengan
   Playwright untuk pemeriksaan tanpa prompt. Browser pemeriksaan selalu ditutup.
5. CONNECTED hanya berasal dari selector authenticated/composer yang terverifikasi.
   LOGIN REQUIRED atau CLOUDFLARE REQUIRED berarti perlu tindakan manual; tekan
   LOGIN kembali jika ingin membuka browser biasa. Tidak ada retry verifikasi.

Profil tetap `/data/browser/browser-profile` pada volume `worker_data` yang sama.
`worker.lock` dan `.office-owner.lock` mencegah akses bersamaan, termasuk adapter
screening langsung. Proses penjaga mewarisi lock sampai Chromium berhenti walaupun
API terhenti. Pemeriksaan yang tidak dapat memastikan browser telah berhenti
berakhir error tanpa membuka Playwright. Singleton yang meragukan tidak dihapus
otomatis. Restart normal menghentikan browser sebelum Xvfb. Reboot/container
rebuild tidak menghapus profil, tetapi keabsahan sesi tetap ditentukan Neurobro.

Update dari folder checkout VPS yang sudah ada:

```sh
git pull --ff-only origin main
docker compose config --quiet
docker compose up -d --build --no-deps --force-recreate --wait --wait-timeout 240 worker
docker compose restart proxy
docker compose ps
```

Jangan menjalankan `down -v` atau menghapus volume/profile. Pembaruan ini tidak
mengubah `.env`, resolusi portrait 430×932, URL noVNC, prompt, risk maupun DRY RUN.
Tes proses memakai executable lokal sintetis dan browser offline, bukan login
Neurobro nyata. Login melalui noVNC dan penerimaan Cloudflare perlu diuji di VPS.
