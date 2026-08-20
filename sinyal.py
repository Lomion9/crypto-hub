import numpy as np
from datetime import datetime

from config import CONFIG

# ==========================================
# SİNYAL MANTIĞI (eşik, yön, genel durum, kaldıraç/absorption yorumlama)
# ==========================================

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
    """ROLLING-MIN / ROLLING-MAX + EŞİK: son N periyotluk pencerenin dip/tepesine
    olan mesafelerden büyük olanı esik_pct'yi geçmiyorsa Nötr döner (akümülasyon/
    dağıtım tespiti Nötr'e ihtiyaç duyar); geçiyorsa hangi mesafe büyükse o yön
    (Artıyor/Düşüyor) döner."""
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
    """Bir kolon (Series, kronolojik sıralı, POZİSYONEL/0-tabanlı index) için,
    her geçerli noktada 'son N periyotluk pencerenin dip/tepesine olan en büyük
    mesafe' değerini (pozisyon, mesafe) çiftleri olarak döndürür — _periyot_durumu
    ile BİREBİR aynı ölçütle. Pozisyon bilgisi, güne göre gruplamak (adaptive eşik
    hesabı) için taşınıyor; eksik/geçersiz noktalar listede hiç yer almaz."""
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
    """Şu ana kadar (bu turda kaynak_tf için yeni bir kayıt yazıldıysa o da dahil —
    aynı bağlantıda commit beklemeden görünür) yazılmış son n adet
    durum_{kaynak_tf}.genel_durum değerini, en yeniden en eskiye döndürür."""
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

    # Long taraf: funding Aşırı Pozitif -> longlar baskın/kaldıraçlı
    long_trap = (fund_positive and oi_status == "Artıyor" and price_status == "Düşüyor")
    long_squeeze = (fund_positive and oi_status == "Düşüyor" and price_status == "Düşüyor")

    # Short taraf: funding Aşırı Negatif -> shortlar baskın/kaldıraçlı (long tarafın aynası)
    short_trap = (fund_negative and oi_status == "Artıyor" and price_status == "Artıyor")
    short_squeeze = (fund_negative and oi_status == "Düşüyor" and price_status == "Artıyor")

    if short_squeeze:
        # NOT: eskiden "Sağlıklı Long (Squeeze + Organik Talep)" döndürülüyordu ama
        # _islem_yonu tam "Sağlıklı Long" arıyordu -> hiç eşleşmiyordu, bu yüzden
        # "sağlıklı long" hiç tetiklenmiyormuş gibi görünüyordu. Etiketler artık
        # _islem_yonu ile birebir aynı (parantezli açıklamalar kaldırıldı).
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

    # AKÜMÜLASYON / DAĞITIM: fiyat yatay (Nötr) ama OI birikiyor (Artıyor) -> pozisyon
    # sessizce kuruluyor demektir; yön, hangi tarafın (spot alım mı satım mı) baskın
    # olduğuna, yani CVD spot'un işaretine ve perp'e göre baskınlığına bakılarak
    # belirlenir. Bu iki durum funding'in extreme olup olmamasından bağımsızdır --
    # trap/squeeze koşulları zaten üstte elenmiş olduğu için buraya sadece price_status
    # Nötr olduğunda düşülür.
    if price_status == "Nötr" and oi_status == "Artıyor":
        if cvd_spot > 0 and cvd_spot >= abs(cvd_perp):
            return "Akümülasyon"
        if cvd_spot < 0 and abs(cvd_spot) >= abs(cvd_perp):
            return "Dağıtım"

    # Fonlama Nötr Pozitif veya Nötr Negatif ise (ya da yukarıdaki hiçbir setup
    # oluşmadıysa) herhangi bir tasfiye/birikim setup'ı aranmaz
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
        return None, None  # bugün henüz hiç kayıt yok

    if len(df_veri) >= periods and df_veri.iloc[-periods]['tarih'] == tarih_str:
        ref = df_veri.iloc[-periods]
    else:
        ref = bugun_df.iloc[0]  # tam N periyot bugünün dışına taşıyor -> bugünün ilk kaydına düş

    return current_cvd_spot - ref['cvd_spot_btc'], current_cvd_perp - ref['cvd_perp_btc']

def compute_adaptive_tf_thresholds(df_veri):
    """Her timeframe için, son `lookback_days` günün HER BİRİNİN kendi 'gürültü'
    seviyesini (o gün içindeki, _rolling_hareket_mesafesi ile ölçülen N-periyotluk
    dip/tepe mesafelerinin `noise_percentile`'ı) ayrı ayrı hesaplar. Bu günlerden
    EN DÜŞÜK gürültüye sahip `quiet_days` tanesini seçip onların ortalamasını eşik
    olarak kullanır -- fikir: piyasanın 'sakin' günlerinde tipik hareket ne kadarsa,
    bunun altında kalan hareketler gürültü/Nötr, üstündekiler gerçek sinyal sayılsın.
    OI ve fiyat için ayrı ayrı hesaplanır (en sakin 3 gün ikisi için farklı olabilir).
    Yeterli gün/veri yoksa o tf için None döner; çağıran taraf statik config
    değerlerine (oi_pct/price_pct) düşer."""
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

        # Tüm seri için TEK SEFERDE hesapla; (pozisyon, mesafe) çiftleri güne göre
        # gruplamak için kullanılacak -- _periyot_durumu'yla birebir aynı ölçüt.
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