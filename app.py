#!/usr/bin/env python3
"""Diário local de opções — servidor HTTP e SQLite, sem dependências externas."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, urlencode
import json
import sqlite3
import datetime
import threading
import webbrowser
import urllib.request
import os

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "diario_opcoes.sqlite3"
HOST, PORT = "127.0.0.1", int(os.environ.get("DIARIO_PORT", "5050"))
DB_PATH.parent.mkdir(exist_ok=True)
LOCK = threading.RLock()


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS operations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
          start_date TEXT NOT NULL, underlying TEXT NOT NULL, strategy TEXT NOT NULL,
          capital_base REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
          initial_structures INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS legs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
          code TEXT NOT NULL DEFAULT '', option_type TEXT NOT NULL CHECK(option_type IN ('CALL','PUT')),
          strike REAL NOT NULL, expiry TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
          quantity INTEGER NOT NULL CHECK(quantity > 0), entry_premium REAL NOT NULL CHECK(entry_premium >= 0),
          multiplier REAL NOT NULL DEFAULT 1 CHECK(multiplier > 0), entry_cost REAL NOT NULL DEFAULT 0,
          entry_date TEXT NOT NULL DEFAULT '', reference_premium REAL, reference_date TEXT
        );
        CREATE TABLE IF NOT EXISTS closeouts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, leg_id INTEGER NOT NULL REFERENCES legs(id) ON DELETE CASCADE,
          close_date TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
          premium REAL NOT NULL CHECK(premium >= 0), costs REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_legs_operation ON legs(operation_id);
        CREATE INDEX IF NOT EXISTS idx_closeouts_leg ON closeouts(leg_id);
        """)
        # Migra bancos antigos sem apagar nem substituir os dados existentes.
        opcols = {r['name']
                  for r in db.execute('PRAGMA table_info(operations)')}
        if 'initial_structures' not in opcols:
            db.execute(
                'ALTER TABLE operations ADD COLUMN initial_structures INTEGER NOT NULL DEFAULT 0')
        legcols = {r['name'] for r in db.execute('PRAGMA table_info(legs)')}
        if 'entry_date' not in legcols:
            db.execute(
                "ALTER TABLE legs ADD COLUMN entry_date TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE legs SET entry_date=(SELECT start_date FROM operations WHERE operations.id=legs.operation_id) WHERE entry_date='' OR entry_date IS NULL")
        db.execute("UPDATE operations SET initial_structures=(SELECT COALESCE(MIN(quantity),0) FROM legs WHERE legs.operation_id=operations.id) WHERE initial_structures=0")


def all_rows(sql, params=()):
    with connect() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def number(v, name, minimum=None):
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: informe um número válido.")
    if minimum is not None and n < minimum:
        raise ValueError(f"{name}: deve ser pelo menos {minimum}.")
    return n


def get_operation(op_id):
    ops = all_rows("SELECT * FROM operations WHERE id=?", (op_id,))
    if not ops:
        return None
    op = ops[0]
    legs = all_rows(
        "SELECT * FROM legs WHERE operation_id=? ORDER BY id", (op_id,))
    total_realized = 0.0
    any_closed = False
    any_remaining = False
    for leg in legs:
        closes = all_rows(
            "SELECT * FROM closeouts WHERE leg_id=? ORDER BY close_date,id", (leg['id'],))
        closed_qty = sum(c['quantity'] for c in closes)
        if closed_qty > leg['quantity']:
            raise ValueError(
                "A quantidade encerrada ultrapassa a quantidade de entrada.")
        remaining = leg['quantity'] - closed_qty
        direction = 1 if leg['side'] == 'LONG' else -1
        pnl = 0.0
        for c in closes:
            entry_cost_share = leg['entry_cost'] * \
                c['quantity'] / leg['quantity']
            pnl += (c['premium'] - leg['entry_premium']) * direction * \
                c['quantity'] * leg['multiplier'] - \
                entry_cost_share - c['costs']
        unrealized = None
        if remaining and leg['reference_premium'] is not None:
            unclosed_entry_cost = leg['entry_cost'] * \
                remaining / leg['quantity']
            unrealized = (leg['reference_premium'] - leg['entry_premium']) * \
                direction * remaining * leg['multiplier'] - unclosed_entry_cost
        elif remaining:
            unclosed_entry_cost = leg['entry_cost'] * \
                remaining / leg['quantity']
        else:
            unclosed_entry_cost = 0.0
        leg.update(closeouts=closes, closed_quantity=closed_qty, remaining_quantity=remaining,
                   realized=round(pnl, 6), unrealized=None if unrealized is None else round(unrealized, 6))
        total_realized += pnl
        any_closed |= closed_qty > 0
        any_remaining |= remaining > 0
    op['legs'] = legs
    # Retorno: usa a base informada pelo usuário; se ausente, infere apenas
    # o débito líquido de montagem. Créditos não são capital em risco: nesses
    # casos, a base de risco deve ser informada manualmente.
    opening_debit = sum((1 if l['side'] == 'LONG' else -1) * l['entry_premium']
                        * l['quantity'] * l['multiplier'] + l['entry_cost'] for l in legs)
    effective_base = float(op.get('capital_base') or 0)
    base_source = 'informada'
    if effective_base <= 0.005:
        effective_base = max(0.0, opening_debit)
        base_source = 'débito líquido inicial' if effective_base > 0.005 else 'não definida'
    op['opening_net_debit'] = round(max(0.0, opening_debit), 2)
    op['capital_base_effective'] = round(effective_base, 2)
    op['capital_base_source'] = base_source
    op['realized'] = round(total_realized, 2)
    op['unrealized'] = None if any(l['remaining_quantity'] and l['unrealized'] is None for l in legs) else round(
        sum(l['unrealized'] or 0 for l in legs), 2)
    op['remaining_contracts'] = sum(l['remaining_quantity'] for l in legs)
    # Pernas históricas zeradas permanecem no registro para documentar rolagens.
    active_quantities = [l['remaining_quantity']
                         for l in legs if l['remaining_quantity'] > 0]
    op['structure_count'] = min(active_quantities, default=0)
    op['initial_structures'] = op.get('initial_structures') or min(
        (l['quantity'] for l in legs), default=0)
    op['closed_structure_count'] = min(
        (l['closed_quantity'] for l in legs), default=0)
    op['status'] = 'ENCERRADA' if not any_remaining else (
        'PARCIAL' if any_closed else 'ABERTA')
    op['closed'] = not any_remaining
    op['estimated_total'] = None if op['unrealized'] is None else round(
        op['realized'] + op['unrealized'], 2)
    op['return_pct'] = round(
        op['realized'] / effective_base * 100, 2) if effective_base > 0 else None
    op['estimated_return_pct'] = round(op['estimated_total'] / effective_base *
                                       100, 2) if op['estimated_total'] is not None and effective_base > 0 else None
    op['closeout_count'] = sum(len(l['closeouts']) for l in legs)
    op['payoff'] = payoff_profile(op)
    return op


def payoff_profile(op):
    """Perfil de payoff no vencimento e referência para centralizar o gráfico.

    Inclui o realizado e as posições ainda abertas, com os custos de entrada
    não alocados às quantidades já encerradas. Break-evens são preços não
    negativos do ativo nos quais esse resultado projetado cruza zero.
    """
    legs = op.get('legs', [])

    def value_at(price):
        total = float(op.get('realized') or 0)
        for leg in legs:
            qty = int(leg.get('remaining_quantity') or 0)
            if not qty:
                continue
            intrinsic = (max(price - leg['strike'], 0) if leg['option_type'] == 'CALL'
                         else max(leg['strike'] - price, 0))
            direction = 1 if leg['side'] == 'LONG' else -1
            entry_cost = float(leg.get('entry_cost') or 0) * \
                qty / leg['quantity']
            total += (intrinsic - leg['entry_premium']) * \
                direction * qty * leg['multiplier'] - entry_cost
        return total

    strikes = sorted({float(l['strike']) for l in legs})
    points = sorted({0.0, *strikes})
    if not points:
        points = [0.0]
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
    slope = sum((1 if l['side'] == 'LONG' else -1) * l['remaining_quantity'] * l['multiplier']
                for l in legs if l['remaining_quantity'] and l['option_type'] == 'CALL')
    if abs(slope) > 1e-12:
        root = last - value_at(last) / slope
        if root > last + 1e-9:
            add_root(root)

    be = sorted(be)
    deduped = []
    for root in be:
        if not deduped or abs(root - deduped[-1]) > 1e-6:
            deduped.append(root)

    if len(deduped) >= 2:
        center = (deduped[0] + deduped[-1]) / 2
        center_basis = 'ponto médio entre break-evens'
    elif deduped:
        center = deduped[0]
        center_basis = 'break-even'
    elif strikes:
        center = (strikes[0] + strikes[-1]) / 2
        center_basis = 'ponto médio do intervalo de strikes'
    else:
        center = 0.0
        center_basis = 'sem strikes cadastrados'

    max_strike = strikes[-1] if strikes else 0.0
    # Faixa informativa simétrica ao redor do centro; a interface pode ampliá-la.
    radius = max((max_strike - min(strikes)) / 2 if len(strikes) > 1 else 0.0,
                 abs(center) * 0.25, max_strike * 0.15, 1.0)
    return {
        'break_even_points': [round(x, 6) for x in deduped],
        'center': round(center, 6),
        'center_basis': center_basis,
        'x_min': round(max(0.0, center - radius), 6),
        'x_max': round(center + radius, 6),
        'strike_min': round(strikes[0], 6) if strikes else None,
        'strike_max': round(max_strike, 6) if strikes else None,
    }


def dashboard():
    operations = [get_operation(o['id']) for o in all_rows(
        "SELECT id FROM operations ORDER BY start_date DESC,id DESC")]
    closed = [o for o in operations if o['closed']]
    wins = [o for o in closed if o['realized'] > 0.005]
    losses = [o for o in closed if o['realized'] < -0.005]
    flat = len(closed) - len(wins) - len(losses)
    groups = {}
    months = {}
    for op in operations:
        key = (op['strategy'], op['underlying'])
        g = groups.setdefault(key, {'strategy': key[0], 'underlying': key[1], 'count': 0, 'structures': 0, 'closed_structures': 0,
                              'open_structures': 0, 'closed': 0, 'realized': 0.0, 'return_realized': 0.0, 'capital_base': 0.0, 'wins': 0, 'losses': 0, 'open': 0})
        g['count'] += 1
        g['structures'] += op['initial_structures']
        g['closed_structures'] += op['closed_structure_count']
        g['open_structures'] += op['structure_count']
        g['realized'] += op['realized']
        g['capital_base'] += op['capital_base_effective']
        g['return_realized'] += op['realized'] if op['capital_base_effective'] > 0 else 0
        g['open'] += not op['closed']
        g['closed'] += op['closed']
        g['wins'] += op['closed'] and op['realized'] > 0.005
        g['losses'] += op['closed'] and op['realized'] < -0.005
        for leg in op['legs']:
            for c in leg['closeouts']:
                m = c['close_date'][:7]
                direction = 1 if leg['side'] == 'LONG' else -1
                pnl = (c['premium'] - leg['entry_premium']) * direction * c['quantity'] * \
                    leg['multiplier'] - leg['entry_cost'] * \
                    c['quantity'] / leg['quantity'] - c['costs']
                mg = months.setdefault(
                    m, {'month': m, 'realized': 0.0, 'closeouts': 0})
                mg['realized'] += pnl
                mg['closeouts'] += 1
    for g in groups.values():
        g['realized'] = round(g['realized'], 2)
        g['return_pct'] = round(
            g['return_realized']/g['capital_base']*100, 2) if g['capital_base'] > 0 else None
    basis_operations = [
        o for o in operations if o['capital_base_effective'] > 0]
    capital_base_total = sum(o['capital_base_effective']
                             for o in basis_operations)
    return_realized_total = sum(o['realized'] for o in basis_operations)
    return_excluded_count = sum(o['capital_base_effective'] <= 0 and abs(
        o['realized']) > 0.005 for o in operations)
    return {'operations': operations, 'closed_count': len(closed), 'win_count': len(wins), 'loss_count': len(losses), 'flat_count': flat,
            'win_rate': round(len(wins)/len(closed)*100, 2) if closed else 0,
            'error_rate': round(len(losses)/len(closed)*100, 2) if closed else 0,
            'realized_total': round(sum(o['realized'] for o in operations), 2),
            'capital_base_total': round(capital_base_total, 2),
            'financial_return_pct': round(return_realized_total/capital_base_total*100, 2) if capital_base_total > 0 else None,
            'capital_base_count': len(basis_operations), 'capital_base_missing_count': return_excluded_count,
            'avg_win': round(sum(o['realized'] for o in wins)/len(wins), 2) if wins else 0,
            'avg_loss': round(sum(o['realized'] for o in losses)/len(losses), 2) if losses else 0,
            'open_count': sum(not o['closed'] for o in operations), 'open_structures': sum(o['structure_count'] for o in operations),
            'closed_structures': sum(o['closed_structure_count'] for o in operations),
            'groups': sorted(groups.values(), key=lambda x: (x['strategy'], x['underlying'])),
            'months': sorted([{'month': k, 'realized': round(v['realized'], 2), 'closeouts': v['closeouts']} for k, v in months.items()], key=lambda x: x['month'])}


def create_operation(data):
    legs = data.get('legs') or []
    if len(legs) < 1:
        raise ValueError('Inclua ao menos uma perna.')
    start_date = data.get('start_date') or datetime.date.today().isoformat()
    datetime.date.fromisoformat(start_date)
    underlying = str(data.get('underlying', '')).strip().upper()
    strategy = str(data.get('strategy', '')).strip()
    if not underlying or not strategy:
        raise ValueError('Preencha ativo e estratégia/estrutura.')
    capital_base = number(data.get('capital_base') or 0, 'Capital-base', 0)
    prepared = []
    for i, l in enumerate(legs, 1):
        typ = l.get('option_type')
        side = l.get('side')
        if typ not in ('CALL', 'PUT') or side not in ('LONG', 'SHORT'):
            raise ValueError(f'Perna {i}: selecione tipo e lado válidos.')
        expiry = l.get('expiry')
        datetime.date.fromisoformat(expiry)
        qty = number(l.get('quantity'), 'Quantidade', 1)
        if not qty.is_integer():
            raise ValueError(f'Perna {i}: quantidade deve ser inteira.')
        prepared.append((str(l.get('code', '')).strip(), typ, number(l.get('strike'), 'Strike', 0), expiry, side, int(qty), number(l.get('entry_premium'), 'Prêmio de entrada', 0), number(l.get('multiplier') or 1, 'Multiplicador', 0.000001), number(l.get('entry_cost') or 0, 'Custos de entrada', 0),
                        None if l.get('reference_premium') in (None, '') else number(l.get('reference_premium'), 'Preço de referência', 0), l.get('reference_date') or None))
    with LOCK, connect() as db:
        initial_structures = min((p[5] for p in prepared), default=0)
        cur = db.execute('INSERT INTO operations(created_at,start_date,underlying,strategy,capital_base,notes,initial_structures) VALUES(?,?,?,?,?,?,?)',
                         (datetime.datetime.now().isoformat(timespec='seconds'), start_date, underlying, strategy, capital_base, str(data.get('notes', '')), initial_structures))
        oid = cur.lastrowid
        db.executemany('INSERT INTO legs(operation_id,code,option_type,strike,expiry,side,quantity,entry_premium,multiplier,entry_cost,entry_date,reference_premium,reference_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', [
                       (oid, *p[:9], start_date, *p[9:]) for p in prepared])
    return get_operation(oid)


def add_leg(data):
    op_id = int(data.get('operation_id') or 0)
    ops = all_rows('SELECT * FROM operations WHERE id=?', (op_id,))
    if not ops:
        raise ValueError('Operação não encontrada.')
    typ = data.get('option_type')
    side = data.get('side')
    if typ not in ('CALL', 'PUT') or side not in ('LONG', 'SHORT'):
        raise ValueError('Selecione tipo e lado válidos.')
    entry_date = data.get('entry_date') or datetime.date.today().isoformat()
    datetime.date.fromisoformat(entry_date)
    expiry = data.get('expiry')
    datetime.date.fromisoformat(expiry)
    qty = number(data.get('quantity'), 'Quantidade', 1)
    if not qty.is_integer():
        raise ValueError('Quantidade deve ser inteira.')
    leg = (op_id, str(data.get('code', '')).strip(), typ, number(data.get('strike'), 'Strike', 0), expiry, side, int(qty), number(data.get('entry_premium'), 'Prêmio de entrada', 0), number(data.get('multiplier') or 1, 'Multiplicador', 0.000001), number(data.get('entry_cost') or 0, 'Custos de entrada', 0), entry_date,
           None if data.get('reference_premium') in (None, '') else number(data.get('reference_premium'), 'Preço de referência', 0), data.get('reference_date') or None)
    with LOCK, connect() as db:
        db.execute('INSERT INTO legs(operation_id,code,option_type,strike,expiry,side,quantity,entry_premium,multiplier,entry_cost,entry_date,reference_premium,reference_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', leg)
    return get_operation(op_id)


def update_capital_base(data):
    op_id = int(data.get('operation_id') or 0)
    if not all_rows('SELECT id FROM operations WHERE id=?', (op_id,)):
        raise ValueError('Operação não encontrada.')
    base = number(data.get('capital_base') or 0, 'Capital-base', 0)
    with LOCK, connect() as db:
        db.execute(
            'UPDATE operations SET capital_base=? WHERE id=?', (base, op_id))
    return get_operation(op_id)


def add_closeout(data):
    leg_id = int(data.get('leg_id') or 0)
    close_date = data.get('close_date') or datetime.date.today().isoformat()
    datetime.date.fromisoformat(close_date)
    qty = number(data.get('quantity'), 'Quantidade encerrada', 1)
    if not qty.is_integer():
        raise ValueError('Quantidade encerrada deve ser inteira.')
    premium = number(data.get('premium'), 'Prêmio de saída', 0)
    costs = number(data.get('costs') or 0, 'Custos de saída', 0)
    legrow = all_rows('SELECT * FROM legs WHERE id=?', (leg_id,))
    if not legrow:
        raise ValueError('Perna não encontrada.')
    leg = legrow[0]
    closed = sum(r['quantity'] for r in all_rows(
        'SELECT quantity FROM closeouts WHERE leg_id=?', (leg_id,)))
    if qty > leg['quantity']-closed:
        raise ValueError(
            f"Quantidade disponível para encerramento: {leg['quantity']-closed}.")
    with LOCK, connect() as db:
        db.execute('INSERT INTO closeouts(leg_id,close_date,quantity,premium,costs,notes) VALUES(?,?,?,?,?,?)',
                   (leg_id, close_date, int(qty), premium, costs, str(data.get('notes', ''))))
    return get_operation(leg['operation_id'])


def update_mark(data):
    leg_id = int(data.get('leg_id') or 0)
    rows = all_rows('SELECT operation_id FROM legs WHERE id=?', (leg_id,))
    if not rows:
        raise ValueError('Perna não encontrada.')
    mark = data.get('reference_premium')
    d = data.get('reference_date') or datetime.date.today().isoformat()
    if mark in (None, ''):
        mark = None
        d = None
    else:
        mark = number(mark, 'Preço de referência', 0)
        datetime.date.fromisoformat(d)
    with LOCK, connect() as db:
        db.execute(
            'UPDATE legs SET reference_premium=?,reference_date=? WHERE id=?', (mark, d, leg_id))
    return get_operation(rows[0]['operation_id'])


def quote_asset(symbol):
    """Consulta cotação no Yahoo Finance; só o símbolo é enviado ao provedor."""
    symbol = str(symbol or '').strip().upper()
    if not symbol or len(symbol) > 32 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789^=._-' for c in symbol):
        raise ValueError('Informe um símbolo válido para consultar a cotação.')
    provider_symbol = symbol if '.' in symbol or symbol.startswith(
        '^') else symbol + '.SA'
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + \
        quote(provider_symbol, safe='^=._-') + '?' + \
        urlencode({'range': '1d', 'interval': '1m'})
    request = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (compatible; DiarioOpcoes/1.0)'})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode('utf-8'))
    chart = payload.get('chart') or {}
    if chart.get('error'):
        raise ValueError(chart['error'].get('description')
                         or 'O provedor não retornou a cotação.')
    results = chart.get('result') or []
    if not results:
        raise ValueError('Nenhuma cotação encontrada para esse ativo.')
    result = results[0]
    meta = result.get('meta') or {}
    price = meta.get('regularMarketPrice')
    timestamp = meta.get('regularMarketTime')
    if price is None:
        indicators = result.get('indicators') or {}
        quotes = (indicators.get('quote') or [{}])[0]
        closes = quotes.get('close') or []
        timestamps = result.get('timestamp') or []
        last = next((i for i in range(len(closes) - 1, -1, -1)
                    if closes[i] is not None), None)
        if last is not None:
            price = closes[last]
            timestamp = timestamps[last] if last < len(
                timestamps) else timestamp
    if price is None:
        raise ValueError(
            'O provedor não retornou um preço disponível para esse ativo.')
    return {'symbol': symbol, 'provider_symbol': provider_symbol, 'price': float(price),
            'currency': meta.get('currency') or 'BRL', 'as_of': timestamp, 'source': 'Yahoo Finance'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def send(self, status, body, ctype='application/json; charset=utf-8'):
        raw = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        n = int(self.headers.get('Content-Length', '0'))
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        p = urlparse(self.path)
        try:
            if p.path == '/':
                return self.send(200, (ROOT/'index.html').read_bytes(), 'text/html; charset=utf-8')
            # Rota usada pela interface para obter a cotação do ativo.
            if p.path in ('/api/quote', '/api/quote/'):
                try:
                    symbol = parse_qs(p.query).get('symbol', [''])[0]
                    return self.send(200, json.dumps(quote_asset(symbol), ensure_ascii=False))
                except Exception as e:
                    return self.send(502, json.dumps({'error': f'Falha na consulta online: {e}'}, ensure_ascii=False))
            if p.path == '/api/dashboard':
                return self.send(200, json.dumps(dashboard(), ensure_ascii=False))
            if p.path.startswith('/api/operations/'):
                raw_id = p.path.rsplit('/', 1)[-1]
                if not raw_id.isdigit():
                    return self.send(400, json.dumps({'error': 'Identificador de operação inválido.'}, ensure_ascii=False))
                operation = get_operation(int(raw_id))
                if operation is None:
                    return self.send(404, json.dumps({'error': 'Operação não encontrada.'}, ensure_ascii=False))
                return self.send(200, json.dumps(operation, ensure_ascii=False))
            if p.path == '/api/export':
                rows = {'operations': all_rows('SELECT * FROM operations ORDER BY id'), 'legs': all_rows('SELECT * FROM legs ORDER BY id'), 'closeouts': all_rows(
                    'SELECT * FROM closeouts ORDER BY id'), 'exported_at': datetime.datetime.now().isoformat(timespec='seconds')}
                return self.send(200, json.dumps(rows, ensure_ascii=False, indent=2), 'application/json; charset=utf-8')
            if p.path == '/api/health':
                return self.send(200, json.dumps({
                'ok': True,
                'version': APP_VERSION,
                'file': str(Path(__file__).resolve()),
                'port': self.server.server_address[1]
            }, ensure_ascii=False))
            return self.send(404, json.dumps({'error': 'Rota não encontrada.'}, ensure_ascii=False))
        except Exception as e:
            return self.send(500, json.dumps({'error': str(e)}, ensure_ascii=False))

    def do_POST(self):
        try:
            data = self.body()
            p = urlparse(self.path).path
            if p == '/api/operations':
                result = create_operation(data)
            elif p == '/api/legs':
                result = add_leg(data)
            elif p == '/api/capital-base':
                result = update_capital_base(data)
            elif p == '/api/closeouts':
                result = add_closeout(data)
            elif p == '/api/marks':
                result = update_mark(data)
            elif p == '/api/import':
                imported = data
                if not isinstance(imported, dict):
                    raise ValueError('Arquivo de backup inválido.')
                with LOCK, connect() as db:
                    db.execute('BEGIN')
                    for o in imported.get('operations', []):
                        db.execute('INSERT OR IGNORE INTO operations(id,created_at,start_date,underlying,strategy,capital_base,notes,initial_structures) VALUES(?,?,?,?,?,?,?,?)', (o['id'], o.get(
                            'created_at', datetime.datetime.now().isoformat()), o['start_date'], o['underlying'], o['strategy'], o.get('capital_base', 0), o.get('notes', ''), o.get('initial_structures', 0)))
                    for l in imported.get('legs', []):
                        db.execute('INSERT OR IGNORE INTO legs(id,operation_id,code,option_type,strike,expiry,side,quantity,entry_premium,multiplier,entry_cost,entry_date,reference_premium,reference_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', tuple(
                            l.get(k) for k in ('id', 'operation_id', 'code', 'option_type', 'strike', 'expiry', 'side', 'quantity', 'entry_premium', 'multiplier', 'entry_cost', 'entry_date', 'reference_premium', 'reference_date')))
                    for c in imported.get('closeouts', []):
                        db.execute('INSERT OR IGNORE INTO closeouts(id,leg_id,close_date,quantity,premium,costs,notes) VALUES(?,?,?,?,?,?,?)', tuple(
                            c.get(k) for k in ('id', 'leg_id', 'close_date', 'quantity', 'premium', 'costs', 'notes')))
                    db.execute(
                        "UPDATE legs SET entry_date=(SELECT start_date FROM operations WHERE operations.id=legs.operation_id) WHERE entry_date='' OR entry_date IS NULL")
                    db.execute(
                        "UPDATE operations SET initial_structures=(SELECT COALESCE(MIN(quantity),0) FROM legs WHERE legs.operation_id=operations.id) WHERE initial_structures=0")
                    db.commit()
                result = {'imported': True}
            else:
                return self.send(404, json.dumps({'error': 'Rota não encontrada.'}, ensure_ascii=False))
            return self.send(200, json.dumps(result, ensure_ascii=False))
        except Exception as e:
            return self.send(400, json.dumps({'error': str(e)}, ensure_ascii=False))

    def do_DELETE(self):
        p = urlparse(self.path)
        try:
            if p.path != '/api/operations':
                return self.send(404, json.dumps({'error': 'Rota não encontrada.'}, ensure_ascii=False))
            oid = int(parse_qs(p.query).get('id', ['0'])[0])
            with LOCK, connect() as db:
                db.execute('DELETE FROM operations WHERE id=?', (oid,))
            return self.send(200, json.dumps({'deleted': True}))
        except Exception as e:
            return self.send(400, json.dumps({'error': str(e)}, ensure_ascii=False))


if __name__ == '__main__':
    init_db()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # Se a porta preferida estiver ocupada/reservada no Windows, usa uma
        # porta livre atribuída pelo sistema e abre o endereço correto.
        server = ThreadingHTTPServer((HOST, 0), Handler)
    url = f'http://{HOST}:{server.server_address[1]}'
    print(f'Diário de opções disponível em {url} — encerre com Ctrl+C.')
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServidor local encerrado.')
