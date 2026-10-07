# Pemulihan satu slot pada kasus VPS yang sudah dibuktikan

VPS mengonfirmasi `ONE_REPAIR_CYCLE_SEEDED`, `target:1`, `epoch:1`.
Pemeriksaan berikutnya mengonfirmasi satu job `SCREENING` dan satu job
`ANALYSIS` berstatus `COMPLETE`, tanpa intent order baru saat snapshot dibaca.
Pada snapshot tersebut, kandidat baru BTC berstatus `REJECTED / NET_RISK_REWARD_BELOW_2`: setup tidak
memenuhi reward/risk bersih minimal 1:2 setelah fee. Penjadwalan dan riset
berhasil; order baru ditolak sebelum submission. ETH saat itu masih pending
dengan fill nol.

Pemeriksaan sebelum exit mengonfirmasi ETH `POSITION_PROTECTED`: LONG 0.181
pada entry 2610, SL 2585, TP 2730, keduanya memakai `MARK_PRICE`, serta satu
receipt entry terverifikasi. Pemulihan journal tidak mengubah level, quantity,
atau basis pemicu intent ETH tersebut.

Pada pemeriksaan sebelumnya, cycle hari itu berstatus `COMPLETE` dengan target
2: BTC ditolak secara teknis setelah pemulihan absent order, sedangkan ETH
`ENTRY_PENDING` dengan fill nol. Penolakan BTC memakai satu kesempatan riset;
ETH memakai satu kesempatan lainnya. Budget cycle habis, tetapi slot posisi
masih tersedia. ETH pending tetap mencadangkan satu slot dan kuota harian,
meskipun pada snapshot itu counter posisi dan entry terisi masih nol.

Pembaruan aturan baru telah terpasang dan source container terverifikasi:
plan baru memakai
`NET_1_TO_2_NEAREST_TICK`, sehingga TP harus tepat pada tick legal terdekat yang
mencapai net RR 1:2 sesudah fee. Penolakan lokal `NET_RISK_REWARD_BELOW_2` atau
`NET_RISK_REWARD_NOT_TARGET_2` tanpa plan/intent order akan meminta screening
pengganti seperti HOLD, dengan batas bersama tiga putaran. Kandidat tetap
`REJECTED`; risiko default net 5 USDT termasuk fee tetap berlaku. Proteksi plan
baru memakai pemicu Terakhir (`CONTRACT_PRICE`). Plan lama tanpa field baru
tetap memakai net RR minimal 2 dan `MARK_PRICE`; ETH di atas tetap dipertahankan.
Pemasangan menghasilkan `PACKAGE_SHA256_OK`, `GATEWAY_PATCH_VERIFIED`,
`SOURCE_MATCH`, dan `VPS_RR_TARGET_LAST_PRICE_VERIFIED`. Worker healthy setelah
restart 7 Oktober 2026 pukul 10:04 UTC, dengan robot ON, satu posisi, satu
receipt, satu slot tersedia, dan target risiko 5 USDT. Snapshot bot
10:03:37 UTC masih berasal dari sebelum restart. Pemulihan closure ETH dan
request SOL kemudian terverifikasi pada 11:14 UTC. Pemeriksaan terbaru
11:19 UTC membuktikan ETH asli `CLOSED` tanpa failure, receipt tetap satu,
posisi aktif nol, dan request/job pending atau needs-review kosong.

Cycle epoch 1 telah `COMPLETE`, target 1, replacements 3, queue 0; kandidat
terakhir ETHUSDT baru ditolak `NET_RISK_REWARD_NOT_TARGET_2` sebelum order.
Epoch 0 juga `COMPLETE`, target 2, replacements 0, queue 0. Robot ON menunggu
`INSUFFICIENT_ACTIONABLE_SETUPS / ROBOT_CYCLE_COMPLETE` tanpa failure robot
atau akun. Satu slot tersisa dari kuota dua entry dengan satu receipt harian,
tetapi tiga putaran pengganti sudah habis tanpa setup valid. Belum ada order
pengganti diterima, sehingga `CONTRACT_PRICE` dan partial fill versi baru belum
dibuktikan. Laporan akun masih `PARTIAL`.

Cycle epoch 1 yang sudah dibuat telah dilanjutkan oleh coordinator yang
diperbarui, tanpa menjalankan helper seed lagi, menambah cycle, atau mereset
journal. Pemasangan gabungan enam file telah diverifikasi dengan backup di VPS;
publikasi GitHub mengikuti verifikasi source tersebut. Fungsi lain pada adapter lokal
tetap dipertahankan. Panduan tersedia pada
[target net RR 1:2, pengganti, dan pemicu Terakhir](RR_REPLACEMENT.md).

## Lingkup helper

[`seed_one_slot.py`](seed_one_slot.py) hanya berlaku untuk bukti kasus ini:

| Bukti yang dipin | Nilai |
| --- | --- |
| Client entry BTC lama | `hao-6dd9743f6a90afa1c72f0931932a` |
| SHA proof absence | `3121f0e78cc28215efc852ac0113765f80b8b92e55b8bce0f36b575d3ddad148` |
| SHA payload BTC | `b9478a9b5b08ed17704b9b4d286a5c4e7e7523d4d21b95897ed2250f07473879` |
| SHA snapshot ledger asal | `8479bc43a41d19e1f746e71b3ebb3dca03e66a8f5ec744aa6b279c615aac2d98` |
| SHA coordinator container | `4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef` |
| SHA adapter container | `7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422` |

Proof privat dan backup asal harus cocok di
`/data/trading/maintenance/absent-entry-guuwx8fy/`. Helper memeriksa hash proof,
row pada backup, serta BTC yang kini `REJECTED` dengan alasan
`NO_ACCEPTED_ORDER_OBSERVED`. Payload dan bukti lama harus identik; hanya
status penolakan, failure code, dan timestamp pembaruannya yang boleh berubah. Proof tersebut berasal dari
pemulihan [unknown order](UNKNOWN_ORDER_RECOVERY.md); helper tidak melakukan
pemeriksaan Binance baru.

Penerapan memerlukan robot ON, hari WIB yang sama, umur intent BTC kurang dari
48 jam, receipt entry hari ini nol,
hanya ETH pending yang teramati dengan fill nol, cycle awal tepat sesuai kasus,
dan tidak ada pekerjaan/order ambigu atau candidate siap lain. Helper mengambil
cycle lock, membuat backup privat baru, mengulang pemeriksaan dalam transaksi,
lalu menambahkan tepat satu cycle `YYYY-MM-DD:robot-v9:1` berstatus `ACTIVE`,
`entry_epoch:1`, `target:1`, antrean kosong, dan `screen:-1`.

Tidak ada row lama, payload, receipt, risiko, adapter, atau posisi yang diubah.
Epoch pada cycle baru tidak membuat receipt fill. Helper tidak mengimpor SDK,
mengirim request jaringan, melakukan screening, atau mengirim order. Dengan
robot tetap ON, coordinator kemudian menjalankan screening satu coin, analisis
baru, validasi, dan pengiriman melalui adapter yang sudah terpasang. ETH yang
masih exposed disaring; BTC dapat dipilih kembali melalui analisis baru dan
client ID baru, tanpa mengirim ulang intent BTC lama.

Coordinator memakai cycle numerik ini sebelum dan sesudah first fill menaikkan
epoch ke 1. Budget tidak direset; batas bersama tiga putaran pengganti tetap berlaku,
dan batas dua entry per hari serta dua posisi/pending tetap diperiksa. Posisi
carryover dan manual tetap menggunakan kapasitas yang sama.

## Inspeksi dan penerapan melalui Termius

Kasus ini sudah berhasil diterapkan; jangan menjalankan helper seed lagi atau
membuat cycle tambahan. Pin dan perintah berikut mendokumentasikan prosedur asal,
bukan langkah pembaruan setelah ETH fill atau perubahan source. Helper
mempertahankan ON dan tidak melakukan restart. Rekonsiliasi proteksi ETH tetap
bergantung pada worker yang berjalan dan polling normal, saat ini 45 detik.
Cycle lock dapat menunda satu tick; jika worker sedang sibuk, helper menunggu
maksimum 15 detik lalu menolak dengan `WORKER_BUSY`.

Dalam prosedur asal, file helper disalin ke `/tmp/seed_one_slot.py` pada VPS
dan inspeksi tanpa perubahan dilakukan dengan:

```bash
cd /root/harun-ai-trading-office
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data < /tmp/seed_one_slot.py
```

Penerapan asal menggunakan `--apply` hanya ketika inspeksi menghasilkan
`REPAIR_READY` dan pemulihan memang belum diterapkan:

```bash
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data --apply < /tmp/seed_one_slot.py
```

Cara pengambilan helper dari GitHub untuk inspeksi asal, tanpa `git pull`
atau mengganti adapter lokal, dicatat berikut:

```bash
(
  set -euo pipefail
  cd /root/harun-ai-trading-office
  git fetch origin main
  git show origin/main:deploy/seed_one_slot.py |
    docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data
)
```

Output `ALREADY_SCHEDULED` berarti cycle yang sama sudah ada; helper tidak
membuka ulang budget. `NO_CHANGE_ENTRY_ALREADY_FILLED` berarti ETH sudah memiliki
receipt terisi dan helper tidak perlu menambahkan cycle. `ONE_REPAIR_CYCLE_SEEDED`
mencetak lokasi backup privat bermode direktori `0700` dan file `0600`.

Penolakan mencetak `CYCLE_REPAIR_REFUSED` dengan stage tetap, misalnya
`SOURCE_MATCH`, `PROOF_BINDING`, `BTC_BINDING`, `ETH_OBSERVATION`,
`CYCLE_ELIGIBILITY`, `BACKUP`, atau `TRANSACTION`; lock sibuk memakai
`WORKER_BUSY` pada stage `LOCK`. Perbedaan bukti harus diperiksa, bukan diatasi
dengan mengganti pin, menghapus journal, atau mereset cycle.

Baca status tanpa memicu rekonsiliasi manual:

```bash
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
```

SL dipasang dan diverifikasi dahulu, kemudian TP, setelah SDK membuktikan fill
entry. Pemeriksaan status/helper sendiri tidak membuktikan proteksi diterima.
OFF menghentikan callback berikutnya tanpa membatalkan LIMIT pending; fill yang
terjadi setelah OFF tidak mendapat proteksi baru dari coordinator yang berhenti.

Jika job analisis `COMPLETE` tetapi intent baru belum ada, baca `status` dan
`failure_code` kandidat pada cycle pemulihan. `COMPLETE` pada job berarti
respons sudah diproses, bukan setup lolos validasi. Pada versi VPS saat observasi
tersebut, `REJECTED / NET_RISK_REWARD_BELOW_2` menghabiskan budget analisis.
Aturan baru di atas mengecualikan kedua kode penolakan net RR sebelum plan/intent
order dan memakai batas pengganti yang sama dengan HOLD. Penolakan lain tetap
memakai aturan sebelumnya. Status utama pada observasi tersebut
dapat kembali menampilkan cycle epoch 0 sebagai `ROBOT_CYCLE_COMPLETE`; alasan
kandidat terbaru tetap tersedia pada kandidat dan `last_decision`. Jangan
menambahkan cycle lain untuk melewati penolakan atau mengirim payload lama.
