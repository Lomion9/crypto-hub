import ccxt
import requests
import pandas as pd
import os
import time
from openpyxl import load_workbook, Workbook
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. STANDART CCXT İLE ÇALIŞANLAR
# ==========================================
def get_standard_ccxt_data(exchange_name, symbol):
    try:
        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        
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
        bybit = ccxt.bybit({'enableRateLimit': True})
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
        res = requests.post(url, json={"type": "metaAndAssetCtxs"}).json()
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
    """Bugün UTC 00:00'dan (TR saatiyle 03:00) itibaren biriken net alım-satım baskısını (CVD)
    BTC cinsinden hesaplar. Rolling 24s pencere yerine gün başlangıcından biriktirme kullanıyoruz;
    böylece sayı 'bugün şu ana kadar net ne oldu' diye net bir anlam taşıyor, dünün kuyruğunu
    sessizce içine katmıyor."""
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

        cvd_btc = 0.0
        for candle in res:
            total_vol = float(candle[5])
            taker_buy_vol = float(candle[9])
            cvd_btc += (taker_buy_vol - (total_vol - taker_buy_vol))
        return cvd_btc
    except Exception as e:
        print(f"  ❌ Binance CVD Hata ({market_type}): {e}")
        return 0.0

# ==========================================
# 3. KÜRESEL AĞIRLIKLI HESAPLAMA MOTORU
# ==========================================
def get_global_macro_data():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🌐 USDT ve USD Tahtalarından Makro Veriler Toplanıyor...")
    markets = {
        'Binance_USDT': get_standard_ccxt_data('binance', 'BTC/USDT:USDT'),
        'Binance_USD': get_custom_binance_usd_data(),
        'Bybit_USDT': get_custom_bybit_data(category='linear', symbol='BTCUSDT'),
        'Bybit_USD': get_custom_bybit_data(category='inverse', symbol='BTCUSD'),
        'OKX_USDT': get_standard_ccxt_data('okx', 'BTC/USDT:USDT'),
        'OKX_USD': get_standard_ccxt_data('okx', 'BTC/USD:BTC'),
        'Huobi_USDT': get_custom_huobi_data(category='linear'),
        'Huobi_USD': get_custom_huobi_data(category='inverse'),
        'Hyperliquid': get_custom_hyperliquid_data()
    }
    
    hl_funding_8h_real = get_hyperliquid_funding_8h_real('BTC')

    if markets['Binance_USDT'][0] == 0:
        try:
            resp = ccxt.binance({'enableRateLimit': True}).fapiPublicGetOpenInterest({'symbol': 'BTCUSDT'})
            markets['Binance_USDT'] = (float(resp.get('openInterest', 0)), get_standard_ccxt_data('binance', 'BTC/USDT:USDT')[1])
        except: pass

    FUNDING_INTERVAL_HOURS = {k: 8 for k in markets.keys()}
    FUNDING_INTERVAL_HOURS['Hyperliquid'] = 1

    # Silinen Borsa Loglarını Geri Getirdik
    print("\n--- Borsa ve Tahta Kırılımları (Funding 8s'e normalize edilmiş) ---")
    normalized = {}
    for borsa, (oi, funding) in markets.items():
        funding_8h = hl_funding_8h_real if (borsa == 'Hyperliquid' and hl_funding_8h_real is not None) else funding * (8 / FUNDING_INTERVAL_HOURS[borsa])
        normalized[borsa] = (oi, funding_8h)
        if oi > 0:
            etiket = "Funding(8s, gerçek)" if (borsa == 'Hyperliquid' and hl_funding_8h_real is not None) else "Funding(8s)"
            print(f"  ✅ {borsa.ljust(15)}: OI = {oi:>10,.2f} BTC  |  {etiket} = %{funding_8h:+.4f}")
        else:
            print(f"  ❌ {borsa.ljust(15)}: Bağlantı Sağlanamadı / Veri 0")

    total_oi_btc = sum(oi for oi, _ in normalized.values() if oi > 0)
    weighted_funding_sum = sum(oi * funding_8h for oi, funding_8h in normalized.values() if oi > 0)
    global_weighted_funding = (weighted_funding_sum / total_oi_btc) if total_oi_btc > 0 else 0

    print("-" * 60)
    print(f"  🌍 KÜRESEL TOPLAM OI         : {total_oi_btc:,.2f} BTC")
    print(f"  ⚖️ AĞIRLIKLI FONLAMA ORANI (8s): %{global_weighted_funding:+.4f}\n")

    return total_oi_btc, global_weighted_funding

# ==========================================
# 4. ZAMAN SERİSİ VE SİNYAL JENERATÖRÜ
# ==========================================
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oi_funding_history.xlsx")
COLUMNS = ['tarih', 'saat', 'oi_btc', 'oi_usd', 'funding_pct', 'price', 'cvd_spot_btc', 'cvd_perp_btc', 'funding_durum', 'oi_durum', 'fiyat_durum', 'cvd_durum', 'genel_durum']
FUNDING_COL = COLUMNS.index('funding_pct') + 1

def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
        return float(r['price'])
    except: return 0.0

def load_history(path=HISTORY_FILE):
    if not os.path.isfile(path):
        return pd.DataFrame(columns=COLUMNS + ['timestamp'])
    
    df = pd.read_excel(path, engine='openpyxl')
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = 0.0 if col not in ['funding_durum', 'oi_durum', 'fiyat_durum', 'cvd_durum', 'genel_durum', 'tarih', 'saat'] else "N/A"
            
    df['funding_pct'] = df['funding_pct'].astype(float)
    df['timestamp'] = pd.to_datetime(df['tarih'] + ' ' + df['saat'], format='%d.%m.%Y %H:%M')
    return df.sort_values('timestamp').reset_index(drop=True)

def funding_status(current_funding):
    current_funding = float(current_funding)
    if current_funding > 0.030:
        return "Aşırı Pozitif"
    if current_funding > 0.0:
        return "Nötr Pozitif"
    if current_funding < -0.030:
        return "Aşırı Negatif"
    if current_funding < 0.0:
        return "Nötr Negatif"
    return "Nötr"


def compute_signals(df, current_oi, current_funding, current_price):
    """Son 3 kayda bakarak ivme (momentum) sinyallerini üretir"""
    fund_status = funding_status(current_funding)

    OI_THRESHOLD = 1000.0   
    PRICE_THRESHOLD = 150.0 
    
    if len(df) >= 3:
        last_3 = df.tail(3)
        max_oi = last_3['oi_btc'].max()
        min_oi = last_3['oi_btc'].min()
        
        if current_oi > (max_oi + OI_THRESHOLD): oi_status = "Artıyor"
        elif current_oi < (min_oi - OI_THRESHOLD): oi_status = "Düşüyor"
        else: oi_status = "Nötr"
            
        max_price = last_3['price'].max()
        min_price = last_3['price'].min()
        
        if current_price > (max_price + PRICE_THRESHOLD): price_status = "Artıyor"
        elif current_price < (min_price - PRICE_THRESHOLD): price_status = "Düşüyor"
        else: price_status = "Nötr"
    else:
        oi_status = "Veri Bekleniyor"
        price_status = "Veri Bekleniyor"
        
    return fund_status, oi_status, price_status

def cvd_durumu(cvd_spot, cvd_perp):
    """Spot ve perp CVD'nin yönünü karşılaştırıp diverjans olup olmadığını etiketler.
    Aynı yöndeyse baskı 'uyumlu' (spot ve kaldıraç aynı tarafta), zıt yöndeyse 'diverjans'
    (biri gerçek talebi, diğeri kaldıraç baskısını gösteriyor demektir)."""
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
    """Funding+OI+fiyat kombinasyonuyla squeeze/trap setup'larını tespit eder.
    Yükseliş tarafı: Short Squeeze (zayıf temel) vs Sağlıklı Long (organik talek destekli).
    Düşüş tarafı: Long Trap (OI artıyor - tuzağa düşme, henüz tasfiye değil) vs
    Long Squeeze (OI düşüyor - gerçek tasfiye/likidasyon şelalesi devam ediyor).
    Her iki düşüş senaryosunda da 'emniyet kilidi': spot alım baskısı perp tarafındaki
    satış/likidasyon baskısını (mutlak değerce) aşıyorsa, bu bir balina emilimi (absorption)
    olabilir - kör 'Short' sinyali vermek yerine uyarı döndürülüyor."""
    fund_positive = fund_status in ("Pozitif", "Aşırı Pozitif", "Nötr Pozitif")
    fund_negative = fund_status in ("Negatif", "Aşırı Negatif", "Nötr Negatif")

    yukselis_squeeze = (fund_negative and oi_status == "Düşüyor" and price_status == "Artıyor")
    long_trap = (fund_positive and oi_status == "Artıyor" and price_status == "Düşüyor")
    long_squeeze = (fund_positive and oi_status == "Düşüyor" and price_status == "Düşüyor")

    if yukselis_squeeze:
        return "Sağlıklı Long (Squeeze + Organik Talep)" if cvd_spot > 0 else "Short Squeeze (Zayıf Temel)"

    if long_trap or long_squeeze:
        absorption_riski = cvd_spot > 0 and cvd_spot > abs(cvd_perp)
        if absorption_riski:
            return "İşlem Açma (Olası Absorption - Spot Alım Baskın)"
        return "Long Trap (Devam Riski)" if long_trap else "Long Squeeze (Tasfiye Devam Ediyor)"

    return "İşlem Açma"

def log_snapshot(oi, funding, price, cvd_spot, cvd_perp, path=HISTORY_FILE):
    now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    oi_usd = oi * price
    funding = float(funding)
    
    df_history = load_history(path)
    fund_status, oi_status, price_status = compute_signals(df_history, oi, funding, price)
    cvd_status = cvd_durumu(cvd_spot, cvd_perp)
    genel = genel_durum(fund_status, oi_status, price_status, cvd_spot, cvd_perp)

    row_data = {
        'tarih': now.strftime('%d.%m.%Y'),
        'saat': now.strftime('%H:%M'),
        'oi_btc': oi,
        'oi_usd': oi_usd,
        'funding_pct': funding,
        'price': price,
        'cvd_spot_btc': cvd_spot,
        'cvd_perp_btc': cvd_perp,
        'funding_durum': fund_status,
        'oi_durum': oi_status,
        'fiyat_durum': price_status,
        'cvd_durum': cvd_status,
        'genel_durum': genel
    }
    
    # Yeni satırı mevcut dataframe'e ekle ve tamamını Excel'e bas (Başlık sorunu yaşanmaması için pandas kullanıyoruz)
    new_row_df = pd.DataFrame([row_data])
    
    # Yalnızca timestamp hariç sütunları kaydet
    save_cols = [c for c in df_history.columns if c != 'timestamp']
    df_to_save = pd.concat([df_history[save_cols], new_row_df], ignore_index=True)
    
    # Excel'e yaz
    df_to_save.to_excel(path, index=False, engine='openpyxl')
    wb = load_workbook(path)
    ws = wb.active
    for row in ws.iter_rows(min_row=2, min_col=FUNDING_COL, max_col=FUNDING_COL, max_row=ws.max_row):
        row[0].number_format = '0.0000'
    wb.save(path)
    wb.close()
    
    print(f"\n🎯 ANLIK SİNYAL DURUMU")
    print(f"  Fiyat Durumu   : {price_status}")
    print(f"  OI Durumu      : {oi_status}")
    print(f"  Funding Durumu : {fund_status}")
    print(f"  CVD Durumu     : {cvd_status}")
    print(f"  🧭 GENEL DURUM : {genel}")
    
    return pd.DataFrame([row_data])

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
    total_oi, global_funding = get_global_macro_data()
    price = get_btc_price()
    
    # CVD: bugün UTC 00:00'dan (TR 03:00) itibaren biriken net alım-satım baskısı
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 CVD Verileri Hesaplanıyor (Bugün 00:00 UTC'den İtibaren)...")
    cvd_spot = get_binance_cvd('spot', 'BTCUSDT', interval='1h')
    cvd_perp = get_binance_cvd('futures', 'BTCUSDT', interval='1h')
    
    print(f"  📊 Spot CVD (24s) : {cvd_spot:+.2f} BTC")
    print(f"  📊 Perp CVD (24s) : {cvd_perp:+.2f} BTC\n")
    
    log_snapshot(total_oi, global_funding, price, cvd_spot, cvd_perp)
    df = load_history()
    print_trend_report(df)
    return df

def run_continuous(interval_minutes=15):
    print(f"Başlatılıyor: Her {interval_minutes} dakikada bir snapshot çalıştırılacak.")
    while True:
        try:
            run_snapshot_and_report()
        except Exception as e:
            print(f"  ❌ Beklenmeyen hata: {e}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Sonraki çalışma {interval_minutes} dakikada başlayacak...\n")
        time.sleep(interval_minutes * 60)

if __name__ == "__main__":
    run_continuous(15)