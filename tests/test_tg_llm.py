"""
H3 — Karakterizasyon testleri: tg() ve llm() bespoke davranışları.

Bu testler mevcut davranışı DEĞİŞTİRMEZ; gelecekteki refactor'ların bu
davranışı kazara kırmaması için kilide alır.
"""
import pytest


# ─── Yardımcılar ──────────────────────────────────────────
class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.ok = (200 <= status < 300)
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise Exception(f"HTTP {self.status_code}")


def _llm_success_resp(content="Analysis done."):
    return FakeResp(200, {"choices": [{"message": {"content": content}}]})


# ─── tg() karakterizasyon testleri ────────────────────────
class TestTgBehavior:
    def test_200_sends_successfully(self, bot, monkeypatch):
        """200 → mesaj gönderilir, exception yok."""
        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: FakeResp(200))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot.tg("hello")   # must not raise

    def test_400_retries_as_plain_text(self, bot, monkeypatch):
        """400 → parse_mode kaldırılarak düz metin olarak yeniden denenir."""
        calls = []
        def fake_post(url, json=None, **k):
            calls.append(json or {})
            if len(calls) == 1:
                return FakeResp(400)    # ilk deneme: Markdown hatası
            return FakeResp(200)        # ikinci deneme: plain text başarılı
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot.tg("*bold*")
        assert len(calls) == 2
        assert "parse_mode" in calls[0]        # ilk çağrı: Markdown ile
        assert "parse_mode" not in calls[1]    # fallback: parse_mode olmadan

    def test_400_fallback_no_raise_even_if_plain_fails(self, bot, monkeypatch):
        """400 → fallback da başarısız olursa dahi exception fırlatılmaz."""
        def fake_post(*a, **k):
            return FakeResp(400)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot.tg("text")    # must not raise

    def test_429_reads_retry_after_header(self, bot, monkeypatch):
        """429 → Retry-After header'ı okunur, o kadar sleep çağrılır."""
        slept = []
        call_count = {"n": 0}
        def fake_post(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeResp(429, headers={"Retry-After": "7"})
            return FakeResp(200)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda s, **k: slept.append(s))
        bot.tg("hi")
        assert 7 in slept    # Retry-After değeri uyuldu

    def test_429_default_backoff_without_header(self, bot, monkeypatch):
        """429 Retry-After header yoksa varsayılan 5*(attempt+1) uygulanır."""
        slept = []
        call_count = {"n": 0}
        def fake_post(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeResp(429, headers={})   # header yok
            return FakeResp(200)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda s: slept.append(s))
        bot.tg("hi")
        assert slept[0] == 5   # attempt=0 → 5*(0+1)=5

    def test_network_error_increments_tg_errors(self, bot, monkeypatch):
        """Ağ hatası → her denemede status_inc("tg_errors") çağrılır."""
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        before = bot._status.get("tg_errors", 0)
        bot.tg("test")
        after = bot._status.get("tg_errors", 0)
        assert after > before    # en az bir hata sayıldı

    def test_total_failure_writes_to_stderr(self, bot, monkeypatch, capsys):
        """4 denemede de başarısız → CRITICAL mesajı stderr'e yazılır."""
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot.tg("lost message")
        assert "CRITICAL" in capsys.readouterr().err


# ─── llm() karakterizasyon testleri ───────────────────────
class TestLlmBehavior:
    def test_200_returns_content(self, bot, monkeypatch):
        """200 → choices[0].message.content döndürülür."""
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: _llm_success_resp("Great analysis."))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        result = bot.llm("prompt", "model")
        assert result == "Great analysis."

    def test_429_uses_linear_backoff_not_exponential(self, bot, monkeypatch):
        """429 → min(180, 60*(attempt+1)) — lineer backoff, _backoff() değil."""
        slept = []
        call_count = {"n": 0}
        def fake_post(*a, **k):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return FakeResp(429)
            return _llm_success_resp()
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda s: slept.append(s))
        bot.llm("p", "m")
        # attempt=0 → 60*(0+1)=60; attempt=1 → 60*(1+1)=120
        assert slept[:2] == [60, 120]

    def test_5xx_uses_backoff(self, bot, monkeypatch):
        """500/502/503/504 → _backoff(attempt, 10, 60) sleep."""
        slept = []
        call_count = {"n": 0}
        def fake_post(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeResp(503)
            return _llm_success_resp()
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda s: slept.append(s))
        bot.llm("p", "m")
        # attempt=0 → _backoff(0, 10, 60) = 10
        assert slept[0] == 10

    def test_timeout_uses_linear_backoff_no_status_inc(self, bot, monkeypatch):
        """Timeout → lineer sleep, status_inc("or_errors") ÇAĞRILMAZ."""
        import requests as req
        slept = []
        call_count = {"n": 0}
        def fake_post(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise req.exceptions.Timeout("timed out")
            return _llm_success_resp()
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda s: slept.append(s))
        before = bot._status.get("or_errors", 0)
        bot.llm("p", "m")
        after = bot._status.get("or_errors", 0)
        # attempt=0 → min(60, 15*(0+1)) = 15
        assert slept[0] == 15
        assert after == before    # timeout or_errors'ı artırmaz

    def test_generic_error_then_success_no_exception(self, bot, monkeypatch):
        """Generic exception sonra başarı → exception fırlatılmaz, içerik döner."""
        call_count = {"n": 0}
        def fake_post(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("unexpected")
            return _llm_success_resp("recovered")
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        result = bot.llm("p", "m")
        assert result == "recovered"

    def test_all_attempts_exhausted_returns_unavailable(self, bot, monkeypatch):
        """4 denemede de başarısız → analysis_unavailable i18n metni döner."""
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        result = bot.llm("p", "m")
        assert result == bot.t("analysis_unavailable")

    def test_all_errors_or_errors_counter_positive(self, bot, monkeypatch):
        """Tüm denemeler generic exception → or_errors > 0 (success'te reset olmaz)."""
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot.llm("p", "m")
        assert bot._status.get("or_errors", 0) > 0

    def test_4xx_non_429_caught_gracefully(self, bot, monkeypatch):
        """401/403 gibi 4xx → raise_for_status → except Exception yakalanır, fallback döner."""
        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: FakeResp(401))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        result = bot.llm("p", "m")
        assert result == bot.t("analysis_unavailable")
