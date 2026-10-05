# Discovery selector Neurobro — hanya observasi

Jalankan dari checkout VPS: `bash deploy/discover-selectors.sh`. Script membangun
lapisan kode dari **image worker lokal yang sedang dipakai**, tanpa download base
image atau paket. Jika image lokal tidak tersedia, berhenti; tidak menghapus profil.
Docker/Compose diperlukan, dan akses admin lokal diperlukan untuk menghentikan
worker serta memasang volume yang sama. Tidak ada endpoint discovery publik.

Worker dihentikan graceful sebelum job discovery dimulai. Job memakai hostname,
volume `/data` dan direktori config privat yang sama. Konfigurasi dipasang writable
hanya pada job ini agar atomic rename dapat dilakukan; service normal tetap memakai
mount read-only. Worker/proxy dihidupkan kembali oleh trap walaupun discovery gagal.
Jangan berbagi profil dengan container lain. Image lama tetap tersedia melalui tag
`harun-office-base:local`; tidak ada volume/profile yang dihapus.

Command server-side: `python -m worker.selector_discovery --config /private/browser.json --data-dir /data`.
Jangan menjalankannya bersamaan dengan session service: lock service, worker dan
profil harus diperoleh, stale-Singleton recovery yang sudah ada harus lolos, lalu
Playwright membuka profil yang sama. Tidak ada perubahan pada manual login browser.

Probe memakai vocabulary semantic umum sebagai **kandidat pencarian**, bukan
selector Neurobro yang diklaim sudah benar. Probe hanya menerima kandidat yang
teramati dan memenuhi pemeriksaan fungsi/struktur DOM, lalu mengamati ulang:

- Composer terlihat/editable dan tunggal; Send adalah kontrol pada form yang sama.
- Authenticated memerlukan composer+Send dan kontrol sign-out eksplisit, bukan
  composer yang mungkin tersedia untuk tamu.
- Login harus kontrol login/form berlabel; captcha adalah iframe provider yang
  teramati; loading mempunyai indikator semantic loading chat. Elemen tersembunyi
  dapat membuktikan selector deteksi, tetapi hanya elemen terlihat menentukan state.
- Pesan harus mempunyai metadata author dalam conversation/log; response/completion
  scoped relatif terhadap pesan assistant. Tidak ada isi pesan yang dibaca.
- Upload, attachment, new-chat dan streaming hanya diverifikasi jika struktur yang
  relevan benar-benar teramati. Kontrol tidak pernah diaktifkan.

Probe tidak membaca textContent/innerText, value, cookie, storage, token, screenshot
atau HTML mentah. Hasil terminal hanya nama field + VERIFIED/UNVERIFIED, state dan
boolean. Tidak ada prompt, fill, Send, new-chat, upload, screening atau trading.

Jika keenam field wajib `composer`, `send`, `authenticated`, `login_required`,
`captcha`, `loading` VERIFIED dan state AUTHENTICATED, config diperbarui atomic
(dengan fsync, pemeriksaan perubahan bersamaan, serta mode/owner privat dipertahankan)
dan `selectors_verified_on` berisi timestamp/version. Field workflow opsional yang
belum terverifikasi dikosongkan sehingga screening tetap gagal validasi. Bagian
Binance dan field config lain dipertahankan. Jangan commit config hasil VPS.

Jika satu field wajib tidak terbukti, **config aktif tidak diubah**, exit 2.
Khususnya UI chat yang sudah login sering tidak memuat login/captcha/loading sama
sekali. Ketidakhadiran elemen bukan bukti selector yang benar. Tool tidak memancing
state tersebut, tidak menandai placeholder sebagai verified, dan tidak menjamin
semua selector dapat ditemukan secara pasif. UNVERIFIED memerlukan audit DOM lokal
lebih lanjut pada state yang relevan; tidak berarti sesi login hilang. Jangan
login ulang kecuali state memang LOGIN_REQUIRED. CLOUDFLARE_REQUIRED berhenti tanpa
interaksi challenge. ERROR bisa berarti ownership/konfigurasi/browser bermasalah.

Setelah config_updated=true dan worker kembali sehat, tekan CEK SESI; CONNECTED
hanya bila pemeriksaan halaman baru membuktikan authenticated + composer editable.
`screening_ready=false` berarti selector workflow masih belum lengkap. Tool tidak
mengaktifkan scheduler atau live trading. Seluruh bukti tes repository adalah DOM
sintetis offline; tidak ada selector Neurobro nyata diverifikasi dari Work.
