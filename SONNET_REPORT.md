# SONNET REPORT — J3: No-AI Mode — LLM'siz Zarif Bozulma

**Tarih:** 2026-06-11
**Görev:** FABLE_PLAN.md § 1 — Görev J3
**Durum:** ✅ Tamamlandı

---

## 1. Yapılan Değişiklikler

| Dosya | Değişiklik |
|---|---|
| `bot.py` — `_CFG_DEFAULTS` | `"no_keys_warned_date": ""` eklendi |
| `bot.py` — LLM bölümü | `_RAW_TEXT_INLINE_LIMIT = 3500`, `_RAW_TEXT_FILE_MAX = 200_000` sabitleri |
| `bot.py` — LLM bölümü | `ai_enabled()` — saf bool, `_ordered_providers()` delegasyonu |
| `bot.py` — LLM bölümü | `_deliver_raw_text()` — ≤3500 inline / üstü sendDocument / document fail → inline fallback |
| `bot.py` — LLM bölümü | `_check_no_keys_reminder()` — günlük spam çiti, cfg'ye tarih yazar |
| `bot.py` — `llm()` | Başına `if not ai_enabled(): return None` eklendi (sıfır HTTP kısa devre) |
| `bot.py` — `diff_analysis()` | Başına `if not ai_enabled(): return ""` eklendi |
| `bot.py` — `analyze_filing()` | `not ai_enabled()` ise `return None, None` |
| `bot.py` — `scan_ticker()` | `analysis is None` yakalanır → `_check_no_keys_reminder()` + `_deliver_raw_text()` + cache + continue |
| `bot.py` — `compare_tickers` | `summary is None` ise ham metin her iki taraf için teslim |
| `bot.py` — `cmd_sentiment()` | `_no_keys` snapshot; ticker döngüsü + portföy sentezi → `no_ai_signal_placeholder` |
| `lang/en.json` | 4 yeni anahtar |
| `lang/tr.json` | 4 yeni anahtar (EN paritesi) |
| `tests/test_no_ai_mode.py` | **Yeni dosya** — 34 test |

---

## 2. llm() Çağrı Yeri Haritası

| # | Konum | Fonksiyon | Kaynak Metin | NO_KEYS Davranışı | ALL_FAILED Davranışı |
|---|---|---|---|---|---|
| 1 | `analyze_filing()` | Dosyalama analizi | `body` (extract_section) | `(None, None)` → scan_ticker ham metin teslimi | `t("analysis_unavailable")` (J2 mevcut) |
| 2 | `diff_analysis()` | Risk faktörü farkı | `yeni_risk` (risk bölümü) | `""` — diff sessizce atlanır | `""` — diff sessizce atlanır |
| 3 | `compare_tickers` | Şirket karşılaştırması | `text_a`, `text_b` (extract_section) | ham metin her iki taraf için teslim | `t("analysis_unavailable")` karşılaştırma mesajına gömülür |
| 4 | `cmd_sentiment()` ticker döngüsü | Form 4 sinyal özeti | `texts[:6000]` | `no_ai_signal_placeholder` | `no_ai_signal_placeholder` (llm None → atanır) |
| 5 | `cmd_sentiment()` portföy sentezi | Portföy değerlendirmesi | Sentez (kaynak metin YOK) | `no_ai_signal_placeholder` — sentez atlanır | `no_ai_signal_placeholder` (llm None → atanır) |

---

## 3. Yeni Testler (test_no_ai_mode.py — sınıf başına)

| Sınıf | Test Sayısı |
|---|---|
| `TestAiEnabled` | 4 |
| `TestLlmNoKeys` | 3 |
| `TestDeliverRawText` | 8 |
| `TestNoKeysReminder` | 4 |
| `TestAnalyzeFilingNoAi` | 3 |
| `TestDiffAnalysisNoAi` | 2 |
| `TestSentimentNoAi` | 2 |
| `TestJ3I18nKeys` | 8 (4 anahtar × 2 dil, parametrize) |

---

## 4. Kabul Kriterleri Sonuçları

| Kriter | Durum |
|---|---|
| `python -m pytest tests/ -q` → `646 passed, 6 skipped` | ✅ |
| Mevcut 612 test kırılmadı | ✅ |
| `NO_KEYS`'te sıfır HTTP çağrısı (mock kanıtı) | ✅ `TestLlmNoKeys::test_zero_http_calls_on_no_keys` |
| Kırpma sınırı (3.500↔dosya geçişi) | ✅ `test_exactly_at_limit_goes_inline` + `test_long_body_sends_document` |
| 200 KB tavanı + `[truncated]` | ✅ `test_200kb_cap_truncated` |
| Boş metin yolu | ✅ `test_empty_body_sends_only_warning` |
| sendDocument başarısızlık yedeği | ✅ `test_document_failure_fallback_inline` |
| Sentez noktalarının atlanması | ✅ `test_no_ai_signal_placeholder_used` |
| Günlük hatırlatma çiti | ✅ `test_no_repeat_same_day` |
| i18n EN/TR parite (4 yeni anahtar) | ✅ `TestJ3I18nKeys` |
| `llm(` çağrı yeri haritası SONNET_REPORT'ta | ✅ Bölüm 2 |
| Commit: `feat: No-AI mode — graceful degradation without LLM (J3)` | ✅ `[main fffca4c]` |
| `git status` temiz | ✅ |
| SONNET_REPORT.md güncellendi | ✅ Bu dosya |

---

## 5. Mimari Notlar

**`ai_enabled()` delegasyonu:** `_ordered_providers()` zaten env var + cfg fallback hiyerarşisini biliyor. `ai_enabled()` bunu çağırır — mantık tekrarı yok.

**`llm()` dönüş tipi:** `str | None`. Mevcut `t("analysis_unavailable")` (ALL_FAILED) string olarak kalmaya devam eder — geriye uyumlu. Yalnız `None` yeni davranış (NO_KEYS).

**Ham metin parse_mode'suz:** Filing metinleri Markdown özel karakterleri içerebilir. `_tg_to()` zaten 400 durumunda plain text'e düşüyor — ek güvence.

**`sendDocument` yedek mantığı:** Doğrudan `requests.post` çağrısı yapılır (tek deneme); exception veya `not r.ok` → `_tg_to()` ile ilk 3500 karakter inline.

**`scan_ticker()` cache davranışı:** NO_KEYS'te analiz yapılamasa da dosyalama cache'lenir — anahtar eklendikten sonra aynı dosyalama tekrar işlenmez.

**J3 dalgası kapandı.**
