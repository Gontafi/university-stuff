"""
Assignment 2 - Software Fault-Tolerant Design
AITU Student Scholarship and Tuition Payment Processing (synthetic data)

Author: Alibek Asset, 255175, CSE-2503
Run:    python3 payment_processor.py
        python3 payment_processor.py --crash   (baseline only, shows the real crash)
Tests:  python3 -m unittest -v test_payment_processor

Every run writes new logs into ./logs:
  run.log          readable log of the whole run (same as the console)
  events.jsonl     structured log, one JSON object per event
  attempt_log.csv  every attempt of the fault-tolerant run (Part C)
  checkpoints/     saved state of every checkpoint (Part D)
  summary.json     all numbers used in the report
"""

import copy
import csv
import hashlib
import json
import logging
import platform
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

VERSION = "2.0"
LOG_DIR = Path(__file__).resolve().parent / "logs"

MAX_RETRIES = 2            # initial attempt + 2 retries = 3 attempts at most
BACKOFF_SECONDS = 0.05     # 0.05 s, then 0.10 s (real system: 1 s, 2 s)
CHECKPOINT_EVERY = 5       # checkpoint after every 5 successful transactions
MAX_AMOUNT = 5_000_000     # validation limit for one payment, KZT


@dataclass(frozen=True)
class Transaction:
    id: str
    amount: int
    failure: str           # "None", "Network", "Timeout" or "Database"


TRANSACTIONS = [Transaction(*row) for row in [
    ("T001", 12000, "None"),
    ("T002", 25000, "Network"),
    ("T003", 8000, "None"),
    ("T004", 45000, "Timeout"),
    ("T005", 13000, "None"),
    ("T006", 70000, "Database"),
    ("T007", 9000, "None"),
    ("T008", 31000, "Network"),
    ("T009", 15000, "None"),
    ("T010", 50000, "Timeout"),
    ("T011", 6000, "None"),
    ("T012", 80000, "Database"),
    ("T013", 11000, "None"),
    ("T014", 22000, "None"),
    ("T015", 40000, "Network"),
]]

# Fault rules from the assignment: initial attempt, retry 1, retry 2.
# True = this attempt fails. After the list ends, attempts succeed.
FAULT_RULES = {
    "Network": [True, False],
    "Timeout": [True, True, False],
    "Database": [True, True, True],
}


# ---------------------------------------------------------------- exceptions

class PaymentError(Exception):
    """Base class for every transaction failure."""
    kind = "Unknown"
    retryable = False

    def __init__(self, txn_id, step, message):
        super().__init__(f"{txn_id} {step}: {message}")
        self.txn_id = txn_id
        self.step = step


class ValidationError(PaymentError):
    kind = "Validation"        # bad input, a retry gives the same result


class NetworkError(PaymentError):
    kind = "Network"
    retryable = True           # request did not reach the gateway, safe to repeat


class GatewayTimeoutError(PaymentError):
    kind = "Timeout"
    retryable = True           # gateway may have charged already -> needs idempotency key


class DatabaseError(PaymentError):
    kind = "Database"
    retryable = True           # retried by the rules, then rollback


# ---------------------------------------------------------------- simulated services

class FaultInjector:
    """Decides if an attempt fails. Follows FAULT_RULES exactly."""

    def __init__(self):
        self.calls = {}

    def fails(self, txn, kind):
        if txn.failure != kind:
            return False
        n = self.calls.get(txn.id, 0)
        self.calls[txn.id] = n + 1
        rule = FAULT_RULES[kind]
        return n < len(rule) and rule[n]


class PaymentGateway:
    """Simulated bank gateway that takes the money from the student account."""

    def __init__(self, faults, use_idempotency_keys=True):
        self.faults = faults
        self.use_keys = use_idempotency_keys
        self.charges = []          # every real charge: [txn_id, amount, refunded]
        self.calls = 0

    def charge(self, txn):
        self.calls += 1
        if self.faults.fails(txn, "Network"):
            raise NetworkError(txn.id, "Process", "connection reset, request did not reach the gateway")
        duplicate = self.use_keys and any(c[0] == txn.id and not c[2] for c in self.charges)
        if not duplicate:
            self.charges.append([txn.id, txn.amount, False])
        # a timeout happens AFTER the money is taken: only the answer is lost
        if self.faults.fails(txn, "Timeout"):
            raise GatewayTimeoutError(txn.id, "Process", "no answer from gateway in time, charge state unknown")
        return "duplicate key, not charged again" if duplicate else "charged"

    def refund(self, txn_id):
        amount = 0
        for c in self.charges:
            if c[0] == txn_id and not c[2]:
                c[2] = True
                amount += c[1]
        return amount

    def net_charged(self):
        return sum(c[1] for c in self.charges if not c[2])


class PaymentDB:
    """Payment table in SQLite. Record step = INSERT + COMMIT."""

    def __init__(self, faults):
        self.faults = faults
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE payments (txn_id TEXT PRIMARY KEY, amount INTEGER NOT NULL)")
        self.conn.commit()

    def record(self, txn):
        # txn_id is the primary key, so recording the same payment twice does nothing
        self.conn.execute("INSERT OR IGNORE INTO payments VALUES (?, ?)", (txn.id, txn.amount))
        if self.faults.fails(txn, "Database"):
            raise DatabaseError(txn.id, "Record", "commit failed, payment row is not saved")
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def rows(self):
        return dict(self.conn.execute("SELECT txn_id, amount FROM payments ORDER BY txn_id"))


# ---------------------------------------------------------------- logging

log = logging.getLogger("payments")
log.addHandler(logging.NullHandler())
log.propagate = False


class TextFormatter(logging.Formatter):
    def format(self, record):
        if getattr(record, "plain", False):
            return record.getMessage()
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        fields = " ".join(f"{k}={v}" for k, v in getattr(record, "fields", {}).items())
        return f"{ts} {record.levelname:<7} {record.getMessage():<18} {fields}"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        data.update(getattr(record, "fields", {}))
        return json.dumps(data, ensure_ascii=False)


def setup_logging(log_dir):
    (log_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    for old in log_dir.glob("checkpoints/*.json"):
        old.unlink()
    log.handlers.clear()
    log.setLevel(logging.INFO)

    text = TextFormatter()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_dir / "run.log", "w", "utf-8")):
        handler.setFormatter(text)
        log.addHandler(handler)

    events = logging.FileHandler(log_dir / "events.jsonl", "w", "utf-8")
    events.setFormatter(JsonFormatter())
    events.addFilter(lambda r: not getattr(r, "plain", False))
    log.addHandler(events)


def show(text=""):
    """Plain line for console and run.log (tables, headers). Not written to events.jsonl."""
    log.info(text, extra={"plain": True})


def show_table(headers, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = "  ".join("{:<%d}" % w for w in widths)
    show(line.format(*headers))
    show(line.format(*["-" * w for w in widths]))
    for row in rows:
        show(line.format(*row))


def kzt(x):
    return f"{x:,}"


# ---------------------------------------------------------------- processor

class PaymentProcessor:
    """Validate -> Process -> Record for a batch of payments.

    retries=False, checkpoints=False : Part B, exception handling only
    retries=True,  checkpoints=True  : Parts C and D, full fault tolerance
    """

    def __init__(self, mode, retries=True, checkpoints=True, use_idempotency_keys=True,
                 backoff=BACKOFF_SECONDS, checkpoint_dir=None):
        self.mode = mode
        self.retries = retries
        self.use_checkpoints = checkpoints
        self.backoff = backoff
        self.checkpoint_dir = checkpoint_dir
        self.faults = FaultInjector()
        self.gateway = PaymentGateway(self.faults, use_idempotency_keys)
        self.db = PaymentDB(self.faults)
        self.state = {"success": 0, "total": 0, "committed": [], "in_flight": {}}
        self.checkpoints = []
        self.status = {}
        self.attempt_log = []
        self.attempted = 0
        self.retry_count = 0
        self.rollback_count = 0
        self.step = 0
        if self.use_checkpoints:
            self.make_checkpoint("start of batch")

    def event(self, level, name, **fields):
        log.log(level, name, extra={"fields": {"mode": self.mode, **fields}})

    # -- one attempt of Validate -> Process -> Record, no error handling inside

    def validate(self, txn):
        if not re.fullmatch(r"T\d{3}", txn.id):
            raise ValidationError(txn.id, "Validate", "bad transaction id")
        if not 0 < txn.amount <= MAX_AMOUNT:
            raise ValidationError(txn.id, "Validate", f"amount {txn.amount} is out of range")

    def attempt(self, txn):
        self.validate(txn)
        self.gateway.charge(txn)
        self.state["in_flight"][txn.id] = txn.amount    # money taken, row not saved yet
        self.db.record(txn)
        del self.state["in_flight"][txn.id]
        self.state["committed"].append(txn.id)
        self.state["success"] += 1
        self.state["total"] += txn.amount

    # -- fault-tolerant processing of one transaction

    def process(self, txn):
        self.attempted += 1
        if txn.id in self.state["committed"]:
            self.event(logging.INFO, "txn_skipped", txn=txn.id, reason="already recorded")
            self.status[txn.id] = "SKIPPED (already recorded)"
            return self.status[txn.id]

        max_attempts = 1 + (MAX_RETRIES if self.retries else 0)
        for n in range(1, max_attempts + 1):
            try:
                self.attempt(txn)
            except PaymentError as err:
                if err.retryable and n < max_attempts:
                    delay = self.backoff * 2 ** (n - 1)
                    self.retry_count += 1
                    self.log_attempt(txn, n, err, f"retry in {delay:.2f} s", "FAIL")
                    time.sleep(delay)
                    continue
                return self.give_up(txn, n, err)
            self.log_attempt(txn, n, None, "record", "SUCCESS")
            self.status[txn.id] = "SUCCESS"
            if self.use_checkpoints and self.state["success"] % CHECKPOINT_EVERY == 0:
                self.make_checkpoint(f"{self.state['success']} successful transactions")
            return "SUCCESS"

    def give_up(self, txn, n, err):
        if isinstance(err, DatabaseError) and self.use_checkpoints:
            cp = self.rollback_to_checkpoint(txn)
            self.log_attempt(txn, n, err, f"rollback to {cp}", "ROLLED BACK")
            self.status[txn.id] = "ROLLED BACK"
        else:
            if not err.retryable:
                action = "not retryable, skip"
            elif self.retries:
                action = "retries used up, skip"
            else:
                action = "skip, continue batch"
            self.log_attempt(txn, n, err, action, "FAILED")
            self.status[txn.id] = "FAILED"
        return self.status[txn.id]

    def log_attempt(self, txn, n, err, action, result):
        self.step += 1
        row = {
            "step": self.step,
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "txn": txn.id,
            "attempt": n,
            "attempt_name": "initial" if n == 1 else f"retry {n - 1}",
            "failure": err.kind if err else "-",
            "exception": type(err).__name__ if err else "-",
            "message": str(err) if err else "-",
            "retryable": err.retryable if err else "-",
            "action": action,
            "result": result,
        }
        self.attempt_log.append(row)
        level = logging.INFO if result == "SUCCESS" else logging.WARNING
        fields = {k: row[k] for k in ("step", "txn", "attempt", "failure", "exception", "action", "result")}
        if err:
            fields["retryable"] = err.retryable
            fields["error"] = str(err)
        self.event(level, "attempt", **fields)

    # -- checkpoint and rollback

    def make_checkpoint(self, reason):
        cp = {
            "name": f"CP{len(self.checkpoints)}",
            "reason": reason,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "last_txn": self.state["committed"][-1] if self.state["committed"] else None,
            "state": copy.deepcopy(self.state),   # deep copy: later changes must not touch the checkpoint
            "db_rows": len(self.db.rows()),
            "gateway_net": self.gateway.net_charged(),
        }
        self.checkpoints.append(cp)
        if self.checkpoint_dir:
            with open(self.checkpoint_dir / f"{cp['name']}.json", "w", encoding="utf-8") as f:
                json.dump(cp, f, indent=2)
        self.event(logging.INFO, "checkpoint", cp=cp["name"], success=self.state["success"],
                   total=self.state["total"], last_txn=cp["last_txn"], reason=reason)
        return cp

    def rollback_to_checkpoint(self, txn):
        cp = self.checkpoints[-1]
        dirty_total = self.state["total"] + sum(self.state["in_flight"].values())
        self.db.rollback()                              # drop the uncommitted INSERT
        refunded = self.gateway.refund(txn.id)          # give the money back to the student
        restored = copy.deepcopy(cp["state"])
        # rows committed after the checkpoint are replayed from the DB (source of truth)
        rows = self.db.rows()
        replayed = [t for t in self.state["committed"] if t not in restored["committed"]]
        for t in replayed:
            restored["committed"].append(t)
            restored["success"] += 1
            restored["total"] += rows[t]
        self.state = restored
        self.rollback_count += 1
        self.event(logging.WARNING, "rollback", txn=txn.id, to=cp["name"], dirty_total=dirty_total,
                   restored_total=self.state["total"], refunded=refunded, replayed=len(replayed),
                   consistent=self.consistency()["ok"])
        return cp["name"]

    def consistency(self):
        db_total = sum(self.db.rows().values())
        return {
            "state_total": self.state["total"],
            "db_total": db_total,
            "gateway_net": self.gateway.net_charged(),
            "ok": self.state["total"] == db_total == self.gateway.net_charged(),
        }

    def run(self, transactions):
        for txn in transactions:
            self.process(txn)
        if self.use_checkpoints and self.state["success"] % CHECKPOINT_EVERY != 0:
            self.make_checkpoint("end of batch")
        return self


def run_baseline(transactions, processor):
    """Part A: no fault tolerance. The first exception stops the program."""
    for txn in transactions:
        processor.attempted += 1
        processor.attempt(txn)          # no try/except on purpose


# ---------------------------------------------------------------- report parts

TOTAL_AMOUNT = sum(t.amount for t in TRANSACTIONS)
AMOUNT = {t.id: t.amount for t in TRANSACTIONS}


def part_a():
    show("\n=== Part A - Baseline (no fault tolerance) ===")
    p = PaymentProcessor("baseline", retries=False, checkpoints=False)
    crash = None
    try:
        run_baseline(TRANSACTIONS, p)
    except PaymentError as err:        # only to measure the crash for the report
        crash = err
        log.error("baseline_crash", extra={"fields": {"mode": "baseline", "exception": type(err).__name__,
                                                      "error": str(err), "attempted": p.attempted}})
    s = p.state["success"]
    res = {
        "attempted": p.attempted,
        "successful": s,
        "lost": len(TRANSACTIONS) - s,
        "failed": p.attempted - s,
        "not_started": len(TRANSACTIONS) - p.attempted,
        "processed_amount": p.state["total"],
        "lost_amount": TOTAL_AMOUNT - p.state["total"],
        "stopped_at": crash.txn_id if crash else None,
        "crash": f"{type(crash).__name__}: {crash}" if crash else None,
    }
    show_table(["Metric", "Result"], [
        ["Transactions attempted", res["attempted"]],
        ["Successful transactions", res["successful"]],
        ["Transactions lost", f"{res['lost']} (1 failed + {res['not_started']} never started)"],
        ["Processed amount (KZT)", kzt(res["processed_amount"])],
        ["Lost amount (KZT)", kzt(res["lost_amount"])],
    ])
    return res


PART_B_ACTION = {
    "Network": "caught, logged as transient, skipped",
    "Timeout": "caught, logged (charge unknown), skipped",
    "Database": "caught, logged, skipped (no rollback yet)",
}


def part_b():
    show("\n=== Part B - Exception handling (no retry, no rollback) ===")
    p = PaymentProcessor("exceptions", retries=False, checkpoints=False).run(TRANSACTIONS)
    rows = []
    for r in p.attempt_log:
        if r["result"] != "SUCCESS":
            rows.append([r["txn"], r["failure"], r["exception"], PART_B_ACTION[r["failure"]], p.status[r["txn"]]])
    show_table(["Transaction", "Failure", "Exception", "Recovery action", "Final status"], rows)
    db_rows = p.db.rows()
    leaked = sorted(t for t in db_rows if p.status.get(t) != "SUCCESS")
    res = {
        "successful": p.state["success"],
        "failed": sum(1 for s in p.status.values() if s != "SUCCESS"),
        "processed_amount": p.state["total"],
        "lost_amount": TOTAL_AMOUNT - p.state["total"],
        "table": [dict(zip(["txn", "failure", "exception", "action", "status"], r)) for r in rows],
        "errors": {r["txn"]: r["message"] for r in p.attempt_log if r["result"] != "SUCCESS"},
        "db_rows": len(db_rows),
        "leaked_rows": leaked,
        "consistency": p.consistency(),
    }
    show(f"Successful {res['successful']}, failed {res['failed']}, "
         f"processed {kzt(res['processed_amount'])} KZT, lost {kzt(res['lost_amount'])} KZT")
    c = res["consistency"]
    show(f"Consistency check: state total {kzt(c['state_total'])}, DB total {kzt(c['db_total'])}, "
         f"gateway charged {kzt(c['gateway_net'])} -> {'OK' if c['ok'] else 'MISMATCH'}")
    if leaked:
        show(f"Rows of failed transactions that still reached the DB (no rollback): {', '.join(leaked)}")
    return res


def part_c_d(checkpoint_dir):
    show("\n=== Parts C and D - Retry + checkpoint/rollback (fault-tolerant run) ===")
    p = PaymentProcessor("fault-tolerant", checkpoint_dir=checkpoint_dir).run(TRANSACTIONS)

    show("\nPart C - attempt log")
    show_table(["Step", "Time", "Txn", "Attempt", "Failure", "Action", "Result"],
               [[r["step"], r["time"], r["txn"], f"{r['attempt']} ({r['attempt_name']})",
                 r["failure"], r["action"], r["result"]] for r in p.attempt_log])

    show("\nPart D - checkpoints")
    cps = [cp for cp in p.checkpoints if cp["name"] != "CP0"]
    show_table(["Checkpoint", "Successful", "Total (KZT)", "Last txn", "Reason"],
               [[cp["name"], cp["state"]["success"], kzt(cp["state"]["total"]), cp["last_txn"], cp["reason"]]
                for cp in cps])
    c = p.consistency()
    show(f"Consistency check: state total {kzt(c['state_total'])}, DB total {kzt(c['db_total'])}, "
         f"gateway charged {kzt(c['gateway_net'])} -> {'OK' if c['ok'] else 'MISMATCH'}")
    return p


def part_e(a, b, ft):
    show("\n=== Part E - Before/after ===")
    n = len(TRANSACTIONS)
    f = {
        "successful": ft.state["success"],
        "failed": sum(1 for s in ft.status.values() if s != "SUCCESS"),
        "processed_amount": ft.state["total"],
        "lost_amount": TOTAL_AMOUNT - ft.state["total"],
        "retries": ft.retry_count,
        "rollbacks": ft.rollback_count,
        "attempts": len(ft.attempt_log),
    }
    base_rate = a["successful"] / n
    ft_rate = f["successful"] / n
    faulty = [t for t in TRANSACTIONS if t.failure != "None"]
    recovered = [t.id for t in faulty if ft.status[t.id] == "SUCCESS"]
    res = {
        "baseline": {"successful": a["successful"], "failed": a["lost"], "lost_amount": a["lost_amount"],
                     "retries": 0, "rollbacks": 0, "completion_rate": base_rate},
        "exceptions_only": {"successful": b["successful"], "failed": b["failed"],
                            "lost_amount": b["lost_amount"], "completion_rate": b["successful"] / n},
        "fault_tolerant": {**f, "completion_rate": ft_rate},
        "completion_gain_pp": (ft_rate - base_rate) * 100,
        "txn_loss_reduction": (a["lost"] - f["failed"]) / a["lost"],
        "amount_loss_reduction": (a["lost_amount"] - f["lost_amount"]) / a["lost_amount"],
        "faulty": len(faulty),
        "recovered": recovered,
        "recovery_rate": len(recovered) / len(faulty),
        "rolled_back": [t for t, s in ft.status.items() if s == "ROLLED BACK"],
        "contribution": {
            "exception_handling": {"txns": b["successful"] - a["successful"],
                                   "amount": b["processed_amount"] - a["processed_amount"]},
            "retry": {"txns": f["successful"] - b["successful"],
                      "amount": f["processed_amount"] - b["processed_amount"]},
            "rollback": {"txns": 0, "amount": 0},
        },
        "consistency": ft.consistency(),
    }
    show_table(["Metric", "Baseline", "Exceptions only", "Fault-tolerant", "Improvement"], [
        ["Successful transactions", a["successful"], b["successful"], f["successful"],
         f"+{f['successful'] - a['successful']}"],
        ["Failed transactions", a["lost"], b["failed"], f["failed"], f"-{a['lost'] - f['failed']}"],
        ["Lost amount (KZT)", kzt(a["lost_amount"]), kzt(b["lost_amount"]), kzt(f["lost_amount"]),
         f"-{kzt(a['lost_amount'] - f['lost_amount'])}"],
        ["Retries", 0, 0, f["retries"], "-"],
        ["Rollbacks", 0, 0, f["rollbacks"], "-"],
        ["Completion rate", f"{base_rate:.2%}", f"{b['successful'] / n:.2%}", f"{ft_rate:.2%}",
         f"+{res['completion_gain_pp']:.2f} pp"],
    ])
    show(f"Transaction-loss reduction: ({a['lost']} - {f['failed']}) / {a['lost']} = {res['txn_loss_reduction']:.2%}")
    show(f"Amount-loss reduction: ({kzt(a['lost_amount'])} - {kzt(f['lost_amount'])}) / "
         f"{kzt(a['lost_amount'])} = {res['amount_loss_reduction']:.2%}")
    show(f"Recovered by retry: {len(recovered)} of {len(faulty)} faulty transactions ({', '.join(recovered)})")
    return res


def idempotency_experiment():
    show("\n=== Extra - same fault-tolerant run WITHOUT idempotency keys ===")
    p = PaymentProcessor("no-idempotency", use_idempotency_keys=False).run(TRANSACTIONS)
    c = p.consistency()
    counts = {}
    for txn_id, _, refunded in p.gateway.charges:
        if not refunded:
            counts[txn_id] = counts.get(txn_id, 0) + 1
    doubled = {t: k for t, k in counts.items() if k > 1}
    res = {
        "gateway_net": c["gateway_net"],
        "db_total": c["db_total"],
        "overcharge": c["gateway_net"] - c["db_total"],
        "charged_more_than_once": doubled,
    }
    show(f"Gateway charged {kzt(res['gateway_net'])} KZT, DB recorded {kzt(res['db_total'])} KZT, "
         f"overcharge {kzt(res['overcharge'])} KZT")
    for t, k in doubled.items():
        show(f"  {t}: charged {k} times x {kzt(AMOUNT[t])} KZT")
    return res


def source_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def main():
    if "--crash" in sys.argv:
        run_baseline(TRANSACTIONS, PaymentProcessor("baseline", retries=False, checkpoints=False))
        return

    setup_logging(LOG_DIR)
    started = datetime.now()
    sha = source_hash()
    show(f"payment_processor.py v{VERSION}  sha256 {sha[:16]}  Python {platform.python_version()}")
    show(f"Run started {started.isoformat(timespec='seconds')}  "
         f"{len(TRANSACTIONS)} transactions, {kzt(TOTAL_AMOUNT)} KZT")

    a = part_a()
    b = part_b()
    ft = part_c_d(LOG_DIR / "checkpoints")
    e = part_e(a, b, ft)
    idem = idempotency_experiment()

    with open(LOG_DIR / "attempt_log.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ft.attempt_log[0]))
        writer.writeheader()
        writer.writerows(ft.attempt_log)

    summary = {
        "version": VERSION,
        "source_sha256": sha,
        "python": platform.python_version(),
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().isoformat(timespec="seconds"),
        "total_amount": TOTAL_AMOUNT,
        "part_a": a,
        "part_b": b,
        "part_c": {"attempt_log": ft.attempt_log, "retries": ft.retry_count},
        "part_d": {"checkpoints": ft.checkpoints,
                   "status": ft.status},
        "part_e": e,
        "idempotency": idem,
    }
    with open(LOG_DIR / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    show(f"\nLogs written to {LOG_DIR}")


if __name__ == "__main__":
    main()
