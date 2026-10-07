# Panduan Termius: VPS 24/7 dan sambungan order Binance Futures

Panduan ini untuk instalasi existing di `/root/harun-ai-trading-office` dengan
Ubuntu 24.04, Docker Compose, dan gateway kustom yang sudah ditempel di VPS.
Worker berjalan di VPS sehingga Termius dan browser Office boleh ditutup.
Pertahankan **ROBOT OFF selama persiapan dan pemeriksaan**.

Untuk adapter teramati dengan fingerprint
`74b1db1b51461af34d0d4f0ecc189d4876bc72a6e021a049f9dcd22c9dd3509c`,
ikuti [perbaikan adapter dan coordinator VPS](VPS_ORDER_FIX.md) terlebih dahulu.
Prosedur itu mengganti class adapter yang sudah diaudit serta lima file worker,
dengan backup dan tanpa menghapus source kustom. Utility binding lama pada
bagian 2 hanya untuk instalasi yang belum memperoleh perbaikan tersebut.

**Pemeriksaan terdahulu belum membuktikan order otomatis siap:** reader Binance
berhasil, tetapi saat itu gateway kustom menampilkan `connected:false`, exception
TP/SL ditelan, dan konfigurasi CROSS/75 belum dikonfirmasi di exchange. Jika
adapter kemudian diubah langsung di VPS, periksa versi yang sekarang berjalan
melalui [audit source container](AUDIT_BEFORE_ON.md) dahulu. Utility file-secret
memperbaiki binding dan metadata; developer tetap perlu memverifikasi kontrak
bagian 5. Gateway standar GitHub tetap stub; gateway kustom berada di VPS.

Setelah pemulihan, ETH telah diverifikasi terisi 0.181 pada entry 2610,
terlindungi SL 2585 dan TP 2730 dengan `MARK_PRICE`, serta tercatat satu receipt.
Ini bukti historis: audit GET 10:45 UTC kemudian membuktikan posisi sudah
ditutup SL pada 10:07:39.052 UTC dengan average fill 2575.81. TP pasangan
`CANCELED`, tanpa posisi, open order, atau open algo ETH. Level dan basis
pemicu intent historis tidak diubah oleh pembaruan untuk order baru.
Untuk target net RR 1:2 pada tick terdekat dan pemicu Terakhir, gunakan
[pemasangan gabungan enam file](RR_REPLACEMENT.md); helper seed satu slot tidak
perlu dijalankan lagi. Pemasangan pembaruan tersebut telah diverifikasi di VPS
sebelum publikasi GitHub: `PACKAGE_SHA256_OK`, `GATEWAY_PATCH_VERIFIED`,
`SOURCE_MATCH`, dan `VPS_RR_TARGET_LAST_PRICE_VERIFIED`, dengan worker healthy
setelah restart 7 Oktober 2026 pukul 10:04 UTC. Robot ON, akun tersambung,
satu posisi, satu receipt, satu slot tersedia, dan risiko 5 USDT teramati.
Snapshot bot 10:03:37 UTC mendahului restart, sehingga hasil pengganti atau
order baru sesudah pemasangan masih perlu dibuktikan dari observasi terbaru.

Audit sesudah restart telah menunjukkan pergantian BTCUSDT yang ditolak RR
ke BNBUSDT yang ditolak target tick, lalu SOLUSDT yang menerima HTTP 422 pada
analisis sebelum order. Lihat
[pemulihan request HTTP 422 tanpa replay](NEUROAPI_422_RECOVERY.md) untuk
partisi context lengkap dan helper satu kasus tersebut. Installer sebelumnya
berhenti pada `UNRESOLVED_ORDER` setelah `SOURCE_MATCH`, sebelum mengubah source.
Pada audit tersebut journal ETH masih `NEEDS_REVIEW / BINANCE_ORDER_EXIT_RACE`
meskipun Binance sudah membuktikan exit. Paket lanjutan telah dijalankan
melalui Termius pada 7 Oktober 2026 pukul 11:14 UTC: closure GET-only tercatat,
receipt tetap satu, source cocok, dan worker healthy. Output mencakup
`ETH_CLOSED_JOURNAL_VERIFIED`, `SOL_HTTP_422_SETTLED`,
`NEUROAPI_422_RECOVERY_VERIFIED`, serta
`ETH_CLOSED_AND_NEUROAPI_FIX_INSTALLED`. Request SOL menjadi
`REJECTED_REQUEST_VALIDATION`, job `REQUEST_REJECTED`, cycle `ACTIVE`, dan
replacement counter tetap dua; pemeriksaan settlement tidak menemukan
unresolved order.

Pemeriksaan terbaru pada 11:19 UTC telah membuktikan request/job
`PENDING`/`NEEDS_REVIEW` kosong, ETH asli `CLOSED` tanpa failure, satu receipt,
dan posisi aktif nol. Cycle epoch 0 `COMPLETE` (target 2, replacement 0,
queue 0); epoch 1 `COMPLETE` (target 1, replacement 3, queue 0). Kandidat
terakhir epoch 1 adalah ETHUSDT baru yang ditolak
`NET_RISK_REWARD_NOT_TARGET_2` sebelum order.

Robot ON menampilkan `INSUFFICIENT_ACTIONABLE_SETUPS`,
`wait_reason=ROBOT_CYCLE_COMPLETE`, tanpa failure robot atau akun, dengan
`checked_at=2026-10-07T11:19:25.463839+00:00`. Akun `CONNECTED` pada
`2026-10-07T11:19:52.454Z`; gateway `CONFIGURED` dan source terverifikasi.
Satu slot tersisa karena satu receipt masih mengurangi kuota dua entry harian.
Batas tiga putaran sudah habis tanpa setup valid, sehingga menunggu bukan
blok teknis yang belum dipulihkan. Laporan dan riwayat masih `PARTIAL`.
Pemeriksaan ini belum membuktikan order baru `CONTRACT_PRICE` atau partial
fill versi baru diterima Binance. Jangan mengulang script gabungan RR atau HTTP 422,
mengirim entry ETH ulang, maupun mereset cycle dan receipt.

## 1. Periksa instalasi yang sedang berjalan

Masuk melalui Termius lalu salin blok berikut. Output tidak menampilkan secret.

```bash
cd /root/harun-ai-trading-office
git --no-optional-locks status --short
docker compose -f compose.yaml ps
docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' harun-office-worker-1
```

Konfigurasi yang terakhir teramati adalah
`/root/harun-ai-trading-office/compose.yaml`. Jika label menunjukkan file lain,
sesuaikan prosedur dengan developer sebelum mengganti container. File
`worker/order_gateway.py` yang modified adalah kode kustom yang harus disimpan.
File untracked seperti `compose.hotfix.yaml` dan `test_api.py` tetap dipertahankan;
keberadaan file hotfix sendiri tidak berarti file tersebut aktif.

## 2. Persiapkan secret, boot otomatis, dan image worker

Blok ini membaca status OFF dari ledger, mengaktifkan Docker saat boot,
mengaktifkan sinkronisasi waktu, lalu memakai utility `--wire-file-secrets`.
Utility menyimpan backup privat di luar Git, mempertahankan metode HTTP kustom,
dan membaca secret melalui loader yang sama dengan reader Binance.
Nilai secret tidak disalin ke command, Git, atau dashboard.

Gunakan seluruh blok berikut dalam satu sesi. `set -euo pipefail` menghentikan
urutan bila ada kegagalan. `git fetch` mengambil utility terbaru tanpa menimpa
working tree; prosedur ini tidak memakai `git pull/reset` atau updater standar.

```bash
cd /root/harun-ai-trading-office
harun_assert_robot_off() {
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - <<'PY'
import sqlite3
try:
    db = sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT enabled FROM robot_settings WHERE id=1').fetchone()
    finally:
        db.close()
except Exception:
    raise SystemExit('ROBOT_OFF_CHECK_FAILED') from None
if row != (0,):
    raise SystemExit('ROBOT_OFF_CHECK_FAILED')
print('ROBOT_OFF_CONFIRMED')
PY
}
(
  set -euo pipefail
  harun_assert_robot_off
  HARUN_COMPOSE_FILES="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' harun-office-worker-1)"
  if [ "$HARUN_COMPOSE_FILES" != /root/harun-ai-trading-office/compose.yaml ]; then
    printf '%s\n' COMPOSE_CONFIGURATION_MISMATCH >&2
    exit 1
  fi
  systemctl enable docker
  timedatectl set-ntp true
  git fetch origin main
  git show origin/main:deploy/prepare_gateway_build.py | python3 -I -B -S - --project /root/harun-ai-trading-office --wire-file-secrets
  docker compose -f compose.yaml config --quiet
  docker compose -f compose.yaml build worker
  harun_assert_robot_off
  docker compose -f compose.yaml stop -t 660 worker
  docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
  harun_assert_robot_off
  docker compose -f compose.yaml ps
)
```

Simpan path `Backup:` yang dicetak utility. Bentuk constructor/status yang belum
dikenali ditolak tanpa menebak atau menghapus kode. Jika blok gagal, selesaikan
error sebelum melanjutkan. Guard OFF tidak menghalangi orang lain mengubah
dashboard; pertahankan OFF di semua tab selama prosedur ini.

Volume ledger, secret, serta proxy tetap dipakai. Build selesai sebelum worker
dihentikan. Grace period 660 detik memberi waktu request yang sudah dimulai
menyimpan hasil; OFF tidak dapat menarik kembali request yang telah terkirim.
Pemulihan file menggunakan prosedur hash-guarded pada
[EXISTING_GATEWAY_BUILD.md](EXISTING_GATEWAY_BUILD.md).

## 3. Verifikasi image dan koneksi tanpa order

Pemeriksaan pertama membandingkan source host dan image tanpa mengimpor adapter.
Command selanjutnya hanya memeriksa proses, provider, akun, dan cache Office.
Tidak ada screening berbayar atau submission order dalam blok ini.

```bash
cd /root/harun-ai-trading-office
(
  set -euo pipefail
  HARUN_GATEWAY_SHA="$(python3 -I -B -S -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("worker/order_gateway.py").read_bytes()).hexdigest())')"
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_GATEWAY_SHA" <<'PY'
import ast
import hashlib
import sys
from pathlib import Path
raw = Path('/app/worker/order_gateway.py').read_bytes()
if hashlib.sha256(raw).hexdigest() != sys.argv[1]:
    raise SystemExit('GATEWAY_SOURCE_MISMATCH')
names = {node.name for node in ast.parse(raw).body if isinstance(node, ast.FunctionDef)}
if not {'build_intent', 'require_implementation'} <= names:
    raise SystemExit('GATEWAY_HELPERS_MISSING')
print('GATEWAY_SOURCE_MATCH')
PY
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker health
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker api-check
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker binance-check
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
)
```

Harapkan `GATEWAY_SOURCE_MATCH`, health `ONLINE`, `NEUROAPI_CONNECTED`, akun
`BINANCE_CONNECTED`, mode `ONE_WAY`, `can_trade:true`, dan single-asset.
Gateway yang berhasil memuat file secret menampilkan
`status:CONFIGURED`, `connected:true`, `failure_code:null`.
`CONFIGURED` berarti credential terbaca lokal; `BINANCE_CONNECTED` berarti GET
akun berhasil. `live_execution:false` pada diagnostic reader menjelaskan fungsi
reader tersebut. Ketiganya belum menjadi bukti penerimaan entry atau proteksi.

Periksa `robot_on:false`, `bot_status:OFF`, UID/GID 10001, state writable, dan lock
regular owner 10001 mode 0600. Jika HYPEUSDT manual masih terbuka, posisi tersebut
tetap tercatat sebagai manual exposure. Status laporan/history `PARTIAL` berarti
cakupan data belum lengkap,
sehingga total trade akun tidak boleh ditebak dari data tersebut.

## 4. Pastikan operasi VPS 24/7

```bash
cd /root/harun-ai-trading-office
systemctl is-active docker
systemctl is-enabled docker
timedatectl show -p NTPSynchronized --value
df -h /
HARUN_WORKER_ID="$(docker compose -f compose.yaml ps -q worker)"
docker inspect --format 'state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{end}} restart_policy={{.HostConfig.RestartPolicy.Name}} restarts={{.RestartCount}}' "$HARUN_WORKER_ID"
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker diagnostics
```

Harapkan Docker `active`/`enabled`, NTP `yes`, container `running`/`healthy`, dan
restart policy `unless-stopped`. NTP dapat memerlukan waktu untuk sinkronisasi;
periksa ulang sebelum penggunaan nyata. VPS harus tetap menyala dan mempunyai
koneksi ke Binance serta NeuroAPI. Worker mengevaluasi robot setiap 45 detik,
akun setiap 15 detik, dan history setiap 60 detik.

Restart policy memulihkan proses yang keluar. Health `unhealthy` sendiri tidak
memicu restart; watchdog worker mengakhiri proses ketika actor/account macet.
Log Docker dibatasi 10 MB × 3. Counter restart yang terus naik membutuhkan
pemeriksaan penyebab. Command `diagnostics` menampilkan kode riset/journal yang
disanitasi, tanpa payload atau secret. Hindari menampilkan raw log gateway
kustom yang mungkin memuat URL bertanda tangan.

Proses yang sengaja dihentikan tidak dipulihkan oleh `unless-stopped` saat boot.
Reboot terencana dan maintenance dilakukan setelah OFF dan drain, lalu periksa
kembali kesehatan, settings tersimpan, serta journal. ROBOT OFF tersimpan tetap
OFF setelah restart. Saat runtime kelak ON, browser/Termius bukan penjadwal.
Restart tidak menyelesaikan order dengan outcome tidak pasti: `NEEDS_REVIEW`
tetap memerlukan rekonsiliasi berbukti oleh developer.

## 5. Kontrak adapter otomatis yang harus diselesaikan developer

Seluruh pengiriman berada di `OrderGateway.submit/reconcile` kustom. Tabel ini
adalah spesifikasi implementasi, bukan command Termius untuk mengirim order.
Dokumentasi resmi Binance diperiksa pada 6 Oktober 2026:
[USD-M Futures REST Trade](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).

| Fase | Request dan pemeriksaan yang wajib ada |
| --- | --- |
| Akun dan kepemilikan | GET akun/posisi/open orders terbaru; One-way, single-asset, izin Futures, kapasitas dan margin tersedia. HYPEUSDT dan semua symbol dengan exposure manual dikecualikan. Hindari perubahan global mode akun yang memengaruhi posisi manual. |
| CROSS dan 75x | GET `/fapi/v1/symbolConfig` serta `/fapi/v1/leverageBracket`. Jika perlu, signed POST `/fapi/v1/marginType` dengan `marginType=CROSSED` dan `/fapi/v1/leverage` dengan `leverage=75`, hanya untuk symbol bot yang telah dipastikan bebas exposure lain. Baca ulang config dan batas notional sebelum entry. |
| Entry | Signed POST `/fapi/v1/order`: `type=LIMIT`, `timeInForce=GTC`, `positionSide=BOTH`, `price=intent.entry.price`, `quantity=intent.entry.quantity`, `newClientOrderId=intent.client_order_id`. LONG memakai BUY; SHORT memakai SELL. |
| SL | Signed POST `/fapi/v1/algoOrder`: `algoType=CONDITIONAL`, `type=STOP_MARKET`, `triggerPrice=intent.protection.stop_loss`, `workingType=intent.protection.working_type`, sisi exit berlawanan dengan entry, `positionSide=BOTH`, ID `clientAlgoId` deterministik. Plan baru memakai Terakhir (`CONTRACT_PRICE`); plan lama tanpa field pemicu tetap `MARK_PRICE`. Proteksi dengan quantity harus mengurangi exposure bot yang terisi (`reduceOnly=true`), tanpa `closePosition=true`. |
| TP | Endpoint algo yang sama dengan `type=TAKE_PROFIT_MARKET` dan `triggerPrice=intent.protection.take_profit`; basis pemicu, sisi, ownership, dan quantity proteksi diverifikasi seperti SL. |
| Konfirmasi | GET `/fapi/v1/order` berdasarkan client/order ID dan GET `/fapi/v1/algoOrder` berdasarkan clientAlgo/algo ID. Cocokkan symbol, sisi, quantity, harga/trigger, `workingType`, status, dan ID dengan intent serta fill aktual. |

Target internal `CROSS` dipetakan ke nilai Binance `CROSSED`. Menaruh field
`leverage:75` pada intent atau payload entry tidak mengatur leverage di exchange.
Jika symbol/notional tidak mendukung 75x, tolak setup; tidak ada penurunan
leverage otomatis. Jangan menganggap response konfigurasi berhasil sebelum
konfigurasi aktual dibaca ulang. Signing, waktu server, timeout, dan rate limit
ditangani di gateway; secret tidak dimasukkan ke journal/Office.

Quantity memakai unit coin/base asset, dihitung oleh worker dari Entry/SL dan
taker commission akun per symbol. Dengan `R` target risiko, `E` harga entry,
`S` harga SL, dan `f` taker fee:

```text
q_raw = R / (abs(E - S) + (E + S) * f)
q = floor_ke_stepSize(q_raw), dibatasi maxQty
planned_loss_SL = q * abs(E - S) + q * E * f + q * S * f <= R
```

Lot minimum, minimum notional, tick harga, dan fee aktual yang dibaca harus lolos.
Plan baru memakai `NET_1_TO_2_NEAREST_TICK`: TP model harus tepat pada tick legal
terdekat yang mencapai net RR 1:2 sesudah fee. TP di bawah net RR 2 ditolak dengan
`NET_RISK_REWARD_BELOW_2`; TP yang sudah melewati minimum tetapi tidak cocok
dengan target tick ditolak dengan `NET_RISK_REWARD_NOT_TARGET_2`. Worker tidak
menggeser harga model. Plan lama tanpa kebijakan ini tetap memakai net RR
minimal 2, termasuk ETH yang telah terisi. Leverage 75 tidak mengalikan quantity atau target
risiko. Panel Robot Trading menyimpan target risiko; default 5 USDT, perubahan
berlaku untuk analisis baru. Funding, slippage, gap, serta likuidasi tidak
tercakup dalam estimasi 5 USDT; adapter harus memeriksa kecukupan margin dan
risiko likuidasi sebelum entry.

Entry LIMIT yang diterima bisa tetap pending. Partial fill langsung membutuhkan
proteksi untuk exposure terisi, kemudian penyesuaian ketika tambahan fill masuk.
Entry dan kedua proteksi bukan transaksi atomik. `except: pass` pada TP/SL
harus diganti penanganan kegagalan yang terverifikasi. Developer menentukan
pemulihan proteksi/exposure milik intent tersebut, menghentikan entry berikutnya,
dan melaporkan `NEEDS_REVIEW` bila hasil belum pasti. Hindari retry entry dengan
ID baru setelah timeout; baca hasil exchange menggunakan ID stabil.

Adapter hanya melaporkan `POSITION_PROTECTED` setelah fill positif dan kedua
proteksi dikonfirmasi di Binance. Waktu fill pertama tetap menjadi dasar satu
receipt entry; pending order, partial fill tambahan, expiry/cancel, exit dan
pembersihan proteksi pasangan harus direkonsiliasi tanpa mengambil alih order
manual. Kontrak hasil lengkap tersedia di [ORDER_INTEGRATION.md](ORDER_INTEGRATION.md).
Bukti order diterima mencakup ID/status entry dari exchange; bukti posisi
terlindungi mencakup fill serta ID dan parameter SL/TP yang cocok. Status sehat
Office, ACK, dan kelulusan tes lokal sendiri tidak membuktikan semua itu.

## 6. Perilaku otomatis setelah adapter tervalidasi

Pemilik akun mengendalikan ON/OFF melalui Office setelah developer memverifikasi
adapter dan penanganan kegagalannya. Alurnya:

`ON → akun/slot → screening → chart Futures 1h/15m → LONG/SHORT/HOLD → sizing → validasi → CROSS/75 terkonfirmasi → entry → fill/proteksi → rekonsiliasi`

Maksimal dua entry bot baru per hari WIB (UTC+7) dan dua posisi bersamaan,
termasuk posisi manual dan carryover. Pending entry mencadangkan kuota serta
slot. Dengan satu HYPEUSDT manual, paling banyak satu coin lain diambil.
Dua posisi aktif menyisakan nol slot. Pergantian hari tidak menutup posisi lama;
posisi yang selesai tidak mengembalikan kuota harian yang sudah dipakai.
HOLD dan penolakan lokal `NET_RISK_REWARD_BELOW_2` atau
`NET_RISK_REWARD_NOT_TARGET_2` tanpa plan/intent order
meminta pengganti dari putaran terakhir, dengan maksimum tiga screening
tambahan bersama dan tetap mengikuti slot. Penolakan tetap `REJECTED`;
kegagalan teknis lain serta unknown tidak diganti.
Setelah cycle selesai pada hari dan jumlah entry terkonfirmasi yang sama,
screening berbayar tidak diulang tanpa batas. Jika putaran pengganti habis tanpa
setup/fill, cycle menunggu hari baru atau fill terkonfirmasi yang mengubah
epoch. Polling 45 detik tetap berjalan selama menunggu.

OFF menghentikan riset, submission baru, dan pemanggilan rekonsiliasi gateway
berikutnya. OFF tidak menutup posisi atau membatalkan entry/TP/SL. Proteksi yang
sudah diterima tetap berada di Binance. Request yang sudah terkirim dapat selesai;
GET akun/history tetap berjalan dan tampil di Office. Pemeriksaan serta
maintenance dalam panduan ini tidak mengaktifkan robot atau mengirim order.
