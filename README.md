# vitalwatch (Python)

[VitalWatch Hub](https://github.com/CestnyTR/vitalwatch-hub) için Python raporlayıcı: sunucu
hataları, yavaş istekler, canlılık.

Bağımlılık yok. Build adımı yok. Python 3.9+.

Kardeş paketler: `cestnytr/vitalwatch-laravel` (Laravel), `vitalwatch-node` (Node),
`t.js` (tarayıcı, hub reposunda `resources/sdk/t.js`).

---

## Kapsam — ve neden bu kadar dar

Bu paket **yalnızca sunucu tarafını** kapsar. Tarayıcı hataları hub'dan servis edilen
`t.js` ile toplanır; o dosya bilinçli olarak pakete taşınmadı — hosted olması bir
düzeltmenin dakikalar içinde tüm sitelere yayılmasını sağlıyor, paket olsaydı her
düzeltme için bütün projelerin yeniden dağıtılması gerekirdi.

Toplanmayanlar — yapılandırmayla dahi açılamaz:

IP adresi · User-Agent · istek gövdesi · query string **değerleri** (yalnızca yol) ·
oturum verisi · çerez · ham SQL literalleri

---

## Kurulum

```bash
pip install git+https://github.com/CestnyTR/vitalwatch-python.git
```

`.env` dosyasına — kardeş paketlerle aynı değişken isimleri:

```env
VITALWATCH_ENABLED=true
VITALWATCH_URL=https://vitalwatch.example.com
VITALWATCH_KEY=proje-anahtari
VITALWATCH_SECRET=<panelden alınan 64 karakterlik secret>
VITALWATCH_ENV=production
```

### Flask

```python
from flask import Flask
from vitalwatch.flask import VitalWatchFlask

app = Flask(__name__)
VitalWatchFlask(app)
```

Tek satır şunları kurar: yakalanmamış istisnalar (`teardown_request`), 5xx ve yavaş
istekler (`after_request`), süreç düzeyi kancalar (`sys.excepthook`,
`threading.excepthook`) ve 8 saatlik canlılık isteği.

- **Tek arıza, tek kayıt:** hata veren istek yalnızca stack'li `exception` açar;
  aynı istek için ayrıca `http_5xx` gönderilmez (0.1.1+).
- **Kendi hata işleyiciniz varsa:** `@app.errorhandler(Exception)` ya da
  `errorhandler(500)` tanımlıysa hata yine stack'iyle gelir. İşleyici 4xx dönerse
  gönderilmez. Eskiden bu durumda hata kayboluyor, yalnızca stack'siz `http_5xx`
  geliyordu.
- **`abort(4xx)` gönderilmez:** bilinçli yanıt, arıza değil.
- **Kurulum sırası serbest:** `VitalWatchFlask(app)` önce, `vitalwatch.init(...)` sonra
  çağrılabilir; istek kancaları da süreç kancaları da (`sys.excepthook`,
  `threading.excepthook`) güncel raporlayıcıyı çağrı anında çözer. Kurulumda
  yapılandırma yoksa canlılık `init(...)` çağrılınca başlar.
- **İki kez kurmak zararsız:** aynı uygulamada ikinci `VitalWatchFlask(app)` hiçbir
  şey eklemez; süreç kancaları süreç başına bir kez kurulur.
- **Uygulamayı düşürmez:** kurulum (bozuk ya da UTF-8 olmayan `.env` dahil)
  hiçbir koşulda hata fırlatmaz.
- **Süreç kancalarını kapatmak:** `VitalWatchFlask(app, hook_process_errors=False)`
  yalnızca istek kancalarını kurar.

### Django

```python
# settings.py
MIDDLEWARE = [
    "vitalwatch.django.VitalWatchMiddleware",  # listenin başına yakın
    # ...
]
```

Ara katman yavaş ve 5xx istekleri ölçer, yakalanmamış istisnayı stack'iyle gönderir.
Hata iki yoldan görülür: Django'nun istisnayı 500'e çevirirken yolladığı
`got_request_exception` sinyali (görünümün yanında alttaki ara katmanlardan çıkan
hatalar da) ve `process_exception`. Kurallar Flask'la aynı:

- **Tek arıza, tek kayıt;** hata veren istek için ayrıca `http_5xx` açılmaz.
- **4xx gönderilmez:** `Http404`, `PermissionDenied`, `BadRequest`,
  `SuspiciousOperation`.
- **Rota deseni gider:** `/urun/<int:urun_id>/`, gerçek id değil. `re_path`
  desenlerinde baştaki `^` ve sondaki `$` atılır.
- **Hata yutulmaz:** `process_exception` `None` döner; Django'nun 500 sayfası ve
  `handler500` olduğu gibi çalışır.
- **Süreç kancaları** ilk kurulumda bir kez açılır; kapatmak için
  `VITALWATCH_HOOK_PROCESS = False`.

### FastAPI / Starlette (ASGI)

```python
from fastapi import FastAPI
from vitalwatch.asgi import VitalWatchASGIMiddleware

app = FastAPI()
app.add_middleware(VitalWatchASGIMiddleware)
```

Starlette'te aynı satır (`Starlette(middleware=[Middleware(VitalWatchASGIMiddleware)])` de
olur). Başka bir ASGI uygulaması doğrudan sarılabilir: `app = VitalWatchASGIMiddleware(app)`.
Saf ASGI ara katmanıdır; Starlette/FastAPI import anında yüklenmez.

- **Tek arıza, tek kayıt;** 4xx `HTTPException` ve doğrulama hataları (422)
  gönderilmez. Bilinçli `HTTPException(5xx)` stack'siz `http_5xx` olarak düşer.
- **Rota deseni gider:** `/urun/{urun_id}`; `include_router` öneki ve `Mount`
  yolu desene eklenir.
- **Kendi hata işleyiciniz varsa:** `@app.exception_handler(Exception)` hatayı
  yine bizim katmanımızdan geçirir. Belirli bir sınıfa bağlı işleyici
  (`@app.exception_handler(ValueError)`) 5xx dönerse hata yine stack'iyle gelir,
  4xx dönerse gönderilmez. Ara katman sırası fark etmez (`add_middleware` ya da
  `middleware=[...]` içinde herhangi bir yer). Elle sarılan uygulamada
  (`VitalWatchASGIMiddleware(app)`) Starlette yığınını ilk istekte kurduğu için bu
  ilk istekten sonra devreye girer; o ilk istekte 5xx stack'siz `http_5xx` olur.
- **Süre yanıtın son baytında durur:** `BackgroundTasks` ve yanıt sonrası işler
  yavaş istek sayılmaz. Uzun akış (SSE, büyük indirme) son parçaya kadar sürer
  ve yavaş sayılabilir.
- **İç içe katman tek kayıt açar:** `Mount` edilen alt uygulamada da
  `VitalWatchASGIMiddleware` varsa istek yalnızca dıştakinde ölçülür.
- **Hata yutulmaz:** raporlanır ve yeniden fırlatılır; lifespan ve websocket
  olduğu gibi geçer.
- **Süreç kancalarını kapatmak:**
  `app.add_middleware(VitalWatchASGIMiddleware, hook_process_errors=False)`.

### Çerçevesiz

```python
import vitalwatch

vitalwatch.hook_process()          # kancalar + canlılık
vitalwatch.report(error)           # elle bildirim

vitalwatch.init(ignore=(KeyboardInterrupt, "BenimBeklenenHatam"))  # sınıf ya da ad
vitalwatch.start_heartbeat()       # yalnızca canlılık (hook_process zaten başlatır)
vitalwatch.stop_heartbeat()
```

`init()` ortam değişkenlerini okur; verilen argümanlar onları ezer. Çağrılmazsa ilk
kullanımda kendiliğinden kurulur.

### Kurulumu doğrulayın

```bash
vitalwatch-status
```

Bu adım atlanmamalı. Paketin en pahalı arıza biçimi **sessiz çalışmamadır**: secret
eksik kopyalanmışsa hiçbir şey patlamaz, hiçbir log düşmez — hub geçersiz imzaya da
`204` döner. Kurulum aylarca çalışmıyor olabilir ve o sessizlik "sorun yok" sanılır.

```bash
vitalwatch-status --test         # gerçek bir sınama olayı — panelde hata olarak görünür
vitalwatch-status --vitalwatch   # yalnızca canlılık isteği — panele hata düşürmez
```

`--test` tek bir kurulumu doğrularken doğru seçim. **Onlarca kurulumu gezen bir
güncelleme döngüsünde `--vitalwatch` kullanılır** — `--test` orada panele onlarca sahte hata
bırakır, yani izleme aracı kendi gürültüsünü üretir.

Verinin gerçekten ulaştığı yalnızca **hub panelinden** doğrulanır: proje satırındaki
bağlantı durumu `Bağlı` görünmelidir. Komut bunu kendi başına söyleyemez, çünkü hub
geçerli ile geçersiz imzayı dışarıya aynı yanıtla karşılar.

Komut ayrıca şunları gösterir:

- **`Sürüm etiketi`** — olaylara eklenecek `release` ve nereden bulunduğu:
  `abc123def456 (kaynak: .git)`; kaynak `VITALWATCH_RELEASE`, bir CI/PaaS değişkeninin
  adı, `.git` ya da `yok`. Ayrıntı: [Sürüm etiketi](#sürüm-etiketi-release).
- **`Güncel sürüm`** — GitHub etiketlerindeki en yeni kararlı sürüm (paket PyPI'da
  değil). Kurulu sürümden yeniyse bir `Güncelleme var: pip install -U
  --force-reinstall "vitalwatch @ git+…@vX.Y.Z"` satırı yazılır; bu bir uyarıdır,
  çıkış kodunu değiştirmez. Ağ yoksa ya da GitHub hız sınırına takıldıysa
  `denetlenemedi` yazar (2 sn zaman aşımı). Ağa hiç çıkılmaması için
  `VITALWATCH_STATUS_OFFLINE=1` (`1`, `true`, `yes`, `on`, `evet`; büyük/küçük harf fark
  etmez) — o durumda `Güncel sürüm` satırı hiç yazılmaz.

---

## Yapılandırma

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `VITALWATCH_ENABLED` | `true` | `false` iken hiçbir veri gönderilmez |
| `VITALWATCH_URL` | — | Hub adresi (https) |
| `VITALWATCH_KEY` | — | Proje anahtarı |
| `VITALWATCH_SECRET` | — | 64 karakterlik imza secret'i |
| `VITALWATCH_ENV` | `APP_ENV` | `production` · `staging` · `local`. Takma adlar çevrilir: `prod`/`live` → production, `stage`/`stg`/`preprod`/`uat` → staging, `dev`/`development`/`test`/`testing` → local. Tanınmayan değeri hub reddeder; teşhis komutu hata verir |
| `VITALWATCH_RELEASE` | kendiliğinden | Sürüm etiketi. Boşsa CI/PaaS commit değişkenlerinden ya da `.git`'ten bulunur — aşağıya bakın |
| `VITALWATCH_STATUS_OFFLINE` | — | `1`/`true`/`yes`/`on`/`evet` iken `vitalwatch-status` güncelleme denetimi için ağa çıkmaz |
| `VITALWATCH_TIMEOUT` | `2.0` | Gönderim zaman aşımı, **saniye**. 100 ve üstü milisaniye sayılır — Node aynı adla ms bekliyor, `2000` yazan 2000 saniye beklemesin. Geçersiz, sıfır/eksi, NaN ya da sonsuz → 2 sn; sonuç 0.1–10 sn aralığına sıkıştırılır |
| `VITALWATCH_SLOW_REQUEST_MS` | `1000` | Yavaş istek eşiği |

Ortam değişkenleri `os.environ` ve `.env` / `.env.local` / `.env.<APP_ENV>`
dosyalarından okunur; süreç ortamı her zaman kazanır. Yalnızca `VITALWATCH_` önekli
anahtarlar okunur.

### Sürüm etiketi (`release`)

`VITALWATCH_RELEASE` neredeyse hiçbir kurulumda doldurulmuyordu; hub bu yüzden bir hatanın
**hangi deploy ile başladığını** söyleyemiyordu. Deploy'larımız çoğunlukla sunucuda
`git pull` + gunicorn yeniden başlatma: yeni süreç açılışta commit'i kendisi okur, deploy
sonrası ilk hatada panel yeni sürümü görür. Etiket kurulumda bir kez çözülür (olay başına
değil); ilk dolu kaynak kazanır:

1. `init(release=...)` ile açıkça verilen değer.
2. `VITALWATCH_RELEASE` — kırpılır, en çok 64 karakter, olduğu gibi.
3. Sırasıyla `GIT_COMMIT`, `GIT_SHA`, `COMMIT_SHA`, `SOURCE_VERSION`,
   `VERCEL_GIT_COMMIT_SHA`, `RENDER_GIT_COMMIT`, `HEROKU_SLUG_COMMIT`, `CI_COMMIT_SHA`.
   7–40 hane hex ise küçük harfle ilk 12 hane; değilse kırpılmış, en çok 64 karakter.
4. Çalışma dizinindeki git checkout'u: `.git/HEAD` → dal ref dosyası, yoksa
   `packed-refs`; ayrık HEAD'de sha'nın kendisi. `.git` bir dosyaysa (submodule vb.)
   `gitdir:` izlenir. Yalnızca geçerli 40 hane sha, küçük harfle ilk 12 hane.
   `git` komutu çalıştırılmaz, dosyalar okunur; okunamazsa sessizce vazgeçilir.
   `ref:` yalnızca `refs/` altını gösterebilir, `..` içeremez. **Git worktree'leri
   (`commondir`) desteklenmez** — dal ref'i ortak dizinde durur ve bulunamaz; orada
   `VITALWATCH_RELEASE` kullanın.
5. Hiçbiri yoksa etiket gönderilmez.

Kural Node ve Laravel paketleriyle aynı. Gunicorn'un çalışma dizini uygulama kökü
değilse (`--chdir` yoksa) 4. adım bir şey bulamaz; `vitalwatch-status` aynı dizinden
çalıştırılınca bunu `kaynak: yok` olarak gösterir.

---

## Neler raporlanır

| Tür | Ne zaman |
|---|---|
| `exception` | Yakalanmamış istisna, `report()` çağrısı |
| `fatal` | `MemoryError`, `RecursionError`, `SystemError` — yorumlayıcının sınırına çarpmak |
| `http_5xx` | Yanıt kodu ≥ 500 |
| `slow_request` | Süre ≥ `VITALWATCH_SLOW_REQUEST_MS` |

Başarılı ve hızlı istekler **raporlanmaz**: her isteği göndermek izlenen uygulamaya da
hub'a da yük olurdu.

---

## Davranış garantileri

1. **Hata yutulmaz.** Handler zinciri korunur, `raise` engellenmez.
2. **Süreç yönetimine karışılmaz.** `sys.exit` çağrılmaz, mevcut `excepthook` zincirlenir.
3. **Kendi hatasını raporlamaz.** Tüm SDK kodu `try/except` ile sarılır.
4. **Kullanıcı isteğini bekletmez.** Gönderim arka plan thread'inden, kısa zaman aşımıyla.
5. **Hub erişilemezse sessizce vazgeçilir.**
6. **`VITALWATCH_ENABLED=false` iken hiçbir veri gönderilmez.**

---

## Güncelleme ve sürümleme

Sürüm **sabitlenmez**: kurulan her zaman deponun son hâlidir. Karşılığında bir kural
zorunlu hâle gelir — **her yayında sürüm numarası yükseltilmelidir.**

Sebep ölçüldü: pip, doğrudan git URL'i verilen bir paketi depoyu klonlayıp metadata'sını
okuduktan sonra sürüm numarasına bakar. Numara kuruluyla aynıysa **kurulumu atlar** ve
`-U` bunu değiştirmez:

| Komut | Sürüm aynıysa | Sürüm yükselmişse |
|---|---|---|
| `pip install -r requirements.txt` | atlar | kurar |
| `pip install -U ...` | atlar | kurar |
| `pip install --force-reinstall ...` | kurar | kurar |

Yani numara yükseltilmeden yapılan bir yayın, kurulu projelere **hiç ulaşmaz** ve bu
sessizce olur — paketin bütün tasarımının karşı durduğu arıza biçimi.

Sürüm çıkarma sırası:

1. `pyproject.toml` içindeki `version` **ve** `src/vitalwatch/reporter.py` içindeki
   `SDK_VERSION` birlikte yükseltilir. İkisi ayrışırsa panelde yanlış SDK sürümü görünür.
2. `CHANGELOG.md`'ye giriş.
3. `python -m unittest discover -s tests`
4. Commit, `git tag vX.Y.Z`, `git push origin vX.Y.Z`.
5. Kurulu projelerde `pip install -r requirements.txt`, ardından `vitalwatch-status` ile
   doğrulama. Panelde SDK sürümünün yenilendiği görülmelidir.

Acil bir düzeltme sürüm numarası yükseltmeden dağıtılacaksa tek yol
`--force-reinstall`; ama o durumda panel eski sürüm numarasını göstermeye devam eder ve
hangi sunucunun güncellendiği izlenemez.

## Test

```bash
# Çerçeve testleri için — yoksa 53 test sessizce atlanır
pip install "Flask==3.0.0" Django fastapi starlette httpx
python -m unittest discover -s tests
```

Çıktıda `skipped=` görünüyorsa bir çerçeve kurulu değildir ve o entegrasyon
**sınanmamıştır** (Flask 17, Django 12, FastAPI/Starlette 24 test).
0.1.0'daki çift kayıt ve kaybolan hata tam bu yüzden fark edilmedi.

Testler hub'ın imza doğrulamasını (`VerifyProjectSignature`) ve gövde sınırlarını
birebir uygulayan sahte bir hub'a karşı koşar: imzanın gerçekten kabul edilip
edilmediği burada görülür, "204 aldım" demek yeterli değildir.
