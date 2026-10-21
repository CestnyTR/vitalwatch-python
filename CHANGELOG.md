# Değişiklik günlüğü

> **Ad değişikliği:** Paket VitalWatch adıyla yeniden adlandırıldı — paket `vitalwatch`
> (`import vitalwatch`, `VitalWatchFlask`), teşhis komutu `vitalwatch-status`, ortam
> değişkenleri `VITALWATCH_*`, imza başlıkları `X-VitalWatch-Signature` /
> `X-VitalWatch-Timestamp`. Aşağıdaki geçmiş kayıtlar yeni adlarla yazılmıştır.

## 0.2.0

### Eklendi

- **Sunucu ajanı: `vitalwatch-agent`.** Uygulamanın dışında kalan arızaları hata
  olayı olarak hub'ın sunucu ucuna gönderir: nginx/apache/Plesk vhost error log'ları
  (`web_server`, 502/504 tahmini, `route`), PHP-FPM (`php_fpm`), PostgreSQL,
  MySQL/MariaDB, MongoDB, Redis (`database`), journald'dan başarısız birim, OOM ve
  çekirdek hataları (`service_failed`, `oom`, `kernel`), 5 dakikada bir
  `systemctl --failed`, `docker events` (`container_failed`: hata kodlu çıkış,
  10 dakikada ≥ 3 `die` = yeniden başlama döngüsü, `unhealthy`, OOM), disk ve inode
  doluluğu (`disk_full`, bağlama noktası başına saatte bir) ve kullanıcı tanımlı
  `[log:<ad>]` desenleri (`log_error`). Komutlar: `run`, `check [--test]`, `systemd`.
  Yalnızca standart kütüphane; sözleşme `vitalwatch-doc` `features/22-sunucu-ajani`.
- Ajan kişisel veriyi sunucudan çıkmadan atar: IPv4/IPv6 → `[ip]`, nginx `client:`,
  `referrer`, `host` alanları ve Apache `[client …]` / `referer:` tamamen silinir,
  query string ve fragment atılır, ardından paketin mevcut maskesi uygulanır.
  SSH/kimlik doğrulama, erişim ve posta logları hiç okunmaz.
- Akış kontrolü: aynı mesaj dakikada 20, toplam dakikada 300 olay; gövde 8 KB'ye ve
  hub'ın istek başına işlediği 8 olaya bölünür; hub erişilemezse 1000 olaya kadar
  bekletme ve üstel geri çekilmeyle yeniden deneme; 5 dakikada bir canlılık isteği
  (`runtime: "agent"`). Hub'ın kendi alan adı her zaman yok sayılır.
- Okuma konumları (inode + ofset) ve journald imleci
  `/var/lib/vitalwatch-agent/state.json`'da; log döndürme ve kırpılma algılanır,
  ilk görülen log sonundan okunur, glob'lar dakikada bir yeniden taranır.

### Değişti

- `vitalwatch-status` güncelleme denetimi artık PyPI'dan (`info.version`) okunuyor;
  ipucu `pip install -U vitalwatch`. README kurulum satırı `pip install vitalwatch`.
- `pyproject.toml`: lisans SPDX ifadesi (`license = "MIT"`), derleme için
  `setuptools>=77`. Eski tablo biçimi setuptools'ta kullanımdan kalkma uyarısı veriyordu.

## 0.1.1

### Eklendi

- **Ortam adı normalleştiriliyor.** Hub yalnızca production/staging/local
  kabul ediyor ve küme dışını — canlılık dahil — 204 dönüp atıyor.
  `APP_ENV=development` ya da `prod` ile kurulan proje hiçbir şey
  göndermiyormuş gibi görünüyordu. Takma adlar çevriliyor (`prod`/`live`,
  `stage`/`stg`/`preprod`/`uat`, `dev`/`development`/`test`/`testing`);
  tanınmayan değer olduğu gibi gidiyor ve teşhis komutu hata veriyor.
  Tablo üç pakette aynı.
  Eskiden tanınmayan değer sessizce `production` sayılıyordu: test verisi
  canlı verinin arasına karışıyordu.
- **Django ara katmanı** (`vitalwatch.django.VitalWatchMiddleware`). Yavaş/5xx istek ve
  yakalanmamış istisna; `got_request_exception` sinyaliyle alttaki ara
  katmanların hataları da stack'iyle gelir. 4xx istisnaları gönderilmez, rota
  deseni gider. `VITALWATCH_HOOK_PROCESS = False` süreç kancalarını kapatır.
- **ASGI ara katmanı** (`vitalwatch.asgi.VitalWatchASGIMiddleware`) — FastAPI, Starlette
  ve diğer ASGI uygulamaları. Hata raporlanıp yeniden fırlatılır; sınıfa bağlı
  `exception_handler` 5xx dönerse hata yine stack'iyle gelir. Rota deseni
  FastAPI'de `scope["route"]`, Starlette'te yönlendirici ağacından okunur.
- Flask'la aynı kurallar: tek arıza tek kayıt, 4xx gönderilmez, raporlayıcı
  çağrı anında çözülür. Çerçeveler yalnızca kendi modüllerinde import edilir;
  bağımlılık sıfır kalıyor. Django 6.1, FastAPI 0.141, Starlette 1.6 ile
  doğrulandı.
- **Sürüm etiketi kendiliğinden bulunuyor** (`vitalwatch.release`). `VITALWATCH_RELEASE`
  neredeyse hiçbir kurulumda dolu değildi; hub bir hatanın hangi deploy ile
  başladığını söyleyemiyordu. Sıra: `init(release=...)` → `VITALWATCH_RELEASE`
  (kırpılır, ≤64) → `GIT_COMMIT`, `GIT_SHA`, `COMMIT_SHA`, `SOURCE_VERSION`,
  `VERCEL_GIT_COMMIT_SHA`, `RENDER_GIT_COMMIT`, `HEROKU_SLUG_COMMIT`,
  `CI_COMMIT_SHA` (hex ise ilk 12 hane, küçük harf) → çalışma dizinindeki
  `.git` (dal ref'i, `packed-refs`, ayrık HEAD, `gitdir:` dosyası; yalnızca
  geçerli 40 hane sha, ilk 12 hane; `ref:` yalnızca `refs/` altı ve `..`
  içermez). Git worktree'leri (`commondir`) kapsam dışı — orada `VITALWATCH_RELEASE`. `git pull` + gunicorn yeniden başlatma ile
  yapılan deploy'da yeni süreç yeni commit'i kendisi okur. Alt süreç yok, hiç
  fırlatmaz; kural Node ve Laravel'le aynı. `vitalwatch-status` etiketi ve kaynağını
  gösteriyor: `Sürüm etiketi  abc123def456 (kaynak: .git)`.
- **`vitalwatch-status` güncelleme denetimi.** Paket PyPI'da değil; GitHub
  etiketlerindeki en yeni kararlı sürüm (`Güncel sürüm`) gösteriliyor, kurulu
  sürümden yeniyse `pip install -U --force-reinstall` komutuyla bir uyarı
  satırı yazılıyor. Uyarıdır — çıkış kodu değişmez. Ağ/hız sınırı hatasında
  `denetlenemedi`; `VITALWATCH_STATUS_OFFLINE` (`1`/`true`/`yes`/`on`/`evet`) denetimi ve satırı
  tamamen atlar.
- **`memory_mb`**: yavaş istek ve `http_5xx` olayları sürecin tepe bellek
  kullanımını (RSS, MB) taşır — Node ve Laravel'le aynı alan. `resource`
  olmayan Windows'ta alan gönderilmez.

### Düzeltildi

- **Tek arıza iki kayıt oluyordu.** Hata veren istek stack'li `exception`'ın
  yanında `after_request`'ten stack'siz bir `http_5xx` daha açıyordu. İstek
  işaretleniyor; işaretli istekte `http_5xx` açılmıyor. Eski test bu çift
  kaydı bilerek bekliyordu, değiştirildi.
- **`@errorhandler(Exception)` varken hata kayboluyordu.** Flask hatayı
  işlenmiş sayıyor, teardown `error=None` alıyordu; yalnızca stack'siz
  `http_5xx` gidiyordu. Uygulamanın `handle_user_exception` /
  `handle_exception` yöntemleri örnek düzeyinde sarılıyor; işleyici 5xx
  dönerse hata stack'iyle gönderiliyor, 4xx dönerse gönderilmiyor.
- **Kancalar raporlayıcıyı kurulumda yakalıyordu.** `VitalWatchFlask(app)` sonrası
  `vitalwatch.init(...)` çağrılırsa olaylar eski örneğe gidiyordu. İstek kancaları
  da, `sys.excepthook` / `threading.excepthook` / kapanış boşaltması ve
  canlılık zamanlayıcısı da raporlayıcıyı artık çağrı anında çözüyor. Kurulum
  anında yapılandırma yoksa canlılık hiç başlamıyordu; `init(...)` onu yeniden
  deniyor.
- **Süreç kancaları zincirleniyordu.** `hook_process()` her çağrıda
  excepthook'u bir kat daha sarıyor, `atexit`'e bir boşaltma daha ekliyordu
  (iki kez `VitalWatchFlask(app)`, her ASGI yığını kurulumu). Artık süreç başına bir
  kez kuruluyor.
- **İki kez `VitalWatchFlask(app)` çift kayıt açıyordu.** `after_request` iki kez
  kaydediliyor, her yavaş istek ve `abort(500)` iki olay oluyordu;
  `app.extensions["vitalwatch"]` ile ikinci kurulum no-op.
- **Kurulum uygulamayı düşürebiliyordu.** UTF-8 olmayan (ör. cp1254 kaydedilmiş)
  `.env` `UnicodeDecodeError` fırlatıp `VitalWatchFlask(app)`'i ve `vitalwatch.report()`'u
  patlatıyordu. Okunamayan dosya atlanıyor; `init_app` ve `report()` hiçbir
  koşulda fırlatmıyor.
- **Flask: metin durum kodu.** İşleyici `("…", "500 HATA")` dönünce 5xx
  anlaşılmıyor, hata stack'siz `http_5xx` oluyordu.
- **ASGI: sınıfa bağlı işleyiciler ara katman sırasına bağlıydı.** VitalWatch
  `ExceptionMiddleware`'in hemen dışında değilse (ör. GZip ondan sonra
  eklendiyse, ya da `middleware=[VitalWatch, CORS]`) işleyicinin 5xx'i stack'siz
  gidiyordu. `ExceptionMiddleware` ilk istekte `.app` zinciri boyunca aranıyor.
- **ASGI: `BackgroundTasks` yavaş istek sayılıyordu.** Süre son gövde parçası
  gidince duruyor; yanıt sonrası işler ölçüme girmiyor.
- **ASGI: iç içe katman çift kayıt açıyordu.** `Mount` edilen alt uygulamada
  da ara katman varsa yavaş istek iki kez gidiyordu; istek dıştakinde ölçülüyor.
- **Rota deseni her istekte hesaplanıyordu.** Starlette'te her hızlı 200 için
  yönlendirici taranıp desenler derleniyordu (300 rotada ~2 ms). Desen artık
  yalnızca olay gerçekten gidecekse hesaplanıyor, Starlette adayları
  önbellekte. Flask ve Django da aynı kuralı izliyor.
- **ASGI: hata denetimi özgün hatayı değiştirebilirdi.** `status_code`
  özelliği fırlatan bir istisnada yeni hata özgününün yerine geçerdi.
- **`VITALWATCH_TIMEOUT` birimi.** Node aynı adla milisaniye bekliyor;
  `2000` yazılınca süreç hub'ı 2000 saniye bekleyebilirdi. Artık saniye,
  100 ve üstü milisaniye; geçersiz, sıfır/eksi, NaN ya da sonsuz değer 2
  saniyeye döner, sonuç 0.1–10 saniyeye sıkıştırılır (kardeş paketlerle ortak
  kural).
- **Flask testleri koşmuyordu.** Flask kurulu olmayan ortamda beş test
  sessizce atlanıyordu; 0.1.1 değişiklikleri Flask 3.0.0 (lens'in sürümü)
  kurulu ortamda doğrulandı.
- **Maskeleme okunur adları bozuyordu.** Yükleme dosya adları okunur kalıyor. `<ad>-<zaman damgası>-<rastgele>.jpeg`
  biçimindeki adlar panelde `/uploads/discount/[token][card]-752066249.jpeg`
  diye çıkıyordu: zaman damgası kart, uzun ad jeton sanılıyordu.

  - **Kart:** yalnızca 2–9 ile başlayan ve Luhn'dan geçen dizi maskelenir;
    milisaniye damgası 1 ile başlıyor. Bilinen taviz: yanlış yazılmış bir kart
    numarası (Luhn'u geçmez) ve 1 ile başlayan UATP kartları artık
    maskelenmiyor.
  - **Jeton:** 16+ karakterlik bölünmemiş parça taşıyan ya da harf içeren hex
    dizi (UUID, hash) maskelenir. Tireyle birleşmiş kısa parçalar okunur addır.
    Bilinen taviz: ayraçla (tire/alt çizgi) kısa parçalara bölünmüş rastgele
    anahtarlar — ör. tire/alt çizgisi sık düşen base64url jetonların bir kısmı —
    maskelenmeden geçebilir.
  - **Telefon:** önünde rakam varsa eşleşmez. Son on hanesi 5 ile başlayan
    zaman damgası `179[phone]` oluyordu.

  Kurallar hub, `t.js`, Node, Laravel ve Python'da birebir aynı; 28 ortak
  vektörle karşılaştırıldı.

## 0.1.0

İlk sürüm. Kardeş paket `vitalwatch-node` 0.4.1 ile aynı kapsam.

- HMAC imzalı sunucu ucu gönderimi (`/api/i/{key}/server`), yönlendirme takip edilmez
- KVKK temizliği: e-posta, telefon, TCKN, IBAN, kart, jeton, SQL literalleri
- `exception` / `fatal` / `http_5xx` / `slow_request`
- Flask entegrasyonu (`vitalwatch.flask.VitalWatchFlask`)
- Süreç kancaları: `sys.excepthook`, `threading.excepthook`
- 8 saatlik canlılık isteği
- `vitalwatch-status` teşhis komutu (`--test`, `--vitalwatch`)

Node paketinden ayrışan yerler ve gerekçeleri:

- **Gönderim kuyruğu.** Node'da `fetch` zaten beklenmeyen bir promise; WSGI senkron
  çalıştığı için burada olaylar sınırlı bir kuyruğa bırakılır ve tek bir arka plan
  thread'i gönderir. Kuyruk dolduğunda olay düşürülür — bloke olmak izlenen uygulamayı
  yavaşlatırdı.
- **Tekilleştirme.** Node `WeakSet` kullanıyor; Python'da yerleşik istisnalar weakref
  kabul etmez (`weakref.ref(ValueError())` → `TypeError`), o yüzden işaret istisnanın
  üzerinde taşınır.
- **Fork temizliği.** Gunicorn `--preload` ile fork ediyor; `os.register_at_fork` ile
  çocuk süreçte kuyruk yeniden kurulur, yoksa olaylar ölü bir thread'e yazılırdı.
- **Unicode desenleri.** Python `re` `\p{L}` desteklemez; karşılıkları `\w` /
  `[^\W\d_]` ile kuruldu, gerçekçi girdide davranış aynıdır.
