# Owner-approved prompt literals; do not normalize LF or whitespace before sending.
# Do not reflow or normalize whitespace before sending.
SCREENING = 'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
REPLACEMENT_SCREENING = 'pilihkan {count} coin pengganti yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance'
ANALYSIS = """Aku berikan data chart realtime di binance future 2 time frame 1 jam dan 15 menit, silahkan analisa dengan akurat dan Profitable. aku mau entry di time frame 15 menit untuk scalping.
Tentukan !
LONG, SHORT, atau HOLD
ENTRY
TP
SL : berdasarkan struktur pasar, swing high/low, likuiditas, volatilitas, wick, false breakout, dan titik invalidasi; jangan letakkan di area yang mudah tersapu wick.
RISK REWARD 1:2"""
