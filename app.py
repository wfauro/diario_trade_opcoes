#!/usr/bin/env python3
"""Diário local de opções — servidor HTTP e SQLite, sem dependências externas.

Variáveis de ambiente:
  DIARIO_PORT  porta preferida (padrão 5050)
  DIARIO_DEV=1 recarrega o servidor quando app.py ou index.html mudam
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, urlencode
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import datetime
import json
import math
import os
import sqlite3
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

APP_VERSION = "1.3.0"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "diario_opcoes.sqlite3"
HOST, PORT = "127.0.0.1", int(os.environ.get("DIARIO_PORT", "5050"))
DEV_RELOAD = os.environ.get("DIARIO_DEV") == "1"
MAX_BODY = 20 * 1024 * 1024  # limite de corpo de requisição (backups grandes)
QUOTE_TTL = 30  # segundos de cache para cotações
DB_PATH.parent.mkdir(exist_ok=True)
LOCK = threading.RLock()
_QUOTE_CACHE = {}


# --------------------------------------------------------------------------
# Banco de dados
# --------------------------------------------------------------------------
@contextmanager
def connect():
    """Abre a conexão, faz commit/rollback e SEMPRE fecha ao sair."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            start_date TEXT NOT NULL,
            underlying TEXT NOT NULL,
            strategy TEXT NOT NULL,
            capital_base REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            initial_structures INTEGER NOT NULL DEFAULT 0,
            initial_leg_count INTEGER NOT NULL DEFAULT 0,
            initial_unit_value REAL
        );
        CREATE TABLE IF NOT EXISTS legs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
          code TEXT NOT NULL DEFAULT '',
          option_type TEXT NOT NULL CHECK(option_type IN ('CALL','PUT')),
          strike REAL NOT NULL, expiry TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
          quantity INTEGER NOT NULL CHECK(quantity > 0),
          entry_premium REAL NOT NULL CHECK(entry_premium >= 0),
          multiplier REAL NOT NULL DEFAULT 1 CHECK(multiplier > 0),
          entry_cost REAL NOT NULL DEFAULT 0,
          entry_date TEXT NOT NULL DEFAULT '',
          reference_premium REAL, reference_date TEXT
        );
        CREATE TABLE IF NOT EXISTS closeouts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          leg_id INTEGER NOT NULL REFERENCES legs(id) ON DELETE CASCADE,
          close_date TEXT NOT NULL,
          quantity INTEGER NOT NULL CHECK(quantity > 0),
          premium REAL NOT NULL CHECK(premium >= 0),
          costs REAL NOT NULL DEFAULT 0,
          notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_legs_operation ON legs(operation_id);
        CREATE INDEX IF NOT EXISTS idx_closeouts_leg ON closeouts(leg_id);
        """)
        # Migra bancos antigos sem apagar nem substituir os dados existentes.
        opcols = {r["name"] for r in db.execute("PRAGMA table_info(operations)")}
        legcols = {r["name"] for r in db.execute("PRAGMA table_info(legs)")}
        if "initial_structures" not in opcols:
            db.execute("ALTER TABLE operations ADD COLUMN initial_structures INTEGER NOT NULL DEFAULT 0")
        if "initial_leg_count" not in opcols:
            db.execute("ALTER TABLE operations ADD COLUMN initial_leg_count INTEGER NOT NULL DEFAULT 0")
        if "initial_unit_value" not in opcols:
            db.execute("ALTER TABLE operations ADD COLUMN initial_unit_value REAL")
        if "entry_date" not in legcols:
            db.execute("ALTER TABLE legs ADD COLUMN entry_date TEXT NOT NULL DEFAULT ''")
        backfill(db)


def backfill(db):
    """Preenche campos derivados de bancos antigos ou de backups antigos.

    Deve rodar DEPOIS de todas as pernas existirem (init_db e importação).
    Só mexe em registros ainda não preenchidos.
    """
    db.execute(
        "UPDATE legs SET entry_date=(SELECT start_date FROM operations "
        "WHERE operations.id=legs.operation_id) WHERE entry_date='' OR entry_date IS NULL")
    # Estrutura original = pernas com a data de entrada igual à data de início;
    # se nenhuma bater, considera todas.
    db.execute("""
        UPDATE operations SET initial_leg_count = COALESCE(
            NULLIF((SELECT COUNT(*) FROM legs
                    WHERE legs.operation_id = operations.id
                      AND legs.entry_date = operations.start_date), 0),
            (SELECT COUNT(*) FROM legs WHERE legs.operation_id = operations.id))
        WHERE initial_leg_count = 0
    """)
    pending = db.execute(
        "SELECT id, initial_leg_count, initial_structures, initial_unit_value "
        "FROM operations WHERE initial_structures=0 OR initial_unit_value IS NULL").fetchall()
    for op in pending:
        legs = [dict(r) for r in db.execute(
            "SELECT side, quantity, entry_premium FROM legs "
            "WHERE operation_id=? ORDER BY id LIMIT ?", (op["id"], op["initial_leg_count"]))]
        if not legs:
            continue
        structures = op["initial_structures"] or min(l["quantity"] for l in legs)
        unit = op["initial_unit_value"]
        if unit is None:
            unit = structure_unit_value(legs)["unit_value"]
        db.execute("UPDATE operations SET initial_structures=?, initial_unit_value=? WHERE id=?",
                   (structures, unit, op["id"]))


def all_rows(sql, params=()):
    with connect() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------
# Validação
# --------------------------------------------------------------------------
def number(v, name, minimum=None):
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: informe um número válido.")
    if not math.isfinite(n):
        raise ValueError(f"{name}: informe um número válido.")
    if minimum is not None and n < minimum:
        raise ValueError(f"{name}: deve ser pelo menos {minimum}.")
    return n

def decimal_number(v, name, minimum=None):
    try:
        n = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name}: informe um número válido.")

    if not n.is_finite():
        raise ValueError(f"{name}: informe um número válido.")

    if minimum is not None and n < Decimal(str(minimum)):
        raise ValueError(f"{name}: deve ser pelo menos {minimum}.")

    return n

def integer(v, name, minimum=1):
    n = number(v, name, minimum)
    if not n.is_integer():
        raise ValueError(f"{name}: deve ser um número inteiro.")
    return int(n)


def parse_date(v, name):
    if not v:
        raise ValueError(f"{name}: informe uma data.")
    try:
        return datetime.date.fromisoformat(str(v))
    except ValueError:
        raise ValueError(f"{name}: data inválida (use AAAA-MM-DD).")


def to_id(v, name):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: identificador inválido.")
    if n <= 0:
        raise ValueError(f"{name}: identificador inválido.")
    return n


def parse_leg(data, entry_date, label=""):
    """Valida uma perna e devolve um dict pronto para INSERT (sem operation_id)."""
    prefix = f"Perna {label}: " if label else ""
    if not isinstance(data, dict):
        raise ValueError(f"{prefix}dados inválidos.")
    typ, side = data.get("option_type"), data.get("side")
    if typ not in ("CALL", "PUT") or side not in ("LONG", "SHORT"):
        raise ValueError(f"{prefix}selecione tipo e lado válidos.")
    expiry = parse_date(data.get("expiry"), f"{prefix}Vencimento")
    if expiry < entry_date:
        raise ValueError(f"{prefix}o vencimento não pode ser anterior à data de entrada.")
    ref_premium = data.get("reference_premium")
    ref_date = None
    if ref_premium in (None, ""):
        ref_premium = None
    else:
        ref_premium = number(ref_premium, "Preço de referência", 0)
        ref_date = parse_date(data.get("reference_date") or datetime.date.today().isoformat(),
                              "Data de referência").isoformat()
    return {
        "code": str(data.get("code", "")).strip(),
        "option_type": typ,
        "strike": number(data.get("strike"), "Strike", 0),
        "expiry": expiry.isoformat(),
        "side": side,
        "quantity": integer(data.get("quantity"), "Quantidade", 1),
        "entry_premium": number(data.get("entry_premium"), "Prêmio de entrada", 0),
        "multiplier": number(data.get("multiplier") or 1, "Multiplicador", 0.000001),
        "entry_cost": number(data.get("entry_cost") or 0, "Custos de entrada", 0),
        "entry_date": entry_date.isoformat(),
        "reference_premium": ref_premium,
        "reference_date": ref_date,
    }


LEG_INSERT = """
INSERT INTO legs(operation_id,code,option_type,strike,expiry,side,quantity,
                 entry_premium,multiplier,entry_cost,entry_date,reference_premium,reference_date)
VALUES(:operation_id,:code,:option_type,:strike,:expiry,:side,:quantity,
       :entry_premium,:multiplier,:entry_cost,:entry_date,:reference_premium,:reference_date)
"""


# --------------------------------------------------------------------------
# Cálculos
# --------------------------------------------------------------------------
def structure_unit_value(legs):
    """Calcula o prêmio líquido e o valor unitário da estrutura."""
    legs = [leg for leg in legs if int(leg["quantity"]) > 0]
    if not legs:
        return {"net_premium": 0.0, "quantity": 0, "unit_value": None}

    net_premium = Decimal("0")
    for leg in legs:
        premium = decimal_number(leg["entry_premium"], "Prêmio", 0)
        quantity_leg = int(leg["quantity"])
        direction = Decimal("1") if leg["side"] == "LONG" else Decimal("-1")
        net_premium += direction * premium * quantity_leg

    quantity = min(int(leg["quantity"]) for leg in legs)
    precision = Decimal("0.000001")

    rounded_net = net_premium.quantize(precision, rounding=ROUND_HALF_UP)
    unit_value = (net_premium / Decimal(quantity)).quantize(
        precision, rounding=ROUND_HALF_UP
    )

    # Mantém os números no formato que o restante do app já espera.
    return {
        "net_premium": float(rounded_net),
        "quantity": quantity,
        "unit_value": float(unit_value),
    }


def calculate_unit_values(initial_legs, current_legs):
    """Valor unitário inicial (estrutura original) e atual (pernas em aberto)."""
    initial = structure_unit_value(initial_legs)
    current = structure_unit_value(current_legs)
    return {
        "initial_unit_value": initial["unit_value"],
        "initial_net_premium": initial["net_premium"],
        "initial_unit_quantity": initial["quantity"],
        "current_unit_value": current["unit_value"],
        "current_net_premium": current["net_premium"],
        "current_unit_quantity": current["quantity"],
    }


def closeout_pnl(leg, closeout):
    """Resultado realizado de um encerramento (já líquido de custos)."""
    direction = 1 if leg["side"] == "LONG" else -1
    entry_cost_share = leg["entry_cost"] * closeout["quantity"] / leg["quantity"]
    return ((closeout["premium"] - leg["entry_premium"]) * direction
            * closeout["quantity"] * leg["multiplier"] - entry_cost_share - closeout["costs"])


def build_operation(op, legs, closes_by_leg):
    warnings = []
    total_realized = 0.0
    any_remaining = False
    any_closed = False
    for leg in legs:
        closes = closes_by_leg.get(leg["id"], [])
        closed_qty = sum(c["quantity"] for c in closes)
        if closed_qty > leg["quantity"]:
            # Não derruba o dashboard por causa de uma operação inconsistente.
            warnings.append(f"Perna {leg['id']}: quantidade encerrada ({closed_qty}) "
                            f"maior que a de entrada ({leg['quantity']}).")
        remaining = max(0, leg["quantity"] - closed_qty)
        direction = 1 if leg["side"] == "LONG" else -1
        pnl = sum(closeout_pnl(leg, c) for c in closes)
        unrealized = None
        if remaining and leg["reference_premium"] is not None:
            unclosed_entry_cost = leg["entry_cost"] * remaining / leg["quantity"]
            unrealized = ((leg["reference_premium"] - leg["entry_premium"]) * direction
                          * remaining * leg["multiplier"] - unclosed_entry_cost)
        leg.update(closeouts=closes, closed_quantity=closed_qty, remaining_quantity=remaining,
                   realized=round(pnl, 6),
                   unrealized=None if unrealized is None else round(unrealized, 6))
        total_realized += pnl
        any_remaining |= remaining > 0
        any_closed |= closed_qty > 0

    op["legs"] = legs
    op["warnings"] = warnings

    # Estrutura original = primeiras N pernas; o resto são rolagens/ajustes.
    n_initial = int(op.get("initial_leg_count") or 0) or len(legs)
    initial_legs = legs[:n_initial]
    stored_initial_unit = op.get("initial_unit_value")
    open_legs = [{"side": l["side"], "quantity": l["remaining_quantity"],
                  "entry_premium": l["entry_premium"]}
                 for l in legs if l["remaining_quantity"] > 0]
    units = calculate_unit_values(initial_legs, open_legs)
    if stored_initial_unit is not None:
        # Mantém o valor gravado na criação da operação.
        units["initial_unit_value"] = stored_initial_unit
    op.update(units)

    initial_structures = int(op.get("initial_structures") or 0) or min(
        (l["quantity"] for l in initial_legs), default=0)
    op["structure_count"] = min((l["remaining_quantity"] for l in initial_legs), default=0)
    op["closed_structure_count"] = max(0, initial_structures - op["structure_count"])
    op["closed"] = not any_remaining

    # Resultado financeiro de entrada de TODAS as pernas, inclusive as de
    # rolagem: elas podem aumentar ou reduzir o valor da estrutura e por isso
    # entram na base e no resultado. O multiplicador participa aqui; o valor
    # unitário é calculado à parte, só com prêmios.
    opening_cash = sum((1 if l["side"] == "LONG" else -1)
                       * l["entry_premium"] * l["quantity"] * l["multiplier"] + l["entry_cost"]
                       for l in legs)
    effective_base = float(op.get("capital_base") or 0)
    base_source = "informada"
    if effective_base <= 0.005:
        # Débito: base é o débito inicial. Crédito: valor absoluto do crédito.
        effective_base = abs(opening_cash)
        if effective_base > 0.005:
            base_source = "débito líquido inicial" if opening_cash >= 0 else "crédito líquido inicial"
        else:
            base_source = "não definida"

    op["opening_net_cash"] = round(opening_cash, 2)
    op["opening_net_debit"] = round(max(0.0, opening_cash), 2)
    op["opening_net_credit"] = round(max(0.0, -opening_cash), 2)
    op["capital_base_effective"] = round(effective_base, 2)
    op["capital_base_source"] = base_source
    op["realized"] = round(total_realized, 2)
    op["unrealized"] = (None if any(l["remaining_quantity"] and l["unrealized"] is None for l in legs)
                        else round(sum(l["unrealized"] or 0 for l in legs), 2))
    op["return_pct"] = round(op["realized"] / effective_base * 100, 2) if effective_base > 0 else None
    # Em operação aberta o retorno é parcial (só o que já foi realizado).
    op["return_is_partial"] = not op["closed"]
    # Campos usados pela interface.
    op["status"] = "ENCERRADA" if op["closed"] else ("PARCIAL" if any_closed else "ABERTA")
    if op["unrealized"] is None:
        op["estimated_total"] = None
        op["estimated_return_pct"] = None
    else:
        op["estimated_total"] = round(op["realized"] + op["unrealized"], 2)
        op["estimated_return_pct"] = (round(op["estimated_total"] / effective_base * 100, 2)
                                      if effective_base > 0 else None)
    op["payoff"] = payoff_profile(op)
    return op


def load_operations(op_id=None):
    """Carrega operações com pernas e encerramentos em uma única conexão."""
    with connect() as db:
        if op_id is None:
            op_rows = db.execute("SELECT * FROM operations ORDER BY start_date DESC, id DESC")
            leg_rows = db.execute("SELECT * FROM legs ORDER BY id")
            close_rows = db.execute("SELECT * FROM closeouts ORDER BY close_date, id")
        else:
            op_rows = db.execute("SELECT * FROM operations WHERE id=?", (op_id,))
            leg_rows = db.execute("SELECT * FROM legs WHERE operation_id=? ORDER BY id", (op_id,))
            close_rows = db.execute(
                "SELECT c.* FROM closeouts c JOIN legs l ON l.id=c.leg_id "
                "WHERE l.operation_id=? ORDER BY c.close_date, c.id", (op_id,))
        ops = [dict(r) for r in op_rows]
        legs_by_op, closes_by_leg = {}, {}
        for r in leg_rows:
            legs_by_op.setdefault(r["operation_id"], []).append(dict(r))
        for r in close_rows:
            closes_by_leg.setdefault(r["leg_id"], []).append(dict(r))
    return [build_operation(op, legs_by_op.get(op["id"], []), closes_by_leg) for op in ops]


def get_operation(op_id):
    found = load_operations(op_id)
    return found[0] if found else None


def payoff_profile(op):
    """Perfil de payoff no vencimento e referência para centralizar o gráfico.

    Inclui o realizado e as posições ainda abertas, com os custos de entrada
    não alocados às quantidades já encerradas. Break-evens são preços não
    negativos do ativo nos quais esse resultado projetado cruza zero.

    Limitação: todas as pernas abertas são avaliadas pelo valor intrínseco,
    como se vencessem juntas. Com vencimentos diferentes (calendários,
    rolagens) o resultado é aproximado — ver 'mixed_expiries'.
    """
    legs = op.get("legs", [])

    def value_at(price):
        total = float(op.get("realized") or 0)
        for leg in legs:
            qty = int(leg.get("remaining_quantity") or 0)
            if not qty:
                continue
            intrinsic = (max(price - leg["strike"], 0) if leg["option_type"] == "CALL"
                         else max(leg["strike"] - price, 0))
            direction = 1 if leg["side"] == "LONG" else -1
            entry_cost = float(leg.get("entry_cost") or 0) * qty / leg["quantity"]
            total += (intrinsic - leg["entry_premium"]) * direction * qty * leg["multiplier"] - entry_cost
        return total

    strikes = sorted({float(l["strike"]) for l in legs})
    points = sorted({0.0, *strikes})
    be = []

    def add_root(x):
        if x >= -1e-9:
            be.append(max(0.0, float(x)))

    values = [value_at(x) for x in points]
    for i, x in enumerate(points):
        if abs(values[i]) <= 1e-8:
            add_root(x)
        if i + 1 < len(points):
            y, y2 = values[i], values[i + 1]
            if y * y2 < 0:
                add_root(x + (0 - y) * (points[i + 1] - x) / (y2 - y))

    # Além do maior strike, puts têm inclinação zero e calls têm inclinação
    # constante. Resolver essa última reta captura BE acima do último strike.
    last = points[-1]
    slope = sum((1 if l["side"] == "LONG" else -1) * l["remaining_quantity"] * l["multiplier"]
                for l in legs if l["remaining_quantity"] and l["option_type"] == "CALL")
    if abs(slope) > 1e-12:
        root = last - value_at(last) / slope
        if root > last + 1e-9:
            add_root(root)

    deduped = []
    for root in sorted(be):
        if not deduped or abs(root - deduped[-1]) > 1e-6:
            deduped.append(root)

    if len(deduped) >= 2:
        center, center_basis = (deduped[0] + deduped[-1]) / 2, "ponto médio entre break-evens"
    elif deduped:
        center, center_basis = deduped[0], "break-even"
    elif strikes:
        center, center_basis = (strikes[0] + strikes[-1]) / 2, "ponto médio do intervalo de strikes"
    else:
        center, center_basis = 0.0, "sem strikes cadastrados"

    max_strike = strikes[-1] if strikes else 0.0
    # Faixa informativa simétrica ao redor do centro; a interface pode ampliá-la.
    radius = max((max_strike - strikes[0]) / 2 if len(strikes) > 1 else 0.0,
                 abs(center) * 0.25, max_strike * 0.15, 1.0)
    open_expiries = {l["expiry"] for l in legs if l.get("remaining_quantity")}
    return {
        "break_even_points": [round(x, 6) for x in deduped],
        "center": round(center, 6),
        "center_basis": center_basis,
        "x_min": round(max(0.0, center - radius), 6),
        "x_max": round(center + radius, 6),
        "strike_min": round(strikes[0], 6) if strikes else None,
        "strike_max": round(max_strike, 6) if strikes else None,
        "mixed_expiries": len(open_expiries) > 1,
    }


def dashboard():
    operations = load_operations()
    closed = [o for o in operations if o["closed"]]
    wins = [o for o in closed if o["realized"] > 0.005]
    losses = [o for o in closed if o["realized"] < -0.005]
    flat = len(closed) - len(wins) - len(losses)
    groups, months = {}, {}
    for op in operations:
        key = (op["strategy"], op["underlying"])
        g = groups.setdefault(key, {
            "strategy": key[0], "underlying": key[1], "count": 0, "structures": 0,
            "closed_structures": 0, "open_structures": 0, "closed": 0, "realized": 0.0,
            "return_realized": 0.0, "capital_base": 0.0, "wins": 0, "losses": 0, "open": 0})
        g["count"] += 1
        g["structures"] += op["initial_structures"]
        g["closed_structures"] += op["closed_structure_count"]
        g["open_structures"] += op["structure_count"]
        g["realized"] += op["realized"]
        g["capital_base"] += op["capital_base_effective"]
        g["return_realized"] += op["realized"] if op["capital_base_effective"] > 0 else 0
        g["open"] += not op["closed"]
        g["closed"] += op["closed"]
        g["wins"] += op["closed"] and op["realized"] > 0.005
        g["losses"] += op["closed"] and op["realized"] < -0.005
        for leg in op["legs"]:
            for c in leg["closeouts"]:
                m = months.setdefault(c["close_date"][:7],
                                      {"month": c["close_date"][:7], "realized": 0.0, "closeouts": 0})
                m["realized"] += closeout_pnl(leg, c)
                m["closeouts"] += 1
    for g in groups.values():
        g["realized"] = round(g["realized"], 2)
        g["return_pct"] = (round(g["return_realized"] / g["capital_base"] * 100, 2)
                           if g["capital_base"] > 0 else None)
    basis_operations = [o for o in operations if o["capital_base_effective"] > 0]
    capital_base_total = sum(o["capital_base_effective"] for o in basis_operations)
    return_realized_total = sum(o["realized"] for o in basis_operations)
    return_excluded_count = sum(o["capital_base_effective"] <= 0 and abs(o["realized"]) > 0.005
                                for o in operations)
    return {
        "operations": operations, "closed_count": len(closed), "win_count": len(wins),
        "loss_count": len(losses), "flat_count": flat,
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else 0,
        "error_rate": round(len(losses) / len(closed) * 100, 2) if closed else 0,
        "realized_total": round(sum(o["realized"] for o in operations), 2),
        "capital_base_total": round(capital_base_total, 2),
        "financial_return_pct": (round(return_realized_total / capital_base_total * 100, 2)
                                 if capital_base_total > 0 else None),
        "capital_base_count": len(basis_operations),
        "capital_base_missing_count": return_excluded_count,
        "avg_win": round(sum(o["realized"] for o in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(o["realized"] for o in losses) / len(losses), 2) if losses else 0,
        "open_count": sum(not o["closed"] for o in operations),
        "open_structures": sum(o["structure_count"] for o in operations),
        "closed_structures": sum(o["closed_structure_count"] for o in operations),
        "groups": sorted(groups.values(), key=lambda x: (x["strategy"], x["underlying"])),
        "months": sorted([{"month": k, "realized": round(v["realized"], 2), "closeouts": v["closeouts"]}
                          for k, v in months.items()], key=lambda x: x["month"]),
    }


# --------------------------------------------------------------------------
# Operações de escrita
# --------------------------------------------------------------------------
def create_operation(data):
    legs = data.get("legs") or []
    if not isinstance(legs, list) or len(legs) < 1:
        raise ValueError("Inclua ao menos uma perna.")
    start = parse_date(data.get("start_date") or datetime.date.today().isoformat(), "Data de início")
    underlying = str(data.get("underlying", "")).strip().upper()
    strategy = str(data.get("strategy", "")).strip()
    if not underlying or not strategy:
        raise ValueError("Preencha ativo e estratégia/estrutura.")
    capital_base = number(data.get("capital_base") or 0, "Capital-base", 0)
    prepared = [parse_leg(l, start, str(i)) for i, l in enumerate(legs, 1)]
    initial_structures = min(p["quantity"] for p in prepared)
    initial_unit_value = structure_unit_value(prepared)["unit_value"]
    with LOCK, connect() as db:
        cur = db.execute(
            """INSERT INTO operations(created_at,start_date,underlying,strategy,capital_base,notes,
                                      initial_structures,initial_leg_count,initial_unit_value)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (datetime.datetime.now().isoformat(timespec="seconds"), start.isoformat(), underlying,
             strategy, capital_base, str(data.get("notes", "")), initial_structures,
             len(prepared), initial_unit_value))
        oid = cur.lastrowid
        db.executemany(LEG_INSERT, [{**p, "operation_id": oid} for p in prepared])
    return get_operation(oid)


def add_leg(data):
    op_id = to_id(data.get("operation_id"), "Operação")
    entry = parse_date(data.get("entry_date") or datetime.date.today().isoformat(), "Data de entrada")
    leg = parse_leg(data, entry)
    leg["operation_id"] = op_id
    with LOCK, connect() as db:
        if not db.execute("SELECT 1 FROM operations WHERE id=?", (op_id,)).fetchone():
            raise ValueError("Operação não encontrada.")
        db.execute(LEG_INSERT, leg)
    return get_operation(op_id)


def update_capital_base(data):
    op_id = to_id(data.get("operation_id"), "Operação")
    base = number(data.get("capital_base") or 0, "Capital-base", 0)
    with LOCK, connect() as db:
        cur = db.execute("UPDATE operations SET capital_base=? WHERE id=?", (base, op_id))
        if cur.rowcount == 0:
            raise ValueError("Operação não encontrada.")
    return get_operation(op_id)


def add_closeout(data):
    leg_id = to_id(data.get("leg_id"), "Perna")
    close_date = parse_date(data.get("close_date") or datetime.date.today().isoformat(), "Data de encerramento")
    qty = integer(data.get("quantity"), "Quantidade encerrada", 1)
    premium = number(data.get("premium"), "Prêmio de saída", 0)
    costs = number(data.get("costs") or 0, "Custos de saída", 0)
    # Leitura e escrita na mesma transação/lock: evita encerrar mais do que existe.
    with LOCK, connect() as db:
        leg = db.execute("SELECT * FROM legs WHERE id=?", (leg_id,)).fetchone()
        if not leg:
            raise ValueError("Perna não encontrada.")
        if leg["entry_date"] and close_date < datetime.date.fromisoformat(leg["entry_date"]):
            raise ValueError("Data de encerramento anterior à data de entrada da perna.")
        closed = db.execute("SELECT COALESCE(SUM(quantity),0) FROM closeouts WHERE leg_id=?",
                            (leg_id,)).fetchone()[0]
        if qty > leg["quantity"] - closed:
            raise ValueError(f"Quantidade disponível para encerramento: {leg['quantity'] - closed}.")
        db.execute("INSERT INTO closeouts(leg_id,close_date,quantity,premium,costs,notes) VALUES(?,?,?,?,?,?)",
                   (leg_id, close_date.isoformat(), qty, premium, costs, str(data.get("notes", ""))))
        op_id = leg["operation_id"]
    return get_operation(op_id)


def update_mark(data):
    leg_id = to_id(data.get("leg_id"), "Perna")
    mark = data.get("reference_premium")
    if mark in (None, ""):
        mark, d = None, None
    else:
        mark = number(mark, "Preço de referência", 0)
        d = parse_date(data.get("reference_date") or datetime.date.today().isoformat(),
                       "Data de referência").isoformat()
    with LOCK, connect() as db:
        row = db.execute("SELECT operation_id FROM legs WHERE id=?", (leg_id,)).fetchone()
        if not row:
            raise ValueError("Perna não encontrada.")
        db.execute("UPDATE legs SET reference_premium=?,reference_date=? WHERE id=?", (mark, d, leg_id))
        op_id = row["operation_id"]
    return get_operation(op_id)


def check_dates(db, op_id):
    """Confere se as datas da operação continuam coerentes depois de uma edição."""
    if db.execute("SELECT 1 FROM legs WHERE operation_id=? AND expiry < entry_date LIMIT 1",
                  (op_id,)).fetchone():
        raise ValueError("O vencimento de alguma perna ficaria anterior à data de entrada.")
    if db.execute("SELECT 1 FROM closeouts c JOIN legs l ON l.id=c.leg_id "
                  "WHERE l.operation_id=? AND c.close_date < l.entry_date LIMIT 1", (op_id,)).fetchone():
        raise ValueError("Há encerramento com data anterior à entrada da perna.")


def recompute_initial(db, op_id):
    """Recalcula os valores da montagem depois que pernas foram editadas ou removidas.

    A estrutura original são as primeiras `initial_leg_count` pernas (por id).
    """
    row = db.execute("SELECT initial_leg_count FROM operations WHERE id=?", (op_id,)).fetchone()
    legs = [dict(r) for r in db.execute(
        "SELECT side, quantity, entry_premium FROM legs WHERE operation_id=? ORDER BY id", (op_id,))]
    if not row or not legs:
        return
    n = min(int(row["initial_leg_count"] or 0) or len(legs), len(legs))
    initial = legs[:n]
    db.execute("UPDATE operations SET initial_leg_count=?, initial_structures=?, initial_unit_value=? WHERE id=?",
               (n, min(l["quantity"] for l in initial), structure_unit_value(initial)["unit_value"], op_id))


def update_operation(data):
    """Corrige data de montagem, ativo, estratégia e observações."""
    op_id = to_id(data.get("operation_id"), "Operação")
    start = parse_date(data.get("start_date"), "Data de montagem")
    underlying = str(data.get("underlying", "")).strip().upper()
    strategy = str(data.get("strategy", "")).strip()
    if not underlying or not strategy:
        raise ValueError("Preencha ativo e estratégia/estrutura.")
    with LOCK, connect() as db:
        row = db.execute("SELECT start_date FROM operations WHERE id=?", (op_id,)).fetchone()
        if not row:
            raise ValueError("Operação não encontrada.")
        db.execute("UPDATE operations SET start_date=?, underlying=?, strategy=? WHERE id=?",
                   (start.isoformat(), underlying, strategy, op_id))
        if "notes" in data:
            db.execute("UPDATE operations SET notes=? WHERE id=?", (str(data.get("notes") or ""), op_id))
        if row["start_date"] != start.isoformat():
            # As pernas lançadas na data antiga de montagem acompanham a nova data.
            db.execute("UPDATE legs SET entry_date=? WHERE operation_id=? AND entry_date=?",
                       (start.isoformat(), op_id, row["start_date"]))
            check_dates(db, op_id)
    return get_operation(op_id)


def update_leg(data):
    """Corrige qualquer campo de uma perna (a marca de referência é mantida)."""
    leg_id = to_id(data.get("leg_id"), "Perna")
    with LOCK, connect() as db:
        row = db.execute("SELECT * FROM legs WHERE id=?", (leg_id,)).fetchone()
        if not row:
            raise ValueError("Perna não encontrada.")
        entry = parse_date(data.get("entry_date") or row["entry_date"], "Data de entrada")
        leg = parse_leg(data, entry)
        closed, first_close = db.execute(
            "SELECT COALESCE(SUM(quantity),0), MIN(close_date) FROM closeouts WHERE leg_id=?",
            (leg_id,)).fetchone()
        if leg["quantity"] < closed:
            raise ValueError(f"Quantidade: já há {closed} contratos encerrados nesta perna. "
                             "Ajuste ou exclua os encerramentos antes de reduzir.")
        if first_close and first_close < entry.isoformat():
            raise ValueError("Data de entrada posterior a um encerramento já registrado nesta perna.")
        db.execute("""UPDATE legs SET code=:code, option_type=:option_type, strike=:strike, expiry=:expiry,
                      side=:side, quantity=:quantity, entry_premium=:entry_premium, multiplier=:multiplier,
                      entry_cost=:entry_cost, entry_date=:entry_date WHERE id=:leg_id""",
                   {**leg, "leg_id": leg_id})
        recompute_initial(db, row["operation_id"])
        op_id = row["operation_id"]
    return get_operation(op_id)


def delete_leg(data):
    """Exclui uma perna e os encerramentos dela. A operação mantém ao menos uma perna."""
    leg_id = to_id(data.get("leg_id"), "Perna")
    with LOCK, connect() as db:
        row = db.execute("SELECT operation_id FROM legs WHERE id=?", (leg_id,)).fetchone()
        if not row:
            raise ValueError("Perna não encontrada.")
        op_id = row["operation_id"]
        ids = [r["id"] for r in db.execute("SELECT id FROM legs WHERE operation_id=? ORDER BY id", (op_id,))]
        if len(ids) <= 1:
            raise ValueError("A operação precisa ter ao menos uma perna. Para remover tudo, exclua a operação.")
        n = int(db.execute("SELECT initial_leg_count FROM operations WHERE id=?", (op_id,)).fetchone()[0] or 0)
        if ids.index(leg_id) < n:
            # Era uma perna da estrutura original.
            db.execute("UPDATE operations SET initial_leg_count=? WHERE id=?", (max(0, n - 1), op_id))
        db.execute("DELETE FROM legs WHERE id=?", (leg_id,))  # encerramentos saem em cascata
        recompute_initial(db, op_id)
    return get_operation(op_id)


def update_closeout(data):
    """Corrige data, quantidade, prêmio, custos e observação de um encerramento."""
    cid = to_id(data.get("closeout_id"), "Encerramento")
    close_date = parse_date(data.get("close_date"), "Data de encerramento")
    qty = integer(data.get("quantity"), "Quantidade encerrada", 1)
    premium = number(data.get("premium"), "Prêmio de saída", 0)
    costs = number(data.get("costs") or 0, "Custos de saída", 0)
    with LOCK, connect() as db:
        c = db.execute("SELECT * FROM closeouts WHERE id=?", (cid,)).fetchone()
        if not c:
            raise ValueError("Encerramento não encontrado.")
        leg = db.execute("SELECT * FROM legs WHERE id=?", (c["leg_id"],)).fetchone()
        others = db.execute("SELECT COALESCE(SUM(quantity),0) FROM closeouts WHERE leg_id=? AND id<>?",
                            (leg["id"], cid)).fetchone()[0]
        if qty > leg["quantity"] - others:
            raise ValueError(f"Quantidade disponível para este encerramento: {leg['quantity'] - others}.")
        if leg["entry_date"] and close_date < datetime.date.fromisoformat(leg["entry_date"]):
            raise ValueError("Data de encerramento anterior à data de entrada da perna.")
        notes = str(data["notes"]) if "notes" in data else c["notes"]
        db.execute("UPDATE closeouts SET close_date=?, quantity=?, premium=?, costs=?, notes=? WHERE id=?",
                   (close_date.isoformat(), qty, premium, costs, notes, cid))
        op_id = leg["operation_id"]
    return get_operation(op_id)


def delete_closeout(data):
    cid = to_id(data.get("closeout_id"), "Encerramento")
    with LOCK, connect() as db:
        row = db.execute("SELECT l.operation_id FROM closeouts c JOIN legs l ON l.id=c.leg_id WHERE c.id=?",
                         (cid,)).fetchone()
        if not row:
            raise ValueError("Encerramento não encontrado.")
        db.execute("DELETE FROM closeouts WHERE id=?", (cid,))
        op_id = row["operation_id"]
    return get_operation(op_id)


def delete_operation(op_id):
    with LOCK, connect() as db:
        cur = db.execute("DELETE FROM operations WHERE id=?", (op_id,))
        if cur.rowcount == 0:
            raise HttpError(404, "Operação não encontrada.")
    return {"deleted": True}


def import_backup(data):
    """Importa um backup exportado. Tudo ou nada: qualquer erro desfaz a importação.

    Operações cujo id já existe são puladas (com suas pernas e encerramentos).
    Conflitos de id em pernas/encerramentos ou referências quebradas abortam.
    """
    if not isinstance(data, dict):
        raise ValueError("Arquivo de backup inválido.")
    counts = {"operations": 0, "legs": 0, "closeouts": 0, "skipped_operations": 0}
    try:
        with LOCK, connect() as db:
            existing = {r["id"] for r in db.execute("SELECT id FROM operations")}
            skipped_ops, skipped_legs = set(), set()
            for o in data.get("operations", []):
                if o["id"] in existing:
                    skipped_ops.add(o["id"])
                    counts["skipped_operations"] += 1
                    continue
                db.execute(
                    """INSERT INTO operations(id,created_at,start_date,underlying,strategy,capital_base,
                                              notes,initial_structures,initial_leg_count,initial_unit_value)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (o["id"], o.get("created_at") or datetime.datetime.now().isoformat(),
                     o["start_date"], o["underlying"], o["strategy"], o.get("capital_base") or 0,
                     o.get("notes") or "", o.get("initial_structures") or 0,
                     o.get("initial_leg_count") or 0, o.get("initial_unit_value")))
                counts["operations"] += 1
            for l in data.get("legs", []):
                if l["operation_id"] in skipped_ops:
                    skipped_legs.add(l["id"])
                    continue
                db.execute(
                    """INSERT INTO legs(id,operation_id,code,option_type,strike,expiry,side,quantity,
                                        entry_premium,multiplier,entry_cost,entry_date,
                                        reference_premium,reference_date)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (l["id"], l["operation_id"], l.get("code") or "", l["option_type"], l["strike"],
                     l["expiry"], l["side"], l["quantity"], l["entry_premium"], l.get("multiplier") or 1,
                     l.get("entry_cost") or 0, l.get("entry_date") or "",
                     l.get("reference_premium"), l.get("reference_date")))
                counts["legs"] += 1
            for c in data.get("closeouts", []):
                if c["leg_id"] in skipped_legs:
                    continue
                db.execute(
                    "INSERT INTO closeouts(id,leg_id,close_date,quantity,premium,costs,notes) VALUES(?,?,?,?,?,?,?)",
                    (c["id"], c["leg_id"], c["close_date"], c["quantity"], c["premium"],
                     c.get("costs") or 0, c.get("notes") or ""))
                counts["closeouts"] += 1
            # Só agora que todas as pernas existem.
            backfill(db)
    except KeyError as e:
        raise ValueError(f"Backup inválido: campo ausente {e}.")
    except sqlite3.IntegrityError as e:
        raise ValueError(f"Backup recusado (nada foi importado): {e}.")
    return {"imported": True, **counts}


# --------------------------------------------------------------------------
# Cotação
# --------------------------------------------------------------------------
def quote_asset(symbol):
    """Consulta cotação no Yahoo Finance; só o símbolo é enviado ao provedor."""
    symbol = str(symbol or "").strip().upper()
    if (not symbol or len(symbol) > 32
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789^=._-" for c in symbol)):
        raise ValueError("Informe um símbolo válido para consultar a cotação.")
    cached = _QUOTE_CACHE.get(symbol)
    if cached and time.time() - cached[0] < QUOTE_TTL:
        return cached[1]
    provider_symbol = symbol if "." in symbol or symbol.startswith("^") else symbol + ".SA"
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + quote(provider_symbol, safe="^=._-") + "?" + urlencode({"range": "1d", "interval": "1m"}))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DiarioOpcoes/1.0)"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(chart["error"].get("description") or "O provedor não retornou a cotação.")
    results = chart.get("result") or []
    if not results:
        raise ValueError("Nenhuma cotação encontrada para esse ativo.")
    result = results[0]
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    timestamp = meta.get("regularMarketTime")
    if price is None:
        quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quotes.get("close") or []
        timestamps = result.get("timestamp") or []
        last = next((i for i in range(len(closes) - 1, -1, -1) if closes[i] is not None), None)
        if last is not None:
            price = closes[last]
            timestamp = timestamps[last] if last < len(timestamps) else timestamp
    if price is None:
        raise ValueError("O provedor não retornou um preço disponível para esse ativo.")
    data = {"symbol": symbol, "provider_symbol": provider_symbol, "price": float(price),
            "currency": meta.get("currency") or "BRL", "as_of": timestamp, "source": "Yahoo Finance"}
    _QUOTE_CACHE[symbol] = (time.time(), data)
    return data


# --------------------------------------------------------------------------
# Servidor HTTP
# --------------------------------------------------------------------------
class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


POST_ROUTES = {
    "/api/operations": create_operation,
    "/api/legs": add_leg,
    "/api/capital-base": update_capital_base,
    "/api/closeouts": add_closeout,
    "/api/marks": update_mark,
    "/api/import": import_backup,
    "/api/operations/update": update_operation,
    "/api/legs/update": update_leg,
    "/api/legs/delete": delete_leg,
    "/api/closeouts/update": update_closeout,
    "/api/closeouts/delete": delete_closeout,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def reply(self, status, obj, indent=None):
        self.send(status, json.dumps(obj, ensure_ascii=False, indent=indent))

    def fail(self, e):
        if isinstance(e, HttpError):
            status, msg = e.status, str(e)
        elif isinstance(e, (ValueError, sqlite3.IntegrityError)):
            status, msg = 400, str(e)
        else:
            traceback.print_exc()  # erro de programação: aparece no console
            status, msg = 500, "Erro interno do servidor (veja o console)."
        try:
            self.reply(status, {"error": msg})
        except OSError:
            pass  # cliente desconectou

    def guard(self):
        """Bloqueia DNS rebinding e requisições vindas de outros sites."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "") not in hosts:
            raise HttpError(403, "Host não permitido.")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            raise HttpError(403, "Origem não permitida.")

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Content-Length inválido.")
        if n < 0 or n > MAX_BODY:
            raise HttpError(413, "Corpo da requisição grande demais.")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("O corpo da requisição não é um JSON válido.")

    def do_GET(self):
        try:
            self.guard()
            p = urlparse(self.path)
            if p.path == "/":
                index = ROOT / "index.html"
                if not index.exists():
                    raise HttpError(404, "index.html não encontrado ao lado do app.py.")
                return self.send(200, index.read_bytes(), "text/html; charset=utf-8")
            if p.path in ("/api/quote", "/api/quote/"):
                try:
                    symbol = parse_qs(p.query).get("symbol", [""])[0]
                    return self.reply(200, quote_asset(symbol))
                except ValueError as e:
                    return self.reply(400, {"error": str(e)})
                except Exception as e:
                    return self.reply(502, {"error": f"Falha na consulta online: {e}"})
            if p.path == "/api/dashboard":
                return self.reply(200, dashboard())
            if p.path.startswith("/api/operations/"):
                raw_id = p.path.rsplit("/", 1)[-1]
                if not raw_id.isdigit():
                    raise HttpError(400, "Identificador de operação inválido.")
                operation = get_operation(int(raw_id))
                if operation is None:
                    raise HttpError(404, "Operação não encontrada.")
                return self.reply(200, operation)
            if p.path == "/api/export":
                return self.reply(200, {
                    "operations": all_rows("SELECT * FROM operations ORDER BY id"),
                    "legs": all_rows("SELECT * FROM legs ORDER BY id"),
                    "closeouts": all_rows("SELECT * FROM closeouts ORDER BY id"),
                    "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
                }, indent=2)
            if p.path == "/api/health":
                return self.reply(200, {"ok": True, "version": APP_VERSION,
                                        "file": str(Path(__file__).resolve()),
                                        "port": self.server.server_address[1]})
            raise HttpError(404, "Rota não encontrada.")
        except Exception as e:
            self.fail(e)

    def do_POST(self):
        try:
            self.guard()
            handler = POST_ROUTES.get(urlparse(self.path).path)
            if handler is None:
                raise HttpError(404, "Rota não encontrada.")
            return self.reply(200, handler(self.read_json()))
        except Exception as e:
            self.fail(e)

    def do_DELETE(self):
        try:
            self.guard()
            p = urlparse(self.path)
            if p.path != "/api/operations":
                raise HttpError(404, "Rota não encontrada.")
            oid = to_id(parse_qs(p.query).get("id", ["0"])[0], "Operação")
            return self.reply(200, delete_operation(oid))
        except Exception as e:
            self.fail(e)


def watch_files_and_reload(server):
    """Reinicia o processo quando app.py ou index.html mudar (só com DIARIO_DEV=1)."""
    files = [Path(__file__).resolve(), (ROOT / "index.html").resolve()]
    watched = {f: f.stat().st_mtime for f in files if f.exists()}
    while True:
        time.sleep(0.8)
        for path, old_mtime in list(watched.items()):
            try:
                new_mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if new_mtime != old_mtime:
                print(f"Arquivo alterado: {path.name}")
                print("Reiniciando o servidor com a nova versão...")
                server.shutdown()
                server.server_close()  # libera a porta antes de reiniciar
                os.environ["DIARIO_PORT"] = str(server.server_address[1])
                os.environ["DIARIO_RELOADED"] = "1"  # não reabre o navegador
                os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    init_db()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # Porta ocupada/reservada (comum no Windows): usa uma porta livre.
        server = ThreadingHTTPServer((HOST, 0), Handler)
    url = f"http://{HOST}:{server.server_address[1]}"
    print(f"Diário de opções disponível em {url} — encerre com Ctrl+C.")
    if DEV_RELOAD:
        threading.Thread(target=watch_files_and_reload, args=(server,), daemon=True).start()
    if os.environ.get("DIARIO_RELOADED") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor local encerrado.")
    finally:
        server.server_close()
