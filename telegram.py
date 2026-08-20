import os
import requests

# ==========================================
# TELEGRAM BİLDİRİMİ
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_LAST_TELEGRAM_DURUMLAR = {}  # {tf: son gönderilen genel_durum}

def should_send_telegram(tf_sonuclari):
    """tf_sonuclari: {tf: {'genel_durum': ..., 'telegram_uygun': ..., ...}}.
    Bir timeframe'in genel_durum'u 'İşlem Açma'/'Veri Bekleniyor' DIŞINDA bir şey
    gösterip kendi son gönderilen durumundan FARKLI OLDUĞUNDA VE 'telegram_uygun'
    (konfirmasyon şartı — bkz. main.log_snapshot/sinyal.son_tf_genel_durumlar)
    sağlandığında True döner. 15dk'nın telegram_uygun'u her zaman False'tur, yani
    15dk hiçbir zaman tek başına mesaj tetiklemez.

    ÖNEMLİ: bir değişiklik farkedilip henüz KONFİRME OLMADIYSA, _LAST_TELEGRAM_DURUMLAR
    GÜNCELLENMEZ — böylece bir sonraki turda konfirmasyon sağlanırsa (aynı genel_durum
    hâlâ geçerliyken) hâlâ 'yeni' sayılıp gönderilir. Sadece FİİLEN gönderilen (ya da
    'İşlem Açma'/'Veri Bekleniyor' gibi nötr) durumlar son-gönderilen state'i günceller."""
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
            # konfirme olmadıysa onceki'yi güncelleme: değişiklik "beklemede" kalsın
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