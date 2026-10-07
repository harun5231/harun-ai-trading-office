# Pemulihan satu slot pada kasus VPS yang sudah dibuktikan

VPS mengonfirmasi `ONE_REPAIR_CYCLE_SEEDED`, `target:1`, `epoch:1`.
Pemeriksaan berikutnya mengonfirmasi satu job `SCREENING` dan satu job
`ANALYSIS` berstatus `COMPLETE`, tanpa intent order baru saat snapshot dibaca.
Kandidat baru BTC berstatus `REJECTED / NET_RISK_REWARD_BELOW_2`: setup tidak
memenuhi reward/risk bersih minimal 1:2 setelah fee. Penjadwalan dan riset
berhasil; order baru ditolak sebelum submission. ETH tetap pending dengan
fill nol, sehingga entry terisi dan TP/SL baru belum terbukti di Binance.

Pada pemeriksaan sebelumnya, cycle hari itu berstatus `COMPLETE` dengan target
2: BTC ditolak secara teknis setelah pemulihan absent order, sedangkan ETH
`ENTRY_PENDING` dengan fill nol. Penolakan BTC memakai satu kesempatan riset;
ETH memakai satu kesempatan lainnya. Budget cycle habis, tetapi slot posisi
masih tersedia. ETH pending tetap mencadangkan satu slot dan kuota harian,
meskipun counter posisi dan entry terisi masih nol.

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
epoch ke 1. Budget tidak direset, maksimum tiga penggantian HOLD tetap berlaku,
dan batas dua entry per hari serta dua posisi/pending tetap diperiksa. Posisi
carryover dan manual tetap menggunakan kapasitas yang sama.

## Inspeksi dan penerapan melalui Termius

Kasus ini sudah berhasil diterapkan; jangan membuat cycle tambahan. Helper
mempertahankan ON dan tidak melakukan restart. Rekonsiliasi proteksi ETH tetap
bergantung pada worker yang berjalan dan polling normal, saat ini 45 detik.
Cycle lock dapat menunda satu tick; jika worker sedang sibuk, helper menunggu
maksimum 15 detik lalu menolak dengan `WORKER_BUSY`.

Untuk file helper yang sudah disalin ke `/tmp/seed_one_slot.py` pada VPS,
jalankan inspeksi tanpa perubahan:

```bash
cd /root/harun-ai-trading-office
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data < /tmp/seed_one_slot.py
```

`--apply` hanya digunakan ketika inspeksi menghasilkan `REPAIR_READY` dan
pemulihan memang belum diterapkan:

```bash
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data --apply < /tmp/seed_one_slot.py
```

Setelah helper tersedia pada GitHub, file dapat diambil langsung dari remote
untuk inspeksi, tanpa `git pull` atau mengganti adapter lokal:

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
respons sudah diproses, bukan setup lolos validasi. `REJECTED` memakai budget
analisis, sedangkan `HOLD` mengikuti batas penggantian normal. Status utama
dapat kembali menampilkan cycle epoch 0 sebagai `ROBOT_CYCLE_COMPLETE`; alasan
kandidat terbaru tetap tersedia pada kandidat dan `last_decision`. Jangan
menambahkan cycle lain untuk melewati penolakan atau mengirim payload lama.
