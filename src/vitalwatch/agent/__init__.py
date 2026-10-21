"""``vitalwatch-agent`` — sunucu ajanı.

Uygulamanın dışında kalan arızaları (web sunucusu 502/504, PHP-FPM sınırı,
veritabanı FATAL, düşen systemd birimi, hata veren Docker konteyneri, OOM,
dolan disk) hata olayı olarak hub'ın sunucu ucuna gönderir. Yalnızca standart
kütüphane; sözleşme: vitalwatch-doc ``features/22-sunucu-ajani``.
"""
