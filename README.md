# HARUN AI TRADING OFFICE

Kantor 3D + worker privat NeuroAPI Starter (`smart`) dan Binance Futures public
market data. **DRY RUN ONLY**: tidak ada eksekutor atau route order Binance.
Autentikasi private Binance tersedia hanya untuk preflight GET read-only. Kantor/animasi/menu tetap; koneksi provider kini lewat API resmi.

Alur: prompt screening literal → tepat dua kontrak USDT perpetual aktif → OHLCV
1h + 15m termasuk candle current → prompt analisis literal + JSON data terpisah
→ keputusan LONG/SHORT/HOLD → Risk Manager → validator ACCEPT/REJECT → paper
LIMIT/CROSS/75x + rencana TP/SL. ENTRY/TP/SL dan keputusan Neurobro tidak diubah.
Quantity Neurobro disimpan sebagai audit; execution_quantity dihitung worker sebagai
quantity base asset legal terbesar dengan risiko SL ≤5 USDT (floor stepSize,
minQty/maxQty/minNotional). Tidak ada parsing prose atau request perbaikan setup.

Validator: harga/tick/rentang, quantity/step/min/max, min notional, percent-price
jika filter tersedia, arah TP/SL, risiko harga ≤5 USDT dan reward/risk ≥2 serta
rasio aktual berdasarkan ENTRY/TP/SL sebagai sumber kebenaran. Leverage tidak masuk rumus risiko.

State SQLite tetap privat/persisten: maksimal dua slot trade/hari total,
Asia/Bangkok, termasuk order paper pending. Satu siklus riset per hari; jika
terputus/ambigu, butuh review, tidak otomatis diulang. HOLD tidak memakai slot dan memicu replacement screening dengan prompt dua coin
yang sama, maksimal tiga request screening replacement per cycle. Kandidat pertama
yang eligible dan belum dianalisis dipilih. Technical REJECT/API failure tidak
memicu replacement. Stop saat dua setup valid; kekurangan setelah HOLD dicatat
INSUFFICIENT_ACTIONABLE_SETUPS. Cycle yang terputus tetap fail-closed tanpa replay. Retry hanya respons HTTP 429/503, maksimal tiga attempt dengan body sama.
Retry-After dihormati; jika melebihi 30 detik atau tidak dapat diparse, berhenti.
Tidak mengirim Idempotency-Key; deduplikasi menggunakan ledger lokal. Timeout/hasil tidak pasti berhenti;
request COMPLETE dapat dibaca kembali lokal tanpa tagihan baru. Tidak ada replay
otomatis setelah restart atau berdasarkan asumsi idempotency provider.

Default lookback 100 candle/frame, configurable 20–500; context freshness 180s
(default, 30–300s), mark ≤60s, jam server diperiksa. Data diperoleh dari API publik
langsung setiap analisis; filter kontrak diambil ulang sebelum reservasi. Data yang
kedaluwarsa selama analisis ditolak. Dashboard tidak menunjukkan saldo Binance
palsu; balance masih null. Monitoring memakai sampel mark price tiap ~30s saat
actor idle: paper fill/exit simulasi, bukan histori tick lengkap atau fill exchange.
Fees/slippage tidak disimulasikan; gap SL dapat membuat paper PNL melampaui 5 USDT.

## Menjalankan

Baca [deploy/DOCKER.md](deploy/DOCKER.md). API key hanya di secret VPS; dashboard
menerima token kontrol worker yang berbeda dan hanya menyimpannya di memori tab.
CEK API memanggil health provider tanpa prompt; JALANKAN DRY RUN memulai siklus
berbayar. Jadwal otomatis default mati; aktifkan OFFICE_AUTO_DRY_RUN=true hanya
setelah koneksi dan satu siklus nyata ditinjau. Tidak ada opsi LIVE.

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
binary. Without that environment opt-in the two UI tests are skipped. Three.js
assets use the same CDN as the office, or a local `test-assets` cache when present.

Audit pre-deployment: [deploy/NEUROAPI_AUDIT.md](deploy/NEUROAPI_AUDIT.md).

Preflight Binance privat: [deploy/BINANCE_PREFLIGHT.md](deploy/BINANCE_PREFLIGHT.md).
`python -m worker binance-check` membaca akun/configuration, tidak menyimpan data
akun di dashboard/ledger dan tidak mengaktifkan live execution.

Phase 1 shadow/preflight: [deploy/BINANCE_SHADOW.md](deploy/BINANCE_SHADOW.md).
`python -m worker binance-shadow` memeriksa setup tersimpan dengan GET mainnet dan
menampilkan rencana inert; tidak mengirim order atau menjalankan riset baru.
