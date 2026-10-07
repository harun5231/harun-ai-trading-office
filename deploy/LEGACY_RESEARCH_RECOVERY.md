# Pemulihan journal riset lama sebelum ROBOT ON

Kasus yang teramati: worker dan Binance terhubung, gateway `CONFIGURED`, tetapi
coordinator melaporkan `ROBOT_REQUEST_NEEDS_REVIEW`. Health GET NeuroAPI berhasil.
Diagnostics menampilkan tiga request `NEEDS_REVIEW`, termasuk satu
`INVALID_SCREENING_SYMBOL` dan dua tanpa kode kegagalan; ID-nya disamarkan.

Coordinator memeriksa seluruh `api_requests` dan `robot_jobs`, termasuk riwayat
lama. Migrasi awal mempertahankan `api_requests` untuk audit, sehingga journal
order kosong belum membuktikan journal riset bersih. ID yang disamarkan sendiri
belum membuktikan request berasal dari versi lama.

## Pemulihan terarah tanpa mengganti source VPS

`retire_legacy_research.py` memeriksa format operation di ledger secara lokal.
Utility hanya menerima format sebelum namespace `robot-v8`, sesuai source
historis repository. Format v8/v9, operation tidak dikenal, dan seluruh request
`PENDING` ditolak sebagai alasan pemulihan otomatis. Request `COMPLETE` tidak
diubah.

Sebelum perubahan, utility memerlukan ROBOT OFF, cycle lock yang sudah ada,
ledger yang valid, serta tidak adanya order intent, receipt entry, job/cycle
penghambat, atau referensi candidate/job/order terhadap request yang akan
dipensiunkan. Pemeriksaan diulang dalam transaksi sebelum menulis. Trigger dan
foreign key yang dapat membuat perubahan tak terduga juga diperiksa.

Untuk setiap request yang memenuhi syarat:

1. Buat backup SQLite lengkap dan privat pada volume data, di luar Git.
2. Simpan seluruh row asli dan fingerprint pada tabel arsip privat.
3. Ubah hanya state dari `NEEDS_REVIEW` menjadi `RETIRED_LEGACY` secara atomik.

Operation, attempts, failure code, output, dan field lainnya tetap dipertahankan.
State baru bukan klaim bahwa request berhasil atau belum ditagih. Outcome lama
yang tidak diketahui tetap tercatat pada arsip; request lama tidak dikirim ulang.
Request di luar lingkup yang diperiksa tetap menghalangi pipeline.

Tidak ada perubahan adapter, Dockerfile, Compose, secret, konfigurasi risiko,
receipt kuota harian, posisi manual, ataupun source worker. Tidak ada screening,
order, close/cancel posisi, ON otomatis, atau rebuild/restart container.

## Jalankan melalui Termius

Pilih **ROBOT OFF** pada Office dan pertahankan OFF di semua tab. Untuk inspeksi
tanpa perubahan, jalankan:

```bash
(
  set -euo pipefail
  cd /root/harun-ai-trading-office
  git fetch origin main
  git show origin/main:deploy/retire_legacy_research.py |
    docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data
)
```

Untuk menerapkan pemulihan yang memenuhi seluruh guard:

```bash
(
  set -euo pipefail
  cd /root/harun-ai-trading-office
  git fetch origin main
  git show origin/main:deploy/retire_legacy_research.py |
    docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - --data-dir /data --apply
  docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
)
```

`--apply` tetap mengklasifikasikan seluruh penghambat sebelum perubahan. Jika
scope, fingerprint, kondisi OFF, atau bukti order berbeda, tool berhenti dengan
kode error. Jangan mengganti namespace atau menghapus row untuk melewati guard.
Kirim output teredaksi tersebut untuk pemeriksaan lanjutan.

Output hanya memuat metadata yang dibatasi: scope, hash journal, tanggal yang valid,
count, kode kegagalan yang diizinkan, dan lokasi backup. Operation mentah,
payload, output model, token, serta credential tidak dicetak. Direktori backup
bermode `0700` dan file backup `0600`.

Sesudah sukses, robot tetap OFF. Pemulihan hanya menghilangkan penghambat dari
riwayat yang telah dipensiunkan; kesehatan NeuroAPI tidak membuktikan bahwa
screening berikutnya menghasilkan coin atau setup yang valid. ON berikutnya
memakai namespace aktif dan alur Futures yang sudah terpasang. Kegagalan baru
tetap dijournal dan tidak dibentuk menjadi setup/order sukses.

## Bukti dan pemulihan balik

Simpan path backup dan ringkasan fingerprint yang dicetak. Arsip menyimpan row
asli, sehingga status sebelumnya dapat ditinjau tanpa menganggap request
berhasil. Memulihkan seluruh ledger setelah robot kembali ON dapat menimpa
bukti entry atau receipt yang lebih baru; pemulihan balik perlu pemeriksaan
state saat itu dan tidak dilakukan otomatis oleh utility ini.

Diagnostics lama dapat menampilkan state arsip sebagai `STATE_REDACTED` karena
allowlist-nya belum mengenal `RETIRED_LEGACY`. Ringkasan utility membaca marker
tersebut secara eksplisit. Marker arsip tidak dipakai untuk membuat order atau
mengembalikan kuota harian.
