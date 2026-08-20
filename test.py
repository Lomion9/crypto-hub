import ccxt
import requests
import pandas as pd
import numpy as np
import os
import time
import json
import sqlite3
from openpyxl import load_workbook, Workbook
from datetime import datetime, timedelta, timezone

# ==========================================
# -1. AYARLAR (config.json)
# ==========================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "timeframes": {
        "15dk": {"periods": 1, "oi_pct": 0.31, "price_pct": 0.22, "kapanis_esigi": 3, "sinir_saatleri": None},
        "1sa":  {"periods": 4, "oi_pct": 0.88, "price_pct": 0.43, "kapanis_esigi": 1, "sinir_saatleri": list(range(24)), "confirm_kaynak": "15dk", "confirm_n": 4},
        "2sa":  {"periods": 8, "oi_pct": 1.65, "price_pct": 0.72, "kapanis_esigi": 1, "sinir_saatleri": [1,3,5,7,9,11,13,15,17,19,21,23], "confirm_kaynak": "15dk", "confirm_n": 8},
        "4sa":  {"periods": 16, "oi_pct": 3.08, "price_pct": 1.08, "kapanis_esigi": 1, "sinir_saatleri": [23,3,7,11,15,19], "confirm_kaynak": "1sa", "confirm_n": 4},
        "8sa":  {"periods": 32, "oi_pct": 5.10, "price_pct": 1.36, "kapanis_esigi": 1, "sinir_saatleri": [3,11,19], "confirm_kaynak": "1sa", "confirm_n": 8},
        "24sa": {"periods": 96, "oi_pct": 7.73, "price_pct": 1.76, "kapanis_esigi": 1, "sinir_saatleri": [3], "confirm_kaynak": "4sa", "confirm_n": 6}
    },
    "funding_thresholds": {
        "extreme_pct": 0.0030
    },
    "adaptive": {
        "enabled": True,
        "lookback_days": 7,
        "quiet_days": 2,
        "noise_percentile": 80
    },
    "telegram": {
        "min_interval_minutes": 60
    },
    "debug": {
        "enabled": False,
        "interval_seconds": 30
    },
    "huobi": {
        "delay_seconds": 20
    }
}

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
            merged = json.loads(json.dumps(DEFAULT_CONFIG))  
            for section, values in user_config.items():
                if section in merged and isinstance(values, dict):
                    merged[section].update(values)
                else:
                    merged[section] = values
            return merged
        except Exception as e:
            print(f"  ⚠️ config.json okunamadı ({e}), varsayılan ayarlar kullanılıyor.")
            return DEFAULT_CONFIG
    else:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"  ℹ️ config.json bulunamadı, varsayılan ayarlarla oluşturuldu: {CONFIG_PATH}")
        return DEFAULT_CONFIG

CONFIG = load_config()

# ==========================================
# 0. TELEGRAM BİLDİRİMİ
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_LAST_TELEGRAM_DURUMLAR = {}  

def should_send_telegram(tf_sonuclari):
    global _LAST_TELEGRAM_DURUMLAR
    gonder = False
    for tf, sonuc in tf_sonuclari.items():
        gd = sonuc['genel_durum']
        onceki = _LAST_TELEGRAM_DURUMLAR.get(tf)
        if gd in ("İşlem Açma", "Veri Bekleniyor"):
            _LAST_TELEGRAM_DURUMLAR[tf] = gd
            continue
        if gd != onceki:
            if sonuc.get('telegram_uygun'):
                gonder = True
                _LAST_TELEGRAM_DURUMLAR[tf] = gd
    return gonder

def send_telegram_message(text, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil, mesaj gönderilmedi.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode
        }, timeout=10)
        if not resp.ok:
            print(f"  ❌ Telegram gönderim hatası: {resp.text}")
    except Exception as e:
        print(f"  ❌ Telegram gönderim hatası: {e}")

def build_telegram_report(failed_borsalar, total_oi, global_funding, price, cvd_spot, cvd_perp, fund_status, tf_sonuclari, kapanan_islemler):
    lines = []
    if failed_borsalar:
        lines.append(f"⚠️ <b>Bağlantı Sağlanamayan Borsalar:</b> {', '.join(failed_borsalar)}")
        lines.append("")
    lines.append(f"🌍 <b>Toplam OI:</b> {total_oi:,.2f} BTC")
    lines.append(f"💰 <b>Fiyat:</b> ${price:,.2f}")
    lines.append(f"⚖️ <b>Küresel Funding (8s):</b> %{global_funding:+.4f}  ({fund_status})")
    lines.append(f"📊 <b>Gün içi toplam CVD</b> — Spot: {cvd_spot:+.2f} BTC | Perp: {cvd_perp:+.2f} BTC")
    lines.append("")
    lines.append("🎯 <b>TIMEFRAME BAZLI SİNYAL DURUMU</b>")
    for tf, sonuc in tf_sonuclari.items():
        lines.append(f"[{tf}] {sonuc['genel_durum']}")
    if kapanan_islemler:
        lines.append("")
        lines.append("💰 <b>KAPANAN SİNYALLER</b>")
        for tf, k in kapanan_islemler.items():
            lines.append(f"[{tf}] {k['sinyal']} ({k['yon']}) -> %{k['kar_yuzde']:+.2f}")
    return "\n".join(lines)

# ==========================================
# 1. STANDART CCXT İLE ÇALIŞANLAR
# ==========================================
def get_standard_ccxt_data(exchange_name, symbol):
    try:
        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class({'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'timeout': 10000})
        
        oi_data = exchange.fetch_open_interest(symbol)
        oi = float(oi_data.get('baseVolume') or oi_data.get('openInterest') or 0)
        
        fund_data = exchange.fetch_funding_rate(symbol)
        funding = float(fund_data.get('fundingRate', 0)) * 100
        
        return oi, funding
    except Exception as e:
        return 0, 0

# ==========================================
# 2. ÖZEL MÜDAHALE GEREKTİRENLER
# ==========================================
def get_custom_bybit_data(category='linear', symbol='BTCUSDT'):
    try:
        bybit = ccxt.bybit({'enableRateLimit': True, 'timeout': 10000})
        resp_oi = bybit.publicGetV5MarketOpenInterest({'category': category, 'symbol': symbol, 'intervalTime': '5min'})
        oi = float(resp_oi['result']['list'][0]['openInterest'])
        
        resp_f = bybit.publicGetV5MarketTickers({'category': category, 'symbol': symbol})
        funding = float(resp_f['result']['list'][0]['fundingRate']) * 100
        
        if category == 'inverse':
            price = float(resp_f['result']['list'][0]['lastPrice'])
            oi = oi / price if price > 0 else 0
            
        return oi, funding
    except:
        return 0, 0

def get_custom_hyperliquid_data():
    try:
        url = "https://api.hyperliquid.xyz/info"
        res = requests.post(url, json={"type": "metaAndAssetCtxs"}, timeout=10).json()
        btc_idx = next(i for i, asset in enumerate(res[0]['universe']) if asset['name'] == 'BTC')
        
        oi = float(res[1][btc_idx]['openInterest'])
        funding = float(res[1][btc_idx]['funding']) * 100
        return oi, funding
    except:
        return 0, 0

def get_hyperliquid_funding_8h_real(coin='BTC'):
    try:
        url = "https://api.hyperliquid.xyz/info"
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = end_ms - 8 * 60 * 60 * 1000
        res = requests.post(url, json={
            "type": "fundingHistory",
            "coin": coin,
            "startTime": start_ms,
            "endTime": end_ms
        }, timeout=10).json()
        if not isinstance(res, list) or len(res) == 0:
            return None
        toplam = sum(float(entry['fundingRate']) for entry in res)
        return toplam * 100
    except Exception:
        return None

def get_custom_binance_usd_data():
    try:
        oi_url = "https://dapi.binance.com/dapi/v1/openInterest?symbol=BTCUSD_PERP"
        oi_resp = requests.get(oi_url, timeout=5).json()
        contracts = float(oi_resp['openInterest'])
        
        fund_url = "https://dapi.binance.com/dapi/v1/premiumIndex?symbol=BTCUSD_PERP"
        fund_resp = requests.get(fund_url, timeout=5).json()
        data = fund_resp[0] if isinstance(fund_resp, list) else fund_resp
        
        funding = float(data['lastFundingRate']) * 100
        price = float(data['markPrice'])
        
        oi_usd = contracts * 100
        oi_btc = oi_usd / price if price > 0 else 0
        return oi_btc, funding
    except Exception as e:
        return 0, 0
    
def get_custom_huobi_data(category='linear'):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36'}
    def _extract_rate(fund_data):
        if isinstance(fund_data, list):
            fund_data = fund_data[0] if fund_data else {}
        if not isinstance(fund_data, dict): return 0.0
        est = fund_data.get('estimated_rate')
        raw = est if est not in (None, '') else fund_data.get('funding_rate', 0)
        try: return float(raw) * 100
        except: return 0.0
    try:
        contract_code = "BTC-USDT" if category == 'linear' else "BTC-USD"
        base = "https://api.hbdm.com/linear-swap-api/v1" if category == 'linear' else "https://api.hbdm.com/swap-api/v1"
        
        oi_resp = requests.get(f"{base}/swap_open_interest?contract_code={contract_code}", headers=headers, timeout=10).json()
        oi_data = oi_resp.get('data', [])
        if not (isinstance(oi_data, list) and len(oi_data) > 0): return 0, 0
        oi_btc = float(oi_data[0].get('amount', 0))

        fund_resp = requests.get(f"{base}/swap_funding_rate?contract_code={contract_code}", headers=headers, timeout=10).json()
        funding = _extract_rate(fund_resp.get('data', {}))
        return oi_btc, funding
    except:
        return 0, 0

def get_binance_cvd(market_type='spot', symbol='BTCUSDT', interval='1h'):

    try:
        now_utc = datetime.now(timezone.utc)
        day_start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(day_start_utc.timestamp() * 1000)
        end_ms = int(now_utc.timestamp() * 1000)

        url = "https://api.binance.com/api/v3/klines" if market_type == 'spot' else "https://fapi.binance.com/fapi/v1/klines"
        res = requests.get(url, params={
            'symbol': symbol, 'interval': interval,
            'startTime': start_ms, 'endTime': end_ms, 'limit': 1000
        }, timeout=10).json()

        if not isinstance(res, list):
            print(f"  ❌ Binance CVD Hata ({market_type}): beklenmeyen cevap -> {res}")
            return 0.0

        cvd_btc = 0.0
        for candle in res:
            total_vol = float(candle[5])
            taker_buy_vol = float(candle[9])
            cvd_btc += (taker_buy_vol - (total_vol - taker_buy_vol))
        return cvd_btc
    except Exception as e:
        print(f"  ❌ Binance CVD Hata ({market_type}): {e}")
        return 0.0

def fetch_with_retry(fetch_func, *args, retries=2, delay=3, **kwargs):
    oi, funding = 0, 0
    for attempt in range(retries + 1):
        oi, funding = fetch_func(*args, **kwargs)
        if oi > 0:
            return oi, funding
        if attempt < retries:
            time.sleep(delay)
    return oi, funding

# ==========================================
# 3. KÜRESEL AĞIRLIKLI HESAPLAMA MOTORU
# ==========================================
def get_global_macro_data():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🌐 USDT ve USD Tahtalarından Makro Veriler Toplanıyor...")
    markets = {
        'Binance_USDT': fetch_with_retry(get_standard_ccxt_data, 'binance', 'BTC/USDT:USDT'),
        'Binance_USD': fetch_with_retry(get_custom_binance_usd_data),
        'Bybit_USDT': fetch_with_retry(get_custom_bybit_data, category='linear', symbol='BTCUSDT'),
        'Bybit_USD': fetch_with_retry(get_custom_bybit_data, category='inverse', symbol='BTCUSD'),
        'OKX_USDT': fetch_with_retry(get_standard_ccxt_data, 'okx', 'BTC/USDT:USDT'),
        'OKX_USD': fetch_with_retry(get_standard_ccxt_data, 'okx', 'BTC/USD:BTC'),
        'Hyperliquid': fetch_with_retry(get_custom_hyperliquid_data)
    }

    huobi_delay = CONFIG.get('huobi', {}).get('delay_seconds', 30)
    if huobi_delay > 0:
        print(f"  ⏳ Huobi çağrılarından önce {huobi_delay}sn bekleniyor (dakika sınırı izdihamından kaçınmak için)...")
        time.sleep(huobi_delay)

    markets['Huobi_USDT'] = fetch_with_retry(get_custom_huobi_data, category='linear')
    markets['Huobi_USD'] = fetch_with_retry(get_custom_huobi_data, category='inverse')
    
    hl_funding_8h_real = get_hyperliquid_funding_8h_real('BTC')

    if markets['Binance_USDT'][0] == 0:
        try:
            resp = ccxt.binance({'enableRateLimit': True, 'timeout': 10000}).fapiPublicGetOpenInterest({'symbol': 'BTCUSDT'})
            markets['Binance_USDT'] = (float(resp.get('openInterest', 0)), get_standard_ccxt_data('binance', 'BTC/USDT:USDT')[1])
        except: pass

    FUNDING_INTERVAL_HOURS = {k: 8 for k in markets.keys()}
    FUNDING_INTERVAL_HOURS['Hyperliquid'] = 1

    print("\n--- Borsa ve Tahta Kırılımları (Funding 8s'e normalize edilmiş) ---")
    normalized = {}
    failed_borsalar = []
    for borsa, (oi, funding) in markets.items():
        funding_8h = hl_funding_8h_real if (borsa == 'Hyperliquid' and hl_funding_8h_real is not None) else funding * (8 / FUNDING_INTERVAL_HOURS[borsa])
        normalized[borsa] = (oi, funding_8h)
        if oi > 0:
            etiket = "Funding(8s, gerçek)" if (borsa == 'Hyperliquid' and hl_funding_8h_real is not None) else "Funding(8s)"
            print(f"  ✅ {borsa.ljust(15)}: OI = {oi:>10,.2f} BTC  |  {etiket} = %{funding_8h:+.4f}")
        else:
            failed_borsalar.append(borsa)
            print(f"  ❌ {borsa.ljust(15)}: Bağlantı Sağlanamadı / Veri 0")

    total_oi_btc = sum(oi for oi, _ in normalized.values() if oi > 0)
    weighted_funding_sum = sum(oi * funding_8h for oi, funding_8h in normalized.values() if oi > 0)
    global_weighted_funding = (weighted_funding_sum / total_oi_btc) if total_oi_btc > 0 else 0

    print("-" * 60)
    print(f"  🌍 KÜRESEL TOPLAM OI         : {total_oi_btc:,.2f} BTC")
    print(f"  ⚖️ AĞIRLIKLI FONLAMA ORANI (8s): %{global_weighted_funding:+.4f}\n")

    return total_oi_btc, global_weighted_funding, failed_borsalar

# ==========================================
# 4. ZAMAN SERİSİ VE SİNYAL JENERATÖRÜ
# ==========================================
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oi_funding_history.db")
HISTORY_FILE = DB_FILE  
VERI_COLS = ['tarih', 'saat', 'oi_btc', 'oi_usd', 'funding_pct', 'price', 'cvd_spot_btc', 'cvd_perp_btc']

def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
        return float(r['price'])
    except: return 0.0

def _init_db(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS veri (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tarih TEXT, saat TEXT, oi_btc REAL, oi_usd REAL, funding_pct REAL,
        price REAL, cvd_spot_btc REAL, cvd_perp_btc REAL
    )''')
    for tf in CONFIG['timeframes'].keys():
        conn.execute(f'''CREATE TABLE IF NOT EXISTS durum_{tf} (
            id INTEGER PRIMARY KEY,
            tarih TEXT, saat TEXT, funding_durum TEXT, oi_durum TEXT,
            fiyat_durum TEXT, cvd_durum TEXT, genel_durum TEXT
        )''')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS sinyal_{tf} (
            kapanis_tarih TEXT, kapanis_saat TEXT, sinyal TEXT, yon TEXT,
            giris_tarih TEXT, giris_saat TEXT, giris_fiyat REAL, cikis_fiyat REAL, kar_yuzde REAL
        )''')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS aktif_islem_{tf} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            genel_durum TEXT, giris_fiyat REAL, giris_tarih TEXT, giris_saat TEXT, farkli_sayac INTEGER
        )''')
    conn.commit()

def load_history(path=DB_FILE):
    conn = sqlite3.connect(path)
    _init_db(conn)
    veri_df = pd.read_sql("SELECT * FROM veri", conn)
    conn.close()

    if veri_df.empty:
        return pd.DataFrame(columns=VERI_COLS + ['timestamp'])

    veri_df['funding_pct'] = veri_df['funding_pct'].astype(float)
    veri_df['timestamp'] = pd.to_datetime(veri_df['tarih'] + ' ' + veri_df['saat'], format='%d.%m.%Y %H:%M')
    return veri_df.sort_values('timestamp').reset_index(drop=True)

def funding_status(current_funding):
    current_funding = float(current_funding)
    esik = CONFIG['funding_thresholds']['extreme_pct']
    if current_funding > esik:
        return "Aşırı Pozitif"
    elif current_funding > 0.0000:
        return "Pozitif"
    elif current_funding < -esik:
        return "Aşırı Negatif"
    elif current_funding < 0.0000:
        return "Negatif"
    return "Nötr"

def _periyot_durumu(df_veri, mevcut_deger, periods, esik_pct, kolon):
    if len(df_veri) < periods:
        return "Veri Bekleniyor"
    pencere = df_veri[kolon].iloc[-periods:]
    if pencere.isna().any() or (pencere <= 0).any() or not mevcut_deger:
        return "Veri Bekleniyor"

    pencere_min = pencere.min()
    pencere_max = pencere.max()
    artis_pct = (mevcut_deger - pencere_min) / pencere_min * 100
    dusus_pct = (pencere_max - mevcut_deger) / pencere_max * 100

    if artis_pct <= esik_pct and dusus_pct <= esik_pct:
        return "Nötr"
    return "Artıyor" if artis_pct >= dusus_pct else "Düşüyor"

def _rolling_hareket_mesafesi(seri, periods):
    sonuc = []
    for i in range(periods, len(seri)):
        pencere = seri.iloc[i - periods:i]
        mevcut = seri.iloc[i]
        if pencere.isna().any() or (pencere <= 0).any() or not mevcut or mevcut <= 0:
            continue
        pmin, pmax = pencere.min(), pencere.max()
        artis = (mevcut - pmin) / pmin * 100
        dusus = (pmax - mevcut) / pmax * 100
        sonuc.append((i, max(artis, dusus)))
    return sonuc

def son_tf_genel_durumlar(conn, kaynak_tf, n):
    rows = conn.execute(
        f"SELECT genel_durum FROM durum_{kaynak_tf} ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]

def cvd_durumu(cvd_spot, cvd_perp):
    spot_yon = "Long" if cvd_spot > 0 else ("Short" if cvd_spot < 0 else "Nötr")
    perp_yon = "Long" if cvd_perp > 0 else ("Short" if cvd_perp < 0 else "Nötr")

    if spot_yon == "Nötr" or perp_yon == "Nötr":
        etiket = "Zayıf Sinyal"
    elif spot_yon == perp_yon:
        etiket = "Uyumlu"
    else:
        etiket = "Diverjans"

    return f"Spot {spot_yon} / Perp {perp_yon} ({etiket})"

def genel_durum(fund_status, oi_status, price_status, cvd_spot, cvd_perp):
    fund_positive = (fund_status == "Aşırı Pozitif")
    fund_negative = (fund_status == "Aşırı Negatif")

    long_trap = (fund_positive and oi_status == "Artıyor" and price_status == "Düşüyor")
    long_squeeze = (fund_positive and oi_status == "Düşüyor" and price_status == "Düşüyor")

    short_trap = (fund_negative and oi_status == "Artıyor" and price_status == "Artıyor")
    short_squeeze = (fund_negative and oi_status == "Düşüyor" and price_status == "Artıyor")

    if short_squeeze:
        return "Sağlıklı Long" if cvd_spot > 0 else "Short Squeeze"

    if long_squeeze:
        absorption_riski = cvd_spot > 0 and cvd_spot > abs(cvd_perp)
        if absorption_riski:
            return "İşlem Açma (Olası Absorption - Spot Alım Baskın)"
        return "Sağlıklı Short" if cvd_spot < 0 else "Long Squeeze"

    if long_trap:
        absorption_riski = cvd_spot > 0 and cvd_spot > abs(cvd_perp)
        if absorption_riski:
            return "İşlem Açma (Olası Absorption - Spot Alım Baskın)"
        return "Long Trap"

    if short_trap:
        dagitim_riski = cvd_spot < 0 and abs(cvd_spot) > abs(cvd_perp)
        if dagitim_riski:
            return "İşlem Açma (Olası Dağıtım - Spot Satış Baskın)"
        return "Short Trap"

    if price_status == "Nötr" and oi_status == "Artıyor":
        if cvd_spot > 0 and cvd_spot >= abs(cvd_perp):
            return "Akümülasyon"
        if cvd_spot < 0 and abs(cvd_spot) >= abs(cvd_perp):
            return "Dağıtım"

    return "İşlem Açma"

def _islem_yonu(genel_durum_deger):
    long_sinyaller = {"Sağlıklı Long", "Short Squeeze", "Short Trap", "Akümülasyon"}
    short_sinyaller = {"Sağlıklı Short", "Long Squeeze", "Long Trap", "Dağıtım"}
    if genel_durum_deger in long_sinyaller:
        return 'long'
    if genel_durum_deger in short_sinyaller:
        return 'short'
    return None


def sinyal_performans_guncelle(conn, tf, genel_durum_deger, price, tarih_str, saat_str, kapanis_esigi=3):
    row = conn.execute(f"SELECT genel_durum, giris_fiyat, giris_tarih, giris_saat, farkli_sayac FROM aktif_islem_{tf} WHERE id=1").fetchone()

    def durumu_kaydet(aktif, sayac):
        conn.execute(f"DELETE FROM aktif_islem_{tf} WHERE id=1")
        if aktif is not None:
            conn.execute(
                f"INSERT INTO aktif_islem_{tf} (id, genel_durum, giris_fiyat, giris_tarih, giris_saat, farkli_sayac) VALUES (1,?,?,?,?,?)",
                (aktif['genel_durum'], aktif['giris_fiyat'], aktif['giris_tarih'], aktif['giris_saat'], sayac)
            )

    def yeni_baslat(gd):
        return {'genel_durum': gd, 'giris_fiyat': price, 'giris_tarih': tarih_str, 'giris_saat': saat_str}

    if row is None:
        if not genel_durum_deger.startswith("İşlem Açma"):
            durumu_kaydet(yeni_baslat(genel_durum_deger), 0)
        return None

    aktif = {'genel_durum': row[0], 'giris_fiyat': row[1], 'giris_tarih': row[2], 'giris_saat': row[3]}
    sayac = row[4]

    if genel_durum_deger == aktif['genel_durum']:
        durumu_kaydet(aktif, 0)
        return None

    sayac += 1
    if sayac < kapanis_esigi:
        durumu_kaydet(aktif, sayac)
        return None

    giris_fiyat = aktif['giris_fiyat']
    yon = _islem_yonu(aktif['genel_durum'])
    ham_degisim = (price - giris_fiyat) / giris_fiyat * 100
    kar_yuzde = -ham_degisim if yon == 'short' else ham_degisim

    kapanan = {
        'kapanis_tarih': tarih_str, 'kapanis_saat': saat_str,
        'sinyal': aktif['genel_durum'], 'yon': yon or 'belirsiz',
        'giris_tarih': aktif['giris_tarih'], 'giris_saat': aktif['giris_saat'],
        'giris_fiyat': giris_fiyat, 'cikis_fiyat': price, 'kar_yuzde': kar_yuzde
    }
    conn.execute(
        f"INSERT INTO sinyal_{tf} (kapanis_tarih, kapanis_saat, sinyal, yon, giris_tarih, giris_saat, giris_fiyat, cikis_fiyat, kar_yuzde) VALUES (?,?,?,?,?,?,?,?,?)",
        (kapanan['kapanis_tarih'], kapanan['kapanis_saat'], kapanan['sinyal'], kapanan['yon'],
         kapanan['giris_tarih'], kapanan['giris_saat'], kapanan['giris_fiyat'], kapanan['cikis_fiyat'], kapanan['kar_yuzde'])
    )

    if not genel_durum_deger.startswith("İşlem Açma"):
        durumu_kaydet(yeni_baslat(genel_durum_deger), 0)
    else:
        durumu_kaydet(None, 0)

    return kapanan

def _periyot_cvd_degisimi(df_veri, current_cvd_spot, current_cvd_perp, periods, tarih_str):
    bugun_df = df_veri[df_veri['tarih'] == tarih_str]
    if bugun_df.empty:
        return None, None  

    if len(df_veri) >= periods and df_veri.iloc[-periods]['tarih'] == tarih_str:
        ref = df_veri.iloc[-periods]
    else:
        ref = bugun_df.iloc[0] 

    return current_cvd_spot - ref['cvd_spot_btc'], current_cvd_perp - ref['cvd_perp_btc']

def compute_adaptive_tf_thresholds(df_veri):
    ac = CONFIG.get('adaptive', {})
    if not ac.get('enabled', True):
        return None
    if 'tarih' not in df_veri.columns or len(df_veri) == 0:
        return None

    quiet_days_n = ac.get('quiet_days', 3)
    lookback_days = ac.get('lookback_days', 7)
    p = ac.get('noise_percentile', 90)

    gunler = sorted(df_veri['tarih'].unique(), key=lambda t: datetime.strptime(t, '%d.%m.%Y'))
    if len(gunler) < quiet_days_n:
        return None
    gunler = gunler[-lookback_days:]

    sonuc = {}
    for tf, tf_conf in CONFIG['timeframes'].items():
        periods = tf_conf['periods']

        oi_mesafe_pos = _rolling_hareket_mesafesi(df_veri['oi_btc'], periods)
        price_mesafe_pos = _rolling_hareket_mesafesi(df_veri['price'], periods)

        gun_gurultu = {}  # {gun: {'oi': persentil, 'price': persentil}}
        for gun in gunler:
            pos_set = set(df_veri.index[df_veri['tarih'] == gun])

            oi_mesafeler = [m for pos, m in oi_mesafe_pos if pos in pos_set]
            price_mesafeler = [m for pos, m in price_mesafe_pos if pos in pos_set]
            if len(oi_mesafeler) < 3 or len(price_mesafeler) < 3:
                continue
            gun_gurultu[gun] = {
                'oi': float(np.percentile(oi_mesafeler, p)),
                'price': float(np.percentile(price_mesafeler, p)),
            }

        if len(gun_gurultu) < quiet_days_n:
            sonuc[tf] = None
            continue

        en_sakin_oi = sorted(gun_gurultu.values(), key=lambda v: v['oi'])[:quiet_days_n]
        en_sakin_price = sorted(gun_gurultu.values(), key=lambda v: v['price'])[:quiet_days_n]
        sonuc[tf] = {
            'oi_pct': float(np.mean([v['oi'] for v in en_sakin_oi])),
            'price_pct': float(np.mean([v['price'] for v in en_sakin_price])),
        }
    return sonuc

def log_snapshot(oi, funding, price, cvd_spot, cvd_perp, path=HISTORY_FILE, now=None):
    if now is None:
        now = datetime.now(timezone(timedelta(hours=3)))
    now = now.replace(tzinfo=None)
    oi_usd = oi * price
    funding = float(funding)

    df_gecmis = load_history(path)  

    row_data = {
        'tarih': now.strftime('%d.%m.%Y'),
        'saat': now.strftime('%H:%M'),
        'oi_btc': oi,
        'oi_usd': oi_usd,
        'funding_pct': funding,
        'price': price,
        'cvd_spot_btc': cvd_spot,
        'cvd_perp_btc': cvd_perp,
    }

    fund_status = funding_status(funding)

    conn = sqlite3.connect(path, timeout=30)  
    _init_db(conn)

    cur = conn.execute(
        f"INSERT INTO veri ({','.join(VERI_COLS)}) VALUES ({','.join(['?']*len(VERI_COLS))})",
        tuple(row_data[c] for c in VERI_COLS)
    )
    yeni_id = cur.lastrowid
    tarih_str, saat_str = row_data['tarih'], row_data['saat']

    tf_sonuclari = {}
    kapanan_islemler = {}
    adaptif = compute_adaptive_tf_thresholds(df_gecmis)
    mevcut_saat, mevcut_dakika = now.hour, now.minute

    for tf, tf_conf in CONFIG['timeframes'].items():
        sinir_saatleri = tf_conf.get('sinir_saatleri')
        if sinir_saatleri is not None and (mevcut_dakika != 0 or mevcut_saat not in sinir_saatleri):
            continue  

        tf_adaptif = adaptif.get(tf) if adaptif else None
        oi_esik = tf_adaptif['oi_pct'] if tf_adaptif else tf_conf['oi_pct']
        price_esik = tf_adaptif['price_pct'] if tf_adaptif else tf_conf['price_pct']

        oi_durum = _periyot_durumu(df_gecmis, oi, tf_conf['periods'], oi_esik, 'oi_btc')
        fiyat_durum = _periyot_durumu(df_gecmis, price, tf_conf['periods'], price_esik, 'price')
        cvd_spot_delta, cvd_perp_delta = _periyot_cvd_degisimi(df_gecmis, cvd_spot, cvd_perp, tf_conf['periods'], tarih_str)

        if oi_durum == "Veri Bekleniyor" or fiyat_durum == "Veri Bekleniyor" or cvd_spot_delta is None:
            genel = "Veri Bekleniyor"
            cvd_durum_tf = "Veri Bekleniyor"
        else:
            cvd_durum_tf = cvd_durumu(cvd_spot_delta, cvd_perp_delta)
            genel = genel_durum(fund_status, oi_durum, fiyat_durum, cvd_spot_delta, cvd_perp_delta)

        conn.execute(
            f"INSERT INTO durum_{tf} (id, tarih, saat, funding_durum, oi_durum, fiyat_durum, cvd_durum, genel_durum) VALUES (?,?,?,?,?,?,?,?)",
            (yeni_id, tarih_str, saat_str, fund_status, oi_durum, fiyat_durum, cvd_durum_tf, genel)
        )

        if tf == '15dk':
            telegram_uygun = False
        elif genel == "Veri Bekleniyor":
            telegram_uygun = False
        else:
            kaynak_tf = tf_conf['confirm_kaynak']
            kaynak_n = tf_conf['confirm_n']
            son_durumlar = son_tf_genel_durumlar(conn, kaynak_tf, kaynak_n)
            telegram_uygun = genel in son_durumlar

        tf_sonuclari[tf] = {'oi_durum': oi_durum, 'fiyat_durum': fiyat_durum, 'cvd_durum': cvd_durum_tf,
                             'genel_durum': genel, 'telegram_uygun': telegram_uygun}

        if genel != "Veri Bekleniyor":
            kapanan = sinyal_performans_guncelle(conn, tf, genel, price, tarih_str, saat_str, tf_conf.get('kapanis_esigi', 3))
            if kapanan:
                kapanan_islemler[tf] = kapanan

    conn.commit()
    conn.close()

    print(f"\n🎯 ANLIK SİNYAL DURUMU (timeframe bazlı)")
    print(f"  Funding Durumu : {fund_status}   |   Gün içi toplam CVD -> Spot:{cvd_spot:+.2f} Perp:{cvd_perp:+.2f}")
    for tf in CONFIG['timeframes'].keys():
        if tf not in tf_sonuclari:
            continue  
        s = tf_sonuclari[tf]
        print(f"  [{tf:>4}] OI:{s['oi_durum']:<16} Fiyat:{s['fiyat_durum']:<12} CVD:{s['cvd_durum']:<10} -> {s['genel_durum']}")
    for tf, k in kapanan_islemler.items():
        print(f"  💰 [{tf}] SİNYAL KAPANDI: {k['sinyal']} ({k['yon']}) -> %{k['kar_yuzde']:+.2f}")

    return {
        'tarih': tarih_str, 'saat': saat_str, 'oi_btc': oi, 'price': price,
        'funding_durum': fund_status,
        'tf_sonuclari': tf_sonuclari, 'kapanan_islemler': kapanan_islemler
    }

def compute_trend(df, hours):
    if df.empty: return None
    cutoff = df['timestamp'].max() - pd.Timedelta(hours=hours)
    past = df[df['timestamp'] <= cutoff]
    if past.empty: return None
    
    ref = past.iloc[-1]
    last = df.iloc[-1]

    oi_change_pct = ((last['oi_btc'] - ref['oi_btc']) / ref['oi_btc'] * 100) if ref['oi_btc'] else None
    funding_change = last['funding_pct'] - ref['funding_pct']
    price_change_pct = ((last['price'] - ref['price']) / ref['price'] * 100) if ref['price'] else None

    return {'window_h': hours, 'oi_change_pct': oi_change_pct, 'funding_change': funding_change, 'price_change_pct': price_change_pct}

def print_trend_report(df):
    print("\n📈 ZAMAN SERİSİ TREND RAPORU")
    print("-" * 60)
    for h in [1, 4, 24]:
        t = compute_trend(df, h)
        if t is None:
            continue
        oi_s = f"{t['oi_change_pct']:+.2f}%" if t['oi_change_pct'] is not None else "N/A"
        fund_s = f"{t['funding_change']:+.4f}"
        price_s = f"{t['price_change_pct']:+.2f}%" if t['price_change_pct'] is not None else "N/A"
        print(f"  Son {h:>2}s  ->  OI: {oi_s:>9}   Funding Δ: {fund_s:>9}   Fiyat: {price_s:>9}")
    print("-" * 60)

def run_snapshot_and_report():
    baslangic_zamani = datetime.now(timezone(timedelta(hours=3))) 
    total_oi, global_funding, failed_borsalar = get_global_macro_data()

    if failed_borsalar:
        print(f"  ⏭️  Bu tur ATLANDI (kayıt eklenmedi) — veri alınamayan borsa(lar): {', '.join(failed_borsalar)}")
        return None

    price = get_btc_price()
    if price <= 0:
        print("  ⏭️  Bu tur ATLANDI (kayıt eklenmedi) — fiyat verisi alınamadı.")
        return None
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 CVD Verileri Hesaplanıyor (Bugün 00:00 UTC'den İtibaren)...")
    cvd_spot = get_binance_cvd('spot', 'BTCUSDT', interval='1h')
    cvd_perp = get_binance_cvd('futures', 'BTCUSDT', interval='1h')
    
    print(f"  📊 Spot CVD (bugün) : {cvd_spot:+.2f} BTC")
    print(f"  📊 Perp CVD (bugün) : {cvd_perp:+.2f} BTC\n")
    
    sonuc = log_snapshot(total_oi, global_funding, price, cvd_spot, cvd_perp, now=baslangic_zamani)

    report_text = build_telegram_report(
        failed_borsalar, total_oi, global_funding, price, cvd_spot, cvd_perp,
        sonuc['funding_durum'], sonuc['tf_sonuclari'], sonuc['kapanan_islemler']
    )
    if should_send_telegram(sonuc['tf_sonuclari']):
        send_telegram_message(report_text)
    else:
        print("  ⏳ Hiçbir timeframe'de yeni sinyal yok, Telegram mesajı atlanıyor.")

    df = load_history()
    print_trend_report(df)
    return df

def _sonraki_sinira_kadar_bekle(interval_minutes):
    simdi = datetime.now(timezone(timedelta(hours=3)))
    gun_baslangic = simdi.replace(hour=0, minute=0, second=0, microsecond=0)
    gecen_dakika = (simdi - gun_baslangic).total_seconds() / 60
    sonraki_dakika = (int(gecen_dakika // interval_minutes) + 1) * interval_minutes
    sonraki = gun_baslangic + timedelta(minutes=sonraki_dakika)
    bekleme_saniye = (sonraki - simdi).total_seconds()
    print(f"[{simdi.strftime('%H:%M:%S')}] Sonraki çalışma tam {sonraki.strftime('%H:%M:%S')}'de (~{bekleme_saniye/60:.1f} dk sonra)\n")
    time.sleep(max(bekleme_saniye, 0))

def run_continuous(interval_minutes=15):
    debug_cfg = CONFIG.get('debug', {})
    debug_on = debug_cfg.get('enabled', False)
    debug_interval = debug_cfg.get('interval_seconds', 30)

    if debug_on:
        print(f"⚠️  DEBUG MODU AKTİF (config.json -> debug.enabled=true): "
              f":00/:15/:30/:45 sınırı BEKLENMEYECEK, her {debug_interval} saniyede bir "
              f"snapshot alınacak. Bitince config.json'da debug.enabled'ı false yap.")
    else:
        print(f"Başlatılıyor: Her saatin {interval_minutes} dakikalık sabit dilimlerinde (örn. :00/:15/:30/:45) çalışılacak.")

    while True:
        if debug_on:
            time.sleep(debug_interval)
        else:
            _sonraki_sinira_kadar_bekle(interval_minutes)
        try:
            run_snapshot_and_report()
        except Exception as e:
            print(f"  ❌ Beklenmeyen hata: {e}")
            try:
                send_telegram_message(f"⚠️ <b>Bot hata verdi, bu tur kaydedilemedi:</b>\n{str(e)[:300]}")
            except Exception:
                pass  # Telegram'ın kendisi de başarısız olursa döngüyü yine de durdurmayalım

if __name__ == "__main__":
    run_continuous(15)