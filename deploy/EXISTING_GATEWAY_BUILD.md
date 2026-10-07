# Build gateway kustom yang sudah ada di VPS

Utility ini menyiapkan dependency/helper pada gateway kustom sederhana yang
sudah ada di `/root/harun-ai-trading-office`. Ia bukan installer revisi alur V3.
Untuk pembaruan alur current, gunakan package terarah yang diaudit di
[panduan Termius](TERMIUS_24_7.md), verifikasi VPS dahulu, lalu publikasi GitHub.

Jalankan utility dari Termius dengan **ROBOT OFF** dan pertahankan OFF. Source
privat, volume data, secret, dan proxy harus tetap utuh. Jangan menjalankan
`git pull`, `git reset`, atau updater yang menimpa file kustom. Guard menolak
source yang tidak sesuai; jangan memakai utility ini untuk melewati guard
installer alur atau memulihkan versi lain.

Jika file kustom kehilangan ekspor `build_intent` dan `require_implementation`
yang dibutuhkan coordinator, utility memulihkan hanya helper yang hilang dari
source Git HEAD dengan
SHA-256 `ba15012f49b7eafbac729f2e509590e4b03a4ba3544124d073c14c924f81642e`,
tanpa mengganti metode kustom. Jika ada import `requests`, utility menambahkan
instalasi `requests==2.32.5` pada Dockerfile lokal. Source dibaca dengan AST;
utility tidak mengimpor adapter, melakukan build, atau mengirim order.
Source asli dan manifest hash disimpan privat pada direktori sibling
`/root/harun-ai-trading-office-gateway-build-backups/`.

Untuk gateway yang sudah berhasil dibuild tetapi masih menampilkan
`connected:false`, `status:null`, dan `failure_code:null` sementara reader
`BINANCE_CONNECTED`, tersedia opsi **`--wire-file-secrets`**. Opsi ini menghubungkan
constructor kustom ke file `BINANCE_API_KEY_FILE` dan `BINANCE_API_SECRET_FILE`
yang sama dengan reader. Kode/default constructor yang ada dan seluruh metode
HTTP `submit`/`reconcile` dipertahankan; normalisasi status mempertahankan field
asal. `CONFIGURED` hanya berarti secret berhasil dimuat secara lokal, tanpa
membuktikan autentikasi Binance, penerimaan order, fill, atau proteksi.

Opsi wiring hanya menerima constructor sederhana dengan parameter string
`api_key=''`, `api_secret=''`, dan `base_url='https://fapi.binance.com'`, assignment
langsung ke atribut yang sudah dikenali, serta status berupa dict sederhana
yang memuat `connected:self._connected`. Fallback literal `getenv`, jika ada,
wajib kosong; field status yang dikenali sebagai credential juga ditolak.
Bentuk source lain ditolak tanpa mencoba menebak konfigurasinya.
Default credential kosong membaca kedua file
secret; pasangan credential eksplisit yang lengkap tetap dipakai. Pasangan
sebagian atau file yang tidak valid menghasilkan `BINANCE_NOT_CONFIGURED`,
tanpa mencampur sumber credential. Persiapan tanpa opsi ini tetap memakai
perilaku sebelumnya; backup dan guard restore berlaku untuk kedua pilihan.

Setelah utility tersedia di `origin/main`, jalankan seluruh blok ini. Pemeriksaan
OFF membaca ledger secara read-only tanpa mengimpor gateway atau memanggil API.
Kegagalan pemeriksaan atau command lain menghentikan blok sebelum langkah berikutnya.
Compose yang dipakai hanya `compose.yaml`, sesuai konfigurasi container existing.

```bash
cd /root/harun-ai-trading-office
check_robot_off() {
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
  check_robot_off
  git fetch origin main
  git show origin/main:deploy/prepare_gateway_build.py | python3 -I -B -S - --project /root/harun-ai-trading-office --wire-file-secrets
  docker compose -f compose.yaml config --quiet
  docker compose -f compose.yaml build worker
  check_robot_off
  docker compose -f compose.yaml stop -t 660 worker
  docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
  check_robot_off
  docker compose -f compose.yaml ps
)
```

Simpan path `Backup:` yang dicetak utility. Jika muncul `BASELINE_MISMATCH` atau
error lain, periksa penyebabnya sebelum melanjutkan; jangan melewati guard.
Untuk persiapan helper/dependency saja, hilangkan `--wire-file-secrets` dari
command utility di atas.
Build yang berhasil hanya memastikan dependency/import tersedia. Worker baru
harus tetap OFF. Verifikasi file image terhadap source host tanpa menjalankan
metode adapter, periksa import, lalu jalankan diagnostic akun GET dan status
read-only Office:

```bash
(
  set -euo pipefail
  check_robot_off
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
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -c 'from worker.order_gateway import OrderGateway, build_intent, require_implementation; print("GATEWAY_IMPORT_OK")'
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker binance-check
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
)
```

`BINANCE_CONNECTED` pada `binance-check` membuktikan pembacaan akun/config berhasil.
`live_execution:false` di diagnostic menjelaskan mode reader tersebut, bukan
status adapter. Label gateway `connected` juga tidak membuktikan POST diterima,
entry terisi, atau SL/TP sudah terpasang di Binance. Periksa metadata
`execution_gateway` pada `robot_status`; `CONFIGURED` tetap sebatas pemuatan
secret. Field `connected` adalah metadata, bukan sakelar eksekusi. Robot harus
tetap OFF sesudah seluruh pemeriksaan; polling read-only Office tetap berjalan.

Build dan wiring file secret hanya menyiapkan dependency/configuration lokal.
Verifikasi versi adapter current terhadap [ORDER_INTEGRATION.md](ORDER_INTEGRATION.md):
CROSS/75×, immutable intent, risk V3, strict OFF mutation guard, dan lifecycle
entry/protection/exit. Gunakan [audit source](AUDIT_BEFORE_ON.md) serta bukti GET
Binance untuk order existing. Pertahankan OFF sampai verifikasi selesai;
panduan build ini tidak mengaktifkan ON atau mengirim order entry/TP/SL.

Untuk mengembalikan dua file lokal, gunakan path backup yang dicetak utility:

```bash
(
  set -euo pipefail
  git show origin/main:deploy/prepare_gateway_build.py | python3 -I -B -S - --project /root/harun-ai-trading-office --restore /root/harun-ai-trading-office-gateway-build-backups/ID_BACKUP
)
```

Restore hanya berjalan bila hash file masih sama dengan hasil persiapan;
perubahan berikutnya tidak ditimpa. Restore file tidak mengganti image/container
yang sedang berjalan. `--restore` dipakai terpisah dari `--wire-file-secrets`.
Data volume dan secret tidak dihapus oleh utility.
