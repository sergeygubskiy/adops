"""HTTP-клиент трекера с REST API — только stdlib (`urllib`).

Адрес и ключ в коде НЕ хранятся: берутся из конфиг-файла `~/.adops/tracker.json`
(права 600) или из переменных окружения `TRACKER_BASE_URL` / `TRACKER_API_KEY`.

Пути методов вынесены в `Endpoints` и переопределяются под конкретный трекер;
значения по умолчанию иллюстративны.

Что здесь важно (и покрыто тестами на моках, без сети):
  - ретраи только транзиентных кодов (408/425/429/5xx) с экспоненциальной паузой и Retry-After;
  - 401/403 — НЕ ретраится, это отдельное исключение: дальше запросы бессмысленны;
  - не-2xx вроде 400 возвращаются вызывающему как есть (он решает, что делать);
  - ключ маскируется во всём, что попадает в сообщения об ошибках и логи.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".adops" / "tracker.json"

_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
_AUTH_FAIL = {401, 403}
_MAX_RETRIES = 4
_BASE_BACKOFF = 1.5

_SECRET_RE = re.compile(r"(?i)(key\s*[:=]?\s*)([A-Za-z0-9._\-]{6,})")


def mask(msg) -> str:
    """Убрать значение ключа из текста перед логированием."""
    return _SECRET_RE.sub(r"\1***", str(msg))


class TrackerAuthError(RuntimeError):
    """401/403 — ключ битый или отозван. Дальше запросы бессмысленны."""


class TrackerError(RuntimeError):
    """Сетевая/протокольная ошибка после ретраев."""


@dataclass(frozen=True)
class Endpoints:
    """Пути методов API относительно `base_url` (иллюстративные значения по умолчанию)."""
    api_prefix: str = "/api/v1"
    campaigns: str = "/campaigns"
    report: str = "/reports/campaign"
    update_cost: str = "/campaigns/{id}/costs"


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Прочитать конфиг → {base_url, api_key}. Нет файла/ключа → RuntimeError с подсказкой."""
    if not path.exists():
        raise RuntimeError(
            f"Нет файла настроек трекера: {path}. Создай его: "
            '{"base_url": "https://<хост трекера>", "api_key": "<ключ>"} (права 600).'
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Не читается {path}: {e}") from None
    if not cfg.get("api_key") or not cfg.get("base_url"):
        raise RuntimeError(f"В {path} должны быть заполнены base_url и api_key.")
    return cfg


class TrackerClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 endpoints: Endpoints = Endpoints(), timeout: float = 60.0, delay: float = 0.15,
                 auth_header: str = "X-Api-Key"):
        self.base_url = (base_url or os.getenv("TRACKER_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("TRACKER_API_KEY")
        if not self.base_url:
            raise RuntimeError("Не задан адрес трекера (TRACKER_BASE_URL или файл настроек).")
        if not self.api_key:
            raise RuntimeError("Не задан ключ трекера (TRACKER_API_KEY или файл настроек).")
        self.endpoints = endpoints
        self.timeout = timeout
        self.delay = delay
        self.auth_header = auth_header

    @classmethod
    def from_config(cls, path: Path = CONFIG_PATH, **kw) -> "TrackerClient":
        cfg = load_config(path)
        return cls(base_url=cfg.get("base_url"), api_key=cfg.get("api_key"), **kw)

    # ── низкий уровень ───────────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, params=None, body=None):
        """→ (status:int, data). Ретрай транзиентов (429/5xx/сеть). 401/403 → TrackerAuthError.
        Сеть исчерпана → TrackerError. Не-2xx (напр. 400) ВОЗВРАЩАЕТСЯ вызывающему."""
        url = f"{self.base_url}{self.endpoints.api_prefix}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {self.auth_header: self.api_key, "Accept": "application/json",
                   "User-Agent": "adops-client/0.1"}
        if data is not None:
            headers["Content-Type"] = "application/json"

        last_exc = None
        for attempt in range(_MAX_RETRIES):
            if self.delay:
                time.sleep(self.delay)
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
                    return r.status, (json.loads(raw) if raw.strip() else {})
            except urllib.error.HTTPError as e:
                status = e.code
                raw = ""
                try:
                    raw = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if status in _AUTH_FAIL:
                    raise TrackerAuthError(f"{method} {path} → {status}: ключ невалиден/нет доступа.") from None
                if status in _RETRYABLE and attempt < _MAX_RETRIES - 1:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    time.sleep(float(ra) if (ra and str(ra).isdigit()) else _BASE_BACKOFF * (2 ** attempt))
                    continue
                try:
                    return status, (json.loads(raw) if raw.strip() else {})
                except ValueError:
                    return status, {"_raw": mask(raw[:400])}
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_exc = e
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_BASE_BACKOFF * (2 ** attempt))
                    continue
                raise TrackerError(f"{method} {path} — сеть недоступна: {type(e).__name__}") from None
        if last_exc:
            raise TrackerError(f"{method} {path} — {type(last_exc).__name__}") from None
        raise TrackerError(f"{method} {path} — неизвестная ошибка")

    # ── чтение ───────────────────────────────────────────────────────────────────────────
    def list_campaigns(self) -> list[dict]:
        """Список кампаний (без итоговой строки и удалённых)."""
        status, data = self._request("GET", self.endpoints.campaigns, params={"limit": 1000})
        if status != 200 or not isinstance(data, list):
            raise TrackerError(f"list_campaigns → {status}: {mask(str(data)[:200])}")
        out = []
        for c in data:
            if c.get("id") == "totals" or c.get("is_deleted"):
                continue
            if isinstance(c.get("id"), int) and c["id"] > 0:
                out.append(c)
        return out

    def tag_report(self, campaign_id: int) -> list[tuple]:
        """Отчёт по меткам одной кампании → [(tag, clicks:int, cost:float), ...] (без итоговой строки)."""
        status, data = self._request("GET", self.endpoints.report,
                                     params={"ids[]": campaign_id, "limit": 5000})
        if status != 200 or not isinstance(data, dict):
            raise TrackerError(f"tag_report({campaign_id}) → {status}")
        out = []
        for r in data.get("report", []):
            if str(r.get("level")) != "1":
                continue
            name = (r.get("name") or "").strip()
            if not name:
                continue                      # итог по кампании (name='') — пропуск
            out.append((name, _num(r.get("clicks")), _num(r.get("cost"))))
        return out

    # ── запись ───────────────────────────────────────────────────────────────────────────
    def update_cost(self, campaign_id: int, body: dict) -> tuple[bool, int, str]:
        """Записать расход. body — из cost.build_cost_body. → (ok, http, msg).
        ok=True только при 200; иное → ok=False + человекочитаемое сообщение (auth — исключение)."""
        path = self.endpoints.update_cost.format(id=campaign_id)
        status, data = self._request("PUT", path, body=body)
        if status == 200:
            return True, 200, "ok"
        return False, status, _err_message(data)


def _num(s):
    """Строку отчёта → число (int если целое). None/'' → 0."""
    if s is None or s == "":
        return 0
    try:
        v = float(s)
    except (ValueError, TypeError):
        return 0
    return int(v) if v.is_integer() else v


def _err_message(data) -> str:
    """Достать человекочитаемое сообщение из тела ошибки."""
    if isinstance(data, dict):
        errs = data.get("errors")
        if isinstance(errs, dict):
            msg = errs.get("message") or errs.get("detail")
            if isinstance(msg, dict):
                parts = [f"{k}: {'; '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in msg.items()]
                return mask("; ".join(parts))[:300]
            if msg:
                return mask(str(msg))[:300]
        for k in ("message", "detail", "_raw"):
            if data.get(k):
                return mask(str(data[k]))[:300]
    return mask(str(data))[:300]
