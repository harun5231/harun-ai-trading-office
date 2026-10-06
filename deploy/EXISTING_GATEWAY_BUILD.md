# Build gateway kustom yang sudah ada di VPS

Panduan ini menyiapkan file gateway kustom yang sudah ditempel di
`/root/harun-ai-trading-office`, lalu mengganti container worker sambil menjaga
volume data, secret, dan proxy. Jalankan dari Termius; Codex belum menjalankan
langkah ini di VPS. Pilih **ROBOT OFF** di Office dan pertahankan OFF.
Jangan menjalankan `git pull`, `git reset`, atau updater yang menimpa file kustom.

File kustom menggantikan ekspor `build_intent` dan `require_implementation` yang
dibutuhkan coordinator, serta mengimpor `requests` yang belum ada pada image
standar. Utility memulihkan hanya helper yang hilang dari source Git HEAD dengan
SHA-256 `ba15012f49b7eafbac729f2e509590e4b03a4ba3544124d073c14c924f81642e`,
tanpa mengganti metode kustom. Jika ada import `requests`, utility menambahkan
instalasi `requests==2.32.5` pada Dockerfile lokal. Source dibaca dengan AST;
utility tidak mengimpor adapter, melakukan build, atau mengirim order.
Source asli dan manifest hash disimpan privat pada direktori sibling
`/root/harun-ai-trading-office-gateway-build-backups/`.

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
  git show origin/main:deploy/prepare_gateway_build.py | python3 -I -B -S - --project /root/harun-ai-trading-office
  docker compose -f compose.yaml config --quiet
  docker compose -f compose.yaml build worker
  check_robot_off
  docker compose -f compose.yaml stop -t 660 worker
  docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
  docker compose -f compose.yaml ps
)
```

Simpan path `Backup:` yang dicetak utility. Jika muncul `BASELINE_MISMATCH` atau
error lain, periksa penyebabnya sebelum melanjutkan; jangan melewati guard.
Build yang berhasil hanya memastikan dependency/import tersedia. Worker baru
harus tetap OFF. Verifikasi file image terhadap source host tanpa menjalankan
adapter, kemudian jalankan diagnostic akun yang hanya melakukan GET:

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
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker binance-check
)
```

`BINANCE_CONNECTED` pada `binance-check` membuktikan pembacaan akun/config berhasil.
`live_execution:false` di diagnostic menjelaskan mode reader tersebut, bukan
status adapter. Label gateway `connected` juga tidak membuktikan POST diterima,
entry terisi, atau SL/TP sudah terpasang di Binance.

Adapter kustom yang dibahas masih menelan exception TP/SL sesudah entry, belum
menunjukkan konfigurasi CROSS/leverage 75 secara eksplisit, dan memakai `getenv`
dengan pemetaan ke file secret VPS yang belum terbukti. Bukti fill pertama serta
lifecycle/rekonsiliasi juga belum diverifikasi. Developer manusia perlu
memperbaiki bagian tersebut sesuai [ORDER_INTEGRATION.md](ORDER_INTEGRATION.md)
sebelum penggunaan nyata. Panduan build ini tidak mengaktifkan ON atau mengirim
order entry/TP/SL.

Untuk mengembalikan dua file lokal, gunakan path backup yang dicetak utility:

```bash
(
  set -euo pipefail
  git show origin/main:deploy/prepare_gateway_build.py | python3 -I -B -S - --project /root/harun-ai-trading-office --restore /root/harun-ai-trading-office-gateway-build-backups/ID_BACKUP
)
```

Restore hanya berjalan bila hash file masih sama dengan hasil persiapan;
perubahan berikutnya tidak ditimpa. Restore file tidak mengganti image/container
yang sedang berjalan. Data volume dan secret tidak dihapus oleh utility.
