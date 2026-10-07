# Target net RR 1:2, coin pengganti, dan pemicu Terakhir

Pemasangan VPS pada 7 Oktober 2026 pukul 10:04 UTC menghasilkan
`VPS_RR_TARGET_LAST_PRICE_VERIFIED`: source host/container cocok dan worker
sehat. Snapshot menunjukkan robot ON, akun tersambung, satu posisi, satu
receipt entry hari itu, satu slot tersedia, dan tidak ada failure code.
Marker pemasangan membuktikan versi kode, bukan penerimaan order baru atau
TP/SL baru oleh Binance. Pemeriksaan 11:19 UTC membuktikan pencarian pengganti
berjalan sampai tiga putaran dan cycle epoch 1 `COMPLETE`, target 1, queue 0.
Kandidat terakhir ETHUSDT baru ditolak `NET_RISK_REWARD_NOT_TARGET_2` sebelum
order. Robot ON menunggu `INSUFFICIENT_ACTIONABLE_SETUPS /
ROBOT_CYCLE_COMPLETE`, tanpa failure robot/akun, dengan satu slot tersisa.
Request/job pending atau needs-review kosong. Laporan akun tetap `PARTIAL`;
belum ada order pengganti yang diterima, sehingga penerimaan `CONTRACT_PRICE`
dan partial fill versi baru belum dibuktikan.

Pemulihan ETH sebelumnya menghasilkan `ETH_PROTECTION_JOURNAL_VERIFIED`:
entry 0,181 ETH pada 2.610, SL 2.585, dan TP 2.730 terverifikasi, kedua
proteksi memakai `MARK_PRICE`, serta satu receipt tersimpan. Pembaruan
mempertahankan setup tersebut. SL kemudian menutup ETH; pencatatan GET-only
11:14 UTC membuktikan `CLOSED`, dengan satu receipt tetap tersimpan dan tidak
ada posisi aktif pada pemeriksaan 11:19 UTC. Lihat
[pemulihan closure dan request SOL](NEUROAPI_422_RECOVERY.md).
Script menolak pemasangan jika masih ada order
`SUBMITTING`/`NEEDS_REVIEW`; ia tidak mengubah status order bermasalah.

Kandidat `REJECTED / NET_RISK_REWARD_BELOW_2` atau
`REJECTED / NET_RISK_REWARD_NOT_TARGET_2` tanpa plan atau intent order boleh
meminta coin pengganti. Status dan alasan penolakan tetap tersimpan. HOLD dan
penolakan ini berbagi maksimal tiga putaran pengganti, hanya untuk hasil
putaran terakhir, sesuai slot dan budget. Kegagalan teknis lain, intent yang
sudah ada, dan outcome belum pasti tidak mendapat penggantian atau replay.

Screening pengganti memakai `message_history` yang sudah didukung NeuroAPI
untuk menyebut jumlah yang diperlukan dan mengecualikan coin yang sudah
terlihat, sedang exposed/pending, dan HYPEUSDT. Prompt inti dan schema satu/dua
coin tetap sama; screening awal tidak ditambah history. Validasi hasil tetap
menolak symbol yang tidak eligible.

Setup baru wajib memakai TP pada tick harga legal pertama yang memenuhi net
RR 1:2 setelah fee entry dan exit. LONG dibulatkan ke atas; SHORT ke bawah.
NeuroAPI diminta memberi harga itu; worker menolak TP yang terlalu dekat atau
lebih jauh dari target tersebut dan mencari coin pengganti sesuai batas tiga
putaran. Worker tidak mengubah harga entry, TP, atau SL hasil analisis. Plan dan
intent lama tetap mengikuti aturan yang tersimpan, termasuk TP ETH 2.730.

Setup baru juga menyimpan `protection_working_type=CONTRACT_PRICE`, sesuai
pemicu **Terakhir** yang dipilih pengguna pada formulir TP/SL Binance. SDK
mengirim dan memverifikasi basis harga dari intent masing-masing. Plan dan
intent lama yang memakai MARK_PRICE tetap menggunakan MARK_PRICE; pembaruan
ini tidak mengubah harga, basis pemicu, quantity, atau proteksi posisi lama.

Cycle epoch 1 dari [pemulihan satu slot](ONE_SLOT_RECOVERY.md) telah dilanjutkan
oleh kode baru dan selesai setelah tiga putaran. Jangan menjalankan helper
seed lagi: source pin helper tersebut
merekam versi lama. Tidak perlu menambah cycle, mereset counter, atau menghapus
journal. Target risiko default net 5 USDT termasuk fee, target net RR 1:2 pada
tick legal terdekat, CROSS 75, dua entry per hari, dan dua posisi/pending tetap
berlaku.

## Pemasangan melalui Termius setelah pemulihan ETH

Gunakan [script pemasangan gabungan](apply_rr_replacement.sh) yang diterima
dari percakapan. Simpan sebagai `/tmp/apply-rr-replacement.sh` pada VPS, lalu
jalankan hanya setelah TP/SL ETH dan pencatatan fill terverifikasi:

```bash
bash /tmp/apply-rr-replacement.sh
```

Script memakai `/root/harun-ai-trading-office` dan mengubah enam file:
`worker/robot.py`, `worker/core.py`, `worker/robot_provenance.py`,
`worker/analysis.py`, `worker/diagnostics.py`, dan bagian terarah pada
`worker/order_gateway.py`. Source awal wajib cocok dengan:

| File | SHA256 awal | SHA256 target |
| --- | --- | --- |
| robot.py | `4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef` | `cc0e1c6b68581fa23402dd61c9de023941a1f13489137282c83ae32ccf0dfa8d` |
| core.py | `11df540717e6e89a7da2777fa373723562f4ca05b87e3540c36c1902dd7cf9df` | `4b08349cc49470c47d2d29f0e6e2d72e086356f42d90b1afab92f98a4f0c6b5e` |
| robot_provenance.py | `f539eecb35ca57f8a8f91e9cbbcae2900f31e9dcfcd72246070d3d967582cb15` | `2975732f7f3888147ae50c81b2b53e85c8c0930c47310f88246378a53ceac082` |
| analysis.py | `f9712ce490f99061a45e32962d6489dd372a74aa18213001cc5d167208543a6d` | `1777b60c663fd971969b526a7c2c785eed3cd057d0edc70fb55453377428b97e` |
| diagnostics.py | `cf6802f0b5692e69f6573a36509828c70cdf226acd1894aff3b068a9c98fe190` | `2b28a42b0f03c76c97dc5111a33b2be167d346f8aba1103a7c0be10f08382f41` |
| order_gateway.py | `7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422` | Dihitung dari SDK lokal dan disimpan di manifest privat |

SDK lokal tidak diganti dengan adapter stub GitHub. Script membatasi perubahan
pada fungsi `build_intent`, metode `_validate_intent`, dan dua ekspresi
workingType untuk bukti/pengiriman proteksi dalam class adapter yang sudah
diaudit. Validator SDK memeriksa target net RR 1:2 untuk intent baru yang
memiliki penanda policy; validasi risiko tetap menggunakan batas USDT per SL.
Struktur AST awal dan target diverifikasi; source lain, fungsi
status, konstruktor, serta nilai konfigurasi/kredensial lokal tetap tersimpan.
Validator tambahan diimpor dalam fungsi build_intent, sehingga import privat
pada awal module tidak perlu ditulis ulang.

Semua source worker dan dua boot helper dibandingkan pada host/container.
Pemeriksaan awal dan pemeriksaan ulang sebelum restart menolak order
`SUBMITTING`/`NEEDS_REVIEW` serta request/job riset yang belum pasti. Backup
keenam source dan tag image lama dibuat sebelum patch. Hash SDK hasil patch
beserta manifest source tersimpan di direktori backup privat.

Image baru dibangun dan diperiksa tanpa jaringan sebelum worker lama dihentikan
secara graceful, agar callback proteksi yang sudah dimulai dapat selesai. Volume,
secret, Dockerfile, dan pilihan ON/OFF tetap digunakan. Script tidak memanggil
screening, pengiriman order, pemulihan journal, atau tick manual. Worker yang
sudah ON dapat melanjutkan pekerjaan otomatis setelah startup.

Pemasangan VPS telah menghasilkan `VPS_RR_TARGET_LAST_PRICE_VERIFIED` dan
snapshot worker sehat. Marker ini membuktikan source yang dipasang, bukan
penerimaan TP/SL Binance. Penerimaan
order tetap dibuktikan dari respons dan riwayat Binance. Penolakan net RR lama
tetap `REJECTED`; pemicu pada intent lama tetap sama.

Jika patch source, build, pemeriksaan image offline, atau startup gagal, script
memulihkan keenam source dan tag image lama. Jika worker sudah dihentikan,
script mencoba menjalankan kembali image lama. Kegagalan pemeriksaan setelah
startup sehat tidak otomatis rollback. Simpan output dan lokasi backup; jangan
memaksa fingerprint, menghapus journal, atau mengulang patch tanpa pemeriksaan.

Sesudah verifikasi VPS dan publikasi GitHub, script dapat diambil dari remote
tanpa `git pull` atau menimpa adapter lokal:

```bash
(
  set -euo pipefail
  cd /root/harun-ai-trading-office
  git fetch origin main
  git show origin/main:deploy/apply_rr_replacement.sh | bash
)
```

Script dipin untuk pemasangan sekali pada versi awal tersebut. VPS yang sudah
memperoleh source target tidak perlu menjalankannya ulang.
