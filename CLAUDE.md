# vitalwatch (Python) — Çalışma Kuralları

Bu paket **izlenen Python projelerine** kurulur. Hub ayrı repodadır:
[vitalwatch-hub](https://github.com/CestnyTR/vitalwatch-hub) — tam şartname orada
`docs/02-spec.md`.

Kardeş paketler: `cestnytr/vitalwatch-laravel` (Laravel), `vitalwatch-node` (Node),
`t.js` (tarayıcı, hub reposunda `resources/sdk/t.js`).

## Dil

Yorumlar ve dokümantasyon Türkçe; **sınıf, fonksiyon ve değişken adları İngilizce** —
yerel değişkenler dahil. Kardeş paketlerle aynı kural. README ve public API
dokümantasyonu Türkçe.

---

## Bu paket public bir repodur

- **Hiçbir sır koda girmez.** Anahtar ve secret ortam değişkeninden okunur.
- Testlerde gerçekçi sağlayıcı öneki taşıyan sahte jeton kullanılmaz — GitHub'ın gizli
  anahtar taraması push'u engeller.
- MIT lisanslı.

---

## Kapsam sınırı — bilinçli

**Tarayıcı kodu bu pakete girmez.** `t.js` hub'dan servis edilir ve öyle kalmalıdır:
hosted olması, bir düzeltmenin dakikalar içinde tüm sitelere yayılmasını sağlıyor.

Bu pakete yalnızca **sunucuda çalışan** kod girer: WSGI/ASGI uygulamaları, Flask,
arka plan işleri, CLI betikleri.

---

## Bağlayıcı kısıtlar

### Toplanmayacaklar

Yapılandırmayla dahi açılamaz — kodda karşılığı bulunmamalı:

- IP adresi
- User-Agent
- İstek gövdesi / form verisi
- Query string **değerleri** (yalnızca yol saklanır)
- Oturum verisi, çerez
- Ham SQL literal değerleri

### Temizlik desenleri kardeş paketlerle aynı kalır

`src/vitalwatch/scrubber.py` içindeki desenler `cestnytr/vitalwatch-laravel` (PHP) ile aynı davranmalıdır.
Ayrışırlarsa aynı hata iki SDK'da farklı maskelenir ve parmak izi bölünür — panelde tek
hata iki ayrı satır olarak görünür.

Bilinen sapma: Python `re` Unicode özellik kaçışlarını (`\p{L}`) desteklemez.
Karşılıkları `\w` / `[^\W\d_]` ile kuruldu. Gerçekçi girdide davranış aynıdır;
değiştirilecekse `tests/test_scrubber.py` bu eşdeğerliği korumalıdır.

---

## Davranış garantileri — ihlal edilemez

1. **Hata yutulmaz.** Handler zinciri korunur, `raise` engellenmez.
2. **Süreç yönetimine karışılmaz.** `sys.exit` çağrılmaz, mevcut `excepthook` zincirlenir.
3. **Kendi hatasını raporlamaz.** Sonsuz döngü riski; tüm SDK kodu `try/except` ile sarılır.
4. **Kullanıcı isteğini bekletmez.** Gönderim arka plan thread'inden, kısa zaman aşımıyla.
5. **Hub erişilemezse sessizce vazgeçilir.**
6. **`VITALWATCH_ENABLED=false` iken hiçbir veri gönderilmez.**

---

## Teknik tercihler

| Konu | Karar |
|---|---|
| Python | `>=3.9` |
| HTTP | `urllib.request` — `requests` bağımlılık olurdu |
| Bağımlılık | **Sıfır.** Flask yalnızca `vitalwatch/flask.py` içinde ve çağrı anında import edilir |
| Test | `unittest` — bağımlılık eklemeden |
| Gönderim | Sınırlı kuyruk + tek daemon thread; kuyruk dolarsa olay düşer |
| Sürümleme | Semver, git tag |

### Node paketinden ayrışan yerler

Bunlar kopyalanmadı çünkü Python'da **sessizce çalışmazlardı**:

- `WeakSet` ile tekilleştirme — yerleşik istisnalar weakref kabul etmez. İşaret
  istisnanın üzerinde taşınır (`_vitalwatch_reported`).
- Beklenmeyen `fetch` — WSGI senkron; kuyruk + arka plan thread'i kullanılır.
- Fork sonrası temizlik gerekir: gunicorn `--preload` ile fork ediyor,
  `os.register_at_fork` olmadan çocuk süreç ölü bir kuyruğa yazar.

Bu üçü bu paketin varlık sebebidir; değiştirilmeden önce test edilmeli.

---

## Sessiz arıza karşıtı

Bu ailenin tekrarlayan hatası **sessizce çalışmama**. Hub geçersiz isteğe de `204` döner,
bu yüzden gönderen taraf reddedildiğini hiçbir zaman öğrenemez.

Karşı tedbirler:

- `vitalwatch-status` — yereldeki yanlış yapılandırmayı gösterir. Secret'in 64 karakter olması
  ayrıca kontrol edilir: en sık hata eksik kopyalamadır.
- Testler hub'ın imza doğrulamasını birebir uygulayan sahte bir hub'a karşı koşar;
  "204 aldım" yeterli sayılmaz, imzanın kabul edildiği doğrulanır.
- Her olayla `sdk_version` gönderilir — panel hangi projenin eski sürümde kaldığını
  gösterir.
