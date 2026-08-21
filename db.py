import os
import sqlite3
import pandas as pd

from config import CONFIG

# ==========================================
# ZAMAN SERİSİ / VERİTABANI
# ==========================================
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oi_funding_history.db")
HISTORY_FILE = DB_FILE  # geri uyumluluk için aynı isim korunuyor
# NOT: 'price' kolonu geriye dönük uyumluluk için KAPANIŞ (close) fiyatını tutmaya
# devam ediyor — sinyal.py'deki tüm eşik/trend mantığı hâlâ bu tek sütuna bakıyor,
# davranışları bozmadan sadece open/high/low ek bilgi olarak ekleniyor (likidasyon
# haritasının 'fiyat bir seviyeye değip geri çekildi mi' tespiti için).
VERI_COLS = ['tarih', 'saat', 'oi_btc', 'oi_usd', 'funding_pct', 'price',
             'price_open', 'price_high', 'price_low', 'oi_linear_btc', 'oi_inverse_btc',
             'cvd_spot_btc', 'cvd_perp_btc']

def _migrate_add_ohlc_columns(conn):
    """Var olan (eski şemalı) bir oi_funding_history.db'de price_open/high/low ve
    oi_linear/inverse kolonları yoksa ekler. CREATE TABLE IF NOT EXISTS zaten var olan
    tabloya yeni kolon eklemediği için bu adım şart — yoksa eski DB'de INSERT hata verir."""
    mevcut_kolonlar = {row[1] for row in conn.execute("PRAGMA table_info(veri)").fetchall()}
    for kolon in ('price_open', 'price_high', 'price_low', 'oi_linear_btc', 'oi_inverse_btc'):
        if kolon not in mevcut_kolonlar:
            conn.execute(f"ALTER TABLE veri ADD COLUMN {kolon} REAL")

def _init_db(conn):
    """veri tablosu + her timeframe için ayrı durum/sinyal/aktif-işlem üçlüsü oluşturur.
    durum_{tf}.id, veri.id ile BİREBİR aynı değeri kullanır (otomatik artan değil, elle
    veriliyor) — böylece hangi durum satırının hangi veri satırına ait olduğu asla
    tarih+saat metin eşleşmesine bağlı kalmaz, id ile garanti hizalı kalır."""
    conn.execute('''CREATE TABLE IF NOT EXISTS veri (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tarih TEXT, saat TEXT, oi_btc REAL, oi_usd REAL, funding_pct REAL,
        price REAL, cvd_spot_btc REAL, cvd_perp_btc REAL
    )''')
    _migrate_add_ohlc_columns(conn)
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
    """Sadece 'veri' tablosunu okur — timeframe durumları artık geçmişe bakmak için
    ayrı bir join gerektirmiyor, her timeframe kendi periyot kadar geriye gidip
    doğrudan 'veri' tablosundaki oi_btc/price'a bakıyor (bkz. sinyal._periyot_durumu)."""
    conn = sqlite3.connect(path)
    _init_db(conn)
    veri_df = pd.read_sql("SELECT * FROM veri", conn)
    conn.close()

    if veri_df.empty:
        return pd.DataFrame(columns=VERI_COLS + ['timestamp'])

    veri_df['funding_pct'] = veri_df['funding_pct'].astype(float)
    veri_df['timestamp'] = pd.to_datetime(veri_df['tarih'] + ' ' + veri_df['saat'], format='%d.%m.%Y %H:%M')
    return veri_df.sort_values('timestamp').reset_index(drop=True)