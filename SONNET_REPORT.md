# SONNET REPORT — L1: Twelve Data İkinci Grounding Sağlayıcı + Kaynak Zinciri

**Tarih:** 2026-06-12
**Görev:** FABLE_PLAN.md § 1 — Görev L1
**Durum:** ✅ Tamamlandı

---

## 1. Değişiklik Özeti

### `bot.py`

| Alan | Değişiklik |
|---|---|
| Lock'lar (~80. satır) | `_twelve_memo_lock = threading.Lock()` eklendi |
| `_CFG_DEFAULTS` | `"twelve_auth_warned_date": ""`, `"facts_source": "auto"` (edgar→auto dahil edildi) |
| Sabitler bölümü | `# ─── Grounding data providers (J5 + L1) ───` → `_FISCAL_AI_PROVIDER`, `_TWELVE_DATA_PROVIDER`, `_DATA_PROVIDERS` tuple |
| Yeni bölüm | `# ─── Twelve Data optional facts source (L1) ───` — `_twelve_memo`, `_TWELVE_INCOME_MAP`, `_TWELVE_BALANCE_MAP`, `_parse_twelve_response`, `_check_twelve_auth_reminder`, `fetch_twelvedata_facts` |
| Yeni bölüm | `# ─── Data source chain (L1) ───` — `_data_source_chain()`, `_data_source_label()` |
| F2 gate | `_fiscal_enabled()` tek-sağlayıcı → `for _dp in _data_source_chain()` döngüsü |
| `cmd_setsource` | `_VALID` artık `_DATA_PROVIDERS` üzerinden türetildi; `setsource_no_key` parametrize |
| `cmd_setapi` | `_DATA_PROVIDERS` döngüsüyle `setapi_data_provider_rejected` |
| `cmd_addapi` | Her iki sağlayıcı için memo temizliği; `_DATA_PROVIDERS` koruması |
| `cmd_delapi` | `all_valid` twelvedata içeriyor; memo temizliği |
| `cmd_apis` | `_DATA_PROVIDERS` döngüsü |
| `cmd_settings` | `_data_source_label()` ile dinamik etiket |

### `lang/en.json` + `lang/tr.json`

| Anahtar | Değişiklik |
|---|---|
| `setsource_current` | `{valid}` artık `twelvedata` içeriyor |
| `setsource_no_key` | `{provider}` placeholder eklendi (statik fiscalai'den genelleştirildi) |
| `setapi_data_provider_rejected` | Yeni — `setapi_fiscalai_rejected` yerini aldı, `{provider}` parametreli |
| `twelvedata_auth_error` | Yeni — plan sınırı uyarısı |
| `help_block` | `/setsource auto|fiscalai|twelvedata|edgar` güncellendi |

### `tests/test_k4_setsource.py` (2 düzeltme)

| Test | Düzeltme |
|---|---|
| `test_invalid_arg_rejected` | `valid="auto | fiscalai | edgar"` → `valid="auto | fiscalai | twelvedata | edgar"` |
| `test_set_fiscalai_no_key_warns` | `bot.t("setsource_no_key")` → `bot.t("setsource_no_key", provider="fiscalai")` |

### `tests/test_l1_twelvedata.py` (yeni, 52 test)

- `_data_source_chain` matrisi: 4 mod × anahtar kombinasyonları (12 test)
- `_parse_twelve_response` fikstürleri: tam eşleşme, dönem uyumsuzluğu, str sayılar, eşik altı, hata gövdesi, boş dönem, None girişler, tuple format, sayısal olmayan atlanma, `date` alanı fallback (10 test)
- Zincir sıralı deneme + fallback (3 test)
- `/setsource twelvedata`: key ile, key olmadan uyarı, persistence, geçerli seçeneklerde görünüm (4 test)
- `_data_source_label()` 7 durum (7 test)
- `/addapi twelvedata`: key depolama, default_provider koruması, memo temizliği, fiscal memo korunması (4 test)
- `/setapi twelvedata` reddi + fiscalai regresyon (2 test)
- i18n parite: yeni anahtarlar EN+TR, placeholder kontrolleri, key eşitliği, help_block (7 test)
- Fiscal AI regresyon guard (3 test)
- `--network` opt-in smoke (2 test, varsayılan skip)

---

## 2. Kabul Kriterleri Doğrulaması

1. ✅ `_data_source_chain()` 4 mod × anahtar matrisi — `TestDataSourceChain` 12 test yeşil
2. ✅ Twelve Data bölümü `_parse_twelve_response`, `fetch_twelvedata_facts`, memo, auth reminder içeriyor
3. ✅ F2 gate `_data_source_chain()` döngüsü kullanıyor; EDGAR fallback `fiscal_facts is None` branch'inde mevcut
4. ✅ `cmd_setsource` `twelvedata` kabul ediyor; `setsource_no_key` `{provider}` parametreli
5. ✅ `cmd_setapi twelvedata` → `setapi_data_provider_rejected` döndürüyor
6. ✅ `cmd_addapi twelvedata` → key saklanıyor, `_twelve_memo` temizleniyor, default_provider'a set edilmiyor
7. ✅ `apikey=<key>` query param kullanılıyor (Twelve Data header auth desteklemiyor — explicit exception)
8. ✅ i18n: 4 yeni/güncellenen anahtar EN+TR parite; key eşitliği testi geçiyor
9. ✅ `python -m pytest tests/ -q` → `874 passed, 9 skipped` (test_k4 ve test_l1 dahil)
10. ✅ 2 commit temiz: `dbb99dd` (testler), `7fefbee` (bot.py + i18n). `git status` temiz.

---

**L1 TAMAMLANDI.**
