"""Transaction draft state machine.

Manages the lifecycle of transaction drafts from extraction through
category/budget confirmation to final commit. This replaces the
flat ``pending_action`` dict with a structured state machine that
supports correction loops and multi-step confirmation flows.
"""
from __future__ import annotations

import enum
import json
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from firefly_companion.conversation import ConversationContext, localize


class DraftPhase(str, enum.Enum):
    """Phases of the transaction draft lifecycle."""

    EXTRACTING = "extracting"
    CATEGORY_CONFIRM = "category_confirm"
    CATEGORY_SELECT = "category_select"
    BUDGET_SUGGEST = "budget_suggest"
    REVIEW = "review"
    COMMITTED = "committed"
    DISCARDED = "discarded"


@dataclass
class StateSnapshot:
    """Immutable snapshot of draft state for recovery and audit."""

    version: int = 1
    timestamp: float = field(default_factory=time.time)
    user_id: int = 0
    draft: TransactionDraft = field(default_factory=lambda: TransactionDraft())
    phase: DraftPhase = DraftPhase.EXTRACTING
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "user_id": self.user_id,
            "draft": self.draft.to_dict(),
            "phase": self.phase.value if isinstance(self.phase, DraftPhase) else self.phase,
            "metadata": self.metadata,
        }


@dataclass
class TransactionDraft:
    """A single transaction draft with confirmation tracking."""

    type: str = "withdrawal"  # withdrawal | deposit | transfer
    amount: str = ""
    date: str = ""
    description: str = ""
    source_name: str | None = None
    destination_name: str | None = None
    category_name: str | None = None
    category_confirmed: bool = False
    budget_name: str | None = None
    budget_confirmed: bool = False
    notes: str | None = None
    merchant: str | None = None
    tags: list[str] = field(default_factory=list)
    currency_code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "amount": self.amount,
            "date": self.date,
            "description": self.description,
            "source_name": self.source_name,
            "destination_name": self.destination_name,
            "category_name": self.category_name,
            "category_confirmed": self.category_confirmed,
            "budget_name": self.budget_name,
            "budget_confirmed": self.budget_confirmed,
            "notes": self.notes,
            "merchant": self.merchant,
            "tags": self.tags,
            "currency_code": self.currency_code,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransactionDraft:
        return cls(
            type=str(data.get("type") or "withdrawal"),
            amount=str(data.get("amount") or ""),
            date=str(data.get("date") or ""),
            description=str(data.get("description") or ""),
            source_name=data.get("source_name"),
            destination_name=data.get("destination_name"),
            category_name=data.get("category_name"),
            category_confirmed=bool(data.get("category_confirmed")),
            budget_name=data.get("budget_name"),
            budget_confirmed=bool(data.get("budget_confirmed")),
            notes=data.get("notes"),
            merchant=data.get("merchant"),
            tags=list(data.get("tags") or []),
            currency_code=data.get("currency_code"),
            payload=dict(data.get("payload") or {}),
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TransactionDraft:
        """Create a draft from a bridge-ready transaction payload."""
        txns = payload.get("transactions") or [{}]
        txn = txns[0] if isinstance(txns, list) and txns else {}
        return cls(
            type=str(txn.get("type") or payload.get("type") or "withdrawal"),
            amount=str(txn.get("amount") or ""),
            date=str(txn.get("date") or ""),
            description=_capitalize_first(str(txn.get("description") or "")),
            source_name=txn.get("source_name"),
            destination_name=txn.get("destination_name"),
            category_name=txn.get("category_name"),
            budget_name=txn.get("budget_name"),
            notes=txn.get("notes"),
            merchant=txn.get("destination_name"),
            tags=list(txn.get("tags") or []),
            currency_code=txn.get("currency_code"),
            payload=payload,
        )

    def sync_payload(self) -> None:
        """Keep the bridge-ready payload aligned with edited draft fields."""
        transactions = self.payload.get("transactions")
        if not isinstance(transactions, list) or not transactions:
            return
        txn = transactions[0]
        if not isinstance(txn, dict):
            return
        txn["type"] = self.type
        txn["amount"] = self.amount
        if self.date:
            txn["date"] = self.date
        txn["description"] = self.description
        txn["source_name"] = self.source_name
        txn["destination_name"] = self.destination_name
        txn["category_name"] = self.category_name
        txn["budget_name"] = self.budget_name
        txn["notes"] = self.notes
        txn["tags"] = self.tags
        txn["currency_code"] = self.currency_code


@dataclass
class DraftSession:
    """Manages one or more transaction drafts through the confirmation flow."""

    phase: DraftPhase = DraftPhase.EXTRACTING
    drafts: list[TransactionDraft] = field(default_factory=list)
    batch: bool = False
    current_index: int = 0
    original_text: str = ""
    language: str = "en"
    source_image_context: bytes | None = field(default=None, repr=False)

    # -- Pending action support (backward compatibility) --
    pending_action_kind: str | None = None
    pending_action_payload: dict[str, Any] = field(default_factory=dict)
    pending_action_preview: str = ""

    @property
    def current_draft(self) -> TransactionDraft | None:
        if 0 <= self.current_index < len(self.drafts):
            return self.drafts[self.current_index]
        return None

    @property
    def is_active(self) -> bool:
        return self.phase not in (DraftPhase.COMMITTED, DraftPhase.DISCARDED)

    @property
    def is_batch(self) -> bool:
        return self.batch or len(self.drafts) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "drafts": [d.to_dict() for d in self.drafts],
            "batch": self.batch,
            "current_index": self.current_index,
            "original_text": self.original_text,
            "language": self.language,
            "pending_action_kind": self.pending_action_kind,
            "pending_action_payload": self.pending_action_payload,
            "pending_action_preview": self.pending_action_preview,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DraftSession:
        phase_str = str(data.get("phase") or "extracting")
        try:
            phase = DraftPhase(phase_str)
        except ValueError:
            phase = DraftPhase.EXTRACTING
        return cls(
            phase=phase,
            drafts=[TransactionDraft.from_dict(d) for d in (data.get("drafts") or [])],
            batch=bool(data.get("batch")),
            current_index=int(data.get("current_index") or 0),
            original_text=str(data.get("original_text") or ""),
            language=str(data.get("language") or "en"),
            pending_action_kind=data.get("pending_action_kind"),
            pending_action_payload=dict(data.get("pending_action_payload") or {}),
            pending_action_preview=str(data.get("pending_action_preview") or ""),
        )


class DraftManager:
    """Orchestrates the draft confirmation flow.

    This class manages state transitions and produces user-facing
    messages for each step of the draft lifecycle.
    """

    def __init__(
        self,
        *,
        object_cache: Any = None,
        category_budget_map: dict[str, str] | None = None,
        auto_confirm_merchants: list[str] | None = None,
        skip_budget_threshold: float = 5.0,
        category_confirmation_mode: str = "ask",
    ):
        self.object_cache = object_cache
        self.category_budget_map = category_budget_map or {}
        self.auto_confirm_merchants = [m.casefold() for m in (auto_confirm_merchants or [])]
        self.skip_budget_threshold = skip_budget_threshold
        self.category_confirmation_mode = category_confirmation_mode
        self.undo_stack: deque = deque(maxlen=3)
        self.draft_versions: dict[int, int] = {}  # user_id → version

    # ---- session creation -----------------------------------------------

    def create_session(
        self,
        payloads: list[dict[str, Any]],
        *,
        original_text: str = "",
        language: str = "en",
    ) -> DraftSession:
        """Create a new draft session from one or more transaction payloads."""
        drafts = [TransactionDraft.from_payload(p) for p in payloads]
        session = DraftSession(
            phase=DraftPhase.REVIEW if not self.object_cache else DraftPhase.CATEGORY_CONFIRM,
            drafts=drafts,
            batch=len(drafts) > 1,
            original_text=original_text,
            language=language,
        )
        # Auto-assign categories for known merchants
        for draft in drafts:
            if self._should_auto_confirm_category(draft):
                draft.category_confirmed = True
        # If all categories are confirmed, skip to budget or review
        if all(d.category_confirmed for d in drafts):
            session.phase = DraftPhase.BUDGET_SUGGEST
            if not self._any_budgets_relevant(session):
                session.phase = DraftPhase.REVIEW
        return session

    def create_pending_action_session(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        preview: str,
        language: str = "en",
    ) -> DraftSession:
        """Create a session for non-transaction pending actions (budget limits, etc.)."""
        return DraftSession(
            phase=DraftPhase.REVIEW,
            pending_action_kind=kind,
            pending_action_payload=payload,
            pending_action_preview=preview,
            language=language,
        )

    # ---- phase transitions ----------------------------------------------

    def advance(self, session: DraftSession, user_input: str = "") -> str:
        """Process user input and advance the session to the next phase.

        Returns a user-facing response message.
        """
        lowered = user_input.strip().casefold()
        ctx = ConversationContext(original_text=user_input, language=session.language)

        if session.phase == DraftPhase.CATEGORY_CONFIRM:
            return self._handle_category_confirm(session, lowered, ctx)
        if session.phase == DraftPhase.CATEGORY_SELECT:
            return self._handle_category_select(session, lowered, ctx)
        if session.phase == DraftPhase.BUDGET_SUGGEST:
            return self._handle_budget_suggest(session, lowered, ctx)
        if session.phase == DraftPhase.REVIEW:
            return self._handle_review(session, lowered, ctx)
        return ""

    # ---- category confirmation ------------------------------------------

    def build_category_confirm_message(self, session: DraftSession) -> str:
        """Build the category confirmation prompt for the current draft."""
        ctx = ConversationContext(original_text=session.original_text, language=session.language)
        draft = session.current_draft
        if not draft:
            return ""

        if draft.category_name:
            return ctx.localized(
                en=f"Proposed category: {draft.category_name}. Is that correct? (yes / no / type a name)",
                it=f"Categoria proposta: {draft.category_name}. Va bene? (si / no / scrivi un nome)",
            )
        return ctx.localized(
            en="I couldn't determine a category. Which one should I use? (type a name, or 'skip')",
            it="Non ho determinato una categoria. Quale uso? (scrivi un nome, o 'salta')",
        )

    def _handle_category_confirm(self, session: DraftSession, lowered: str, ctx: ConversationContext) -> str:
        draft = session.current_draft
        if not draft:
            session.phase = DraftPhase.REVIEW
            return self.build_review_message(session)

        # Accept
        if lowered in {"sì", "si", "yes", "ok", "va bene", "giusto", "correct", "y"}:
            draft.category_confirmed = True
            draft.sync_payload()
            return self._advance_after_category(session, ctx)

        # Reject → go to category selection
        if lowered in {"no", "n", "nope", "sbagliato", "wrong", "cambia"}:
            session.phase = DraftPhase.CATEGORY_SELECT
            return self.build_category_select_message(session)

        # Skip category
        if lowered in {"skip", "salta", "nessuna", "none"}:
            draft.category_name = None
            draft.category_confirmed = True
            draft.sync_payload()
            return self._advance_after_category(session, ctx)

        # Direct name entry
        if lowered and len(lowered) < 60:
            resolved = self._resolve_category_name(lowered)
            if resolved:
                draft.category_name = resolved
            else:
                draft.category_name = user_input_title_case(lowered)
            draft.category_confirmed = True
            draft.sync_payload()
            return self._advance_after_category(session, ctx)

        return self.build_category_confirm_message(session)

    def _advance_after_category(self, session: DraftSession, ctx: ConversationContext) -> str:
        """Move to the next draft's category or to budget suggestion."""
        # Check if there are more drafts needing category confirmation
        for i, draft in enumerate(session.drafts):
            if not draft.category_confirmed:
                session.current_index = i
                session.phase = DraftPhase.CATEGORY_CONFIRM
                return self.build_category_confirm_message(session)

        # All categories confirmed → budget suggestion
        session.current_index = 0
        session.phase = DraftPhase.BUDGET_SUGGEST
        if not self._any_budgets_relevant(session):
            session.phase = DraftPhase.REVIEW
            return self.build_review_message(session)
        return self.build_budget_suggest_message(session)

    # ---- category selection ---------------------------------------------

    def build_category_select_message(self, session: DraftSession) -> str:
        """Build a numbered list of existing categories for selection."""
        ctx = ConversationContext(original_text=session.original_text, language=session.language)
        categories = self.object_cache.categories() if self.object_cache else []
        if not categories:
            return ctx.localized(
                en="No categories found. Type a category name to create one, or 'skip'.",
                it="Nessuna categoria trovata. Scrivi un nome per crearne una, o 'salta'.",
            )
        lines = [ctx.localized(en="Choose a category:", it="Scegli una categoria:")]
        for i, cat in enumerate(categories, 1):
            lines.append(f"  {i}. {cat}")
        lines.append(ctx.localized(
            en="\nType a number, a name, or 'skip'.",
            it="\nScrivi il numero, un nome, o 'salta'.",
        ))
        return "\n".join(lines)

    def _handle_category_select(self, session: DraftSession, lowered: str, ctx: ConversationContext) -> str:
        draft = session.current_draft
        if not draft:
            session.phase = DraftPhase.REVIEW
            return self.build_review_message(session)

        categories = self.object_cache.categories() if self.object_cache else []

        # Skip
        if lowered in {"skip", "salta", "nessuna", "none"}:
            draft.category_name = None
            draft.category_confirmed = True
            draft.sync_payload()
            return self._advance_after_category(session, ctx)

        # Number selection
        if lowered.isdigit():
            index = int(lowered) - 1
            if 0 <= index < len(categories):
                draft.category_name = categories[index]
                draft.category_confirmed = True
                draft.sync_payload()
                return self._advance_after_category(session, ctx)

        # Name entry
        if lowered and len(lowered) < 60:
            resolved = self._resolve_category_name(lowered)
            if resolved:
                draft.category_name = resolved
            else:
                draft.category_name = user_input_title_case(lowered)
            draft.category_confirmed = True
            draft.sync_payload()
            return self._advance_after_category(session, ctx)

        return self.build_category_select_message(session)

    # ---- budget suggestion -----------------------------------------------

    def build_budget_suggest_message(self, session: DraftSession) -> str:
        """Build a budget suggestion for the current draft."""
        ctx = ConversationContext(original_text=session.original_text, language=session.language)
        draft = session.current_draft
        if not draft or not draft.category_name:
            session.phase = DraftPhase.REVIEW
            return self.build_review_message(session)

        suggestions = self._budget_suggestions_for(draft)
        if not suggestions:
            return self._advance_after_budget(session, ctx)

        if len(suggestions) == 1:
            return ctx.localized(
                en=f"Related budget found: {suggestions[0]}. Associate it? (yes / no)",
                it=f"Budget associabile: {suggestions[0]}. Lo associo? (si / no)",
            )

        lines = [ctx.localized(en="Compatible budgets:", it="Budget compatibili:")]
        for i, name in enumerate(suggestions, 1):
            lines.append(f"  {i}. {name}")
        lines.append(ctx.localized(
            en="Which one? (number / no)",
            it="Quale? (numero / no)",
        ))
        return "\n".join(lines)

    def _handle_budget_suggest(self, session: DraftSession, lowered: str, ctx: ConversationContext) -> str:
        draft = session.current_draft
        if not draft:
            session.phase = DraftPhase.REVIEW
            return self.build_review_message(session)

        suggestions = self._budget_suggestions_for(draft)

        # Skip
        if lowered in {"no", "n", "skip", "salta", "nessuno", "nope"}:
            draft.budget_confirmed = True
            draft.sync_payload()
            return self._advance_after_budget(session, ctx)

        # Accept single suggestion
        if lowered in {"sì", "si", "yes", "ok", "y"} and len(suggestions) == 1:
            draft.budget_name = suggestions[0]
            draft.budget_confirmed = True
            draft.sync_payload()
            return self._advance_after_budget(session, ctx)

        # Number selection
        if lowered.isdigit():
            index = int(lowered) - 1
            if 0 <= index < len(suggestions):
                draft.budget_name = suggestions[index]
                draft.budget_confirmed = True
                draft.sync_payload()
                return self._advance_after_budget(session, ctx)

        # Name entry
        if lowered and len(lowered) < 60:
            found = self.object_cache.find_budget(lowered) if self.object_cache else None
            if found:
                draft.budget_name = found
            else:
                draft.budget_name = user_input_title_case(lowered)
            draft.budget_confirmed = True
            draft.sync_payload()
            return self._advance_after_budget(session, ctx)

        return self.build_budget_suggest_message(session)

    def _advance_after_budget(self, session: DraftSession, ctx: ConversationContext) -> str:
        """Move to the next draft's budget or to review."""
        for i, draft in enumerate(session.drafts):
            if draft.category_confirmed and not draft.budget_confirmed:
                suggestions = self._budget_suggestions_for(draft)
                if suggestions:
                    session.current_index = i
                    session.phase = DraftPhase.BUDGET_SUGGEST
                    return self.build_budget_suggest_message(session)
                draft.budget_confirmed = True

        session.phase = DraftPhase.REVIEW
        session.current_index = 0
        return self.build_review_message(session)

    # ---- review ----------------------------------------------------------

    def build_review_message(self, session: DraftSession) -> str:
        """Build the final review message showing all drafts."""
        ctx = ConversationContext(original_text=session.original_text, language=session.language)

        # Pending action (non-transaction)
        if session.pending_action_kind and session.pending_action_preview:
            return session.pending_action_preview

        if not session.drafts:
            return ctx.localized(en="No drafts to review.", it="Nessuna bozza da rivedere.")

        lines: list[str] = []
        if session.is_batch:
            lines.append(ctx.localized(
                en=f"{len(session.drafts)} transactions prepared:",
                it=f"{len(session.drafts)} transazioni preparate:",
            ))
        else:
            lines.append(ctx.localized(
                en="Draft prepared:",
                it="Bozza preparata:",
            ))

        for i, draft in enumerate(session.drafts):
            if session.is_batch:
                lines.append(f"\n{'-' * 20} #{i + 1}")
            kind_label = _type_label(draft.type, ctx)
            lines.append(f"  {ctx.localized(en='Transaction', it='Movimento')}: {kind_label}")
            try:
                amt_str = f"€{float(draft.amount.replace(',', '.')):.2f}"
            except (ValueError, TypeError):
                amt_str = draft.amount
            amt_str = _format_money(draft.amount)
            lines.append(f"  {ctx.localized(en='Amount', it='Importo')}: {amt_str}")
            desc = draft.description or "—"
            lines.append(f"  {ctx.localized(en='Description', it='Descrizione')}: {desc}")
            if draft.date:
                lines.append(f"  {ctx.localized(en='Date', it='Data')}: {draft.date}")
            if draft.category_name:
                lines.append(f"  {ctx.localized(en='Category', it='Categoria')}: {draft.category_name}")
            if draft.budget_name:
                lines.append(f"  Budget: {draft.budget_name}")
            if draft.source_name:
                lines.append(f"  {ctx.localized(en='From', it='Da')}: {draft.source_name}")
            if draft.destination_name:
                lines.append(f"  {ctx.localized(en='To', it='A')}: {draft.destination_name}")
            if draft.notes:
                lines.append(f"  Note: {draft.notes}")

        lines.append("")
        lines.append(ctx.localized(
            en="Say 'confirm' to save, or 'cancel' to discard.",
            it="Scrivi 'conferma' per salvare, o 'annulla' per scartare.",
        ))
        lines.append(ctx.localized(
            en="You can also say: 'change amount to X', 'change description to Y', 'change category', 'change from X', 'change to X', 'list accounts', 're-read'. Use /cancel to force-discard.",
            it="Puoi anche dire: 'cambia importo a X', 'cambia descrizione a Y', 'cambia categoria', 'cambia da X', 'cambia a X', 'lista conti', 'rileggi'. Usa /cancel per scartare forzatamente.",
        ))
        return "\n".join(lines)

    def _handle_review(self, session: DraftSession, lowered: str, ctx: ConversationContext) -> str:
        # This phase just shows the review — actual commit/cancel is handled
        # by the bot's control mode (has_commit_intent / has_cancel_intent).
        return self.build_review_message(session)

    # ---- correction handling --------------------------------------------

    def apply_correction(self, session: DraftSession, correction: str) -> str:
        """Apply a user correction to the current draft.

        Supports:
        - "cambia importo a X" / "change amount to X"
        - "cambia categoria" / "change category"
        - "cambia data a X" / "change date to X"
        - "rileggi" / "re-read" (returns special marker)
        """
        import re

        lowered = correction.strip().casefold()
        ctx = ConversationContext(original_text=session.original_text, language=session.language)
        draft = session.current_draft or (session.drafts[0] if session.drafts else None)
        if not draft:
            return ctx.localized(en="No draft to modify.", it="Nessuna bozza da modificare.")

        # Change amount
        amount_match = re.search(
            r"(?:cambia|change|modifica)\s+(?:importo|amount|cifra)\s+(?:a|to|in)\s+([\d.,]+)",
            lowered,
        )
        if amount_match:
            new_amount = amount_match.group(1).replace(",", ".")
            draft.amount = new_amount
            draft.sync_payload()
            return self.build_review_message(session)

        # Change description
        desc_match = re.search(
            r"(?:cambia|change|modifica|aggiorna|set)\s+(?:descrizione|description)\s+(?:a|to|in|=)?\s*(.+)",
            lowered,
        )
        if desc_match:
            new_desc = desc_match.group(1).strip().rstrip(".,!?")
            if new_desc:
                draft.description = new_desc
                draft.sync_payload()
                return self.build_review_message(session)

        # Change category → back to category selection
        if any(phrase in lowered for phrase in {"cambia categoria", "change category", "modifica categoria"}):
            draft.category_confirmed = False
            session.phase = DraftPhase.CATEGORY_SELECT
            return self.build_category_select_message(session)

        # Change date
        date_match = re.search(
            r"(?:cambia|change|modifica)\s+(?:data|date)\s+(?:a|to|in)\s+(\S+)",
            lowered,
        )
        if date_match:
            from firefly_companion.date_parser import parse_flexible_date
            new_date = parse_flexible_date(date_match.group(1))
            if new_date:
                draft.date = new_date.isoformat()
                draft.sync_payload()
                return self.build_review_message(session)

        # Change source account
        source_match = re.search(
            r"(?:cambia|change|modifica)\s+(?:conto\s+)?(?:da|sorgente|source|from)\s+(?:a\s+)?(.+)",
            lowered,
        )
        if source_match:
            new_source = source_match.group(1).strip().rstrip(".,!?")
            if new_source:
                draft.source_name = new_source.title()
                draft.sync_payload()
                return self.build_review_message(session)

        # Change destination account
        dest_match = re.search(
            r"(?:cambia|change|modifica)\s+(?:conto\s+)?(?:a|destinazione|destination|to)\s+(.+)",
            lowered,
        )
        if dest_match:
            new_dest = dest_match.group(1).strip().rstrip(".,!?")
            if new_dest and new_dest not in {"X", "x"}:
                draft.destination_name = new_dest.title()
                draft.sync_payload()
                return self.build_review_message(session)

        # Request account list
        if any(phrase in lowered for phrase in {"lista conti", "conti disponibili", "list accounts", "show accounts", "quali conti"}):
            return "__LIST_ACCOUNTS__"

        # Re-read image
        if any(phrase in lowered for phrase in {"rileggi", "re-read", "reread", "leggi di nuovo"}):
            return "__REREAD__"

        return ctx.localized(
            en="I didn't understand that correction. Try: 'change amount to X', 'change category', 'change from X', 'change to X', 're-read'.",
            it="Non ho capito la correzione. Prova: 'cambia importo a X', 'cambia categoria', 'cambia da X', 'cambia a X', 'rileggi'.",
        )

    def is_correction(self, text: str) -> bool:
        """Check if user input looks like a draft correction."""
        lowered = text.strip().casefold()
        correction_markers = {
            "cambia", "change", "modifica", "rileggi", "re-read", "reread",
            "correggi", "fix", "sbagliato", "wrong", "not right",
            "lista conti", "conti disponibili", "list accounts", "quali conti",
            "descrizione", "description", "aggiorna",
        }
        return any(marker in lowered for marker in correction_markers)

    # ---- commit / discard -----------------------------------------------

    def mark_committed(self, session: DraftSession) -> None:
        session.phase = DraftPhase.COMMITTED

    def mark_discarded(self, session: DraftSession) -> None:
        session.phase = DraftPhase.DISCARDED

    # ---- helpers --------------------------------------------------------

    def _should_auto_confirm_category(self, draft: TransactionDraft) -> bool:
        """Check if this draft's merchant is in the auto-confirm list."""
        if self.category_confirmation_mode == "always_ask":
            return False
        if draft.category_confirmed:
            return True
        if not draft.merchant and not draft.destination_name:
            return self.category_confirmation_mode == "auto" and bool(draft.category_name)
        merchant_lower = (draft.merchant or draft.destination_name or "").casefold()
        for known in self.auto_confirm_merchants:
            if known in merchant_lower or merchant_lower in known:
                return bool(draft.category_name)
        return self.category_confirmation_mode == "auto" and bool(draft.category_name)

    def _any_budgets_relevant(self, session: DraftSession) -> bool:
        """Check if any draft has budget suggestions available."""
        for draft in session.drafts:
            if draft.budget_confirmed:
                continue
            if self._budget_suggestions_for(draft):
                return True
            draft.budget_confirmed = True
        return False

    def _budget_suggestions_for(self, draft: TransactionDraft) -> list[str]:
        """Get budget suggestions for a draft based on its category."""
        if not self.object_cache or not draft.category_name:
            return []
        return self.object_cache.find_budgets_for_category(
            draft.category_name,
            category_budget_map=self.category_budget_map,
        )

    def _resolve_category_name(self, text: str) -> str | None:
        """Try to resolve user input to an existing Firefly category."""
        if not self.object_cache:
            return None
        return self.object_cache.find_category_fuzzy(text)

    def push_undo(self, snapshot: StateSnapshot) -> None:
        """Save a snapshot before modifying draft."""
        self.undo_stack.append(snapshot)

    def undo(self) -> StateSnapshot | None:
        """Restore the last saved snapshot."""
        try:
            return self.undo_stack.pop()
        except IndexError:
            return None

    def get_draft_version(self, user_id: int) -> int:
        """Get current version of a draft."""
        return self.draft_versions.get(user_id, 0)

    def increment_draft_version(self, user_id: int) -> int:
        """Increment draft version and return new version."""
        current = self.draft_versions.get(user_id, 0)
        new_version = current + 1
        self.draft_versions[user_id] = new_version
        return new_version

    def check_draft_version(self, user_id: int, expected_version: int) -> bool:
        """Check if draft version matches expected. Returns True if match."""
        return self.draft_versions.get(user_id, 0) == expected_version


# ---- state persistence helpers ------------------------------------------

def save_draft_session(state: dict[str, Any], session: DraftSession | None) -> None:
    """Persist a draft session into the bot state dict."""
    if session is None or not session.is_active:
        state.pop("draft", None)
        state.pop("pending_action", None)
        state.pop("pending_transaction", None)
    else:
        state["draft"] = session.to_dict()
        # Backward compatibility: also set pending_transaction/pending_action
        if session.drafts and not session.pending_action_kind:
            if len(session.drafts) == 1:
                state["pending_transaction"] = session.drafts[0].payload
                state["pending_action"] = {
                    "kind": "transaction_create",
                    "payload": session.drafts[0].payload,
                    "preview": session.pending_action_preview or "",
                }
            else:
                payloads = [d.payload for d in session.drafts if isinstance(d.payload, dict)]
                state["pending_action"] = {
                    "kind": "transaction_batch_create",
                    "payload": {"transactions": payloads},
                    "preview": session.pending_action_preview or "",
                }
        if session.pending_action_kind:
            state["pending_action"] = {
                "kind": session.pending_action_kind,
                "payload": session.pending_action_payload,
                "preview": session.pending_action_preview,
            }


def load_draft_session(state: dict[str, Any]) -> DraftSession | None:
    """Load a draft session from the bot state dict."""
    if "draft" in state:
        return DraftSession.from_dict(state["draft"])
    # Backward compatibility: hydrate from old pending_transaction
    if "pending_transaction" in state:
        payload = state["pending_transaction"]
        if isinstance(payload, dict):
            session = DraftSession(
                phase=DraftPhase.REVIEW,
                drafts=[TransactionDraft.from_payload(payload)],
            )
            return session
    if "pending_action" in state:
        action = state["pending_action"]
        if isinstance(action, dict):
            return DraftSession(
                phase=DraftPhase.REVIEW,
                pending_action_kind=str(action.get("kind") or ""),
                pending_action_payload=dict(action.get("payload") or {}),
                pending_action_preview=str(action.get("preview") or ""),
            )
    return None


# ---- text helpers -------------------------------------------------------

def _capitalize_first(text: str) -> str:
    """Capitalize only the first character, leaving the rest unchanged."""
    t = text.strip()
    return t[:1].upper() + t[1:] if t else t


def user_input_title_case(text: str) -> str:
    """Convert user input to title case for category/budget names."""
    return text.strip().title()


def _type_label(transaction_type: str, ctx: ConversationContext) -> str:
    """Localized label for a transaction type."""
    labels = {
        "withdrawal": ctx.localized(en="Expense (money out)", it="Spesa (soldi in uscita)"),
        "deposit": ctx.localized(en="Income (money in)", it="Entrata (soldi in entrata)"),
        "transfer": ctx.localized(en="Transfer", it="Trasferimento"),
    }
    return labels.get(transaction_type, transaction_type)


def _format_money(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "EUR ?"
    try:
        amount = Decimal(raw.replace(",", "."))
    except Exception:
        return f"EUR {raw}"
    return f"EUR {amount.quantize(Decimal('0.01'))}"
