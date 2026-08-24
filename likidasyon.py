# ==========================================
# LİKİDASYON HARİTASI
# ==========================================
# Yöntem özeti: Coinglass'ınkine benzer, sabit-pencereli kümülatif-delta model.
# Linear (USDT) ve inverse (USD/coin-margined) OI ayrı katman olarak tutulur
# (teminat matematiği farklı). Fiyat serisi Binance'in 15dk OHLC'si -- sadece
# close değil, High/Low de kullanılıyor (fiyat bir seviyeye değip geri çekilse
# bile o seviyedeki kümeleri temizlemek için). OI verisi db.py üzerinden
# mevcut oi_funding_history.db'den okunur; bu script sadece okur, main.py hâlâ
# tek yazan taraf ve main.py'den tamamen ayrı çalışır (kendi zamanlaması).

from config import CONFIG
import pandas as pd

# ==========================================
# 1a) KALDIRAÇ DAĞILIMI (funding'e göre dinamik)
# ==========================================
def kaldirac_dagilimi(funding_pct):
    """Taban ağırlıktan (config.likidasyon.kaldirac_taban_agirlik) başlar, funding
    ne kadar ekstremse (funding_thresholds.extreme_pct eşiğine oranla) düşük
    kaldıraçtan (5x, 10x) yüksek kaldıraca (50x, 100x) o kadar ağırlık kaydırır.
    25x sabit kalır -- düşük ve yüksek uçlar arasında bir 'menteşe' gibi davranır.
    Kayma miktarı siddet_tavan_kati'nin ötesinde artmaya devam etmez (tavan var).
    Dönen sözlüğün ağırlıkları toplamı her zaman 1.0 olarak korunur (sadece
    düşükten yükseğe kayıyor, toplamdan bir şey kaybolmuyor/eklenmiyor)."""
    lc = CONFIG['likidasyon']
    taban = {int(k): v for k, v in lc['kaldirac_taban_agirlik'].items()}
    extreme_esik = CONFIG['funding_thresholds']['extreme_pct']
    tavan_kati = lc['siddet_tavan_kati']
    max_kaydirma = lc['max_kaydirma_orani']

    if extreme_esik <= 0:
        return taban

    siddet = min(abs(funding_pct) / extreme_esik, tavan_kati) / tavan_kati  # 0..1
    kaydirma_orani = siddet * max_kaydirma

    dusuk_kaldiraclar = [5, 10]
    yuksek_kaldiraclar = [50, 100]

    toplam_dusuk = sum(taban[k] for k in dusuk_kaldiraclar)
    alinan_pay = toplam_dusuk * kaydirma_orani

    yeni = dict(taban)
    for k in dusuk_kaldiraclar:
        pay_orani = taban[k] / toplam_dusuk if toplam_dusuk > 0 else 0
        yeni[k] = taban[k] - alinan_pay * pay_orani

    toplam_yuksek = sum(taban[k] for k in yuksek_kaldiraclar)
    for k in yuksek_kaldiraclar:
        pay_orani = taban[k] / toplam_yuksek if toplam_yuksek > 0 else 0
        yeni[k] = taban[k] + alinan_pay * pay_orani

    return yeni

# ==========================================
# 1b) TEK BİR OI DELTA'SINI FİYAT KÜMELERİNE ÇEVİRME
# ==========================================
def cvd_agirlikli_long_payi(cvd_perp_delta, oi_delta_abs):
    """Bir periyottaki OI artışının (oi_delta_abs, BTC) ne kadarının long, ne
    kadarının short pozisyon olduğunu, o periyottaki perp CVD değişiminin
    (cvd_perp_delta -- net taker alım/satım baskısı) büyüklüğüne göre tahmin
    eder. CVD'yi (spot değil) PERP tercih ediyoruz çünkü OI değişimi zaten
    perp/futures piyasasında oluyor, oradaki taker baskısı en doğrudan sinyal.
    cvd_perp_delta OI artışına kıyasla ne kadar büyükse, o kadar tek yöne
    (0.0=tamamen short, 1.0=tamamen long) yaklaşır; delta yoksa (oi_delta_abs<=0)
    ya da CVD nötrse 0.5 (yarı yarıya) döner."""
    if oi_delta_abs <= 0:
        return 0.5
    oran = cvd_perp_delta / (abs(cvd_perp_delta) + oi_delta_abs)  # -1..1 arası
    long_payi = 0.5 + oran / 2
    return max(0.0, min(1.0, long_payi))

def delta_kumeleri_hesapla(delta_oi, acilis_fiyati, cvd_perp_delta, funding_pct):
    """Bir periyotta artan OI'yi (delta_oi, BTC), o anki fiyattan (acilis_fiyati)
    açılmış varsayılan pozisyonlar olarak modelleyip fiyat kümelerine dönüştürür:
      1) cvd_agirlikli_long_payi ile long/short'a böler,
      2) kaldirac_dagilimi ile her kaldıraç seviyesine dağıtır,
      3) isolated marj formülüyle (bakım marjı config'den) her seviyenin
         likidasyon fiyatını hesaplar.
    delta_oi <= 0 ise (OI artışı yoksa, bu fonksiyon sadece ARTIŞLARI işler,
    azalışlar ayrı bir adımda -- 1e -- ele alınacak) boş sözlük döner.
    Dönüş: {(likidasyon_fiyati, 'long'|'short'): miktar_btc, ...}"""
    if delta_oi <= 0:
        return {}

    long_payi = cvd_agirlikli_long_payi(cvd_perp_delta, delta_oi)
    long_miktar = delta_oi * long_payi
    short_miktar = delta_oi * (1 - long_payi)

    dagilim = kaldirac_dagilimi(funding_pct)
    bakim_marji = {int(k): v for k, v in CONFIG['likidasyon']['bakim_marji'].items()}

    kumeler = {}
    for kaldirac, agirlik in dagilim.items():
        bm = bakim_marji[kaldirac]
        # GÜVENLİK PAYI: bm, 1/kaldıraç'ın (başlangıç marjı) en fazla %90'ı kadar
        # olabilir -- bm >= 1/kaldıraç olursa likidasyon fiyatı giriş fiyatının
        # YANLIŞ tarafına geçer (long'un likidasyonu girişin üstüne, short'unki
        # altına düşer -- fiziksel olarak imkansız). config.json yanlış/aşırı bir
        # değerle güncellense bile bu satır formülü her zaman doğru tarafta tutar.
        bm_guvenli = min(bm, (1 / kaldirac) * 0.9)

        if long_miktar > 0:
            miktar = long_miktar * agirlik
            likit_fiyat = acilis_fiyati * (1 - 1 / kaldirac + bm_guvenli)
            anahtar = (likit_fiyat, 'long')
            kumeler[anahtar] = kumeler.get(anahtar, 0.0) + miktar

        if short_miktar > 0:
            miktar = short_miktar * agirlik
            likit_fiyat = acilis_fiyati * (1 + 1 / kaldirac - bm_guvenli)
            anahtar = (likit_fiyat, 'short')
            kumeler[anahtar] = kumeler.get(anahtar, 0.0) + miktar

    return kumeler

# ==========================================
# 1d) OHLC (HIGH/LOW) İLE TEMİZLEME
# ==========================================
def _kumeleri_temizle(kumeler, price_low, price_high):
    """Bir mumun [price_low, price_high] aralığına giren TÜM kümeleri (long ya da
    short farketmeksizin) siler -- fiyat o seviyeye gerçekten değmiş demektir,
    oradaki tahmini pozisyonlar likide olmuş sayılır. kumeler sözlüğü YERİNDE
    (in-place) değiştirilir. Silinen bir seviye, pencerede daha sonra yeni bir
    delta ile tekrar dolabilir -- bu fonksiyon sadece o ana kadar birikmiş
    kümeleri temizler, gelecekteki eklemeleri engellemez."""
    silinecekler = [anahtar for anahtar in kumeler if price_low <= anahtar[0] <= price_high]
    for anahtar in silinecekler:
        del kumeler[anahtar]

# ==========================================
# 1e) OI AZALIŞINI ORANTILI YANSITMA
# ==========================================
def _kumeleri_oransal_kucult(kumeler, kucultme_orani):
    """OI bir periyotta azaldığında (kucultme_orani = azalış_miktarı / önceki_OI,
    0..1 arası), hangi spesifik kümenin kapandığını bilemediğimiz için TÜM aktif
    kümeleri aynı yüzdeyle küçültür -- keyfi bir seçim yapmaktan (ör. sadece en
    yeni kümeyi silmek) kaçınan en basit/tarafsız yaklaşım. kucultme_orani 1.0'ı
    geçerse (aşırı durum, OI önceki değerinden daha fazla azalmış görünüyorsa)
    1.0'a sabitlenir -- negatif miktar üretilmez. kumeler sözlüğü YERİNDE
    değiştirilir; 0'a inen kümeler sözlükte kalır (0 miktarlı), sonraki bir
    delta ile tekrar dolabilirler -- gerçek silme sadece _kumeleri_temizle'de
    (fiyatın seviyeye değmesiyle) olur."""
    oran = min(max(kucultme_orani, 0.0), 1.0)
    if oran <= 0:
        return
    kalan_carpan = 1.0 - oran
    for anahtar in kumeler:
        kumeler[anahtar] *= kalan_carpan

# ==========================================
# 1c) PENCERE BOYUNCA İLERLEYİP KÜMELERİ BİRİKTİR
# ==========================================
from datetime import timedelta

def pencere_kumeleri_biriktir(df, saat_penceresi, oi_kolonu='oi_linear_btc', temizleme_aktif=True):
    """db.load_history() çıktısını (kronolojik sıralı, index 0..n-1) alır, son
    `saat_penceresi` saatlik pencere içindeki ARDIŞIK satırlar arasındaki
    oi_kolonu artışlarını delta_kumeleri_hesapla ile fiyat kümelerine çevirip
    TEK BİR sözlükte toplar. Henüz OI azalışını işleme (1e) yok.

    Her satırda ÖNCE o satırın price_low/price_high'ı önceki (daha eski)
    kümeleri temizler (mum içinde fiyat önce hareket edip eski seviyeleri
    süpürür), SONRA o satırın kendi delta'sı yeni küme olarak eklenir (kapanışta
    yeni pozisyon açılır). temizleme_aktif=False verilirse 1c'deki ham
    davranışa (hiç temizleme yapmadan) döner -- 1c'nin eski testleriyle
    karşılaştırma/regresyon amaçlı.

    Pencerenin İLK satırının delta'sını hesaplayabilmek için, pencere
    başlangıcından hemen önceki satırı da (kendisi bir 'yeni pozisyon' olarak
    işlenmeden, sadece referans/'anchor' olarak) kullanır -- yoksa pencerenin
    ilk anındaki OI, sanki sıfırdan başlıyormuş gibi yanlış yorumlanır.

    Dönüş: {(likidasyon_fiyati, 'long'|'short'): miktar_btc, ...}"""
    if df.empty or len(df) < 2:
        return {}

    pencere_sonu = df['timestamp'].max()
    pencere_basi_zamani = pencere_sonu - timedelta(hours=saat_penceresi)
    pencere_ici = df[df['timestamp'] > pencere_basi_zamani]
    if pencere_ici.empty:
        return {}

    ilk_index = pencere_ici.index.min()
    baslangic_index = max(ilk_index - 1, df.index.min())  # bir önceki satır = anchor

    toplam_kumeler = {}
    onceki = df.loc[baslangic_index]
    for i in range(baslangic_index + 1, df.index.max() + 1):
        if i not in df.index:
            continue
        satir = df.loc[i]

        if temizleme_aktif and pd.notna(satir.get('price_low')) and pd.notna(satir.get('price_high')):
            _kumeleri_temizle(toplam_kumeler, satir['price_low'], satir['price_high'])

        oi_simdi = satir[oi_kolonu]
        oi_once = onceki[oi_kolonu]

        if pd.isna(oi_simdi) or pd.isna(oi_once):
            # Bu satır ya da bir öncekinde bu katmanın OI'si henüz loglanmamış
            # (ör. borsa.py bu değişiklikten önce yazılmış eski bir satır) --
            # delta hesaplanamaz, bu adımı atla ama zinciri kırma (onceki'yi
            # yine de güncelle ki bir sonraki geçerli satırdan itibaren devam
            # edebilsin).
            onceki = satir
            continue

        delta_oi = oi_simdi - oi_once

        if delta_oi > 0:
            cvd_perp_delta = satir['cvd_perp_btc'] - onceki['cvd_perp_btc']
            yeni_kumeler = delta_kumeleri_hesapla(
                delta_oi=delta_oi,
                acilis_fiyati=satir['price'],
                cvd_perp_delta=cvd_perp_delta,
                funding_pct=satir['funding_pct'],
            )
            for anahtar, miktar in yeni_kumeler.items():
                toplam_kumeler[anahtar] = toplam_kumeler.get(anahtar, 0.0) + miktar

        elif delta_oi < 0 and onceki[oi_kolonu] > 0:
            _kumeleri_oransal_kucult(toplam_kumeler, abs(delta_oi) / onceki[oi_kolonu])

        onceki = satir

    return toplam_kumeler

# ==========================================
# 1f) HEPSİNİ BİRLEŞTİR — DIŞA AÇILAN ASIL FONKSİYON
# ==========================================
KATMANLAR = {
    'linear': 'oi_linear_btc',
    'inverse': 'oi_inverse_btc',
}
PENCERELER = (12, 24)  # saat

def format_usd_kisaltma(deger):
    """Dolar değerini okunaklı kısaltmayla döndürür -- 1 milyar+ için 2 ondalık
    ve 'B' (ör. '22.74B'), 1 milyon+ için 1 ondalık ve 'M' (ör. '24.2M'),
    1 bin+ için 1 ondalık ve 'K', altında ise tam sayı formatında."""
    deger = abs(deger)
    if deger >= 1_000_000_000:
        return f"{deger/1_000_000_000:.2f}B"
    if deger >= 1_000_000:
        return f"{deger/1_000_000:.1f}M"
    if deger >= 1_000:
        return f"{deger/1_000:.1f}K"
    return f"{deger:,.0f}"

def tum_haritalari_hesapla(db_path=None):
    """Tüm katman × pencere kombinasyonlarını (linear/inverse × 12s/24s = 4 harita)
    tek çağrıda hesaplar. db.py'den geçmişi okur (main.py'nin yazdığı canlı
    veri), her kombinasyon için pencere_kumeleri_biriktir'i çağırır.

    Dönüş: {
      'katmanlar': {
        'linear':  {12: {(fiyat, yon): miktar, ...}, 24: {...}},
        'inverse': {12: {...}, 24: {...}},
      },
      'guncel_oi': {'linear': <şu anki linear OI, BTC>, 'inverse': <şu anki inverse OI, BTC>},
    }
    Yeterli veri yoksa (ör. oi_linear/inverse_btc kolonları henüz boşsa, ya da
    pencere kadar geçmiş birikmemişse) ilgili harita boş sözlük olarak döner --
    hata fırlatmaz, çağıran taraf (sinyal hedefleme ileride) boş haritayı
    'bu pencerede veri yok' olarak yorumlayabilir."""
    from db import load_history, DB_FILE
    df = load_history(db_path or DB_FILE)

    katmanlar = {}
    guncel_oi = {}
    for katman_adi, oi_kolonu in KATMANLAR.items():
        katmanlar[katman_adi] = {}
        for saat in PENCERELER:
            katmanlar[katman_adi][saat] = pencere_kumeleri_biriktir(
                df, saat_penceresi=saat, oi_kolonu=oi_kolonu, temizleme_aktif=True
            )
        if not df.empty and oi_kolonu in df.columns:
            son_deger = df[oi_kolonu].iloc[-1]
            guncel_oi[katman_adi] = float(son_deger) if pd.notna(son_deger) else None
        else:
            guncel_oi[katman_adi] = None

    return {'katmanlar': katmanlar, 'guncel_oi': guncel_oi}

def harita_ozeti_yazdir(sonuc, guncel_fiyat=None):
    """tum_haritalari_hesapla çıktısını terminale okunaklı özet olarak basar --
    her katman için şu anki toplam OI'yi (BTC + $ kısaltmalı), sonra her
    katman/pencere için küme sayısı, toplam miktar (BTC + $ kısaltmalı) ve
    (varsa) en büyük 3 kümeyi (BTC + $ kısaltmalı) gösterir. guncel_fiyat
    verilmezse $ dönüşümleri ve mesafe (%) bilgisi atlanır. Hızlı gözle
    kontrol / main.py'ye entegrasyon öncesi debug amaçlı."""
    katmanlar = sonuc['katmanlar']
    guncel_oi = sonuc.get('guncel_oi', {})

    for katman_adi, pencereler in katmanlar.items():
        oi_btc = guncel_oi.get(katman_adi)
        if oi_btc is not None and guncel_fiyat:
            oi_usd = oi_btc * guncel_fiyat
            print(f"\n=== {katman_adi.upper()} — şu anki toplam OI: ${format_usd_kisaltma(oi_usd)} ===")
        elif oi_btc is not None:
            print(f"\n=== {katman_adi.upper()} — şu anki toplam OI: {oi_btc:,.2f} BTC (fiyat alınamadı, $ dönüşümü yapılamadı) ===")
        else:
            print(f"\n=== {katman_adi.upper()} — şu anki toplam OI: veri yok ===")

        for saat, kumeler in pencereler.items():
            toplam_btc = sum(kumeler.values())
            toplam_gosterim = f"${format_usd_kisaltma(toplam_btc * guncel_fiyat)}" if guncel_fiyat else f"{toplam_btc:,.2f} BTC (fiyat yok)"
            print(f"  📍 [{saat}s] {len(kumeler)} küme, toplam {toplam_gosterim}")
            if not kumeler:
                continue
            en_buyukler = sorted(kumeler.items(), key=lambda kv: -kv[1])[:3]
            for (fiyat, yon), miktar in en_buyukler:
                mesafe = f" ({'-' if yon=='long' else '+'}{abs(fiyat-guncel_fiyat)/guncel_fiyat*100:.2f}%)" if guncel_fiyat else ""
                miktar_gosterim = f"${format_usd_kisaltma(miktar * guncel_fiyat)}" if guncel_fiyat else f"{miktar:,.2f} BTC (fiyat yok)"
                print(f"      {yon:<6} ${fiyat:>10,.2f}{mesafe}  {miktar_gosterim}")

# ==========================================
# DOĞRUDAN ÇALIŞTIRMA (python likidasyon.py)
# ==========================================
if __name__ == "__main__":
    from borsa import get_btc_price

    print("📍 Likidasyon haritaları hesaplanıyor (mevcut oi_funding_history.db'den, linear/inverse × 12s/24s)...\n")
    guncel_fiyat = get_btc_price()
    if guncel_fiyat <= 0:
        print("  ⚠️ Güncel fiyat çekilemedi, $ dönüşümü ve mesafe (%) bilgisi olmadan devam ediliyor.")
        guncel_fiyat = None

    sonuc = tum_haritalari_hesapla()
    harita_ozeti_yazdir(sonuc, guncel_fiyat=guncel_fiyat)

    toplam_kume = sum(len(k) for pencereler in sonuc['katmanlar'].values() for k in pencereler.values())
    if toplam_kume == 0:
        print("\n  ℹ️ Hiçbir haritada küme yok -- muhtemelen DB'de henüz linear/inverse OI "
              "verisi biriken yeterli geçmiş (en az birkaç saat) yok, ya da botu daha "
              "yeni bu sürüme güncellediysen eski satırlarda bu kolonlar boş. Bot birkaç "
              "saat daha çalışsın, sonra tekrar dene.")