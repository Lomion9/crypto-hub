import os
import json

# ==========================================
# AYARLAR (config.json)
# ==========================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "timeframes": {
        "15dk": {"periods": 1, "oi_pct": 0.31, "price_pct": 0.22, "kapanis_esigi": 3, "sinir_saatleri": None},
        "1sa":  {"periods": 4, "oi_pct": 0.88, "price_pct": 0.43, "kapanis_esigi": 1, "sinir_saatleri": list(range(24)), "confirm_kaynak": "15dk", "confirm_n": 4},
        "2sa":  {"periods": 8, "oi_pct": 1.65, "price_pct": 0.72, "kapanis_esigi": 1, "sinir_saatleri": [1,3,5,7,9,11,13,15,17,19,21,23], "confirm_kaynak": "15dk", "confirm_n": 8},
        "4sa":  {"periods": 16, "oi_pct": 3.08, "price_pct": 1.08, "kapanis_esigi": 1, "sinir_saatleri": [23,3,7,11,15,19], "confirm_kaynak": "1sa", "confirm_n": 4},
        "8sa":  {"periods": 32, "oi_pct": 5.10, "price_pct": 1.36, "kapanis_esigi": 1, "sinir_saatleri": [3,11,19], "confirm_kaynak": "1sa", "confirm_n": 8},
        "24sa": {"periods": 96, "oi_pct": 7.73, "price_pct": 1.76, "kapanis_esigi": 1, "sinir_saatleri": [3], "confirm_kaynak": "4sa", "confirm_n": 6}
    },
    "funding_thresholds": {
        "extreme_pct": 0.0030
    },
    "adaptive": {
        "enabled": True,
        "lookback_days": 7,
        "quiet_days": 2,
        "noise_percentile": 80
    },
    "telegram": {
        "min_interval_minutes": 60
    },
    "debug": {
        "enabled": False,
        "interval_seconds": 30
    },
    "huobi": {
        "delay_seconds": 20
    }
}

def load_config():
    """config.json varsa oradan okur; yoksa (ilk çalıştırma) varsayılan değerlerle
    dosyayı kendisi oluşturur. Eşikleri değiştirmek için artık kod açmana gerek yok —
    sadece config.json içindeki sayıyı değiştirip kaydetmen yeterli (script'i yeniden
    başlattığında yeni değerler devreye girer)."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
            merged = json.loads(json.dumps(DEFAULT_CONFIG))  # derin kopya
            for section, values in user_config.items():
                if section in merged and isinstance(values, dict):
                    merged[section].update(values)
                else:
                    merged[section] = values
            return merged
        except Exception as e:
            print(f"  ⚠️ config.json okunamadı ({e}), varsayılan ayarlar kullanılıyor.")
            return DEFAULT_CONFIG
    else:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"  ℹ️ config.json bulunamadı, varsayılan ayarlarla oluşturuldu: {CONFIG_PATH}")
        return DEFAULT_CONFIG

CONFIG = load_config()