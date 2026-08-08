"""Cashier / finance role lock for r008 body sensing in autotune.

Roles (do not blur):

- **Cashier** = live host / supervise: owns the RTDE till, dispatches trials,
  writes sealed ledger / queue / authority. Must not treat its own phase marks
  as proof of EE motion.
- **Teller machine** = ``robot_body_truth``: samples TCP/tp/force/packets and
  emits verdicts. Never opens a second RTDE recipe while the cashier owns the
  till (live path is run-dir only).
- **Finance journal** = ``body_observer``: reconciles cashier claims vs body
  truth into ``r008-body-observer.jsonl``. Formal campaigns: diagnose only —
  no kill, no schedule edits, no seal.
- **Internal audit / close books** = canary harness gate only. Sustained
  ``contacted_stalled`` may abort a canary. Formal BO campaigns do not
  auto-REVOKED from body finance until a separate SOP raises that gate.

Body sensing answers "is the arm moving?"; it does not decide integral limits
or MAE training labels. Reconcile sidecars must never enter the GP ledger.
"""

from __future__ import annotations

from typing import Final

ROLE_CASHIER: Final = "cashier_host"
ROLE_TELLER: Final = "teller_body_truth"
ROLE_FINANCE: Final = "finance_body_observer"
ROLE_INTERNAL_AUDIT: Final = "internal_audit_canary_gate"

FORMAL_FINANCE_MAY_CLOSE_BOOKS: Final = False
CANARY_AUDIT_MAY_ABORT_ON_STALL: Final = True

SCHEMA: Final = "step5d.autotune-v4/r008-cashier-finance-roles-v1"


def roles_document() -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "roles": {
            ROLE_CASHIER: {
                "owner": "live host / supervise",
                "owns": ["RTDE", "sealed ledger", "queue", "authority"],
                "must_not": ["self-certify EE motion from phase marks alone"],
            },
            ROLE_TELLER: {
                "owner": "robot_body_truth",
                "owns": ["verdicts from run-dir / out-of-window RTDE"],
                "must_not": ["second RTDE while cashier holds the till"],
            },
            ROLE_FINANCE: {
                "owner": "body_observer",
                "owns": ["r008-body-observer.jsonl", "r008-body-reconcile.jsonl"],
                "must_not": [
                    "kill formal host",
                    "edit schedule",
                    "seal",
                    "write GP / r006-observations training rows",
                ],
                "formal_may_close_books": FORMAL_FINANCE_MAY_CLOSE_BOOKS,
            },
            ROLE_INTERNAL_AUDIT: {
                "owner": "canary harness gate",
                "owns": ["canary abort on sustained contacted_stalled"],
                "formal_may_close_books": False,
                "canary_may_abort_on_stall": CANARY_AUDIT_MAY_ABORT_ON_STALL,
            },
        },
    }


__all__ = [
    "SCHEMA",
    "ROLE_CASHIER",
    "ROLE_TELLER",
    "ROLE_FINANCE",
    "ROLE_INTERNAL_AUDIT",
    "FORMAL_FINANCE_MAY_CLOSE_BOOKS",
    "CANARY_AUDIT_MAY_ABORT_ON_STALL",
    "roles_document",
]
