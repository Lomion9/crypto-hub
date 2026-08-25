import sqlite3
import pandas as pd

from config import CONFIG
from db import DB_FILE, _init_db

# ==========================================
# PERFORMANS RAPORU (sinyal_{tf} tablolarından)
# ==========================================
# Amaç: sistem aylardır sinyal üretiyor ama şu ana kadar bu sinyallerin
# GERÇEKTEN kâr edip etmediğine hiç bakılmadı. Bu script her tf için kapanan
# işlemleri okuyup win-rate, ortalama kâr/zarar, kategori bazlı kırılım ve
# yön (long/short) dağılımını raporluyor -- config/eşik ayarlarını körlemesine
# değil, gerçek sonuçlara bakarak ayarlamak için.

def _kategori_normalize(sinyal_metni):
    """Eski (parantezli açıklamalı, ör. 'Sağlıklı Short (Squeeze + Organik Satış)')
    ve yeni (sade, 'Sağlıklı Short') genel_durum etiketlerini TEK bir kategoriye
    indirger -- yoksa aynı kategori raporda iki ayrı satır gibi görünür."""
    return sinyal_metni.split(" (")[0].strip()

def sinyal_verilerini_yukle(tf, path=DB_FILE):
    conn = sqlite3.connect(path)
    _init_db(conn)  # eski bir DB'de sinyal_{tf}.kapanis_tipi kolonu henüz yoksa burada eklenir
    try:
        df = pd.read_sql(f"SELECT * FROM sinyal_{tf}", conn)
    except Exception:
        df = pd.DataFrame(columns=['kapanis_tarih', 'kapanis_saat', 'sinyal', 'yon',
                                    'giris_tarih', 'giris_saat', 'giris_fiyat', 'cikis_fiyat',
                                    'kar_yuzde', 'kapanis_tipi'])
    conn.close()
    if not df.empty:
        df['kategori'] = df['sinyal'].apply(_kategori_normalize)
        if 'kapanis_tipi' not in df.columns:
            df['kapanis_tipi'] = None
        df['kapanis_tipi'] = df['kapanis_tipi'].fillna('Bilinmiyor (TP takibi öncesi)')
    return df

def _ozet_satiri(alt_df):
    n = len(alt_df)
    if n == 0:
        return None
    kazanan = (alt_df['kar_yuzde'] > 0).sum()
    return {
        'islem_sayisi': n,
        'win_rate_pct': kazanan / n * 100,
        'ort_kar_pct': alt_df['kar_yuzde'].mean(),
        'toplam_kar_pct': alt_df['kar_yuzde'].sum(),  # NAIVE toplam -- bileşik değil, pozisyon büyüklüğü yok
        'en_iyi_pct': alt_df['kar_yuzde'].max(),
        'en_kotu_pct': alt_df['kar_yuzde'].min(),
    }

def tf_performans_ozeti(tf, path=DB_FILE):
    df = sinyal_verilerini_yukle(tf, path)
    if df.empty:
        return {'tf': tf, 'genel': None, 'kategori_kirilimi': {}, 'yon_kirilimi': {}, 'kapanis_tipi_kirilimi': {}}

    genel = _ozet_satiri(df)
    kategori_kirilimi = {kat: _ozet_satiri(g) for kat, g in df.groupby('kategori')}
    yon_kirilimi = {yon: _ozet_satiri(g) for yon, g in df.groupby('yon')}
    kapanis_tipi_kirilimi = {kt: _ozet_satiri(g) for kt, g in df.groupby('kapanis_tipi')}

    return {
        'tf': tf, 'genel': genel,
        'kategori_kirilimi': kategori_kirilimi,
        'yon_kirilimi': yon_kirilimi,
        'kapanis_tipi_kirilimi': kapanis_tipi_kirilimi,
    }

def _satir_yazdir(baslik, ozet, girinti="    "):
    if ozet is None:
        return
    print(f"{girinti}{baslik:<28} {ozet['islem_sayisi']:>4} işlem   "
          f"win-rate %{ozet['win_rate_pct']:>5.1f}   "
          f"ort %{ozet['ort_kar_pct']:>+6.2f}   "
          f"toplam(naive) %{ozet['toplam_kar_pct']:>+7.2f}   "
          f"en iyi/kötü %{ozet['en_iyi_pct']:>+6.2f} / %{ozet['en_kotu_pct']:>+6.2f}")

def tum_tf_performans_raporu(path=DB_FILE):
    print("=" * 100)
    print("📊 PERFORMANS RAPORU — sinyal_{tf} tablolarından, tüm kapanan işlemler")
    print("=" * 100)
    print("NOT: 'toplam(naive)' basit toplamdır (bileşik faiz/pozisyon büyüklüğü YOK) — sadece")
    print("kaba bir yön göstergesi, gerçek getiri değil. Aynı anda birden fazla tf açık olabildiği")
    print("için tf'ler arası toplamlar da doğrudan toplanabilir/karşılaştırılabilir değildir.\n")

    tum_yon_toplam = {'long': 0, 'short': 0}

    for tf in CONFIG['timeframes'].keys():
        if tf == '15dk':
            continue  # 15dk artık bağımsız bir sinyal değil (bkz. main.py notu), DB'de sadece eski/tarihsel veri var
        ozet = tf_performans_ozeti(tf, path)
        if ozet['genel'] is None:
            print(f"[{tf}] Henüz kapanmış işlem yok.\n")
            continue

        print(f"[{tf}]")
        _satir_yazdir("TÜMÜ", ozet['genel'])
        print()
        print("    -- Yön bazlı --")
        for yon in ('long', 'short'):
            _satir_yazdir(yon, ozet['yon_kirilimi'].get(yon), girinti="      ")
            if yon in ozet['yon_kirilimi']:
                tum_yon_toplam[yon] += ozet['yon_kirilimi'][yon]['islem_sayisi']
        print("    -- Kategori bazlı --")
        for kat, o in sorted(ozet['kategori_kirilimi'].items(), key=lambda kv: -kv[1]['islem_sayisi']):
            _satir_yazdir(kat, o, girinti="      ")
        print("    -- Kapanış tipi bazlı --")
        for kt, o in ozet['kapanis_tipi_kirilimi'].items():
            _satir_yazdir(kt, o, girinti="      ")
        print()

    # YAPISAL UYARI: bir yön diğerine göre ezici çoğunluktaysa (ör. sürekli yükselen
    # bir piyasada funding hep pozitif kalıp sistem sürekli short söylüyorsa) bunu
    # gizlemek yerine açıkça işaretle -- win-rate/ortalama tek başına bu asimetriyi
    # göstermeyebilir.
    toplam = tum_yon_toplam['long'] + tum_yon_toplam['short']
    if toplam > 0:
        for yon, sayi in tum_yon_toplam.items():
            oran = sayi / toplam * 100
            if oran >= 85:
                diger = 'short' if yon == 'long' else 'long'
                print(f"⚠️  YAPISAL UYARI: kapanan işlemlerin %{oran:.0f}'i '{yon}' yönünde, "
                      f"'{diger}' neredeyse hiç üretilmemiş (15dk hariç, tüm tf'ler toplamında "
                      f"{sayi}/{toplam}). Bu ya piyasanın bu dönemde gerçekten tek yönlü olmasından "
                      f"(ör. sürekli trend + funding hep aynı taraftaydı) ya da genel_durum "
                      f"mantığının bir yönü sistematik olarak az/çok üretmesinden kaynaklanabilir "
                      f"-- fiyatın gerçek yönüyle (veri.price min/max) karşılaştırıp hangisi "
                      f"olduğuna bakmakta fayda var.")

if __name__ == "__main__":
    tum_tf_performans_raporu()