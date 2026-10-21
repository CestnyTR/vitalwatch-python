"""Hub'a HMAC imzalı POST.

Davranış garantisi: **hiçbir koşulda hata fırlatmaz.** Hub erişilemezse,
yapılandırma eksikse veya ağ koparsa sessizce vazgeçilir — izlenen uygulamada
hata, log kirliliği veya yavaşlama oluşmaz.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

#: Hub gövde sınırı (vitalwatch-hub, IngestController::MAX_BODY_BYTES).
#: Aşan istek **okunmadan atılır** ve yine 204 döner — yani sessizce kaybolur.
MAX_BODY_BYTES = 8192


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Yönlendirme takip edilmez: imzalı gövdeyi bilinmeyen bir adrese
    göndermek istemeyiz. ``None`` dönmek yönlendirmeyi iptal eder."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: Zaman aşımı sınırları (saniye); kardeş paketlerle aynı.
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 10.0


class HubClient:
    def __init__(self, url=None, key=None, secret=None, timeout=2.0):
        self.url = url or None
        self.key = key or None
        self.secret = secret or None
        self.timeout = timeout_seconds(timeout)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def configured(self):
        return bool(self.url and self.key and self.secret)

    def send(self, payload):
        """Sonuç döndürür ama **asla hata fırlatmaz.**

        Sonucu yalnızca teşhis komutu okuyor; izleme yolu görmezden geliyor.
        Gönderim başarısızsa yapılacak bir şey yok, izlenen uygulamayı bundan
        haberdar etmek log kirliliğinden başka işe yaramaz.

        Yine de sonucu üretmek zorunlu: kardeş pakette ``--test`` "gönderildi"
        derken gerçekte hiçbir şey gitmemiş olabiliyordu ve kuran kişi
        kurulumu çalışır sanıyordu.

        :rtype: dict — ``{"sent": bool, "status": int|None, "error": str|None}``
        """
        if not self.configured():
            return {"sent": False, "status": None, "error": "missing-config"}

        try:
            body = self._encode(payload)
        except (TypeError, ValueError) as error:
            return {"sent": False, "status": None, "error": f"govde-kodlanamadi: {error}"}

        if body is None:
            return {"sent": False, "status": None, "error": "body-too-large"}

        timestamp = str(int(time.time()))

        # Zaman damgası imzaya dahildir; olmasaydı saldırgan damgayı değiştirip
        # eski bir gövdeyi yeniden oynatabilirdi.
        signature = hmac.new(
            self.secret.encode("utf-8"),
            f"{timestamp}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()

        request = urllib.request.Request(
            f"{self.url.rstrip('/')}/api/i/{urllib.parse.quote(self.key, safe='')}/server",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-VitalWatch-Signature": f"sha256={signature}",
                "X-VitalWatch-Timestamp": timestamp,
            },
        )

        try:
            # Zaman aşımı kısa ve zorunlu: raporlama isteği hiçbir koşulda
            # kullanıcının isteğini bekletmemeli.
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            # 4xx/5xx ve (yönlendirme kapalı olduğu için) 3xx buraya düşer.
            status = error.code
        except Exception as error:  # ağ, DNS, TLS, zaman aşımı
            # Sessizce vazgeç. İzleme paketinin izlediği uygulamayı bozması,
            # çözdüğü sorundan büyük bir sorundur.
            return {"sent": False, "status": None, "error": str(error) or error.__class__.__name__}

        # Hub başarıda da geçersiz istekte de 204 döner (saldırgana geri
        # bildirim verilmez). Yani 204 "kabul edildi" DEĞİL, yalnızca "istek
        # ulaştı" demek. Teşhis komutu bunu açıkça yazıyor.
        return {"sent": status < 400, "status": status, "error": None}

    @staticmethod
    def _encode(payload):
        """JSON'a çevir ve 8 KB sınırına sığdır.

        Sınır hub tarafında sessiz bir eleme: gövde büyükse istek hiç okunmaz
        ve yine 204 döner. Bu yüzden burada küçültülür — önce stack, sonra
        mesaj kırpılır; olayı hiç göndermemektense stack'siz göndermek yeğdir.
        """
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        if len(body) <= MAX_BODY_BYTES:
            return body

        if payload.get("stack"):
            payload = {key: value for key, value in payload.items() if key != "stack"}
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        if len(body) <= MAX_BODY_BYTES:
            return body

        if isinstance(payload.get("msg"), str):
            payload = dict(payload, msg=payload["msg"][:200])
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        return body if len(body) <= MAX_BODY_BYTES else None


def timeout_seconds(value):
    """``VITALWATCH_TIMEOUT`` saniye; 100 ve üstü milisaniye sayılır.

    Node paketi aynı adla milisaniye bekliyor. ``2000`` yazan birinin süreci
    hub'a 2000 saniye bağlaması, izleme paketinin uygulamayı kilitlemesi
    demekti. Kardeş paketlerle ortak kural:

    - geçersiz, sıfır/eksi, NaN ya da sonsuz → 2 sn;
    - sonuç [0.1, 10] saniyeye sıkıştırılır: 50 yazan 50 sn beklemesin,
      0.01 yazan her gönderimi zaman aşımına düşürmesin.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 2.0

    if number != number or number <= 0 or number == float("inf"):
        return 2.0

    seconds = number / 1000 if number >= 100 else number

    return min(max(seconds, MIN_TIMEOUT), MAX_TIMEOUT)
