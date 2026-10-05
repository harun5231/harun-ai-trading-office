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

## Phase 2 — sanitized semantic inventory

Script `deploy/discover-selectors.sh` sekarang menjalankan `--phase2` memakai
existing local image/offline overlay yang sama. Tidak perlu login ulang. Volume,
hostname, ownership, safe Singleton recovery, serta metode stop/restore worker
sama seperti Phase 1. Tidak ada download MCR, endpoint baru, atau perubahan noVNC.

Tool mengeluarkan dua JSON: inventory/evidence lalu ringkasan status. Inventory
maksimal 200 elemen relevan dengan tag/type/role, metadata semantic aman, boolean
visible/editable/disabled/contenteditable, hubungan form/application shell,
fingerprint tag maksimal empat tingkat, kandidat CSS dan jumlah kecocokan.
ID seperti `e0` hanya nomor record lokal, bukan ID pengguna atau ID DOM.

String diperiksa **di dalam halaman sebelum dikirim keluar**: panjang maksimal
80, karakter terbatas, vocabulary kata UI generik yang tertutup. Email, nomor,
JWT/UUID, URL/query, bearer/secret/token, nama akun dan identifier acak tidak
lolos. Unknown words juga disamarkan, bukan dicetak untuk debugging. Atribut
account/profile/user dan label dalam subtree pesan/log tidak diekspor. Data
attributes memakai daftar nama UI generik; class, ID acak, href, src, input value,
innerText/textContent, HTML, cookie dan storage tidak dikeluarkan. Deteksi provider
challenge menggunakan kecocokan CSS boolean, tanpa membaca URL iframe ke output.
Tag custom diganti OTHER dan tidak dipakai untuk menghasilkan selector.

Kandidat selector berasal dari tag/atribut yang benar-benar diamati, bukan
selector Neurobro yang sudah diasumsikan. Field mempunyai status CANDIDATE,
VERIFIED, AMBIGUOUS atau UNVERIFIED dengan kode bukti generik. Jika DOM berubah
antara dua observasi atau inventory terpotong, bukti diturunkan dan config tidak
boleh diperbarui. Selector yang tidak bisa dibuktikan tetap membutuhkan audit.

Authenticated tidak lagi bergantung pada logout. Kombinasi composer editable
unik dengan penanda semantic chat + Send pada form yang sama + application/main
shell yang memuat keduanya dan penanda autentikasi eksplisit diperlukan. Shell
umum saja menjadi CANDIDATE, karena UI chat dapat tersedia bagi tamu. Selector
authenticated yang disimpan juga menyertakan penanda autentikasinya. Tidak ada
inferensi login dari nama akun atau isi pesan. Jika Neurobro tidak menyediakan
bukti struktural tersebut, tool tidak akan mengklaim authenticated.

Selector pesan dan lifecycle completion/streaming dilaporkan sebagai kandidat
untuk review scoping berikutnya, tanpa membaca chat atau memancing respons.
Enam field wajib harus VERIFIED dan state AUTHENTICATED sebelum atomic write
privat; screening tetap terblokir jika selector workflow belum lengkap. Output
inventory tidak otomatis disimpan ke disk, tidak dikirim ke GitHub dan tidak
memuat screenshot. Tes memakai DOM sintetis adversarial, bukan DOM/sesi VPS nyata.

Setelah menjalankan script, kirim **kedua JSON yang disanitasi** dari terminal
untuk audit berikutnya. UNVERIFIED bukan bukti sesi hilang. Jangan mengirim config
privat, cookie, HTML mentah, token atau kredensial.

## Phase 3: persistent evidence and structural proof

Run the existing offline overlay script; it now selects Phase 3 (`--phase3`
 takes precedence over the retained compatibility flag `--phase2`). It uses the
running worker image, never downloads MCR, stops the existing owner gracefully,
retains the identical named profile volume, and restores worker/proxy on exit.
No DOM content, messages, storage, credentials, prompts, clicks or uploads are read
or performed. DOM observations are bounded and must agree twice.

Evidence is saved atomically to `/private/selector-discovery.json`, owner copied
from browser.json, mode 0600, with file and directory fsync. Save must succeed
before any config update. A closed-schema second privacy filter rejects unknown
keys or strings. The server-side viewer revalidates the file before printing it.
Evidence survives browser/container/SSH closure; use a systemd transient service
below so discovery and worker restoration also survive Termius disconnection.

Structural rules require all invariants, never a highest-score tie breaker:
visible, editable, enabled chat-semantic input; semantic form/container;
application shell; related send control(s), with send separately requiring exactly one. Multiple qualified inputs
remain AMBIGUOUS. Send and upload are restricted to that same container, and
selectors must still be globally unique. No nth-child, coordinates or DOM order.
Inventory includes generic ancestors, parent/container references, sibling control
counts and per-composer structural proof. Generic guest chat is not authentication.
An explicit authenticated shell marker, or chat-semantic application shell plus
independent conversation log and new-chat control, supplies additional positive
proof. Visible login/challenge/loading blocks auth. Known challenge-provider
definitions can be VERIFIED with zero current matches; login/loading definitions
still require observed semantic evidence (hidden elements may prove definitions).
Missing evidence stays unverified, never invents a selector.

Session readiness requires all six core definitions verified. Screening readiness
additionally requires every workflow selector verified; lifecycle metadata alone
is not enough. Session-only config keeps `selectors_verified_on: null` so existing
screening validation blocks, while CEK SESI re-runs the read-only structural proof.
No endpoint, credentials or network security policy changes are introduced.

From the existing installation folder, start durable deployment/discovery:

```sh
sudo systemd-run --unit="harun-selector-$(date +%s)" --collect \
  --working-directory="$PWD" /bin/bash deploy/discover-selectors.sh
```

Use `journalctl -u 'harun-selector-*' -n 60 --no-pager` for brief result and
restoration status. Evidence is overwritten only after a complete successful save.
After worker restoration, read the sanitized evidence without opening a browser:

```sh
docker exec "$(docker ps -q --filter label=com.docker.compose.project=harun-office --filter label=com.docker.compose.service=worker | head -n 1)" python -m worker.selector_evidence
```

Local tests use synthetic HTML, not the user's VPS or real Neurobro DOM. Readiness
is not a claim that real selectors were verified. No login repeat is requested;
only a genuinely observed LOGIN_REQUIRED warrants manual login.

## Phase 3 safe diagnostics / preflight

Phase 3 now reports a fixed stage and reason instead of reflecting exception text.
Stages: PRIVATE_CONFIG_CHECK, SERVICE_LOCK_ACQUIRE, PROFILE_OWNER_ACQUIRE,
STALE_SINGLETON_RECOVERY, PROFILE_RELEASE_CHECK, PLAYWRIGHT_START,
PERSISTENT_CONTEXT_OPEN, NEUROBRO_NAVIGATION, DOM_DISCOVERY, EVIDENCE_SANITIZE,
EVIDENCE_WRITE, CONFIG_COMMIT. BROWSER_CLOSE separately identifies failed shutdown.

Before any Chromium launch, the host checks that the main worker container has
stopped. The job requires an existing profile, safe/readable private JSON, an
actual temporary write/fsync/owner test, session-service + worker + office-owner
locks, and the existing conservative process/stale-Singleton proof. Failure stops
before browser launch or config mutation. The ordinary worker's `/private` bind
remains **read-only**; only the temporary discovery job mounts that directory RW.
The Docker host stop check and profile locks complement one another; locks cannot
prove container state across unrelated hosts or copies of the profile volume.

If a later stage fails, `/private/selector-discovery-diagnostic.json` is written
atomically with mode 0600 and the config owner, if preflight proved the destination
safe. Existing selector-discovery.json is never replaced with an error. Unsafe or
unwritable destinations cannot receive diagnostics; terminal `diagnostic_saved`
reports this explicitly. A completed run replaces an earlier diagnostic with
reason COMPLETED; this does not mean selectors were verified.

Diagnostic fields are limited to stage/reason enums, UTC timestamp, singleton
count, lock-file presence, browser-process proof and cleanup-failure boolean.
Lock-file presence does not claim that a lock is held. `browser_process_detected`
is false only after a successful quiet proof and otherwise null (unknown): a
failed /proc inspection is not proof of an active browser. Counts describe the
profile after cleanup. Exceptions, process command lines, URLs, DOM and account
content are never printed or serialized. Existing evidence uses its independent
closed-schema sanitizer. Failed browser cleanup does not hand off to a workflow;
the one-shot container exits before the host restores the worker.

After reconnect, read only the short persisted diagnostic:

```sh
docker exec "$(docker ps -q --filter label=com.docker.compose.project=harun-office --filter label=com.docker.compose.service=worker | head -n 1)" python -m worker.selector_diagnostic
```

Continue to use the offline overlay and systemd-run command above. No MCR download
is required. Nothing in this diagnostic path requests login, sends a message,
uploads a file, invokes screening or trading, or deletes a profile/volume.
