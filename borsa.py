import ccxt
import requests
import time
from datetime import datetime, timezone

from config import CONFIG

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
    """Bir borsa çağrısı geçici bir hatadan (network blip, rate limit, borsanın
    anlık kesintisi vb.) dolayı 0 dönerse, tüm satırı 'başarısız' loglamadan önce
    birkaç kez daha dener. Kalıcı bir sorun varsa (borsa gerçekten çökmüşse)
    yine de son denemeden sonra 0,0 döner ve failed_borsalar listesine düşer."""
    oi, funding = 0, 0
    for attempt in range(retries + 1):
        oi, funding = fetch_func(*args, **kwargs)
        if oi > 0:
            return oi, funding
        if attempt < retries:
            time.sleep(delay)
    return oi, funding

def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
        return float(r['price'])
    except: return 0.0

def get_btc_ohlc_15m():
    """Binance'ten en son kapanmış 15dk'lık mumu (open, high, low, close) çeker.
    Likidasyon haritasının fiyatın bir seviyeye iğne atıp geri çekilmesini
    ('temizleme') görebilmesi için tek bir anlık fiyat yetmiyor — mumun
    high/low'u lazım. limit=2 çekip SON KAPANMIŞ mumu (ohlcv[-2]) kullanıyoruz;
    ohlcv[-1] hâlâ oluşmakta olan/tamamlanmamış mum olurdu, onun high/low'u
    henüz kesinleşmemiş olur."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={'symbol': 'BTCUSDT', 'interval': '15m', 'limit': 2},
            timeout=5
        ).json()
        if not isinstance(r, list) or len(r) < 2:
            return None
        mum = r[-2]  # son KAPANMIŞ mum
        return {
            'open': float(mum[1]),
            'high': float(mum[2]),
            'low': float(mum[3]),
            'close': float(mum[4]),
        }
    except Exception as e:
        print(f"  ❌ Binance OHLC Hata: {e}")
        return None

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

    # Huobi/HTX: diğer borsalarla aynı anda (dakika sınırında herkesin isteği
    # aynı saniyeye denk geldiği "izdiham" anında) çağrılmıyor — kısa bir süre
    # bekleyip trafiğin sakinleştiği bir ana kaydırıyoruz. Süre config.json ->
    # huobi.delay_seconds ile ayarlanabilir (debug modunda hızlıca test etmek
    # istersen 0'a çekebilirsin).
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

    # Silinen Borsa Loglarını Geri Getirdik
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

    # Linear (USDT/USDC marjinli — Hyperliquid de USDC marjinli olduğu için buraya
    # dahil) ile inverse (USD/coin marjinli) OI'yi AYRI topluyoruz — teminat matematiği
    # farklı olduğu için likidasyon haritası bu ikisine farklı formül uygulayacak.
    # Terminal/Telegram çıktısı hâlâ sadece total_oi_btc'yi (toplam) gösteriyor, bu
    # ayrım sadece DB'ye ek kolon olarak gidiyor, görünürdeki hiçbir şey değişmiyor.
    oi_linear_btc = sum(
        oi for borsa, (oi, _) in normalized.items()
        if oi > 0 and (borsa.endswith('_USDT') or borsa == 'Hyperliquid')
    )
    oi_inverse_btc = sum(
        oi for borsa, (oi, _) in normalized.items()
        if oi > 0 and borsa.endswith('_USD') and not borsa.endswith('_USDT')
    )

    print("-" * 60)
    print(f"  🌍 KÜRESEL TOPLAM OI         : {total_oi_btc:,.2f} BTC")
    print(f"  ⚖️ AĞIRLIKLI FONLAMA ORANI (8s): %{global_weighted_funding:+.4f}\n")

    return total_oi_btc, global_weighted_funding, failed_borsalar, oi_linear_btc, oi_inverse_btc