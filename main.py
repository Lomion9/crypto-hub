import sqlite3
import time
import pandas as pd
from datetime import datetime, timedelta, timezone

from config import CONFIG
from db import DB_FILE, HISTORY_FILE, VERI_COLS, _init_db, load_history
from sinyal import (
    funding_status, _periyot_durumu, cvd_durumu, genel_durum, _islem_yonu,
    _periyot_cvd_degisimi, compute_adaptive_tf_thresholds,
    son_tf_genel_durumlar, sinyal_performans_guncelle, TRAP_KATEGORILERI,
)
from borsa import get_global_macro_data, get_btc_price, get_btc_ohlc_15m, get_binance_cvd
from telegram import should_send_telegram, send_telegram_message, build_telegram_report
from likidasyon import hedef_belirle, tum_haritalari_hesapla, harita_ozeti_yazdir

# ==========================================
# 4. ZAMAN SERİSİ VE SİNYAL JENERATÖRÜ
# ==========================================
def log_snapshot(oi, funding, price, cvd_spot, cvd_perp, path=HISTORY_FILE, now=None, ohlc=None,
                  oi_linear=None, oi_inverse=None):
    if now is None:
        now = datetime.now(timezone(timedelta(hours=3)))
    now = now.replace(tzinfo=None)
    oi_usd = oi * price
    funding = float(funding)

    # ohlc çekilemediyse (None) open/high/low'u close (price) ile dolduruyoruz —
    # sinyal mantığı zaten sadece 'price' (close) kolonunu kullanıyor, bu satır
    # sadece likidasyon haritasının ileride bu satırı 'iğnesiz düz mum' gibi
    # görmesini sağlıyor, mevcut davranışı hiç etkilemiyor.
    if ohlc is None:
        ohlc = {'open': price, 'high': price, 'low': price, 'close': price}

    # oi_linear/oi_inverse verilmediyse (eski çağrı biçimi / test) toplamı linear'a
    # yazıyoruz — likidasyon.py henüz devrede değilken davranışı bozmamak için.
    if oi_linear is None:
        oi_linear = oi
    if oi_inverse is None:
        oi_inverse = 0.0

    df_gecmis = load_history(path)  # sadece 'veri' tablosu — her tf kendi periyodu kadar geriye bakacak

    row_data = {
        'tarih': now.strftime('%d.%m.%Y'),
        'saat': now.strftime('%H:%M'),
        'oi_btc': oi,
        'oi_usd': oi_usd,
        'funding_pct': funding,
        'price': price,
        'price_open': ohlc['open'],
        'price_high': ohlc['high'],
        'price_low': ohlc['low'],
        'oi_linear_btc': oi_linear,
        'oi_inverse_btc': oi_inverse,
        'cvd_spot_btc': cvd_spot,
        'cvd_perp_btc': cvd_perp,
    }

    fund_status = funding_status(funding)

    conn = sqlite3.connect(path, timeout=30)  # dosya anlık kilitliyse (örn. DB Browser açıksa) 30sn'ye kadar bekler
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
    # Aynı turda birden fazla tf aynı yöne (ör. hem 1sa hem 4sa 'long') işaret
    # edebilir -- hedef_belirle her seferinde DB'yi baştan taradığı için,
    # yön başına en fazla BİR kez çağırıp sonucu bu turun geri kalanında
    # tekrar kullanıyoruz (gereksiz tekrar hesaplama/DB okuması yapmamak için).
    hedef_onbellek = {}

    for tf, tf_conf in CONFIG['timeframes'].items():
        sinir_saatleri = tf_conf.get('sinir_saatleri')
        if sinir_saatleri is not None and (mevcut_dakika != 0 or mevcut_saat not in sinir_saatleri):
            continue  # bu tf'in kendi saat sınırı değil, bu turda yazma yapılmıyor

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

        # TELEGRAM KONFİRMASYON ŞARTI: 15dk kendi sinyalini asla doğrudan Telegram'a
        # göndermez (telegram_uygun=False sabit). Diğer tf'ler için: kendi
        # confirm_kaynak tf'inden son confirm_n adet genel_durum kaydından (bu
        # turda kaynak tf için yeni yazılan kayıt varsa o da dahil) en az biri, BU
        # tf'in genel_durum'uyla birebir aynı olmalı — yoksa mesaj atlanır (ama
        # durum_{tf} tablosuna ve genel akışa normal şekilde yazılmaya devam eder).
        # Zincir: 1sa/2sa -> son 4/8 adet 15dk durumu, 4sa/8sa -> son 4/8 adet 1sa
        # durumu, 24sa -> son 6 adet 4sa durumu.
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

        # HEDEF/TP: sadece 1sa ve üzeri, yönü belli (İşlem Açma/Veri Bekleniyor
        # dışında) sinyaller için -- 15dk artık kendi başına bir sinyal değil,
        # bu yüzden hedef üretilmiyor.
        #
        # TRAP KATEGORİLERİ (Long Trap / Short Trap) ÖZEL: bunlar tespit
        # edildiğinde pozisyon hemen açılmaz (bkz. sinyal.py notu) -- önce
        # tuzaklanan yöndeki hedefe (bekleme_tetik_fiyat) ulaşılması beklenir,
        # ulaşılınca GERÇEK (ters yönlü) pozisyon açılıp onun TP'si (hedef,
        # ters yöndeki hedeften) kullanılır. İkisi de hedef_belirle'den geliyor,
        # sadece farklı yönler için.
        hedef = None
        bekleme_tetik_fiyat = None
        if tf != '15dk' and genel != "Veri Bekleniyor":
            if genel in TRAP_KATEGORILERI:
                orijinal_yon = TRAP_KATEGORILERI[genel]  # tuzaklanan/sürüklenen yön
                gercek_yon = _islem_yonu(genel)           # tuzak tamamlanınca açılacak gerçek yön

                if orijinal_yon not in hedef_onbellek:
                    hedef_onbellek[orijinal_yon] = hedef_belirle(orijinal_yon, db_path=path)
                bekleme_hedef = hedef_onbellek[orijinal_yon]
                bekleme_tetik_fiyat = bekleme_hedef['tp'] if bekleme_hedef else None
                tf_sonuclari[tf]['bekleme_tetik_fiyat'] = bekleme_tetik_fiyat

                if gercek_yon not in hedef_onbellek:
                    hedef_onbellek[gercek_yon] = hedef_belirle(gercek_yon, db_path=path)
                hedef = hedef_onbellek[gercek_yon]
                tf_sonuclari[tf]['hedef'] = hedef
            else:
                yon = _islem_yonu(genel)
                if yon:
                    if yon not in hedef_onbellek:
                        hedef_onbellek[yon] = hedef_belirle(yon, db_path=path)
                    hedef = hedef_onbellek[yon]
                    tf_sonuclari[tf]['hedef'] = hedef

            # Fiyat, hedefin dayandığı likidite kümesine (hedef_fiyat -- TP'nin
            # kendisi değil, TP'nin türetildiği ham küme fiyatı) ZATEN çok
            # yakınsa (< %0.5) sinyal Telegram'a gönderilmiyor -- bu durumda
            # hedef pratikte "tükenmiş" sayılır (fiyat oraya varmak üzere/vardı
            # bile), yeni bir sinyal olarak bildirmek yanıltıcı olur. durum_{tf}
            # tablosuna yazma ve genel akış (kâr/zarar takibi dahil) etkilenmez,
            # sadece Telegram gönderimi engellenir.
            if hedef:
                mesafe_pct = abs(hedef['hedef_fiyat'] - price) / price * 100
                tf_sonuclari[tf]['hedef_mesafe_pct'] = mesafe_pct
                tf_sonuclari[tf]['hedef_cok_yakin'] = mesafe_pct < 0.5

        # 15dk için AÇIK/KAPALI sinyal takibi (aktif_islem_15dk / sinyal_15dk /
        # aktif_bekleme_15dk) artık yapılmıyor -- 15dk sadece 1sa'nın
        # confirm_kaynak'ı olarak (durum_15dk üzerinden) bir KONTROL NOKTASI,
        # kendi başına izlenen/kapatılan bir sinyal değil.
        # tp=hedef['tp'] SADECE yeni açılacak bir pozisyon için kullanılır --
        # zaten açık olan bir pozisyonun TP'si kendi hedef_tp'sinde sabit
        # kalır (sinyal.py bunu ayrıca garanti ediyor).
        if tf != '15dk' and genel != "Veri Bekleniyor":
            tp = hedef['tp'] if hedef else None
            kapanan = sinyal_performans_guncelle(conn, tf, genel, price, tarih_str, saat_str,
                                                  tf_conf.get('kapanis_esigi', 3), tp=tp,
                                                  bekleme_tetik_fiyat=bekleme_tetik_fiyat)
            if kapanan:
                kapanan_islemler[tf] = kapanan

    conn.commit()
    conn.close()

    print(f"\n🎯 ANLIK SİNYAL DURUMU (timeframe bazlı)")
    print(f"  Funding Durumu : {fund_status}   |   Gün içi toplam CVD -> Spot:{cvd_spot:+.2f} Perp:{cvd_perp:+.2f}")
    for tf in CONFIG['timeframes'].keys():
        if tf not in tf_sonuclari:
            continue  # bu tf'in sınır saati değildi, bu turda yazılmadı
        s = tf_sonuclari[tf]
        print(f"  [{tf:>4}] OI:{s['oi_durum']:<16} Fiyat:{s['fiyat_durum']:<12} CVD:{s['cvd_durum']:<10} -> {s['genel_durum']}")
        if s['genel_durum'] in TRAP_KATEGORILERI and s.get('bekleme_tetik_fiyat'):
            bekleme_satiri = f"        🪤 Tuzak tespit edildi, HENÜZ POZİSYON AÇILMADI — bekleniyor: ${s['bekleme_tetik_fiyat']:,.2f}"
            if s.get('hedef'):
                bekleme_satiri += f"  |  tetiklenince TP: ${s['hedef']['tp']:,.2f}"
            print(bekleme_satiri)
        hedef = s.get('hedef')
        if hedef and s['genel_durum'] not in TRAP_KATEGORILERI:
            print(f"        🎯 Hedef: ${hedef['hedef_fiyat']:,.2f} ({hedef['hedef_miktar_btc']:,.2f} BTC likidite)  |  TP: ${hedef['tp']:,.2f}")
            if s.get('hedef_cok_yakin'):
                print(f"        ⚠️ Fiyat hedefe çok yakın (%{s['hedef_mesafe_pct']:.2f} < %0.5) — Telegram'a gönderilmeyecek")
        elif hedef is None and 'hedef' in s:
            print(f"        ⚠️ Hedef bulunamadı (12s penceresinde karşıt yönde küme yok)")
    for tf, k in kapanan_islemler.items():
        print(f"  💰 [{tf}] SİNYAL KAPANDI ({k['kapanis_tipi']}): {k['sinyal']} ({k['yon']}) -> %{k['kar_yuzde']:+.2f}")

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
    total_oi, global_funding, failed_borsalar, oi_linear, oi_inverse = get_global_macro_data()

    if failed_borsalar:
        print(f"  ⏭️  Bu tur ATLANDI (kayıt eklenmedi) — veri alınamayan borsa(lar): {', '.join(failed_borsalar)}")
        return None

    ohlc = get_btc_ohlc_15m()
    if ohlc is None or ohlc['close'] <= 0:
        # OHLC çekilemezse anlık fiyata düş (Binance'in kline endpoint'i kısa bir
        # kesinti yaşarsa turu tamamen atlamak yerine, en azından tek fiyatla devam)
        price = get_btc_price()
        if price <= 0:
            print("  ⏭️  Bu tur ATLANDI (kayıt eklenmedi) — fiyat verisi alınamadı.")
            return None
        ohlc = {'open': price, 'high': price, 'low': price, 'close': price}
    else:
        price = ohlc['close']

    print(f"  🕯️  15dk Mum (Binance) -> Açılış: ${ohlc['open']:,.2f}  Yüksek: ${ohlc['high']:,.2f}  "
          f"Düşük: ${ohlc['low']:,.2f}  Kapanış: ${ohlc['close']:,.2f}")

    # CVD: bugün UTC 00:00'dan (TR 03:00) itibaren biriken net alım-satım baskısı
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 CVD Verileri Hesaplanıyor (Bugün 00:00 UTC'den İtibaren)...")
    cvd_spot = get_binance_cvd('spot', 'BTCUSDT', interval='1h')
    cvd_perp = get_binance_cvd('futures', 'BTCUSDT', interval='1h')

    print(f"  📊 Spot CVD (bugün) : {cvd_spot:+.2f} BTC")
    print(f"  📊 Perp CVD (bugün) : {cvd_perp:+.2f} BTC\n")

    sonuc = log_snapshot(total_oi, global_funding, price, cvd_spot, cvd_perp, now=baslangic_zamani, ohlc=ohlc,
                          oi_linear=oi_linear, oi_inverse=oi_inverse)

    # Likidasyon haritalarını bu turun yeni yazılan satırıyla birlikte tazele
    # ve terminale bas -- yönlü bir sinyal olsun olmasın HER 15dk'da bir
    # çalışır, böylece harita her zaman güncel kalır (hedef_belirle zaten
    # DB'den taze okuyor, bu sadece görünürlük/log amaçlı).
    try:
        likidasyon_sonucu = tum_haritalari_hesapla()
        harita_ozeti_yazdir(likidasyon_sonucu, guncel_fiyat=price)
    except Exception as e:
        print(f"  ⚠️ Likidasyon haritası hesaplanamadı: {e}")

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
    """Sabit dakika sıfırlarına (örn. 15dk için :00/:15/:30/:45) göre bekler —
    time.sleep(interval*60) kullanmıyoruz çünkü her turun işlem süresi (API çağrıları
    vb.) birikip zamanla saatten kaymaya sebep olur. Bu fonksiyon her seferinde
    GERÇEK saate göre yeniden hesaplar, drift birikmez."""
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
        print(f"⚠️  DEBUG MODU AKTİF"
              f" her {debug_interval} saniyede bir ")
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