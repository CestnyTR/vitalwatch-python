# vitalwatch (Python)

[VitalWatch Hub](https://github.com/CestnyTR/vitalwatch-hub) için Python raporlayıcı: sunucu
hataları, yavaş istekler, canlılık. Ayrıca sunucunun kendisini izleyen
[`vitalwatch-agent`](#sunucu-ajanı-vitalwatch-agent): web sunucusu, PHP-FPM, veritabanı,
systemd, Docker, OOM ve disk.

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
pip install vitalwatch
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
- **`Güncel sürüm`** — PyPI'daki en yeni kararlı sürüm (`info.version`). Kurulu sürümden
  yeniyse bir `Güncelleme var: pip install -U vitalwatch` satırı yazılır; bu bir
  uyarıdır, çıkış kodunu değiştirmez. Ağ yoksa ya da PyPI yanıt vermezse
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

## Sunucu ajanı (`vitalwatch-agent`)

SDK uygulamanın **içinden** bakar. Uygulamanın dışında kalan arızalar — nginx'in
`502/504`'ü, PHP-FPM'in `pm.max_children` sınırı, PostgreSQL `FATAL`, düşen bir systemd
servisi, sürekli yeniden başlayan bir Docker konteyneri, OOM killer, dolan disk — ancak
sunucudan görülür. `vitalwatch-agent` sunucuda servis olarak çalışır, bunları izler ve
**hata olayı** olarak hub'ın sunucu ucuna gönderir. Dil bağımsızdır: .NET, Go ya da statik
site — sunucuda ne çalışıyorsa onun web sunucusu, veritabanı ve servis yöneticisi izlenir.

Aynı paketle gelir; bağımlılık yok. Önerilen: her sunucu için hub'da ayrı bir proje
(ör. `vps-ornek`), beyanı `agent`.

### Kurulum

```bash
sudo python3 -m venv /opt/vitalwatch
sudo /opt/vitalwatch/bin/pip install vitalwatch
sudo ln -s /opt/vitalwatch/bin/vitalwatch-agent /usr/local/bin/vitalwatch-agent

sudo install -d -m 700 /etc/vitalwatch
sudo nano /etc/vitalwatch/agent.ini          # aşağıdaki örnek
sudo chmod 600 /etc/vitalwatch/agent.ini

sudo vitalwatch-agent check                  # kaynaklar ve yapılandırma
sudo vitalwatch-agent check --test           # panele bir sınama olayı düşürür
sudo sh -c 'vitalwatch-agent systemd > /etc/systemd/system/vitalwatch-agent.service'
sudo systemctl daemon-reload && sudo systemctl enable --now vitalwatch-agent
```

Sanal ortam `/opt` altında olmalı: `ProtectHome=yes` ev dizinlerini, `PrivateTmp=yes`
`/tmp`'yi servisten gizler. `systemd` komutu çalışan kurulumun yorumlayıcı yolunu yazar;
yol ev dizinindeyse ya da `/tmp` altındaysa ilgili korumayı gevşetip yorum satırıyla
belirtir.

### `/etc/vitalwatch/agent.ini`

```ini
[hub]
url = https://vitalwatch.example.com
key = vps-anahtari
secret = <panelden alınan 64 karakter>
env = production

[sources]
# hepsi varsayılan "auto": varsa ve okunabiliyorsa açık. "on": yoksa check hata verir.
nginx = auto
apache = auto
plesk = auto
php_fpm = auto
postgresql = auto
mysql = auto
mongodb = auto
redis = auto
journal = auto
systemd = auto
docker = auto
disk = auto
journal_errors = off      ; tüm err seviyesi journald satırları (gürültülü)
docker_logs =             ; logu izlenecek konteyner adları, virgülle

[disk]
threshold = 90

[ignore]
hosts =                   ; hub'ın kendi host'u her zaman eklenir
patterns =                ; satır başına bir düzenli ifade; ham satır eşleşirse atılır

[log:plesk-panel]
path = /var/log/plesk/panel.log
pattern = \b(ERR|CRIT|ALERT|EMERG)\b
kind = log_error
```

Yol sırası: `--config` → `VITALWATCH_AGENT_CONFIG` → `/etc/vitalwatch/agent.ini`.
`VITALWATCH_URL`, `VITALWATCH_KEY`, `VITALWATCH_SECRET`, `VITALWATCH_ENV` ve
`VITALWATCH_ENABLED` ortam değişkenleri `[hub]`'ı ezer. Ajan `.env` dosyası **okumaz**:
`check` bir proje dizininde çalıştırılınca o projenin anahtarını almasın. Satır sonu
yorumu yalnızca `;` (düzenli ifadelerde `#` geçebilir). Durum dosyası
`/var/lib/vitalwatch-agent/state.json` (`VITALWATCH_AGENT_STATE_DIR` ile değişir).

### Komutlar

| Komut | Ne yapar |
|---|---|
| `vitalwatch-agent run` | Servis olarak çalışır. SIGTERM'de alt süreçleri kapatır, kuyruğu son kez gönderir, durumu kaydeder |
| `vitalwatch-agent check` | Her kaynağı `bulundu` / `bulunamadı` / `okunamıyor` / `kapalı` olarak listeler; hub yapılandırmasını denetler (secret 64 karakter, https, ortam adı) |
| `vitalwatch-agent check --test` | Ek olarak bir `log_error` sınama olayı gönderir ve HTTP durumunu olduğu gibi yazar |
| `vitalwatch-agent systemd` | Sertleştirilmiş birim dosyasını yazdırır: `root`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/vitalwatch-agent`, `NoNewPrivileges`, `ProtectHome`, `PrivateTmp`, `Restart=always` |

Servis root çalışır, çünkü Plesk vhost logları ve Docker soketi başka türlü okunamıyor.
Ajan durum dosyası dışında hiçbir yere yazmaz; yalnızca `journalctl`, `systemctl --failed`,
`docker events` ve `docker logs` komutlarını **okumak** için çalıştırır. `journalctl` ya da
`docker` yoksa veya izin yoksa stderr'e tek bir uyarı yazar ve çalışmaya devam eder.

### Ne toplanır

| Tür | Kaynak | Örnek |
|---|---|---|
| `web_server` | `/var/log/nginx/*error*.log`, `/var/log/apache2/*error*.log`, `/var/log/httpd/*error*`, Plesk `/var/www/vhosts/system/*/logs/{error_log,proxy_error_log}` | `[ornek.com] upstream timed out … while reading response header from upstream` (+ `route: GET /api/x`, `status: 504`) |
| `php_fpm` | `/var/log/php*-fpm.log`, `/var/log/php-fpm/*.log`, `/opt/plesk/php/*/var/log/php-fpm.log` | `[pool www] server reached pm.max_children setting (5), consider raising it` |
| `database` | `/var/log/postgresql/*.log`, `/var/log/mysql/error.log`, `/var/log/mariadb/*.log`, `/var/log/mysqld.log`, `/var/log/mongodb/mongod.log`, `/var/log/redis/*.log` | `FATAL: remaining connection slots are reserved …` |
| `service_failed` | journald + 5 dakikada bir `systemctl --failed` | `php8.4-fpm.service: Failed with result 'exit-code'` |
| `container_failed` | `docker events` | `api: exited with code 137`, `api: restart loop (3 in 10 min)`, `api: unhealthy`, `api: killed by OOM` |
| `oom` | journald (çekirdek, systemd-oomd) | `Out of memory: Killed process <n> (php-fpm8.4)` |
| `kernel` | journald (çekirdek) | `php-fpm8.4[<n>]: segfault in libc.so.6`, disk G/Ç hataları |
| `disk_full` | `/proc/mounts`'taki gerçek dosya sistemleri | `/var: disk 93% full`, `/: inodes 91% used` — bağlama noktası başına saatte en fazla bir |
| `log_error` | `[log:<ad>]` bölümleri, `docker_logs`, `journal_errors = on` | desen eşleşen satır |

- Her log ilk görüldüğünde **sonundan** okunur; geçmiş içe aktarılmaz. Konumlar
  (inode + ofset) ve journald imleci durum dosyasında tutulur; log döndürme ve kırpılma
  algılanır. Glob'lar dakikada bir yeniden taranır: Plesk'te yeni açılan site yeniden
  başlatma gerektirmeden izlenir.
- Gürültü elenir: 404'lerin `open() … failed`'ı, TLS el sıkışma hataları, `[warn]`,
  PHP uyarıları (yalnızca `Fatal error` / `Parse error` gelir), uygulama düzeyi SQL
  hataları (SDK'nın işi), `docker stop`'un ürettiği çıkış kodları.
- Aynı mesaj dakikada en fazla 20, toplam dakikada en fazla 300 olay. Gönderim 5 sn'de
  bir ya da 25 olayda; gövde 8 KB ve istek başına 8 olaya bölünür. Hub erişilemezse en
  fazla 1000 olay bekletilir ve üstel geri çekilmeyle yeniden denenir.
- 5 dakikada bir canlılık isteği (`{"events": [], "runtime": "agent", …}`): ajan durursa
  projenin sunucu kanalı susar ve hub'ın kurulum sağlığı bunu gösterir.
- Hub'ın kendi alan adı (`url`'in host'u) her zaman yok sayılır: hub aynı sunucudaysa
  kendi hatası ajanı, ajanın gönderimi yeni hatayı tetiklemesin.

### Bilinçli olarak toplanmayanlar

- **IP adresleri** — her satırda `[ip]` olur; nginx'in `client:` alanı tamamen silinir.
- **Query string ve fragment** — istek satırından, URL'lerden ve yollardan atılır;
  `route` yalnızca `GET /yol` taşır.
- **`referrer` / `referer` ve `host` başlıkları** — tamamen silinir. Site adı
  istemcinin yazdığı `host`'tan değil, yapılandırmadaki `server_name`'den ya da Plesk
  yolundan alınır.
- Ardından SDK'nın maskesi: e-posta, kart, telefon, TC kimlik, IBAN, uzun jetonlar;
  veritabanı mesajlarındaki tırnaklı değerler `?` olur.
- **SSH / kimlik doğrulama logları** (her satır IP taşır, IP'siz hâli anlamsız),
  **erişim logları** (`access.log` — hata değil trafik; ziyaret modülü zaten var),
  **posta logları** (alıcı adresleri) hiç okunmaz. `journal_errors = on` modunda da
  `sshd`, `sudo`, `postfix`, `dovecot` gibi kaynaklar atlanır.
- CPU/RAM/disk **grafiği**, sürekli metrik ve süreç listesi yoktur: yalnızca eşik
  aşıldığında tek olay.

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

Paket PyPI'dadır: `pip install -U vitalwatch` en yeni kararlı sürümü kurar. Sürüm
**sabitlenmezse** kurulan her zaman son yayındır; karşılığında bir kural zorunlu hâle
gelir — **her yayında sürüm numarası yükseltilmelidir.** pip, numarası kuruluyla aynı
olan paketi yeniden kurmaz; PyPI aynı numarayla ikinci yüklemeyi zaten reddeder.
Numara yükseltilmeden yapılan bir düzeltme kurulu projelere **hiç ulaşmaz** ve bu
sessizce olur — paketin bütün tasarımının karşı durduğu arıza biçimi.

Sürüm çıkarma sırası:

1. `pyproject.toml` içindeki `version` **ve** `src/vitalwatch/reporter.py` içindeki
   `SDK_VERSION` birlikte yükseltilir. İkisi ayrışırsa panelde yanlış SDK sürümü görünür.
2. `CHANGELOG.md`'ye giriş.
3. `python -m pytest -q`
4. Commit, `git tag vX.Y.Z`, `git push origin vX.Y.Z`. Etiket `publish` iş akışını
   tetikler: testler geçerse ve etiket paket sürümüyle eşleşirse PyPI'ye yüklenir.
5. Kurulu projelerde `pip install -U vitalwatch`, ardından `vitalwatch-status` ile
   doğrulama. Panelde SDK sürümünün yenilendiği görülmelidir. Sunucu ajanı için
   `/opt/vitalwatch/bin/pip install -U vitalwatch && systemctl restart vitalwatch-agent`.

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
