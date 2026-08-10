import ccxt
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. STANDART CCXT İLE ÇALIŞANLAR
# ==========================================
def get_standard_ccxt_data(exchange_name, symbol):
    try:
        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        
        # OI ve Funding çekimi
        oi_data = exchange.fetch_open_interest(symbol)
        # baseVolume genelde normalize edilmiş (BTC cinsinden) değeri verir
        oi = float(oi_data.get('baseVolume') or oi_data.get('openInterest') or 0)
        
        fund_data = exchange.fetch_funding_rate(symbol)
        funding = float(fund_data.get('fundingRate', 0)) * 100
        
        return oi, funding
    except Exception as e:
        return 0, 0

# ==========================================
# 2. ÖZEL MÜDAHALE GEREKTİRENLER (BYBIT & HYPERLIQUID)
# ==========================================
def get_custom_bybit_data(category='linear', symbol='BTCUSDT'):
    """Bybit için hem USDT (linear) hem de USD (inverse) çekebilir"""
    try:
        bybit = ccxt.bybit({'enableRateLimit': True})
        
        # OI İsteki
        resp_oi = bybit.publicGetV5MarketOpenInterest({'category': category, 'symbol': symbol, 'intervalTime': '5min'})
        oi = float(resp_oi['result']['list'][0]['openInterest'])
        
        # Funding & Fiyat İsteki
        resp_f = bybit.publicGetV5MarketTickers({'category': category, 'symbol': symbol})
        funding = float(resp_f['result']['list'][0]['fundingRate']) * 100
        
        # USD (Inverse) kontratlar Bybit'te 1 USD'dir. Gerçek BTC miktarını bulmak için fiyata bölüyoruz.
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
    """Hyperliquid saatlik funding ödediği için son 8 saatin gerçekleşmiş tüm oranlarını
    tek tek çekip toplar. Bu, tek bir anlık orana dayanan (funding_1h * 8) tahminden farklı
    olarak, diğer borsaların 8 saatlik oranıyla doğrudan karşılaştırılabilir gerçek bir değerdir."""
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
    """Binance USD (Coin-M) Tahtası - Doğrudan DAPI Bağlantısı"""
    try:
        # 1. Open Interest İstediği (DAPI)
        oi_url = "https://dapi.binance.com/dapi/v1/openInterest?symbol=BTCUSD_PERP"
        oi_resp = requests.get(oi_url, timeout=5).json()
        contracts = float(oi_resp['openInterest'])
        
        # 2. Funding Rate ve Fiyat İsteği (DAPI)
        fund_url = "https://dapi.binance.com/dapi/v1/premiumIndex?symbol=BTCUSD_PERP"
        fund_resp = requests.get(fund_url, timeout=5).json()
        
        # Binance API'si bazen liste, bazen sözlük (dict) döner, bunu garantiye alıyoruz:
        data = fund_resp[0] if isinstance(fund_resp, list) else fund_resp
        
        funding = float(data['lastFundingRate']) * 100
        price = float(data['markPrice'])
        
        # 3. Kontratı BTC'ye Çevirme (Binance'te 1 BTC kontratı = 100 USD değerindedir)
        oi_usd = contracts * 100
        oi_btc = oi_usd / price if price > 0 else 0
        
        return oi_btc, funding
    except Exception as e:
        return 0, 0
    
def get_custom_huobi_data(category='linear'):
    """Huobi (HTX) - Doğrudan REST API Bağlantısı (Canlı Fonlama Oranı + Doğru OI Hesabı)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36'
    }

    def _extract_rate(fund_data):
        # data bazen dict bazen list gelebiliyor -> tek noktadan normalize et
        if isinstance(fund_data, list):
            fund_data = fund_data[0] if fund_data else {}
        if not isinstance(fund_data, dict):
            return 0.0

        est = fund_data.get('estimated_rate')
        # ESKİ HATA: `est or fund_data.get('funding_rate', 0)` -> est gerçekten
        # sayısal 0 gelirse Python bunu falsy sayıp yanlışlıkla funding_rate'e
        # düşüyordu. None kontrolü ile bu tuzağı kaldırdık.
        raw = est if est not in (None, '') else fund_data.get('funding_rate', 0)
        try:
            return float(raw) * 100
        except (TypeError, ValueError):
            return 0.0

    try:
        contract_code = "BTC-USDT" if category == 'linear' else "BTC-USD"
        base = (
            "https://api.hbdm.com/linear-swap-api/v1"
            if category == 'linear'
            else "https://api.hbdm.com/swap-api/v1"
        )

        # 1. Open Interest
        oi_resp = requests.get(
            f"{base}/swap_open_interest?contract_code={contract_code}",
            headers=headers, timeout=10
        ).json()
        oi_data = oi_resp.get('data', [])
        if not (isinstance(oi_data, list) and len(oi_data) > 0):
            return 0, 0

        # ESKİ HATA: linear'da volume*0.001, inverse'de volume*100/fiyat ile
        # OI'yi yeniden hesaplıyordu (varsayılan kontrat büyüklüğüne dayanarak,
        # inverse'de ayrıca 3. bir API çağrısı gerektirerek). Huobi cevabında
        # zaten 'amount' alanı OI'yi doğrudan BTC cinsinden veriyor -> onu kullan.
        oi_btc = float(oi_data[0].get('amount', 0))

        # 2. Funding Rate
        fund_resp = requests.get(
            f"{base}/swap_funding_rate?contract_code={contract_code}",
            headers=headers, timeout=10
        ).json()
        funding = _extract_rate(fund_resp.get('data', {}))

        # OI doğru geldiyse, funding isteği bozulsa/boş dönse bile
        # sıfırlayıp elimizdeki geçerli veriyi çöpe atmıyoruz.
        return oi_btc, funding

    except Exception as e:
        print(f"  ❌ Huobi Hata ({category}): {e.__class__.__name__} - {e}")
        return 0, 0
# ==========================================
# 3. KÜRESEL AĞIRLIKLI HESAPLAMA MOTORU
# ==========================================
def get_global_macro_data():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🌐 USDT ve USD Tahtalarından Makro Veriler Toplanıyor...")
    
    markets = {}
    
    # 1. BINANCE (USDT + USD)
    markets['Binance_USDT'] = get_standard_ccxt_data('binance', 'BTC/USDT:USDT')
    markets['Binance_USD']  = get_custom_binance_usd_data()
    
    # 2. BYBIT (USDT + USD)
    markets['Bybit_USDT']   = get_custom_bybit_data(category='linear', symbol='BTCUSDT')
    markets['Bybit_USD']    = get_custom_bybit_data(category='inverse', symbol='BTCUSD')
    
    # 3. OKX (USDT + USD)
    markets['OKX_USDT']     = get_standard_ccxt_data('okx', 'BTC/USDT:USDT')
    markets['OKX_USD']      = get_standard_ccxt_data('okx', 'BTC/USD:BTC')
    
    # 4. HUOBI (USDT + USD)
    markets['Huobi_USDT']   = get_custom_huobi_data(category='linear')
    markets['Huobi_USD']    = get_custom_huobi_data(category='inverse')
    
    # 5. HYPERLIQUID (Zaten tek pazar)
    markets['Hyperliquid']  = get_custom_hyperliquid_data()
    hl_funding_8h_real = get_hyperliquid_funding_8h_real('BTC')  # gerçek 8s toplamı (varsa)

    # Binance USDT Fallback (Sıfır dönerse)
    if markets['Binance_USDT'][0] == 0:
        try:
            binance = ccxt.binance({'enableRateLimit': True})
            resp = binance.fapiPublicGetOpenInterest({'symbol': 'BTCUSDT'})
            oi = float(resp.get('openInterest', 0))
            funding = get_standard_ccxt_data('binance', 'BTC/USDT:USDT')[1]
            markets['Binance_USDT'] = (oi, funding)
        except:
            pass

    # Funding ödeme periyotları (saat). Hyperliquid saatlik öder, diğerleri 8 saatlik.
    FUNDING_INTERVAL_HOURS = {
        'Binance_USDT': 8, 'Binance_USD': 8,
        'Bybit_USDT': 8,   'Bybit_USD': 8,
        'OKX_USDT': 8,     'OKX_USD': 8,
        'Huobi_USDT': 8,   'Huobi_USD': 8,
        'Hyperliquid': 1,
    }

    print("\n--- Borsa ve Tahta Kırılımları (Funding 8s'e normalize edilmiş) ---")
    normalized = {}
    for borsa, (oi, funding) in markets.items():
        if borsa == 'Hyperliquid' and hl_funding_8h_real is not None:
            # Tahmin (anlık oran * 8) yerine gerçek 8 saatlik toplam kullanılıyor
            funding_8h = hl_funding_8h_real
        else:
            interval = FUNDING_INTERVAL_HOURS.get(borsa, 8)
            funding_8h = funding * (8 / interval)
        normalized[borsa] = (oi, funding_8h)
        if oi > 0:
            etiket = "Funding(8s, gerçek)" if (borsa == 'Hyperliquid' and hl_funding_8h_real is not None) else "Funding(8s)"
            print(f"  ✅ {borsa.ljust(15)}: OI = {oi:>10,.2f} BTC  |  {etiket} = %{funding_8h:+.4f}")
        else:
            print(f"  ❌ {borsa.ljust(15)}: Bağlantı Sağlanamadı / Veri 0")

    # Tüm tahtaları (borsa gruplaması yapmadan) doğrudan OI-ağırlıklı ortalamaya indirgiyoruz.
    total_oi_btc = sum(oi for oi, _ in normalized.values() if oi > 0)
    weighted_funding_sum = sum(oi * funding_8h for oi, funding_8h in normalized.values() if oi > 0)
    global_weighted_funding = (weighted_funding_sum / total_oi_btc) if total_oi_btc > 0 else 0

    print("-" * 60)
    print(f"  🌍 KÜRESEL TOPLAM OI         : {total_oi_btc:,.2f} BTC")
    print(f"  ⚖️ AĞIRLIKLI FONLAMA ORANI (8s): %{global_weighted_funding:+.4f}\n")

    return total_oi_btc, global_weighted_funding


# ==========================================
# 4. ZAMAN SERİSİ (SNAPSHOT LOG + TREND)
# ==========================================
import os
from openpyxl import load_workbook, Workbook

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oi_funding_history.xlsx")
COLUMNS = ['tarih', 'saat', 'oi_btc', 'oi_usd', 'funding_pct', 'price']
FUNDING_COL = COLUMNS.index('funding_pct') + 1  # openpyxl 1-index

def get_btc_price():
    """Basit spot fiyat - mevcut OI/funding fonksiyonlarına dokunmadan ayrı çekiliyor"""
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
        return float(r['price'])
    except Exception:
        return 0.0

def log_snapshot(oi, funding, price, path=HISTORY_FILE):
    """Her çalıştırmada bir satır ekler; tarih (gün.ay.yıl) ve saat (saat:dakika) ayrı sütunlarda
    okunabilir metin olarak, funding ise 0.0000 formatlı float olarak yazılır."""
    now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)  # GMT+3
    oi_usd = oi * price
    funding = float(funding)
    row = [now.strftime('%d.%m.%Y'), now.strftime('%H:%M'), oi, oi_usd, funding, price]

    if os.path.isfile(path):
        wb = load_workbook(path)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.append(COLUMNS)

    ws.append(row)
    ws.cell(row=ws.max_row, column=FUNDING_COL).number_format = '0.0000'
    wb.save(path)
    wb.close()
    return pd.DataFrame([dict(zip(COLUMNS, row))])

def load_history(path=HISTORY_FILE):
    if not os.path.isfile(path):
        return pd.DataFrame(columns=COLUMNS + ['timestamp'])
    df = pd.read_excel(path, engine='openpyxl')
    df['funding_pct'] = df['funding_pct'].astype(float)
    # Trend hesapları için tarih + saat'i tek bir timestamp'e geri birleştiriyoruz
    df['timestamp'] = pd.to_datetime(df['tarih'] + ' ' + df['saat'], format='%d.%m.%Y %H:%M')
    return df.sort_values('timestamp').reset_index(drop=True)

def compute_trend(df, hours):
    """Şimdiki değeri, 'hours' saat öncesine en yakın (o andan eski olan) satırla kıyaslar"""
    if df.empty:
        return None
    cutoff = df['timestamp'].max() - pd.Timedelta(hours=hours)
    past = df[df['timestamp'] <= cutoff]
    if past.empty:
        return None
    ref = past.iloc[-1]
    last = df.iloc[-1]

    oi_change_pct = ((last['oi_btc'] - ref['oi_btc']) / ref['oi_btc'] * 100) if ref['oi_btc'] else None
    funding_change = last['funding_pct'] - ref['funding_pct']
    price_change_pct = ((last['price'] - ref['price']) / ref['price'] * 100) if ref['price'] else None

    return {
        'window_h': hours,
        'oi_change_pct': oi_change_pct,
        'funding_change': funding_change,
        'price_change_pct': price_change_pct,
    }

def print_trend_report(df):
    print("\n📈 ZAMAN SERİSİ TREND RAPORU")
    print("-" * 60)
    for h in [1, 4, 24]:
        t = compute_trend(df, h)
        if t is None:
            print(f"  Son {h:>2}s: yeterli geçmiş veri yok (henüz {h}s öncesine ait snapshot toplanmadı)")
            continue
        oi_s = f"{t['oi_change_pct']:+.2f}%" if t['oi_change_pct'] is not None else "N/A"
        fund_s = f"{t['funding_change']:+.4f}"
        price_s = f"{t['price_change_pct']:+.2f}%" if t['price_change_pct'] is not None else "N/A"
        print(f"  Son {h:>2}s  ->  OI: {oi_s:>9}   Funding Δ: {fund_s:>9}   Fiyat: {price_s:>9}")
    print("-" * 60)

    # Basit squeeze heuristiği: funding düşüyor + OI düşüyor + fiyat yükseliyor
    t1 = compute_trend(df, 1)
    if t1 and t1['funding_change'] is not None and t1['oi_change_pct'] is not None and t1['price_change_pct'] is not None:
        if t1['funding_change'] < 0 and t1['oi_change_pct'] < 0 and t1['price_change_pct'] > 0:
            print("  ⚠️  Son 1 saatte short-squeeze imzası: funding düşüyor, OI düşüyor, fiyat yükseliyor.\n")

def run_snapshot_and_report():
    total_oi, global_funding = get_global_macro_data()
    price = get_btc_price()
    log_snapshot(total_oi, global_funding, price)
    df = load_history()
    print_trend_report(df)
    return df


if __name__ == "__main__":
    run_snapshot_and_report()