# SONNET REPORT — J4: Portföy Değer Geçmişi — Günlük Anlık Görüntü + Δ Satırları

**Tarih:** 2026-06-11
**Görev:** FABLE_PLAN.md § 1 — Görev J4
**Durum:** ✅ Tamamlandı

---

## 1. Yapılan Değişiklikler

### bot.py

- **`PORTFOLIO_HISTORY`** sabit yolu eklendi (`BASE_DIR / "portfolio_history.json"`).
- **`_phistory_lock`** threading.Lock() eklendi (J4 JSON IO koruması).
- **`_PORTFOLIO_HISTORY_CAP = 730`** sabit eklendi.
- **`_prune_history(h, cap)`** — PURE: en eski kayıtları kırpar; cap=730 varsayılan.
- **`_compute_delta(history, today_val, days)`** — PURE: hedef tarihe ≤ ve en fazla 5 gün eski en yakın kaydı bulur; paya-0 → None; tolerans dışı → None.
- **`load_portfolio_history()` / `save_portfolio_history(h)`** — `_phistory_lock` altında `_read_json` / `_atomic_write_json` kullanır.
- **`maybe_snapshot_portfolio_value(agg, prices)`** — tüm fiyatlar mevcutsa UTC günlük toplam değeri yazar; eksik fiyat → log.debug + atla; aynı gün üzerine yazar; prune uygular.
- **`_format_delta(abs_d, pct_d)`** — PURE: n/a veya emoji+$+% biçimleri döner.
- **`cmd_pnl()`** genişletildi: `maybe_snapshot_portfolio_value` çağrısı eklendi; tüm fiyatlar tam ise delta satırı eklenir (eksik fiyat varsa satır eklenmez). Bugünkü anahtar delta hesabından hariç tutulur (kendisiyle kıyas yok).
- **`background_thread()`**: zamanlanmış tarama sonrası fırsatçı portföy anlık görüntüsü alır.

### lang/en.json + lang/tr.json

- **`pnl_delta_line`** — EN/TR pariteli yeni i18n anahtarı. `{total}`, `{d1}`, `{d7}`, `{d30}` parametreleri.

### .gitignore / .dockerignore

- `portfolio_history.json` her ikisine eklendi.

---

## 2. Test Sonuçları

```
677 passed, 6 skipped in 2.72s
```

Yeni test dosyası: `tests/test_portfolio_history.py` — 31 test:
- `TestPruneHistory` (4): alt/eşit/üzeri cap, 730 sabit.
- `TestComputeDelta` (9): tam eşleşme, tolerans içi, sınır (5g), dışı (6g), aday yok, tek-kayıt-bugün hariç, sıfır-pay, negatif delta, en-yakın seçimi.
- `TestMaybeSnapshot` (6): YF_OK=False, boş agg, eksik fiyat, tam → yazar, aynı-gün üzerine yazar, prune uygulanır.
- `TestLoadSaveHistory` (3): eksik dosya, bozuk dosya, roundtrip.
- `TestCmdPnlDelta` (4): ilk gün (geçmiş yok), geçmiş varsa delta satırı, YF_OK=False, kısmi fiyat.
- `TestFormatDelta` (3): None→n/a, pozitif emoji, negatif emoji.
- `TestJ4I18nKeys` (2): EN + TR `pnl_delta_line` var ve anahtar adına geri düşmüyor.

---

## 3. Kabul Kriterleri Doğrulaması

1. ✅ Yeni testler — tüm spec senaryoları kapsandı.
2. ✅ `677 passed, 6 skipped` — mevcut 646 test kırılmadı.
3. ✅ `grep -n "portfolio_history.json" .gitignore .dockerignore` → her ikisinde de mevcut.
4. ✅ Commit mesajı: `feat: portfolio value history — daily snapshots + 1d/7d/30d deltas (J4)`.

---

## 4. Mimari Notlar

- Delta hesabı `today_key` dışarıda tutularak yapılır — yeni yazılan anlık görüntüyle kıyas hatasını önler.
- `_prune_history(h, cap=_PORTFOLIO_HISTORY_CAP)` şeklinde açık geçirme: `cap` varsayılan parametresi tanım anında değerlendirildiğinden, monkeypatch testlerinin çalışması için gereklidir.
- Zamanlanmış taramadaki anlık görüntü yeni fiyat GET isteği yapar (tarama zaten yfinance kullanmıyor); `time.sleep(0.5)` ticker arası oran sınırlaması için korundu.
