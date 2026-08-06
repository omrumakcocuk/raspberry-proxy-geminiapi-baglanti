# Gemini Live WebSocket Proxy

Gemini Live API ile istemci arasında mesajları değiştirmeden aktaran asenkron bir
WebSocket proxy'sidir. Her istemcinin Orbit kullanıcı token'ı, Gemini bağlantısı
açılmadan önce Orbit Manager üzerinden doğrulanır. Orbit endpoint'i güncel olarak
yalnızca `GET` kabul ettiği için doğrulama isteği Bearer header ile `GET` gönderilir.

```text
İstemci  <── WebSocket ──>  Proxy  <── WebSocket ──>  Gemini Live API
```

## Güvenlik uyarısı

Varsayılan olarak yalnızca `127.0.0.1` üzerinde dinler. İnternete açarken TLS/WSS,
rate limit ve origin kontrolü ekleyin. Tarayıcı query parametresi kullanacaksa token'ın
URL ve erişim loglarına yazılmaması ayrıca sağlanmalıdır.

Gemini API anahtarı istemciye gönderilmez. Proxy anahtarı upstream bağlantı URL'sine
ekler; anahtarlı URL ve mesaj içerikleri loglanmaz.

## Gereksinimler

- Python 3.11 veya üzeri
- Google AI Studio'dan alınmış bir Gemini API anahtarı

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

`.env` içindeki `GEMINI_API_KEY` alanını doldurun. Uygulama çalışma dizinindeki
`.env` dosyasını otomatik yükler; kabukta tanımlanmış ortam değişkenleri her zaman
dosyadaki değerlerden önceliklidir.

## Çalıştırma

```bash
gemini-live-proxy
```

Alternatif olarak:

```bash
uvicorn gemini_live_proxy.main:app --host 127.0.0.1 --port 8000
```

Kontroller:

```bash
curl http://127.0.0.1:8000/health
export ORBIT_USER_TOKEN='kullanıcının_tokenı'
python examples/text_client.py
```

Örnek istemci, Gemini'nin metin transkripsiyonunu terminale yazar ve gelen 24 kHz
PCM ses parçalarını `aplay` üzerinden gecikmeden canlı oynatır. Diske ses kaydetmez.
Linux sisteminde `aplay` komutu için `alsa-utils` paketinin kurulu olması gerekir.

Sürekli, çift yönlü mikrofon görüşmesi için proxy çalışırken ikinci terminalde:

```bash
python examples/voice_client.py
```

Bu istemci mikrofonu `arecord` ile 16 kHz mono PCM olarak okur, 40 ms'lik parçaları
proxy üzerinden Gemini'ye iletir ve 24 kHz cevabı canlı oynatır. Gemini konuşurken
yeniden konuşursanız otomatik aktivite algılama mevcut cevabı kesebilir. Hoparlör
sesinin yeniden mikrofona girmemesi için kulaklık kullanılması önerilir.

Sesli istemci Türkçe yanıtı oturumun sistem talimatıyla zorlar. Otomatik konuşma
algılama, cümle içindeki kısa duraklamaları konuşma sonu saymaması için düşük bitiş
hassasiyeti ve 1000 ms sessizlik toleransıyla yapılandırılmıştır.

Hoparlör yankısının Gemini tarafından yeni kullanıcı konuşması olarak algılanmasını
önlemek için Gemini ses üretirken mikrofonun proxy'ye aktarımı geçici olarak durur.
Cevap tamamlandığında mikrofon otomatik olarak yeniden açılır. Bu varsayılan davranış
kararlı yarı çift yönlü (half-duplex) iletişim sağlar.

`/health`, anahtar yoksa HTTP servisi ayakta olsa bile `not_configured` döndürür.

## WebSocket protokolü

İstemci `ws://127.0.0.1:8000/ws/live` adresine Orbit token'ını
`Authorization: Bearer <user_token>` başlığıyla göndererek bağlanır. Browser WebSocket
API'si özel header desteklemediği için `ws://127.0.0.1:8000/ws/live?token=<user_token>`
alternatifi de vardır; bunu üretimde yalnızca WSS ile kullanın. İlk mesaj Gemini'nin
`BidiGenerateContentSetup` mesajı olmalıdır:

```json
{
  "setup": {
    "model": "models/gemini-3.1-flash-live-preview",
    "generationConfig": {
      "responseModalities": ["AUDIO"]
    }
  },
  "outputAudioTranscription": {}
}
```

Gemini'den `setupComplete` geldikten sonra metin örneği:

```json
{
  "realtimeInput": {
    "text": "Merhaba"
  }
}
```

Proxy JSON şemasını yorumlamaz. Metin ve binary WebSocket frame'lerini iki yönde
değiştirmeden aktarır. Böylece ses, görüntü, tool call ve gelecekte eklenecek mesaj
türleri için proxy değişikliği gerekmez. Gemini Live mesajlarının medya içeriği pratikte
JSON içindeki base64 alanlarıyla gönderilir.

## Yapılandırma

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `GEMINI_API_KEY` | boş | Google AI Studio API anahtarı |
| `GEMINI_WS_URL` | resmi v1beta endpoint | Upstream WebSocket adresi |
| `GEMINI_AUTH_MODE` | `api_key_query` | Upstream kimlik doğrulama yöntemi |
| `PROXY_HOST` | `127.0.0.1` | Dinleme adresi |
| `PROXY_PORT` | `8000` | Dinleme portu |
| `ACCESS_CONTROL_ENABLED` | `true` | Orbit abonelik doğrulamasını etkinleştirir |
| `SUBSCRIPTION_VERIFY_URL` | Orbit Manager endpoint'i | Abonelik doğrulama adresi |
| `SUBSCRIPTION_VERIFY_TIMEOUT_SECONDS` | `10` | Orbit doğrulama zaman aşımı |
| `MAX_MESSAGE_BYTES` | `16777216` | Tek WebSocket mesajı üst sınırı |
| `UPSTREAM_OPEN_TIMEOUT_SECONDS` | `15` | Gemini bağlantı zaman aşımı |
| `UPSTREAM_PING_INTERVAL_SECONDS` | `20` | Ping aralığı |
| `UPSTREAM_PING_TIMEOUT_SECONDS` | `20` | Ping cevap zaman aşımı |
| `LOG_LEVEL` | `INFO` | Log seviyesi |

Desteklenen upstream kimlik doğrulama modları:

- `api_key_query`: `GEMINI_API_KEY` değerini `key` query parametresi olarak gönderir.
- `access_token_query`: `GEMINI_ACCESS_TOKEN` değerini `access_token` olarak gönderir.
- `bearer_header`: `GEMINI_ACCESS_TOKEN` değerini Bearer header ile gönderir.

Ephemeral token kullanılırsa `GEMINI_WS_URL` ayrıca
`BidiGenerateContentConstrained` endpoint'ine değiştirilmelidir.

## Testler

```bash
pytest
```

Testler Orbit yanıtlarının yorumlanmasını, kimlik bilgilerinin doğru kanalda
eklenmesini ve metin/binary mesajların değiştirilmeden aktarılmasını kontrol eder.
Gerçek Orbit ve Gemini bağlantıları otomatik testlere dahil değildir.

## Eşzamanlı bağlantı yük testi

Proxy'yi bir terminalde çalıştırın. İkinci terminalde token'ı komut geçmişine
yazmadan alın ve proxy PID'sini bularak testi başlatın:

```bash
python examples/load_test.py --clients 5 --duration 30
```

Çıktı her saniye aktif bağlantı, hata, proxy CPU ve RSS bellek değerlerini gösterir.
Varsayılan test Gemini oturumlarını açıp boşta tutar. Her client'ın bir metin isteği
de göndermesi için `--prompt "Merhaba"` eklenebilir; bu seçenek Gemini kotası ve
maliyeti tüketir. Gerçek zamanlı mikrofon taşıma yükünü taklit etmek için her client'tan
40 ms aralıklarla sessiz PCM gönderen `--audio` seçeneğini kullanın:

```bash
python examples/load_test.py --clients 5 --duration 30 --audio
```

Gerçek Gemini oturumları açıldığı için önce 2, sonra 5, 10 ve 20 client ile kademeli
test önerilir.

Bir kez mikrofona konuşup aynı kaydı 20 ayrı asistana göndererek tüm cevapları almak
için proxy çalışırken:

```bash
python examples/multi_voice_test.py --clients 20
```

Yerel ses algılama konuşmaya başladığınızı ve varsayılan 900 ms sessizlikten sonra
cümleyi bitirdiğinizi belirler; sabit kayıt süresi yoktur. Kayıt bitince aynı ses
bütün oturumlara varsayılan 4x hızla paralel gönderilir. Her asistanın ilk cevap
ses paketi geldiğinde ayrı oynatıcı başlar; cevaplar aynı anda duyulabilir ve proxy
çift yönlü yük altında ölçülür. Yalnızca transkripsiyon ve CPU/RAM ölçümü için
`--no-play` ekleyin. Bu test 20 gerçek Gemini Live oturumu açar ve kota tüketir.

## Sonraki üretim adımları

Yerel bağlantı doğrulandıktan sonra aşağıdakiler eklenmelidir:

1. Bağlantı başına rate limit ve eşzamanlı oturum sınırı.
2. TLS sağlayan bir reverse proxy ve origin kontrolü.
3. Ölçümleme, merkezi loglama ve upstream hata metrikleri.
4. Gemini `goAway` ve session resumption mesajlarını kullanan istemci yeniden bağlanması.
