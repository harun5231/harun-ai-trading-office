# HARUN AI TRADING OFFICE

Kantor 3D + worker privat NeuroAPI Starter (`smart`) dan Binance Futures public
market data. **DRY RUN ONLY**: tidak ada eksekutor atau route order Binance.
Autentikasi private Binance memakai GET read-only untuk preflight, posisi dan
saldo USDT nyata. Kantor/animasi/menu tetap; koneksi provider kini lewat API resmi.

Office kini memiliki **ROBOT TRADING ON/OFF**, risk target dan kartu approval.
Default OFF; pilihan persistent. Perubahan OFF → ON segera membangunkan
coordinator, lalu riset dipoll setiap 45 detik, maksimum satu operasi NeuroAPI
per tick setelah membaca posisi Futures nyata melalui GET. Semua posisi manual
termasuk HYPE memakai concurrent capacity, tetapi tidak dianggap bot entry.
Polling ini menjalankan riset; live order tetap dinonaktifkan.

Setup v7 terverifikasi kini menyediakan **SALIN TIKET** entry LIMIT, TP dan SL
untuk ditinjau dan dikirim sendiri di Binance, serta **UJI SIMULASI** lokal untuk
lima skenario lifecycle. Tiket memakai quantity dan level immutable dari proof,
risk target default 5 USDT; OK/APPROVED maupun salin tiket tidak mengirim order.
Simulasi memakai jejak fill/proteksi sintetis, tanpa API key baru atau panggilan
provider. Hasilnya tidak mengubah saldo, posisi, PnL akun atau counter entry nyata.
Setup unresolved tetap membatasi riset berbayar berikutnya. Rincian penggunaan
dan pembaruan frontend: [deploy/MANUAL_SIMULATION.md](deploy/MANUAL_SIMULATION.md).

Alur: kapasitas nyata (maksimal 2 posisi dan 2 bot entry/hari Asia/Bangkok) →
screening literal 1 atau 2 coin sesuai slot → active USDT perpetual catalog →
realtime 1h/15m + contract rules → ANALYSIS literal → LONG/SHORT/HOLD → deterministic
Risk Manager → SETUP_READY → OK/APPROVED atau TIDAK/USER_REJECTED. **OK tidak mengirim
order.** HOLD/TIDAK dapat mencari replacement sesuai slot tersisa, maksimal tiga
screening replacement/cycle. HYPEUSDT dan symbol yang sedang terbuka dikecualikan
dari analisis; kandidat lain yang valid tetap dilanjutkan. Hasil screening yang
seluruhnya dikecualikan atau cycle lama yang selesai tanpa hasil pulih melalui
replacement bounded yang sama. Technical failure tidak memicu replacement.
`wait_reason` menjelaskan WAITING normal, kapasitas penuh atau blocker lifecycle.

Risk target default 5 USDT, editable positif sampai 100. Worker memilih quantity
legal terbesar dengan loss harga ke SL tidak melebihi target, memakai Decimal /
rational floor dan seluruh filter kontrak. Quantity Neurobro audit-only; keputusan
serta ENTRY/TP/SL tetap immutable, actual RR minimal 2, CROSS/75x target.

Setup/approval v7 terpisah dari execution receipt dan tidak memakai live-entry
counter. Cycle/request claim persistent mencegah polling/restart membayar request
setara berulang. Unknown outcome fail-closed. Retry tetap hanya HTTP 429/503,
bounded dan menghormati Retry-After; tidak mengandalkan Idempotency-Key. Legacy v6
records, hashes, commands dan paper reports tidak dimigrasikan atau dihapus.

Rincian state machine, provenance dan endpoint:
[deploy/ROBOT_WORKFLOW.md](deploy/ROBOT_WORKFLOW.md).

Default lookback 100 candle/frame, configurable 20–500; context freshness 180s
(default, 30–300s), mark ≤60s, jam server diperiksa. Data diperoleh dari API publik
langsung setiap analisis; filter kontrak diambil ulang sebelum reservasi. Data yang
kedaluwarsa selama analisis ditolak. Panel robot menampilkan wallet/available
balance USDT dari private GET Binance; saldo menjadi null bila account tidak
tersedia. Snapshot paper tetap memiliki balance null. Monitoring memakai sampel
mark price tiap ~30s saat actor idle: paper fill/exit simulasi, bukan histori tick
lengkap atau fill exchange.
Fees/slippage tidak disimulasikan; gap SL dapat membuat paper PNL melampaui 5 USDT.

## Menjalankan

Baca [deploy/DOCKER.md](deploy/DOCKER.md). API key hanya di secret VPS; dashboard
menerima token kontrol worker yang berbeda dan hanya menyimpannya di memori tab.
CEK API memanggil health provider tanpa prompt. JALANKAN DRY RUN meminta evaluasi
coordinator; ROBOT OFF tetap menghalangi riset. Aktifkan riset hanya lewat kontrol
ROBOT pada Office. Flag jadwal lama tidak membypass OFF. Tidak ada opsi LIVE.
Worker pulih melalui restart Compose dan watchdog heartbeat. Saat update/SIGTERM,
request baru dihentikan dan operasi berjalan ditunggu hingga 600 detik; Docker
memberi grace 660 detik. Outcome yang belum pasti tetap memerlukan review.

## Dokumentasi resmi yang diperiksa 2026-10-05

- [Starter/mode smart](https://neuroapi.neurobro.ai/docs/pricing)
- [Request/output schema dan message_history](https://neuroapi.neurobro.ai/docs/best-practices/examples)
- [Idempotency dan replay 24 jam](https://neuroapi.neurobro.ai/docs/guides/idempotency)
- [Ask reference](https://neuroapi-staging.neurobro.ai/docs/api/agent-ask)
- [Binance USD-M market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)

NeuroAPI: POST https://api.neurobro.ai/api/v1/agent/ask, X-API-Key,
mode=smart, stream=false, output_schema → output (answer=null). Structured output
smart bersifat best-effort di provider; worker tetap memvalidasi sendiri. JSON
market data berada dalam satu user message_history.content; prompt tetap terpisah
persis, tanpa system_prompt tambahan. Quantity/RR unit dijelaskan di schema agar
angka tidak ambigu. Tidak mengasumsikan atau memerlukan header kuota.

Binance: GET /fapi/v1/exchangeInfo, /time, /klines, /premiumIndex pada
https://fapi.binance.com. Endpoint trading tidak ada dalam allowlist transport.

## Tests

`python -m unittest discover -s tests -v` — mocks/fixtures, tidak memakai key,
tidak mengirim order. Dev-only render checks menguji UI Three.js lokal; dependency
tersebut tidak disalin/diinstal di image worker. Tidak ada test autentikasi provider
melalui DOM. Pengujian sintetis tidak membuktikan koneksi API akun pengguna.

Future live execution memerlukan implementasi terpisah: fill/partial-fill
reconciliation, protective-order acknowledgment, cancellation of sibling exit,
reduce-only guarantees, recovery dan failure-to-protect handling. Paper plan hanya
mencatat kebutuhan itu; tidak mengklaim order/proteksi live sudah berfungsi.

Optional UI test setup (development only, not VPS image): install `playwright`,
then its local test engine, and run `OFFICE_UI_TESTS=1 python -m unittest discover
-s tests -v`. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can select an already installed test
binary. Without that environment opt-in the UI tests are skipped. Three.js
assets use the same CDN as the office, or a local `test-assets` cache when present.

Audit pre-deployment: [deploy/NEUROAPI_AUDIT.md](deploy/NEUROAPI_AUDIT.md).

Preflight Binance privat: [deploy/BINANCE_PREFLIGHT.md](deploy/BINANCE_PREFLIGHT.md).
`python -m worker binance-check` membaca akun/configuration, tidak menyimpan data
akun di dashboard/ledger dan tidak mengaktifkan live execution.

Phase 1 shadow/preflight: [deploy/BINANCE_SHADOW.md](deploy/BINANCE_SHADOW.md).
`python -m worker binance-shadow` memeriksa setup tersimpan dengan GET mainnet dan
menampilkan rencana inert; tidak mengirim order atau menjalankan riset baru.

Shadow/preflight implementation (live submission hard DISABLED, scheduler OFF): [deploy/BINANCE_LIVE.md](deploy/BINANCE_LIVE.md). No command or configuration can enable real order submission.
