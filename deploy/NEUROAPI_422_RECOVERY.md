# Request NeuroAPI, batas input, dan pemulihan tanpa replay

Coordinator memakai NeuroAPI `smart`, non-streaming, structured output untuk
screening dan analisis; NeuroAPI tidak mengirim order Binance. Claim operation
persis disimpan sebelum POST. Namespace tetap `robot-v9`/`analysis-v9` saat
risk model berubah menjadi V3.

## Batas request

[Changelog resmi NeuroAPI](https://neuroapi.neurobro.ai/docs/changelog)
menetapkan prompt maksimal 32.000 karakter, `message_history` maksimal 50
pesan, dan content tiap pesan maksimal 32.000 karakter.
[HTTP 422](https://neuroapi.neurobro.ai/docs/guides/errors) berarti respons
validasi request diterima; body yang sama tidak diputar ulang untuk pemulihan.

Module `worker/neuroapi_request.py` menjaga body kecil identik dengan bentuk
lama. Context besar dibagi menjadi pesan bernomor dalam satu request: seluruh
metadata, candle 15m/1h, harga, timestamp, filters, commission, dan risk constraints
tetap utuh serta berurutan. Tidak ada truncation, ringkasan pengganti, atau
POST terpisah per partisi. Prompt/jumlah pesan/content divalidasi sebelum POST.
Input yang tetap melampaui batas ditolak lokal dengan
`NEUROAPI_REQUEST_LIMIT_EXCEEDED`, tanpa request provider baru.

## Hasil provider dan validasi worker

Screening meminta symbol USD-M USDT perpetual aktif sesuai slot, tanpa symbol manual,
exposed, atau pending. Analisis model menghasilkan LONG/SHORT/HOLD dan
original Entry/TP/SL. HOLD memakai numeric fields null. Worker memeriksa
minimal declared RR 2 dan jarak harga minimal 1:2; hasil lengkap tetap diarsipkan
meskipun validasi lokal akhirnya menolak setup.

Plan V3 mempertahankan Entry/SL dan menormalisasi TP eksekusi ke net 1:2
berdasarkan fee akun fresh dan adverse exit reserve 0,5%. Original TP dan bukti
normalisasi terikat provenance. RR model lebih tinggi dari dua tidak ditolak
hanya karena TP model lebih jauh. Risk target existing ditangkap pada claim;
provider tidak menentukan size coin.

Respons COMPLETE yang cocok dapat diselesaikan lokal setelah crash antara
penerimaan output dan pembaruan job/cycle. Worker memverifikasi operation,
body/output hash, kind/symbol/cycle, attempts, target asal, serta metadata waktu
chart asli yang masih fresh. Waktu fetch/claim/server dan close candle 15m/1h
disimpan sebelum claim. Metadata hilang/stale ditolak lokal dengan
`STALE_MARKET_CONTEXT`; worker tidak membentuk POST baru, mengganti ID operation,
atau mereset replacement budget.

## Terminal 422 dan outcome unknown

HTTP 422 yang diterima menghasilkan request `REJECTED_REQUEST_VALIDATION`;
analysis job menjadi `REQUEST_REJECTED`. Candidate tetap `REJECTED / HTTP_422`
tanpa plan/intent. Tidak ada output model palsu atau claim COMPLETE.

Satu respons 422 dengan request/job terminal yang cocok, attempts satu,
output/idempotency null, tanpa plan/intent dapat meminta coin berbeda dalam
batas tiga putaran pengganti yang sama dengan HOLD/RR. Putaran lama tetap
terhitung. Request yang ditolak tidak diulang. Billing outcome tetap UNKNOWN:
status 422 sendiri tidak membuktikan apakah provider menagihnya atau penyebab
validasi spesifik ketika body/error historis tidak tersedia.

Timeout, network failure, response/provenance tidak valid, dan outcome tanpa
bukti tetap `NEEDS_REVIEW` serta memblokir pekerjaan baru. Jangan menghapus
claim, mengubah menjadi sukses, mereset attempts/counter, memakai operation
baru untuk replay, atau memaksa order Binance. Perlu review bukti kasus,
bukan menjalankan helper/settlement historis untuk versi berbeda.

## OFF dan diagnostics

Setiap request provider baru memeriksa ON pada batas pengiriman. OFF tidak
menarik request yang sudah terkirim; output yang diterima masih dicatat, tanpa
POST berikutnya. GET account/status tetap tersedia.

```bash
cd /root/harun-ai-trading-office
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker diagnostics
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker api-check
```

Diagnostics membaca state/attempts/kode aman, bukan raw output, token, signed
query, atau secret. Health provider yang connected bukan bukti suatu analisis
lolos atau order diterima. Lihat [alur robot](ROBOT_WORKFLOW.md) dan
[kontrak adapter](ORDER_INTEGRATION.md).
