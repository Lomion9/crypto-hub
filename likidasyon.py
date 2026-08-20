# ==========================================
# LİKİDASYON HARİTASI (tasarım aşamasında — henüz uygulanmadı)
# ==========================================
#
# Şu ana kadar konuşmada netleşen kararlar:
#   - Yöntem: Coinglass'ın kullandığı gibi, seçilebilir bir zaman penceresi
#     (ör. 12s/24s) içindeki OI DEĞİŞİMLERİNİ (delta) birikimli olarak modelle;
#     pencere dışına çıkan eski delta'lar otomatik düşer (sabit-pencereli
#     kümülatif model — saf "anlık" model YETERSİZ, çünkü pencere büyüyünce
#     hem kümelerin yeri hem miktarı gerçekten değişiyor).
#   - Linear (USDT) ve inverse (USD/coin-margined) OI'ler AYRI katmanlar olarak
#     tutulacak, tek bir karışık toplama indirgenmeyecek (teminat matematiği
#     farklı olduğu için).
#   - Fiyat serisi: Binance'ten 15 dakikalık OHLC (sadece close değil — High/Low
#     da gerekli, çünkü fiyat bir seviyeye iğne atıp geri çekilse bile o
#     seviyedeki likidasyonları temizler; bunu görmezsen harita şişer/yanlış
#     kalır).
#   - OI verisi: db.py üzerinden mevcut oi_funding_history.db'den OKUNACAK
#     (main.py hâlâ tek yazan taraf; bu script sadece okuyor, veri tekrarı yok).
#   - Bu script main.py'den TAMAMEN AYRI çalışacak (ayrı süreç/zamanlama),
#     main.py'ye entegre edilmeyecek.
#
# Henüz karara bağlanmadı:
#   - Kaldıraç dağılımı varsayımı (hangi kaldıraç seviyelerine ne ağırlık
#     verilecek, funding'e göre dinamik mi olacak yoksa sabit mi).
#   - Fiyat aralığı ve bin genişliği (haritanın ne kadar geniş/hassas olacağı).
#   - Likidasyon formülü detayları (isolated marj varsayımıyla, bakım marjı
#     oranları borsaya göre mi sabit mi olacak).
#   - Görselleştirme şekli (x=fiyat/y=yoğunluk mu, yoksa x=zaman/y=fiyat/
#     renk=yoğunluk mu).
#
# Bu kararlar netleşince buraya gerçek implementasyon eklenecek.