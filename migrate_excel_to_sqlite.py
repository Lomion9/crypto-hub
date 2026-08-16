"""
Mevcut oi_funding_history.xlsx dosyasındaki tüm veriyi (Veri, Durum, Gunluk_Ozet,
Sinyal_Performans sayfaları) yeni oi_funding_history.db (SQLite) dosyasına taşır.
Bir kereye mahsus çalıştırılır: python migrate_excel_to_sqlite.py

Orijinal .xlsx dosyan SİLİNMEZ, olduğu gibi kalır — istersen yedek olarak saklarsın.
"""
import os
import sqlite3
import pandas as pd

KLASOR = os.path.dirname(os.path.abspath(__file__))
EXCEL_PATH = os.path.join(KLASOR, "oi_funding_history.xlsx")
DB_PATH = os.path.join(KLASOR, "oi_funding_history.db")

VERI_COLS = ['tarih', 'saat', 'oi_btc', 'oi_usd', 'funding_pct', 'price', 'cvd_spot_btc', 'cvd_perp_btc']
DURUM_COLS = ['tarih', 'saat', 'funding_durum', 'oi_durum', 'fiyat_durum', 'cvd_durum', 'genel_durum']
OZET_COLS = ['tarih', 'genel_durum', 'adet']
PERFORMANS_COLS = ['kapanis_tarih', 'kapanis_saat', 'sinyal', 'yon', 'giris_tarih', 'giris_saat', 'giris_fiyat', 'cikis_fiyat', 'kar_yuzde']


def init_db(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS veri (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tarih TEXT, saat TEXT, oi_btc REAL, oi_usd REAL, funding_pct REAL,
        price REAL, cvd_spot_btc REAL, cvd_perp_btc REAL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS durum (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tarih TEXT, saat TEXT, funding_durum TEXT, oi_durum TEXT,
        fiyat_durum TEXT, cvd_durum TEXT, genel_durum TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS gunluk_ozet (
        tarih TEXT, genel_durum TEXT, adet INTEGER
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS sinyal_performans (
        kapanis_tarih TEXT, kapanis_saat TEXT, sinyal TEXT, yon TEXT,
        giris_tarih TEXT, giris_saat TEXT, giris_fiyat REAL, cikis_fiyat REAL, kar_yuzde REAL
    )''')
    conn.commit()


def main():
    if not os.path.isfile(EXCEL_PATH):
        print(f"❌ {EXCEL_PATH} bulunamadı. Bu script'i Excel dosyanla aynı klasörde çalıştır.")
        return

    if os.path.isfile(DB_PATH):
        cevap = input(f"⚠️  {os.path.basename(DB_PATH)} zaten var. Üzerine yazılsın mı? (evet/hayır): ").strip().lower()
        if cevap not in ("evet", "e", "yes", "y"):
            print("İptal edildi.")
            return
        os.remove(DB_PATH)

    xls = pd.ExcelFile(EXCEL_PATH, engine='openpyxl')
    print(f"Excel'de bulunan sayfalar: {xls.sheet_names}")

    if 'Veri' in xls.sheet_names and 'Durum' in xls.sheet_names:
        # Yeni format: veri/durum zaten ayrı sayfalarda
        veri_df = pd.read_excel(EXCEL_PATH, sheet_name='Veri', engine='openpyxl')
        durum_df = pd.read_excel(EXCEL_PATH, sheet_name='Durum', engine='openpyxl')
        ozet_df = pd.read_excel(EXCEL_PATH, sheet_name='Gunluk_Ozet', engine='openpyxl') if 'Gunluk_Ozet' in xls.sheet_names else pd.DataFrame(columns=OZET_COLS)
        performans_df = pd.read_excel(EXCEL_PATH, sheet_name='Sinyal_Performans', engine='openpyxl') if 'Sinyal_Performans' in xls.sheet_names else pd.DataFrame(columns=PERFORMANS_COLS)
    else:
        # Eski format: tüm sütunlar tek sayfada (Sheet1) — burada ikiye bölüyoruz
        eski_df = pd.read_excel(EXCEL_PATH, engine='openpyxl')
        for col in VERI_COLS + DURUM_COLS:
            if col not in eski_df.columns:
                eski_df[col] = 0.0 if col not in ['tarih', 'saat', 'funding_durum', 'oi_durum', 'fiyat_durum', 'cvd_durum', 'genel_durum'] else "N/A"
        veri_df = eski_df[VERI_COLS].copy()
        durum_df = eski_df[DURUM_COLS].copy()
        ozet_df = durum_df.groupby(['tarih', 'genel_durum']).size().reset_index(name='adet')[OZET_COLS] if not durum_df.empty else pd.DataFrame(columns=OZET_COLS)
        performans_df = pd.DataFrame(columns=PERFORMANS_COLS)
        print("ℹ️  Eski (tek sayfalı) format tespit edildi, Veri/Durum otomatik ayrıştırıldı.")

    if len(veri_df) != len(durum_df):
        print(f"❌ Veri ({len(veri_df)} satır) ile Durum ({len(durum_df)} satır) satır sayıları uyuşmuyor, "
              f"otomatik id eşleştirmesi güvenli değil. Elle kontrol gerekiyor, işlem durduruldu.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    veri_df[VERI_COLS].to_sql('veri', conn, if_exists='append', index=False)
    durum_df[DURUM_COLS].to_sql('durum', conn, if_exists='append', index=False)
    if not ozet_df.empty:
        ozet_df[OZET_COLS].to_sql('gunluk_ozet', conn, if_exists='append', index=False)
    if not performans_df.empty:
        performans_df[PERFORMANS_COLS].to_sql('sinyal_performans', conn, if_exists='append', index=False)

    conn.commit()
    conn.close()

    print(f"\n✅ Taşıma tamamlandı: {os.path.basename(DB_PATH)}")
    print(f"   veri: {len(veri_df)} satır")
    print(f"   durum: {len(durum_df)} satır")
    print(f"   gunluk_ozet: {len(ozet_df)} satır")
    print(f"   sinyal_performans: {len(performans_df)} satır")
    print(f"\nOrijinal Excel dosyan ({os.path.basename(EXCEL_PATH)}) dokunulmadan duruyor, yedek olarak saklayabilirsin.")


if __name__ == "__main__":
    main()