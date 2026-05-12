from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from typing import Any

import requests
from requests import Response
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import BridgeSettings


class FireflyAPIError(RuntimeError):
    """Raised when Firefly III returns an error or malformed response."""


@dataclass(slots=True)
class FireflyClient:
    settings: BridgeSettings
    logger: logging.Logger
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.session is not None:
            return

        retry = Retry(
            total=self.settings.request_retries,
            connect=self.settings.request_retries,
            read=self.settings.request_retries,
            status=self.settings.request_retries,
            backoff_factor=self.settings.retry_backoff_seconds,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session = requests.Session()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self.session = session

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.api_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.access_token}",
        }
        if self.settings.force_connection_close:
            headers["Connection"] = "close"

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.settings.timeout_seconds,
                verify=self.settings.verify_tls,
            )
        except RequestException as exc:
            hint = ""
            if "RemoteDisconnected" in repr(exc) or "Remote end closed connection without response" in str(exc):
                hint = (
                    " The Firefly server or reverse proxy closed the HTTP connection unexpectedly. "
                    "Check FIREFLY_BASE_URL, reverse-proxy timeouts, and whether the remote instance accepts "
                    "requests from this container."
                )
            raise FireflyAPIError(f"Request to Firefly III failed for {method.upper()} {url}: {exc}.{hint}") from exc

        return self._parse_response(response)

    def _request_candidates(
        self,
        method: str,
        paths: list[str],
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        for path in paths:
            try:
                return self._request(method, path, params=params, json_body=json_body)
            except FireflyAPIError as exc:
                message = str(exc)
                errors.append(f"{path}: {message}")
                if "HTTP 404" not in message:
                    raise
        raise FireflyAPIError("All candidate Firefly III endpoints failed: " + " | ".join(errors))

    def _parse_response(self, response: Response) -> dict[str, Any]:
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"message": response.text.strip()}
            raise FireflyAPIError(f"Firefly III returned HTTP {response.status_code}: {payload}")

        if response.status_code == 204 or not response.text.strip():
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise FireflyAPIError("Firefly III returned non-JSON data.") from exc

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        page = 1
        collected: list[dict[str, Any]] = []

        while True:
            current_params = dict(params or {})
            current_params.setdefault("page", page)
            current_params.setdefault("limit", 50)
            payload = self._request("GET", path, params=current_params)
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise FireflyAPIError(f"Unexpected paginated payload for {path}: {payload}")
            collected.extend(data)

            pagination = payload.get("meta", {}).get("pagination", {})
            total_pages = int(pagination.get("total_pages", page))
            if page >= total_pages:
                break
            page += 1

        return collected

    def export_collection(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return every page from a Firefly III collection endpoint."""
        return self._paginate(path, params=params)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "about")

    def list_accounts(self, account_type: str | None = None) -> list[dict[str, Any]]:
        params = {"type": account_type} if account_type and account_type != "all" else None
        return self._paginate("accounts", params=params)

    def list_categories(self) -> list[dict[str, Any]]:
        return self._paginate("categories")

    def list_budgets(self) -> list[dict[str, Any]]:
        return self._paginate("budgets")

    def create_category(self, name: str) -> dict[str, Any]:
        return self._request("POST", "categories", json_body={"name": name})

    def delete_category(self, category_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"categories/{category_id}")

    def create_budget(self, name: str) -> dict[str, Any]:
        return self._request("POST", "budgets", json_body={"name": name})

    def delete_budget(self, budget_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"budgets/{budget_id}")

    def create_account(
        self,
        *,
        name: str,
        account_type: str,
        opening_balance: str | None = None,
        opening_balance_date: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "type": account_type,
        }
        if opening_balance:
            payload["opening_balance"] = opening_balance
        if opening_balance_date:
            payload["opening_balance_date"] = opening_balance_date
        return self._request("POST", "accounts", json_body=payload)

    def delete_account(self, account_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"accounts/{account_id}")

    def list_budget_limits(self, budget_id: str, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        params["budget_id"] = budget_id
        payload = self._request_candidates(
            "GET",
            [f"budgets/{budget_id}/limits", "budget-limits"],
            params=params,
        )
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise FireflyAPIError(f"Unexpected budget limit payload: {payload}")
        return data

    def create_budget_limit(
        self,
        *,
        budget_id: str,
        amount: str,
        start: str,
        end: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "budget_id": budget_id,
            "amount": amount,
            "start": start,
            "end": end,
        }
        if notes:
            payload["notes"] = notes
        return self._request_candidates("POST", [f"budgets/{budget_id}/limits", "budget-limits"], json_body=payload)

    def update_budget_limit(
        self,
        *,
        budget_limit_id: str,
        amount: str,
        start: str,
        end: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "amount": amount,
            "start": start,
            "end": end,
        }
        if notes:
            payload["notes"] = notes
        return self._request_candidates("PUT", [f"budget-limits/{budget_limit_id}"], json_body=payload)

    def list_transactions(self, *, start: date, end: date, limit: int = 100) -> list[dict[str, Any]]:
        return self._paginate(
            "transactions",
            params={"start": start.isoformat(), "end": end.isoformat(), "limit": min(limit, 100)},
        )

    def summary_basic(self, *, start: date, end: date) -> dict[str, Any]:
        return self._request("GET", "summary/basic", params={"start": start.isoformat(), "end": end.isoformat()})

    def create_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "transactions", json_body=payload)

    def delete_transaction(self, transaction_id: int) -> bool:
        """Delete a transaction. Returns True if successful."""
        try:
            response = self._request("DELETE", f"transactions/{transaction_id}")
            return True
        except Exception:
            return False

    def update_transaction(self, transaction_id: int, updates: dict[str, Any]) -> bool:
        """Update transaction fields (amount, date, category, etc). Returns True if successful."""
        try:
            response = self._request("GET", f"transactions/{transaction_id}")
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict):
                return False
            attributes = data.get("attributes", {})
            transactions = attributes.get("transactions")
            if not isinstance(transactions, list) or not transactions or not isinstance(transactions[0], dict):
                return False
            merged = dict(transactions[0])
            merged.update(updates)
            self._request(
                "PUT",
                f"transactions/{transaction_id}",
                json_body={"transactions": [merged]},
            )
            return True
        except Exception:
            return False

    def list_recurrences(self) -> list[dict[str, Any]]:
        return self._paginate("recurrences")

    def create_recurrence(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "recurrences", json_body=payload)

    def delete_recurrence(self, recurrence_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"recurrences/{recurrence_id}")
