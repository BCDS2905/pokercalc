"""
PokerCalc — arquivo único
pip install flask pybind11
bash build.sh              →  compila evaluator.cpp
python app.py              →  http://localhost:8080
python app.py --test       →  roda os testes
"""
import sys, random, itertools, threading, uuid, time, os, secrets
from collections import Counter, defaultdict
from flask import Flask, request, jsonify, Response, redirect

# ─── Tenta carregar avaliador C++ (20x mais rápido) ──────
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

try:
    import evaluator as _cpp
    _USE_CPP = True
    print("  ♠  Modo turbo: C++ carregado com sucesso!")
except ImportError:
    _USE_CPP = False
    print(f"  ⚠  evaluator.so não encontrado em {_APP_DIR} — usando Python puro")

app = Flask(__name__)

# ─── SEGURANÇA ─────────────────────────────────────────
# SECRET_KEY: gerado automaticamente se não definido no ambiente
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Headers de segurança em todas as respostas
import gzip, io

@app.after_request
def set_security_headers(response):
    # ── CORS — permite requisições do Vercel e pokercalc.com.br ──
    origin = request.headers.get('Origin', '')
    allowed_origins = ['pokercalc.com.br', 'www.pokercalc.com.br', 'vercel.app', 'localhost', '127.0.0.1']
    if any(a in origin for a in allowed_origins):
        response.headers['Access-Control-Allow-Origin']  = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'

    # ── Segurança ──
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    if os.environ.get('FLASK_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers.pop('Server', None)
    response.headers['X-Powered-By'] = 'PokerCalc'

    # ── Cache-Control por tipo de conteúdo ──
    path = request.path
    if path in ('/favicon.svg', '/favicon.ico'):
        # favicon: cache por 7 dias
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    elif path == '/healthcheck':
        # healthcheck: sem cache
        response.headers['Cache-Control'] = 'no-store'
    elif request.method == 'GET' and path == '/':
        # HTML principal: revalidar sempre (conteúdo pode mudar com deploy)
        response.headers['Cache-Control'] = 'public, max-age=300, must-revalidate'
    elif request.method in ('POST',):
        # APIs de cálculo: sem cache
        response.headers['Cache-Control'] = 'no-store'

    # ── Compressão Gzip ──
    # Comprime respostas HTML/JSON grandes se o cliente aceitar
    if (response.status_code == 200
            and 'gzip' in request.headers.get('Accept-Encoding', '')
            and not response.direct_passthrough
            and len(response.get_data()) > 1000):
        content_type = response.content_type.lower()
        if any(t in content_type for t in ('text/', 'application/json', 'image/svg')):
            buf = io.BytesIO()
            with gzip.GzipFile(mode='wb', fileobj=buf, compresslevel=6) as gz:
                gz.write(response.get_data())
            response.set_data(buf.getvalue())
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length']   = len(response.get_data())
            response.headers['Vary']              = 'Accept-Encoding'

    return response

# CORS: só permite requisições da própria origem
@app.before_request
def check_origin():
    if request.method == 'POST':
        origin = request.headers.get('Origin', '')
        host   = request.headers.get('Host', '')
        allowed = ['localhost', '127.0.0.1', host,
                   'pokercalc.com.br', 'www.pokercalc.com.br',
                   'vercel.app', 'onrender.com']
        if origin and not any(a in origin for a in allowed):
            return jsonify({'error': 'Origem não permitida.'}), 403
# Limita cada IP a 30 requisições por minuto nas rotas de cálculo
_RATE   = defaultdict(list)   # ip → [timestamps]
_RATE_WINDOW  = 60            # janela em segundos
_RATE_MAX     = 30            # máx requisições por janela

def _check_rate(ip):
    """Retorna True se o IP está dentro do limite, False se excedeu."""
    now    = time.time()
    times  = _RATE[ip]
    # remove timestamps fora da janela
    _RATE[ip] = [t for t in times if now - t < _RATE_WINDOW]
    if len(_RATE[ip]) >= _RATE_MAX:
        return False
    _RATE[ip].append(now)
    return True

RANKS     = ['2','3','4','5','6','7','8','9','T','J','Q','K','A']
SUITS     = ['s','h','d','c']
RANK_VAL  = {r: i for i, r in enumerate(RANKS)}
FULL_DECK = [r+s for r in RANKS for s in SUITS]

# ─── CACHE ────────────────────────────────────
_CACHE = {}; _CACHE_MAX = 512

def _ckey(hole, board, opp, sims):
    return (tuple(sorted(hole)), tuple(sorted(board)), opp, sims)

def cache_get(k):   return _CACHE.get(k)
def cache_set(k,v):
    if len(_CACHE) >= _CACHE_MAX: del _CACHE[next(iter(_CACHE))]
    _CACHE[k] = v

# ─── JOBS ─────────────────────────────────────
_JOBS = {}; _TTL = 300; _JOBS_TTL = _TTL  # jobs ficam 5 minutos em memória

def _prune():
    now = time.time()
    for jid in [k for k,v in list(_JOBS.items()) if now-v['ts']>_TTL]: del _JOBS[jid]

def new_job():
    _prune(); jid = str(uuid.uuid4())[:8]
    _JOBS[jid] = {'status':'running','progress':0,'result':None,'partial':None,'error':None,'ts':time.time()}
    return jid

def job_done(jid, r):
    if jid in _JOBS: _JOBS[jid].update({'status':'done','progress':100,'result':r,'ts':time.time()})
def job_fail(jid, e):
    if jid in _JOBS: _JOBS[jid].update({'status':'error','error':e,'ts':time.time()})
def job_prog(jid, p):
    if jid in _JOBS: _JOBS[jid]['progress'] = p
def job_partial(jid, r):
    if jid in _JOBS: _JOBS[jid]['partial'] = r

# ─── AVALIADOR OTIMIZADO (bitmask, ~2x mais rápido) ──
SUIT_IDX  = {'s':0,'h':1,'d':2,'c':3}
CARD_INT  = {c: RANK_VAL[c[:-1]]*4+SUIT_IDX[c[-1]] for c in FULL_DECK}
INT_RANK  = [i//4 for i in range(52)]
INT_SUIT  = [i%4  for i in range(52)]
COMBOS7   = list(itertools.combinations(range(7),5))
_STRAIGHTS = set()
for _s in range(9): _STRAIGHTS.add(0b11111<<_s)
_STRAIGHTS.add(0b1000000001111)  # Wheel A-2-3-4-5

def eval5_int(c0,c1,c2,c3,c4):
    r0=INT_RANK[c0];r1=INT_RANK[c1];r2=INT_RANK[c2];r3=INT_RANK[c3];r4=INT_RANK[c4]
    s0=INT_SUIT[c0];s1=INT_SUIT[c1];s2=INT_SUIT[c2];s3=INT_SUIT[c3];s4=INT_SUIT[c4]
    fl=s0==s1==s2==s3==s4
    mask=(1<<r0)|(1<<r1)|(1<<r2)|(1<<r3)|(1<<r4)
    st=mask in _STRAIGHTS
    cnt=[0]*13
    cnt[r0]+=1;cnt[r1]+=1;cnt[r2]+=1;cnt[r3]+=1;cnt[r4]+=1
    fours=threes=pairs=0;top4=top3=top2a=top2b=-1
    for rank in range(12,-1,-1):
        c=cnt[rank]
        if c==4:   fours+=1;top4=rank
        elif c==3: threes+=1;top3=rank
        elif c==2:
            if pairs==0: top2a=rank
            else: top2b=rank
            pairs+=1
    sh=3 if(st and mask==0b1000000001111) else(max(r0,r1,r2,r3,r4) if st else 0)
    if st and fl: return(8,sh,0,0,0,0)
    if fours:
        k=max(r for r in(r0,r1,r2,r3,r4) if r!=top4)
        return(7,top4,k,0,0,0)
    if threes and pairs: return(6,top3,top2a,0,0,0)
    if fl:
        sr=sorted([r0,r1,r2,r3,r4],reverse=True)
        return(5,sr[0],sr[1],sr[2],sr[3],sr[4])
    if st: return(4,sh,0,0,0,0)
    if threes:
        kr=sorted([r for r in(r0,r1,r2,r3,r4) if r!=top3],reverse=True)
        return(3,top3,kr[0],kr[1],0,0)
    if pairs>=2:
        k=max(r for r in(r0,r1,r2,r3,r4) if r!=top2a and r!=top2b)
        return(2,max(top2a,top2b),min(top2a,top2b),k,0,0)
    if pairs==1:
        kr=sorted([r for r in(r0,r1,r2,r3,r4) if r!=top2a],reverse=True)
        return(1,top2a,kr[0],kr[1],kr[2],0)
    sr=sorted([r0,r1,r2,r3,r4],reverse=True)
    return(0,sr[0],sr[1],sr[2],sr[3],sr[4])

def hand_rank_int(card_ints):
    n=len(card_ints)
    if n==5: return eval5_int(*card_ints)
    combos=COMBOS7 if n==7 else list(itertools.combinations(range(n),5))
    ci=card_ints
    return max(eval5_int(ci[i],ci[j],ci[k],ci[l],ci[m]) for i,j,k,l,m in combos)

# Mantém interface original (strings) para get_draws, beating_hands, testes
def evaluate_five(cards):
    ranks = sorted([RANK_VAL[c[:-1]] for c in cards], reverse=True)
    suits = [c[-1] for c in cards]
    fl = len(set(suits)) == 1
    st = (ranks[0]-ranks[4]==4 and len(set(ranks))==5) or ranks==[12,3,2,1,0]
    if st and ranks==[12,3,2,1,0]: ranks=[3,2,1,0,-1]
    counts = Counter(ranks)
    freq   = sorted(counts.values(), reverse=True)
    groups = sorted(counts.keys(), key=lambda r:(counts[r],r), reverse=True)
    if   st and fl:       cat=8
    elif freq==[4,1]:     cat=7
    elif freq==[3,2]:     cat=6
    elif fl:              cat=5
    elif st:              cat=4
    elif freq[0]==3:      cat=3
    elif freq[:2]==[2,2]: cat=2
    elif freq[0]==2:      cat=1
    else:                 cat=0
    return (cat, groups)

def hand_rank(cards):
    return max(evaluate_five(list(c)) for c in itertools.combinations(cards, 5))

def label_pre(hole):
    r1,r2=hole[0][:-1],hole[1][:-1]; s1,s2=hole[0][-1],hole[1][-1]
    if RANK_VAL[r1]<RANK_VAL[r2]: r1,r2,s1,s2=r2,r1,s2,s1
    d=lambda r:'10' if r=='T' else r
    if r1==r2: return f"Par de {d(r1)}"
    return f"{d(r1)}{d(r2)} {'Suited' if s1==s2 else 'Offsuit'}"

def label_board(hole, board):
    return ['Carta Alta','Um Par','Dois Pares','Trinca','Sequência',
            'Flush','Full House','Quadra','Straight Flush'][hand_rank(hole+board)[0]]

# ─── MONTE CARLO (C++ ou Python) ──────────────
def monte_carlo(hole, board, opponents=1, n=10000, cb=None):
    if _USE_CPP:
        res = _cpp.monte_carlo(hole, board, opponents, n)
        if cb: cb(90)
        return {'win': res[0], 'tie': res[1], 'lose': res[2]}
    # Fallback Python
    base   = [CARD_INT[c] for c in FULL_DECK if c not in hole+board]
    hole_i = [CARD_INT[c] for c in hole]
    board_i= [CARD_INT[c] for c in board]
    need=5-len(board); wins=ties=losses=0; step=max(n//20,1)
    for i in range(n):
        random.shuffle(base)
        sb=board_i+base[:need]; idx=need
        mine    =hand_rank_int(hole_i+sb)
        best_opp=max(hand_rank_int(base[idx+j*2:idx+j*2+2]+sb) for j in range(opponents))
        if mine>best_opp: wins+=1
        elif mine==best_opp: ties+=1
        else: losses+=1
        if cb and (i+1)%step==0: cb(int((i+1)/n*90))
    return {'win':round(wins/n*100,1),'tie':round(ties/n*100,1),'lose':round(losses/n*100,1)}

def get_draws(hole, board):
    if 5-len(board)<=0: return {}
    all_c=hole+board; deck=[c for c in FULL_DECK if c not in all_c]
    ranks=[RANK_VAL[c[:-1]] for c in all_c]; suits=[c[-1] for c in all_c]
    sc=Counter(suits); rc=Counter(ranks); res={}; mx=max(sc.values())
    if mx==4:
        fs=[s for s,v in sc.items() if v==4][0]; outs=sum(1 for c in deck if c[-1]==fs)
        res['flush_draw']={'label':'Flush Draw','outs':outs,'pct_next':round(outs/len(deck)*100,1)}
    def so():
        uniq=sorted(set(ranks));
        if 12 in uniq: uniq=[-1]+uniq
        out_set=set()
        for s in range(-1,10):
            nd=set(range(s,s+5)); have=nd&set(uniq); miss=nd-have
            if len(have)>=4 and len(miss)==1:
                mv=list(miss)[0]; av=mv if mv>=0 else 12
                for c in deck:
                    if RANK_VAL[c[:-1]]==av: out_set.add(c)
        return len(out_set)
    sv=so()
    if sv: res['straight_draw']={'label':'Straight Draw','outs':sv,'pct_next':round(sv/len(deck)*100,1)}
    pairs=[r for r,v in rc.items() if v==2]; trips=[r for r,v in rc.items() if v==3]
    if trips:
        o=sum(1 for c in deck if RANK_VAL[c[:-1]] in trips)
        res['quads_draw']={'label':'Quadra (Draw)','outs':o,'pct_next':round(o/len(deck)*100,1)}
    if pairs:
        o=sum(1 for c in deck if RANK_VAL[c[:-1]] in pairs)
        res['set_draw']={'label':'Trinca (Do Par)','outs':o,'pct_next':round(o/len(deck)*100,1)}
    br=[RANK_VAL[c[:-1]] for c in board]
    if br:
        ov=[r for r in [RANK_VAL[c[:-1]] for c in hole] if r>max(br)]
        if ov and not pairs and not trips:
            o=min(sum(1 for c in deck if RANK_VAL[c[:-1]] in ov)*3//len(ov),6)
            res['overcard']={'label':'Par Superior (Overcard)','outs':o,'pct_next':round(o/len(deck)*100,1)}
    return res

def monte_carlo_multi(hands, board=[], n=10000, cb=None):
    used=[c for h in hands for c in h]+list(board)
    if len(used)!=len(set(used)): raise ValueError("Cartas duplicadas.")
    if _USE_CPP:
        res = _cpp.monte_carlo_multi(hands, list(board), n)
        if cb: cb(100)
        return [{'win':r[0],'tie':r[1],'lose':r[2]} for r in res]
    # Fallback Python
    base_i =[CARD_INT[c] for c in FULL_DECK if c not in used]
    hands_i=[[CARD_INT[c] for c in h] for h in hands]
    board_i=[CARD_INT[c] for c in board]
    nh=len(hands); need=5-len(board)
    wins=[0]*nh; ties=[0]*nh; losses=[0]*nh; step=max(n//20,1)
    for i in range(n):
        random.shuffle(base_i)
        sb=board_i+base_i[:need]
        scores=[hand_rank_int(h+sb) for h in hands_i]; best=max(scores)
        winners=[j for j,s in enumerate(scores) if s==best]
        if len(winners)==1:
            wins[winners[0]]+=1
            for j in range(nh):
                if j!=winners[0]: losses[j]+=1
        else:
            for j in winners: ties[j]+=1
            for j in range(nh):
                if j not in winners: losses[j]+=1
        if cb and (i+1)%step==0: cb(int((i+1)/n*100))
    return [{'win':round(wins[i]/n*100,1),'tie':round(ties[i]/n*100,1),'lose':round(losses[i]/n*100,1)} for i in range(nh)]

def get_beating_hands(hole, board):
    if not board: return None
    HN=['Carta Alta','Um Par','Dois Pares','Trinca','Sequência','Flush','Full House','Quadra','Straight Flush']
    my=hand_rank(hole+board); avail=[c for c in FULL_DECK if c not in hole+board]
    bg={}; tc=0; total=0
    for opp in itertools.combinations(avail,2):
        total+=1; os=hand_rank(list(opp)+board)
        if os>my:
            cat=os[0]
            if cat not in bg: bg[cat]={'count':0,'examples':[]}
            bg[cat]['count']+=1
            if len(bg[cat]['examples'])<6: bg[cat]['examples'].append(list(opp))
        elif os==my: tc+=1
    tb=sum(g['count'] for g in bg.values())
    result=[{'hand':HN[cat],'cat':cat,'count':bg[cat]['count'],'examples':bg[cat]['examples'],
             'pct':round(bg[cat]['count']/total*100,1)} for cat in sorted(bg.keys(),reverse=True)]
    return {'groups':result,'total_beat':tb,'total_tie':tc,'total_combos':total,
            'pct_beat':round(tb/total*100,1) if total else 0,'is_nuts':tb==0}

# ─── WORKERS ──────────────────────────────────
def _run_calc(jid, hole, board, opp, sims):
    try:
        k=_ckey(hole,board,opp,sims); c=cache_get(k)
        if c: job_done(jid,c); return

        drw=get_draws(hole,board)
        nm =label_pre(hole) if not board else label_board(hole,board)
        st ={0:'Pré-Flop',3:'Flop',4:'Turn',5:'River'}.get(len(board),'Flop')

        # ── Fase 1: resultado rápido com 500 sims ──
        def cb(p): job_prog(jid, int(p * 0.95))
        eq   = monte_carlo(hole,board,opp,sims,cb=cb)
        moe  = round(100/(sims**0.5),2)
        job_prog(jid, 95)
        beating = get_beating_hands(hole,board) if eq['win']>=50 and board else None
        r = {'equity':eq,'draws':drw,'hand_name':nm,'street':st,
             'simulations':sims,'margin_of_error':moe,'beating_hands':beating,'partial':False}
        cache_set(k,r); job_done(jid,r)
    except Exception as e: job_fail(jid,str(e))

def _run_cmp(jid, hands, board, sims):
    try:
        k=('cmp',tuple(tuple(h) for h in hands),tuple(board),sims); c=cache_get(k)
        if c: job_done(jid,c); return
        eqs=monte_carlo_multi(hands,board,sims,cb=lambda p:job_prog(jid,p))
        st={0:'Pré-Flop',3:'Flop',4:'Turn',5:'River'}.get(len(board),'Pré-Flop')
        rh=[{'index':i,'cards':hands[i],'description':label_pre(hands[i]),'equity':eqs[i]} for i in range(len(hands))]
        ranked=sorted(rh,key=lambda x:x['equity']['win'],reverse=True)
        for pos,h in enumerate(ranked): h['rank']=pos+1
        moe=round(100/(sims**0.5),2)
        r={'hands':rh,'board':board,'street':st,'simulations':sims,'margin_of_error':moe}
        cache_set(k,r); job_done(jid,r)
    except Exception as e: job_fail(jid,str(e))

# ─── ROTAS ────────────────────────────────────
@app.route('/calculate', methods=['POST'])
def calculate():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if not _check_rate(ip):
        return jsonify({'error':'Muitas requisições. Aguarde um momento e tente novamente.'}), 429
    d = request.get_json(silent=True)
    if not d or not isinstance(d, dict):
        return jsonify({'error': 'JSON inválido.'}), 400
    try:
        hole  = d.get('hole_cards', [])
        board = d.get('board', [])
        opp   = int(d.get('opponents', 1))
        sims  = min(int(d.get('simulations', 5000)), 100000)
        if not isinstance(hole, list) or not isinstance(board, list):
            return jsonify({'error': 'Formato inválido.'}), 400
    except (TypeError, ValueError):
        return jsonify({'error': 'Parâmetros inválidos.'}), 400
    if len(hole)!=2:               return jsonify({'error':'Informe 2 cartas na mão.'}),400
    if len(board) not in [0,3,4,5]:return jsonify({'error':'Board: 0,3,4 ou 5 cartas.'}),400
    if len(hole+board)!=len(set(hole+board)): return jsonify({'error':'Cartas duplicadas.'}),400
    # Valida que todas as cartas são strings do deck
    all_cards_valid = all(isinstance(c,str) and c in FULL_DECK for c in hole+board)
    if not all_cards_valid: return jsonify({'error':'Cartas inválidas.'}),400
    jid=new_job()
    threading.Thread(target=_run_calc,args=(jid,hole,board,opp,sims),daemon=True).start()
    return jsonify({'job_id':jid})

@app.route('/compare', methods=['POST'])
def compare():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if not _check_rate(ip):
        return jsonify({'error':'Muitas requisições. Aguarde um momento e tente novamente.'}), 429
    d = request.get_json(silent=True)
    if not d or not isinstance(d, dict):
        return jsonify({'error': 'JSON inválido.'}), 400
    try:
        sims  = min(int(d.get('simulations', 5000)), 100000)
        hands = d.get('hands', [])
        board = d.get('board', [])
        if not isinstance(hands, list) or not isinstance(board, list):
            return jsonify({'error': 'Formato inválido.'}), 400
    except (TypeError, ValueError):
        return jsonify({'error': 'Parâmetros inválidos.'}), 400
    for h in hands:
        if not h or len(h)!=2: return jsonify({'error':'Cada jogador precisa de 2 cartas.'}),400
        if not all(isinstance(c,str) and c in FULL_DECK for c in h):
            return jsonify({'error':'Cartas inválidas.'}),400
    if len(board) not in [0,3,4,5]: return jsonify({'error':'Board: 0, 3, 4 ou 5 cartas.'}),400
    all_cards=[c for h in hands for c in h]+board
    if len(all_cards)!=len(set(all_cards)): return jsonify({'error':'Cartas duplicadas.'}),400
    jid=new_job()
    threading.Thread(target=_run_cmp,args=(jid,hands,board,sims),daemon=True).start()
    return jsonify({'job_id':jid})

@app.route('/status/<jid>')
def status(jid):
    j=_JOBS.get(jid)
    if not j: return jsonify({'error':'Job não encontrado.'}),404
    return jsonify({
        'status':   j['status'],
        'progress': j['progress'],
        'result':   j['result'],
        'partial':  j['partial'],
        'error':    j['error']
    })

# ─── TESTES ───────────────────────────────────
def run_tests():
    ok=0; fail=0
    def check(name, cond):
        nonlocal ok,fail
        if cond: print(f'  ✓  {name}'); ok+=1
        else:    print(f'  ✗  {name}  ← FALHOU'); fail+=1
    print('\n══ PokerCalc — Testes ══\n')
    check('Straight Flush=8', evaluate_five(['As','Ks','Qs','Js','Ts'])[0]==8)
    check('Quadra=7',         evaluate_five(['As','Ad','Ac','Ah','2s'])[0]==7)
    check('Full House=6',     evaluate_five(['As','Ad','Ac','Kh','Ks'])[0]==6)
    check('Flush=5',          evaluate_five(['2s','5s','7s','9s','Js'])[0]==5)
    check('Sequência=4',      evaluate_five(['2s','3h','4d','5c','6s'])[0]==4)
    check('Wheel A-5=4',      evaluate_five(['As','2h','3d','4c','5s'])[0]==4)
    check('Trinca=3',         evaluate_five(['As','Ad','Ac','Kh','2s'])[0]==3)
    check('Dois Pares=2',     evaluate_five(['As','Ad','Kh','Ks','2s'])[0]==2)
    check('Um Par=1',         evaluate_five(['As','Ad','Kh','Qs','2s'])[0]==1)
    check('Carta Alta=0',     evaluate_five(['As','Kh','Qd','Js','9c'])[0]==0)
    check('hand_rank 7 cartas FH', hand_rank(['As','Ad','Kh','Ks','Kd','2c','3h'])[0]==6)
    eq=monte_carlo(['As','Ah'],[],n=3000)
    check(f"AA win>75% ({eq['win']}%)", eq['win']>75)
    k=_ckey(['As','Ah'],[],1,999); cache_set(k,{'ok':True})
    check('Cache set/get', cache_get(k)=={'ok':True})
    jid=new_job()
    check('Job status=running', _JOBS[jid]['status']=='running')
    job_prog(jid,55); check('Job progress=55', _JOBS[jid]['progress']==55)
    job_done(jid,{'x':1}); check('Job status=done', _JOBS[jid]['status']=='done')
    draws=get_draws(['As','Ks'],['Qs','Js','2h'])
    check('Flush draw detectado', 'flush_draw' in draws)
    bh=get_beating_hands(['As','Ah'],['Ad','Ac','Kh'])
    check('Quads ases — poucos combos que batem', bh['total_beat']<=10)
    print(f'\n  {ok} passaram · {fail} falharam\n')
    return fail==0

# ─── HTML ─────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<meta name="description" content="Calculadora de probabilidades de poker Texas Hold'em — equity, pot odds, confronto de mãos."/>
<meta name="theme-color" content="#071a10"/>
<title>PokerCalc — Calculadora de Poker</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet"/>
<style>
:root{--felt:#0d2318;--felt-edge:#071a10;--gold:#c9a84c;--gold-light:#e8c96d;--gold-dim:#7a6230;--cream:#f5ead4;--red-suit:#d63031;--card-bg:#f9f3e8;--card-shadow:rgba(0,0,0,.7);--glow-gold:0 0 20px rgba(201,168,76,.4);}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
body{font-family:'Rajdhani',sans-serif;background:var(--felt-edge);color:var(--cream);min-height:100vh;overflow-x:hidden;-webkit-text-size-adjust:100%;}
body::before{content:'';position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4'%3E%3Crect width='4' height='4' fill='%230d2318'/%3E%3Crect x='0' y='0' width='1' height='1' fill='%230f2a1c' opacity='0.4'/%3E%3C/svg%3E");pointer-events:none;z-index:0;}
.z1{position:relative;z-index:1;}
.hdr{border-bottom:1px solid rgba(201,168,76,.2);background:linear-gradient(180deg,rgba(7,26,16,.95) 0%,transparent 100%);backdrop-filter:blur(8px);}
.logo{font-weight:700;letter-spacing:.12em;color:var(--gold);text-shadow:var(--glow-gold);}
.glass{background:rgba(13,35,24,.7);border:1px solid rgba(201,168,76,.15);border-radius:12px;backdrop-filter:blur(6px);}
.stitle{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);opacity:.8;font-weight:600;}
.playing-card{width:48px;height:68px;background:var(--card-bg);border-radius:6px;border:1px solid rgba(255,255,255,.15);display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;transition:all .15s ease;box-shadow:0 4px 12px var(--card-shadow),inset 0 1px 0 rgba(255,255,255,.9);user-select:none;flex-shrink:0;}
.playing-card:hover{transform:translateY(-4px) scale(1.06);box-shadow:0 10px 24px var(--card-shadow),var(--glow-gold);}
.playing-card.used{opacity:.18;cursor:not-allowed;pointer-events:none;}
.card-rank{font-family:'Rajdhani',sans-serif;font-weight:700;font-size:14px;line-height:1;}
.card-suit{font-size:14px;line-height:1;}
.red-card .card-rank,.red-card .card-suit{color:var(--red-suit);}
.black-card .card-rank,.black-card .card-suit{color:#1a1a1a;}
.slot{width:60px;height:84px;border-radius:8px;border:2px dashed rgba(201,168,76,.35);background:rgba(13,35,24,.6);display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;transition:all .2s;flex-shrink:0;}
.slot.filled{border:2px solid rgba(201,168,76,.6);background:var(--card-bg);box-shadow:0 6px 20px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.8);}
.slot.filled:hover{box-shadow:0 0 0 2px var(--red-suit),0 6px 16px rgba(0,0,0,.5);}
.slot.active{border-color:var(--gold);box-shadow:0 0 0 2px rgba(201,168,76,.4);animation:pulse-slot 1.5s ease infinite;}
.slot-rank{font-family:'Rajdhani',sans-serif;font-weight:700;font-size:17px;line-height:1;}
.slot-suit{font-size:17px;line-height:1;}
.slot.filled.red-card .slot-rank,.slot.filled.red-card .slot-suit{color:var(--red-suit);}
.slot.filled.black-card .slot-rank,.slot.filled.black-card .slot-suit{color:#1a1a1a;}
@keyframes pulse-slot{0%,100%{box-shadow:0 0 8px rgba(201,168,76,.4);}50%{box-shadow:0 0 20px rgba(201,168,76,.8);}}
.gauge{position:relative;width:130px;height:130px;display:flex;align-items:center;justify-content:center;}
.g-svg{transform:rotate(-90deg);}
.g-track{fill:none;stroke:rgba(255,255,255,.05);stroke-width:10;}
.g-fill{fill:none;stroke-width:10;stroke-linecap:round;transition:stroke-dashoffset .7s cubic-bezier(.34,1.56,.64,1);}
.g-center{position:absolute;text-align:center;display:flex;flex-direction:column;align-items:center;}
.g-pct{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:700;}
.g-lbl{font-size:10px;letter-spacing:.15em;text-transform:uppercase;opacity:.6;}
.bar-track{height:9px;border-radius:5px;background:rgba(255,255,255,.06);overflow:hidden;}
.bar-fill{height:100%;border-radius:5px;transition:width .6s cubic-bezier(.34,1.56,.64,1);}
.bw{background:linear-gradient(90deg,#00b85e,#00d472);}
.bt{background:linear-gradient(90deg,#c9a84c,#e8c96d);}
.bl{background:linear-gradient(90deg,#c0392b,#e74c3c);}
.btn-calc{background:linear-gradient(135deg,#c9a84c 0%,#e8c96d 50%,#c9a84c 100%);background-size:200% 200%;color:#0d1f12;font-family:'Rajdhani',sans-serif;font-weight:700;font-size:14px;letter-spacing:.15em;text-transform:uppercase;border:none;border-radius:8px;padding:12px 28px;cursor:pointer;transition:all .2s;box-shadow:0 4px 16px rgba(201,168,76,.3);}
.btn-calc:hover{box-shadow:0 6px 24px rgba(201,168,76,.5);transform:translateY(-1px);}
.btn-calc:disabled{opacity:.4;cursor:not-allowed;transform:none;}
.btn-rst{background:transparent;border:1px solid rgba(201,168,76,.3);color:var(--gold);font-family:'Rajdhani',sans-serif;font-weight:600;font-size:12px;letter-spacing:.12em;text-transform:uppercase;border-radius:8px;padding:9px 18px;cursor:pointer;transition:all .2s;}
.btn-rst:hover{background:rgba(201,168,76,.1);}
.mode-tab{font-family:'Rajdhani',sans-serif;font-weight:600;font-size:13px;letter-spacing:.15em;text-transform:uppercase;padding:7px 20px;border-radius:7px;border:1px solid transparent;cursor:pointer;transition:all .2s;background:transparent;color:rgba(201,168,76,.45);}
.mode-tab:hover{color:var(--gold);background:rgba(201,168,76,.06);}
.mode-tab.active{color:var(--felt-edge);background:linear-gradient(135deg,#c9a84c,#e8c96d);border-color:var(--gold);box-shadow:var(--glow-gold);}
.stab{padding:3px 9px;border-radius:6px;font-size:16px;cursor:pointer;transition:all .15s;border:1px solid transparent;background:transparent;}
.stab.on{border-color:var(--gold);background:rgba(201,168,76,.12);}
.stab:hover:not(.on){background:rgba(255,255,255,.05);}
.pill{background:rgba(201,168,76,.07);border:1px solid rgba(201,168,76,.18);border-radius:20px;padding:5px 12px;display:flex;align-items:center;gap:9px;}
.player-panel{background:rgba(13,35,24,.8);border:1px solid rgba(201,168,76,.12);border-radius:10px;padding:12px 14px;cursor:pointer;transition:all .2s;}
.player-panel:hover{border-color:rgba(201,168,76,.3);}
.player-panel.active-player{border-color:rgba(201,168,76,.7);box-shadow:0 0 0 1px rgba(201,168,76,.25),var(--glow-gold);}
.player-panel.done{border-color:rgba(0,212,114,.25);}
.result-card{background:rgba(13,35,24,.8);border:1px solid rgba(201,168,76,.15);border-radius:12px;padding:16px;position:relative;overflow:hidden;transition:all .3s;}
.result-card.winner{border-color:rgba(0,212,114,.4);}
.result-card.winner::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#00d472,transparent);}
.result-card.loser{opacity:.6;}
.mini-card{width:38px;height:53px;background:var(--card-bg);border-radius:5px;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 3px 10px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.9);flex-shrink:0;}
.mini-card .mr{font-family:'Rajdhani',sans-serif;font-weight:700;font-size:12px;line-height:1;}
.mini-card .ms{font-size:12px;line-height:1;}
.mini-card.red .mr,.mini-card.red .ms{color:var(--red-suit);}
.mini-card.black .mr,.mini-card.black .ms{color:#1a1a1a;}
.rnk{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;flex-shrink:0;}
.r1{background:linear-gradient(135deg,#c9a84c,#e8c96d);color:#0d1f12;}
.r2{background:rgba(255,255,255,.12);color:var(--cream);}
.rx{background:rgba(255,255,255,.06);color:rgba(255,255,255,.4);}
.global-bar{position:relative;height:26px;border-radius:6px;overflow:hidden;background:rgba(255,255,255,.04);}
.global-seg{position:absolute;top:0;bottom:0;transition:width .7s cubic-bezier(.34,1.56,.64,1);display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;overflow:hidden;}
.loader{display:none;width:18px;height:18px;border:2px solid rgba(201,168,76,.3);border-top-color:var(--gold);border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
.fade-in{animation:fi .35s ease forwards;}
@keyframes fi{from{opacity:0;transform:translateY(7px);}to{opacity:1;transform:none;}}
.hand-badge{display:inline-flex;align-items:center;gap:5px;background:rgba(201,168,76,.1);border:1px solid rgba(201,168,76,.3);border-radius:20px;padding:3px 12px;font-size:12px;font-weight:600;color:var(--gold-light);letter-spacing:.08em;}
.progress-wrap{height:4px;border-radius:2px;background:rgba(255,255,255,.07);overflow:hidden;margin-top:8px;}
.progress-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,#c9a84c,#e8c96d);width:0%;transition:width .35s ease;}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-track{background:var(--felt-edge);}
::-webkit-scrollbar-thumb{background:var(--gold-dim);border-radius:3px;}
select{background:rgba(13,35,24,.9);border:1px solid rgba(201,168,76,.25);color:var(--cream);outline:none;border-radius:6px;padding:6px 10px;font-family:'Rajdhani',sans-serif;font-size:13px;}
/* ════════════════════════════════════════
   TABLET — 860px
════════════════════════════════════════ */
@media(max-width:860px){
  .cols{flex-direction:column!important;}
  .left-col{width:100%!important;}
  .hdr-right .sim-info{display:none;}
  .gauge{width:100px!important;height:100px!important;}
  .g-svg{width:100px!important;height:100px!important;}
  .g-pct{font-size:18px!important;}
  .gauges-row{gap:8px!important;}
  main{padding:8px!important;}
  .glass{border-radius:10px;}
  .logo-sub{display:none;}
  .hdr-tabs{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;max-width:70vw;}
  .hdr-tabs::-webkit-scrollbar{display:none;}
  .mode-tab{padding:6px 12px!important;font-size:11px!important;letter-spacing:.06em!important;white-space:nowrap;}
}

/* ════════════════════════════════════════
   MOBILE — 600px  (redesign completo)
════════════════════════════════════════ */
@media(max-width:600px){
  /* Espaço para bottom nav */
  body{padding-bottom:68px!important;}

  /* Header: só logo */
  header{padding:10px 16px!important;}
  .hdr-right{display:none!important;}
  .logo{font-size:18px!important;}

  /* Bottom nav visível */
  #mobile-bottom-nav{display:flex!important;}

  /* Main */
  main{padding:8px!important;}
  .glass{padding:14px!important;border-radius:12px;}

  /* Slots: sempre em linha horizontal, grandes e tocáveis */
  #hole-slots{
    display:flex!important;flex-direction:row!important;
    gap:10px!important;justify-content:center!important;
  }
  #board-slots,#cmp-board-slots{
    display:flex!important;flex-direction:row!important;
    gap:8px!important;flex-wrap:nowrap!important;
    justify-content:center!important;
    overflow-x:auto;padding-bottom:4px;
  }
  .slot{width:58px!important;height:80px!important;flex-shrink:0!important;border-radius:8px!important;}
  .slot-rank{font-size:18px!important;}
  .slot-suit{font-size:18px!important;}

  /* Deck mobile: por naipe, scroll horizontal */
  #deck-grid-wrap-desktop{display:none!important;}
  #deck-mobile{display:flex!important;}
  /* Esconde filtro de naipe no mobile (deck já é por naipe) */
  #mode-calc .stab, #mode-compare .stab{display:none!important;}
  .playing-card{width:50px!important;height:70px!important;flex-shrink:0!important;border-radius:7px!important;}
  .card-rank,.card-suit{font-size:14px!important;}

  /* Quick hands: scroll horizontal, sem quebra */
  .qh-scroll{flex-wrap:nowrap!important;overflow-x:auto!important;-webkit-overflow-scrolling:touch;scrollbar-width:none;gap:6px!important;}
  .qh-scroll::-webkit-scrollbar{display:none;}
  .qh-btn{min-width:50px;flex-shrink:0;padding:7px 10px!important;font-size:12px!important;min-height:36px;}

  /* Gauges */
  .gauge{width:88px!important;height:88px!important;}
  .g-svg{width:88px!important;height:88px!important;}
  .g-pct{font-size:15px!important;}
  .g-lbl{font-size:9px!important;}
  .gauges-row{gap:6px!important;}

  /* Street pills */
  #sp-pre,#sp-flop,#sp-turn,#sp-river{padding:3px 8px!important;font-size:9px!important;}

  /* EV */
  .ev-input{font-size:16px!important;padding:10px 12px!important;}
  .ev-bet-btn{padding:9px 0!important;min-height:40px;font-size:11px!important;}
  #import-btn{font-size:12px!important;padding:12px!important;}

  /* Confronto */
  .result-card{padding:12px!important;}
  #cmp-results{grid-template-columns:1fr!important;}
  .player-panel{padding:10px 12px!important;}

  /* Ocultar elementos desnecessários */
  .ref-table-wrap{display:none!important;}
  #moe-badge{display:none!important;}
  .logo-sub{display:none!important;}
}

/* ════════════════════════════════════════
   MOBILE — 400px (iPhone SE)
════════════════════════════════════════ */
@media(max-width:400px){
  .playing-card{width:44px!important;height:62px!important;}
  .card-rank,.card-suit{font-size:12px!important;}
  .slot{width:52px!important;height:72px!important;}
  .gauge{width:76px!important;height:76px!important;}
  .g-svg{width:76px!important;height:76px!important;}
  .g-pct{font-size:13px!important;}
}

/* ── Tooltip ── */
.tip-wrap{position:relative;display:inline-flex;align-items:center;cursor:help;}
.tip-icon{width:14px;height:14px;border-radius:50%;background:rgba(201,168,76,.2);border:1px solid rgba(201,168,76,.35);color:var(--gold);font-size:9px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;margin-left:5px;flex-shrink:0;font-family:'JetBrains Mono',monospace;transition:background .15s;}
.tip-wrap:hover .tip-icon{background:rgba(201,168,76,.4);}
.tip-box{display:none;position:absolute;left:50%;transform:translateX(-50%);bottom:calc(100% + 8px);width:220px;background:rgba(7,20,12,.97);border:1px solid rgba(201,168,76,.3);border-radius:8px;padding:10px 12px;font-family:'Rajdhani',sans-serif;font-size:12px;line-height:1.5;color:rgba(255,255,255,.75);z-index:9999;box-shadow:0 8px 24px rgba(0,0,0,.6);pointer-events:none;}
.tip-box::after{content:'';position:absolute;left:50%;transform:translateX(-50%);top:100%;border:5px solid transparent;border-top-color:rgba(201,168,76,.3);}
.tip-wrap:hover .tip-box{display:block;}
.tip-right .tip-box{left:auto;right:0;transform:none;}
.tip-right .tip-box::after{left:auto;right:12px;transform:none;}
/* tooltip abrindo para baixo (para elementos no topo da página) */
.tip-down .tip-box{bottom:auto;top:calc(100% + 8px);}
.tip-down .tip-box::after{top:auto;bottom:100%;border-top-color:transparent;border-bottom-color:rgba(201,168,76,.3);}
.tip-down.tip-right .tip-box{left:auto;right:0;transform:none;}

/* ── Quick Hands ── */
.qh-btn{padding:4px 9px;border-radius:6px;font-family:'Rajdhani',sans-serif;font-weight:700;font-size:11px;letter-spacing:.06em;cursor:pointer;border:1px solid rgba(201,168,76,.2);background:rgba(13,35,24,.8);color:rgba(201,168,76,.7);transition:all .15s;white-space:nowrap;}
.qh-btn:hover{background:rgba(201,168,76,.15);border-color:rgba(201,168,76,.5);color:var(--gold);}
.qh-scroll{display:flex;flex-wrap:wrap;gap:4px;padding:2px 0 4px;}
/* ── EV Calculator ── */
.ev-field-wrap{position:relative;display:flex;align-items:center;}
.ev-field-unit{position:absolute;right:12px;font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:rgba(201,168,76,.5);pointer-events:none;}
.ev-bet-btn{padding:7px 0;border-radius:8px;font-family:'Rajdhani',sans-serif;font-weight:700;font-size:13px;letter-spacing:.04em;cursor:pointer;border:1px solid rgba(201,168,76,.2);background:rgba(13,35,24,.8);color:rgba(201,168,76,.6);transition:all .2s;text-align:center;}
.ev-bet-btn:hover{background:rgba(201,168,76,.1);border-color:rgba(201,168,76,.45);color:var(--gold);}
.ev-bet-btn.active-bet{background:linear-gradient(135deg,rgba(201,168,76,.25),rgba(232,201,109,.15));border-color:var(--gold);color:var(--gold-light);box-shadow:0 0 12px rgba(201,168,76,.2);}
.verdict-box{border-radius:14px;padding:28px 20px;text-align:center;transition:all .4s ease;border:2px solid rgba(201,168,76,.15);background:rgba(13,35,24,.9);}
.verdict-box.v-call{border-color:rgba(0,212,114,.5);background:linear-gradient(135deg,rgba(0,30,15,.95),rgba(0,20,10,.95));}
.verdict-box.v-fold{border-color:rgba(231,76,60,.5);background:linear-gradient(135deg,rgba(30,5,5,.95),rgba(20,0,0,.95));}
.verdict-box.v-margin{border-color:rgba(201,168,76,.45);background:linear-gradient(135deg,rgba(20,15,0,.95),rgba(13,10,0,.95));}
.needs-bar{height:14px;border-radius:7px;background:rgba(255,255,255,.06);position:relative;overflow:visible;}
.needs-fill{height:100%;border-radius:7px;transition:width .6s cubic-bezier(.34,1.56,.64,1);}
.needs-marker{position:absolute;top:-4px;width:3px;height:22px;border-radius:2px;background:#e74c3c;transition:left .5s ease;box-shadow:0 0 8px rgba(231,76,60,.6);}
.needs-marker-label{position:absolute;top:-18px;font-family:'JetBrains Mono',monospace;font-size:9px;color:#e74c3c;white-space:nowrap;transform:translateX(-50%);transition:left .5s ease;}
input[type=number]::-webkit-inner-spin-button{opacity:0.4;}
input[type=number]{-moz-appearance:textfield;}
input.ev-input{width:100%;padding:10px 40px 10px 14px;border-radius:10px;font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:700;background:rgba(13,35,24,.9);border:1px solid rgba(201,168,76,.25);color:var(--cream);outline:none;transition:border-color .15s;}
input.ev-input.ev-input-lg{font-size:22px;padding:13px 48px 13px 16px;}
input.ev-input:focus{border-color:var(--gold);}
.math-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05);font-family:'JetBrains Mono',monospace;font-size:12px;}
.math-row:last-child{border-bottom:none;}
/* ── Splash Screen ── */
#splash{
  position:fixed;inset:0;z-index:99999;
  background:#071a10;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:0;
  transition:opacity .6s ease;
}
#splash.hidden{opacity:0;pointer-events:none;}

/* Felt texture no splash */
#splash::before{
  content:'';position:absolute;inset:0;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4'%3E%3Crect width='4' height='4' fill='%230d2318'/%3E%3Crect x='0' y='0' width='1' height='1' fill='%230f2a1c' opacity='0.5'/%3E%3C/svg%3E");
  pointer-events:none;
}

/* Logo container */
.splash-logo{
  position:relative;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:20px;
  z-index:1;
}

/* Os 4 naipes em cruz */
.splash-suits{
  position:relative;width:120px;height:120px;
  display:flex;align-items:center;justify-content:center;
}
.splash-suit-item{
  position:absolute;font-size:38px;line-height:1;
  animation:suitFadeIn .6s ease forwards;
  opacity:0;
}
.splash-suit-item.s{ top:0;    left:50%; transform:translateX(-50%); color:#c9a84c; animation-delay:.1s; }
.splash-suit-item.h{ right:0;  top:50%;  transform:translateY(-50%); color:#e74c3c; animation-delay:.25s; }
.splash-suit-item.d{ bottom:0; left:50%; transform:translateX(-50%); color:#e74c3c; animation-delay:.4s; }
.splash-suit-item.c{ left:0;   top:50%;  transform:translateY(-50%); color:#c9a84c; animation-delay:.55s; }

/* Centro da cruz */
.splash-suit-center{
  width:48px;height:48px;border-radius:50%;
  background:linear-gradient(135deg,#c9a84c,#e8c96d);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 0 30px rgba(201,168,76,.5);
  animation:centerPop .4s cubic-bezier(.34,1.56,.64,1) .6s both;
  opacity:0;
}
.splash-suit-center span{
  font-family:'Rajdhani',sans-serif;font-weight:700;
  font-size:20px;color:#071a10;letter-spacing:.05em;
}

/* Nome */
.splash-name{
  font-family:'Rajdhani',sans-serif;font-weight:700;
  font-size:36px;letter-spacing:.3em;color:var(--gold);
  text-shadow:0 0 30px rgba(201,168,76,.5);
  animation:nameSlide .5s ease .8s both;
  opacity:0;
}
.splash-name span{color:rgba(255,255,255,.35);}

/* Tagline */
.splash-tag{
  font-family:'JetBrains Mono',monospace;font-size:11px;
  letter-spacing:.2em;color:rgba(255,255,255,.25);
  text-transform:uppercase;
  animation:nameSlide .5s ease 1s both;
  opacity:0;
  margin-top:-8px;
}

/* Barra de progresso */
.splash-bar-wrap{
  width:160px;height:2px;border-radius:1px;
  background:rgba(255,255,255,.06);overflow:hidden;
  animation:nameSlide .4s ease 1.1s both;opacity:0;
  margin-top:32px;
}
.splash-bar{
  height:100%;border-radius:1px;
  background:linear-gradient(90deg,#7a6230,#c9a84c,#e8c96d,#c9a84c);
  background-size:200% 100%;
  animation:barShimmer 1.5s ease 1.2s infinite, splash-load 3s ease 1s forwards;
  width:0%;
}

/* Mensagem de loading */
.splash-msg{
  font-family:'Rajdhani',sans-serif;font-size:12px;
  color:rgba(255,255,255,.2);letter-spacing:.1em;
  animation:nameSlide .4s ease 1.3s both;opacity:0;
  margin-top:12px;text-align:center;
}

@keyframes suitFadeIn{
  from{opacity:0;transform:translateX(-50%) scale(.5);}
  to  {opacity:1;transform:translateX(-50%) scale(1);}
}
@keyframes suitFadeIn-h{
  from{opacity:0;transform:translateY(-50%) scale(.5);}
  to  {opacity:1;transform:translateY(-50%) scale(1);}
}
.splash-suit-item.h{animation-name:suitFadeIn-h;}
.splash-suit-item.c{animation-name:suitFadeIn-h;}
@keyframes centerPop{
  from{opacity:0;transform:scale(0);}
  to  {opacity:1;transform:scale(1);}
}
@keyframes nameSlide{
  from{opacity:0;transform:translateY(12px);}
  to  {opacity:1;transform:translateY(0);}
}
@keyframes barShimmer{
  0%  {background-position:200% 0;}
  100%{background-position:-200% 0;}
}
@keyframes splash-load{
  0%  {width:0%;}
  50% {width:60%;}
  80% {width:85%;}
  100%{width:100%;}
}
/* ── Bottom Nav (mobile) ── */
#mobile-bottom-nav{
  display:none;position:fixed;bottom:0;left:0;right:0;
  background:rgba(7,20,12,.98);border-top:1px solid rgba(201,168,76,.2);
  backdrop-filter:blur(12px);z-index:1000;height:68px;
}
.mob-tab{
  flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:3px;padding:8px 4px;
  background:transparent;border:none;cursor:pointer;
  font-family:'Rajdhani',sans-serif;font-weight:700;
  font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:rgba(201,168,76,.35);transition:all .2s;
}
.mob-tab.active{color:var(--gold);}
.mob-tab-icon{font-size:22px;line-height:1;transition:all .2s;}
.mob-tab.active .mob-tab-icon{text-shadow:0 0 14px rgba(201,168,76,.7);}
/* ── Mobile deck ── */
#deck-mobile{flex-direction:column;gap:8px;display:none;}
.deck-suit-row{
  display:flex;gap:5px;align-items:center;
  overflow-x:auto;-webkit-overflow-scrolling:touch;
  padding:3px 0;scrollbar-width:none;
}
.deck-suit-row::-webkit-scrollbar{display:none;}
.deck-suit-label{
  font-family:'Rajdhani',sans-serif;font-weight:700;font-size:20px;
  width:26px;flex-shrink:0;display:flex;align-items:center;
}
.poker-table-wrap{position:relative;width:100%;padding-bottom:58%;min-height:200px;}
.poker-table-inner{position:absolute;inset:0;}
.t-card{position:absolute;background:var(--card-bg);border-radius:5px;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 3px 10px rgba(0,0,0,.65),inset 0 1px 0 rgba(255,255,255,.85);transition:transform .2s,opacity .2s;}
.t-card .tr{font-family:'Rajdhani',sans-serif;font-weight:700;line-height:1;}
.t-card .ts{line-height:1;}
.t-card.red .tr,.t-card.red .ts{color:var(--red-suit);}
.t-card.black .tr,.t-card.black .ts{color:#1a1a1a;}
.t-card.empty{background:rgba(255,255,255,.05);border:1.5px dashed rgba(255,255,255,.12);box-shadow:none;}
.t-seat{position:absolute;display:flex;flex-direction:column;align-items:center;gap:3px;transition:all .3s;}
.t-seat-cards{display:flex;gap:3px;}
.t-seat-name{font-family:'Rajdhani',sans-serif;font-weight:700;font-size:10px;letter-spacing:.08em;color:rgba(255,255,255,.5);}
.t-seat-eq{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12px;transition:color .3s;}
.t-seat-rnk{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-size:8px;font-weight:700;}
</style>
</head>
<body>
<!-- SPLASH SCREEN -->
<div id="splash">
  <div class="splash-logo">
    <!-- Cruz de naipes -->
    <div class="splash-suits">
      <span class="splash-suit-item s">♠</span>
      <span class="splash-suit-item h">♥</span>
      <span class="splash-suit-item d">♦</span>
      <span class="splash-suit-item c">♣</span>
      <div class="splash-suit-center"><span>PC</span></div>
    </div>
    <!-- Nome -->
    <div class="splash-name">POKER<span>CALC</span></div>
    <div class="splash-tag">Probability Engine</div>
    <!-- Barra -->
    <div class="splash-bar-wrap"><div class="splash-bar"></div></div>
    <p class="splash-msg">Carregando...</p>
  </div>
</div>
<!-- BOTTOM NAV — só aparece em mobile (≤600px) -->
<nav id="mobile-bottom-nav" style="display:none">
  <button class="mob-tab active" id="mob-tab-calc" onclick="switchMode('calc');setMobTab('calc')">
    <span class="mob-tab-icon">♠</span>
    <span>Calculadora</span>
  </button>
  <button class="mob-tab" id="mob-tab-ev" onclick="switchMode('ev');setMobTab('ev')">
    <span class="mob-tab-icon">📊</span>
    <span>Vale a Pena?</span>
  </button>
  <button class="mob-tab" id="mob-tab-compare" onclick="switchMode('compare');setMobTab('compare')">
    <span class="mob-tab-icon">⚔️</span>
    <span>Confronto</span>
  </button>
</nav>
<div class="z1 min-h-screen flex flex-col">

<header class="hdr px-4 py-3 flex items-center justify-between sticky top-0 z-50">
  <div class="flex items-center gap-2">
    <span style="color:#d63031;font-size:20px">♠</span>
    <span class="logo text-xl tracking-widest">POKERCALC</span>
    <span class="text-xs font-mono logo-sub" style="color:var(--gold-dim)">PROBABILITY ENGINE</span>
  </div>
  <div class="flex items-center gap-2 hdr-right">
    <div class="flex gap-1 p-1 rounded-lg hdr-tabs" style="background:rgba(13,35,24,.7);border:1px solid rgba(201,168,76,.12)">
      <button class="mode-tab active" id="tab-calc"    onclick="switchMode('calc')">Calculadora</button>
      <button class="mode-tab"        id="tab-ev"      onclick="switchMode('ev')">Vale a Pena?</button>
      <button class="mode-tab"        id="tab-compare" onclick="switchMode('compare')">Confronto ♠♥</button>
    </div>
    <span id="sim-counter" class="sim-info text-xs font-mono" style="color:var(--gold-dim)">— simulações</span>
    <span id="moe-badge" style="display:none;background:rgba(201,168,76,.08);border:1px solid rgba(201,168,76,.2);border-radius:6px;padding:2px 8px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--gold-dim)">±—%</span>
  </div>
</header>

<main class="flex-1 p-4 max-w-7xl mx-auto w-full">

<!-- CALCULADORA -->
<div id="mode-calc">
<div class="flex gap-4 cols">
  <div class="flex flex-col gap-4 left-col" style="width:400px;flex-shrink:0">
    <div class="glass p-4">
      <!-- Sua Mão -->
      <div class="flex items-center gap-1 mb-2">
        <p class="stitle">Sua Mão</p>
        <div class="tip-wrap tip-down">
          <span class="tip-icon">?</span>
          <div class="tip-box">As 2 cartas privadas que só você vê. Clique em uma carta do deck para preenchê-las.</div>
        </div>
      </div>
      <div class="flex gap-3 mb-3" id="hole-slots"></div>
      <!-- Quick hands -->
      <div class="mb-4">
        <p class="text-xs mb-2" style="color:rgba(255,255,255,.3);letter-spacing:.08em">ATALHOS RÁPIDOS</p>
        <div class="qh-scroll" id="quick-hands"></div>
      </div>
      <!-- Board -->
      <div class="flex items-center gap-1 mb-2">
        <p class="stitle">Board</p>
        <div class="tip-wrap tip-down">
          <span class="tip-icon">?</span>
          <div class="tip-box">As cartas comunitárias abertas na mesa. Flop = 3 cartas, Turn = 4ª carta, River = 5ª carta.</div>
        </div>
      </div>
      <div class="flex gap-2 flex-wrap" id="board-slots"></div>
    </div>
    <div class="glass p-4 flex-1">
      <div class="flex items-center justify-between mb-3">
        <p class="stitle">Deck</p>
        <div class="flex gap-1">
          <button class="stab on" id="tab-all" onclick="filterSuit('all','calc')" style="font-size:11px;font-family:'Rajdhani',sans-serif;font-weight:600;letter-spacing:.1em;color:var(--gold)">TODOS</button>
          <button class="stab" id="tab-s" onclick="filterSuit('s','calc')" style="color:#aaa">♠</button>
          <button class="stab" id="tab-h" onclick="filterSuit('h','calc')" style="color:var(--red-suit)">♥</button>
          <button class="stab" id="tab-d" onclick="filterSuit('d','calc')" style="color:var(--red-suit)">♦</button>
          <button class="stab" id="tab-c" onclick="filterSuit('c','calc')" style="color:#aaa">♣</button>
        </div>
      </div>
      <!-- Desktop: grid normal -->
      <div id="deck-grid-wrap-desktop">
        <div id="deck-grid" style="display:flex;flex-wrap:wrap;gap:4px;max-height:260px;overflow-y:auto;padding:4px"></div>
      </div>
      <!-- Mobile: uma linha por naipe com scroll horizontal -->
      <div id="deck-mobile" style="display:none"></div>
    </div>
    <!-- Opções + Botão juntos -->
    <div class="glass p-4">
      <input type="hidden" id="simulations" value="20000"/>
      <!-- Só oponentes -->
      <div class="mb-3">
        <label class="text-xs" style="color:var(--gold-dim)">Oponentes</label>
        <select id="opponents" class="w-full mt-1"><option value="1">1 oponente</option><option value="2">2</option><option value="3">3</option><option value="4">4</option><option value="5">5</option><option value="6">6</option><option value="7">7</option></select>
      </div>
      <!-- Botão CALCULAR ODDS grande -->
      <button class="btn-calc w-full flex items-center justify-center gap-3" id="calc-btn" onclick="doCalculate()"
        style="padding:18px 28px;font-size:18px;letter-spacing:.2em;border-radius:10px;">
        <span id="btn-txt">CALCULAR ODDS</span>
        <div class="loader" id="loader"></div>
      </button>
      <div class="progress-wrap" id="calc-prog-wrap" style="display:none"><div class="progress-fill" id="calc-prog-fill"></div></div>
      <button class="btn-rst w-full mt-2" onclick="resetCalc()" style="font-size:11px;padding:7px;">RESET</button>
    </div>
  </div>
  <div class="flex-1 flex flex-col gap-4">
    <!-- MESA DA CALCULADORA -->
    <div class="glass overflow-hidden">
      <div class="poker-table-wrap">
        <div class="poker-table-inner" id="calc-poker-table"></div>
      </div>
    </div>
    <div class="glass p-5">
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center gap-1">
          <p class="stitle">Win Equity</p>
          <div class="tip-wrap tip-down tip-right">
            <span class="tip-icon">?</span>
            <div class="tip-box"><strong style="color:var(--gold)">Equity</strong> é a sua % de chance de ganhar a mão se ela fosse jogada até o final milhares de vezes. Ex: 70% de equity = você ganha 7 em cada 10 cenários simulados.</div>
          </div>
        </div>
        <span class="hand-badge" id="hand-badge" style="display:none"><span style="color:var(--gold)">♠</span><span id="hand-name">—</span></span>
      </div>
      <div class="flex gap-2 mb-4">
        <span id="sp-pre"   class="text-xs px-3 py-1 rounded-full font-semibold" style="background:rgba(201,168,76,.15);border:1px solid rgba(201,168,76,.3);color:var(--gold);letter-spacing:.1em">PRÉ-FLOP</span>
        <span id="sp-flop"  class="text-xs px-3 py-1 rounded-full" style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);color:rgba(255,255,255,.3);letter-spacing:.1em">FLOP</span>
        <span id="sp-turn"  class="text-xs px-3 py-1 rounded-full" style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);color:rgba(255,255,255,.3);letter-spacing:.1em">TURN</span>
        <span id="sp-river" class="text-xs px-3 py-1 rounded-full" style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);color:rgba(255,255,255,.3);letter-spacing:.1em">RIVER</span>
      </div>
      <div class="flex justify-around items-center py-2 gauges-row">
        <div class="gauge"><svg class="g-svg" width="130" height="130" viewBox="0 0 130 130"><circle class="g-track" cx="65" cy="65" r="50"/><circle class="g-fill" id="gc-win"  cx="65" cy="65" r="50" stroke="#00d472"    stroke-dasharray="314" stroke-dashoffset="314"/></svg><div class="g-center"><span class="g-pct" id="win-pct"  style="color:#00d472">—</span><span class="g-lbl" style="color:#00d472">VITÓRIA</span></div></div>
        <div class="gauge"><svg class="g-svg" width="130" height="130" viewBox="0 0 130 130"><circle class="g-track" cx="65" cy="65" r="50"/><circle class="g-fill" id="gc-tie"  cx="65" cy="65" r="50" stroke="var(--gold)" stroke-dasharray="314" stroke-dashoffset="314"/></svg><div class="g-center"><span class="g-pct" id="tie-pct"  style="color:var(--gold)">—</span><span class="g-lbl" style="color:var(--gold-dim)">EMPATE</span></div></div>
        <div class="gauge"><svg class="g-svg" width="130" height="130" viewBox="0 0 130 130"><circle class="g-track" cx="65" cy="65" r="50"/><circle class="g-fill" id="gc-lose" cx="65" cy="65" r="50" stroke="#e74c3c"    stroke-dasharray="314" stroke-dashoffset="314"/></svg><div class="g-center"><span class="g-pct" id="lose-pct" style="color:#e74c3c">—</span><span class="g-lbl" style="color:#c0392b">DERROTA</span></div></div>
      </div>
      <div id="eq-bars" style="display:none" class="mt-3 space-y-2">
        <div class="flex items-center gap-3"><span class="text-xs w-14 font-mono" id="wp" style="color:#00d472">0%</span><div class="bar-track flex-1"><div class="bar-fill bw" id="bw" style="width:0%"></div></div></div>
        <div class="flex items-center gap-3"><span class="text-xs w-14 font-mono" id="tp" style="color:var(--gold)">0%</span><div class="bar-track flex-1"><div class="bar-fill bt" id="bt" style="width:0%"></div></div></div>
        <div class="flex items-center gap-3"><span class="text-xs w-14 font-mono" id="lp" style="color:#e74c3c">0%</span><div class="bar-track flex-1"><div class="bar-fill bl" id="bl" style="width:0%"></div></div></div>
      </div>
    </div>
    <div class="glass p-5">
      <div class="flex items-center gap-1 mb-4">
        <p class="stitle">Probabilidades de Draw</p>
        <div class="tip-wrap tip-right">
          <span class="tip-icon">?</span>
          <div class="tip-box"><strong style="color:var(--gold)">Draw</strong> é quando você está a uma carta de completar uma mão forte. <strong style="color:var(--gold)">Outs</strong> são as cartas do deck que completam o seu draw. Ex: flush draw = 9 outs = ~19% de chance no próximo street.</div>
        </div>
      </div>
      <div id="draws-box"><p class="text-xs text-center py-6" style="color:rgba(255,255,255,.2);letter-spacing:.1em">CALCULE PARA VER OS DRAWS DISPONÍVEIS</p></div>
    </div>
    <div class="glass p-5" id="winning-cards-panel" style="display:none">
      <div class="flex items-center justify-between mb-2">
        <div class="flex items-center gap-2">
          <span style="font-size:15px">⚠️</span>
          <p class="stitle">Mãos que te Vencem</p>
          <div class="tip-wrap">
            <span class="tip-icon">?</span>
            <div class="tip-box">Todos os pares de cartas que o oponente poderia ter e que ganhariam de você com o board atual. Útil para saber o quão segura está sua mão.</div>
          </div>
        </div>
        <span id="beat-badge" class="text-xs font-mono px-2 py-0.5 rounded" style="border:1px solid rgba(231,76,60,.3);background:rgba(231,76,60,.1);color:#e74c3c">—</span>
      </div>
      <p class="text-xs mb-4" style="color:rgba(255,255,255,.3);line-height:1.5">Todos os pares possíveis no deck que superam sua mão atual.</p>
      <!-- Sem limite de altura — mostra todos os grupos -->
      <div id="winning-cards-box"></div>
    </div>
    <!-- Referência compacta — linha única -->
    <div class="glass px-4 py-3 ref-table-wrap">
      <div class="flex items-center justify-between flex-wrap gap-2">
        <span class="stitle" style="font-size:10px">Regra dos Outs</span>
        <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs font-mono" style="color:rgba(255,255,255,.4)">
          <span>4 outs <span style="color:var(--cream)">8.7%</span></span>
          <span>8 outs <span style="color:var(--cream)">17.4%</span></span>
          <span>9 outs <span style="color:var(--cream)">19.6%</span></span>
          <span style="color:var(--gold-dim)">Turn / River</span>
        </div>
      </div>
    </div>
  </div>
</div>
</div>

<!-- CONFRONTO -->
<div id="mode-compare" style="display:none">
<div class="flex gap-4 cols">
  <div class="flex flex-col gap-4 left-col" style="width:420px;flex-shrink:0">
    <div class="glass p-4">
      <div class="flex items-center justify-between mb-3">
        <p class="stitle">Jogadores</p>
        <div class="flex items-center gap-2">
          <button onclick="chgPlayers(-1)" class="w-8 h-8 rounded font-mono font-bold text-lg flex items-center justify-center" style="background:rgba(201,168,76,.1);border:1px solid rgba(201,168,76,.2);color:var(--gold)">−</button>
          <span id="nhd" class="w-8 text-center font-mono font-bold text-lg" style="color:var(--gold)">2</span>
          <button onclick="chgPlayers(1)"  class="w-8 h-8 rounded font-mono font-bold text-lg flex items-center justify-center" style="background:rgba(201,168,76,.1);border:1px solid rgba(201,168,76,.2);color:var(--gold)">+</button>
          <span class="text-xs font-mono" style="color:rgba(255,255,255,.25)">(2–8)</span>
        </div>
      </div>
      <div id="players-list" class="space-y-2 mb-4"></div>

      <!-- Board do Confronto -->
      <div class="mb-4">
        <div class="flex items-center justify-between mb-2">
          <p class="stitle">Board (opcional)</p>
          <div class="flex gap-2 items-center">
            <span id="cmp-street-badge" class="text-xs px-2 py-0.5 rounded font-semibold" style="background:rgba(201,168,76,.1);border:1px solid rgba(201,168,76,.2);color:var(--gold);letter-spacing:.1em">PRÉ-FLOP</span>
            <button onclick="clearCmpBoard()" class="text-xs" style="color:rgba(255,255,255,.3);background:none;border:none;cursor:pointer;font-family:'Rajdhani',sans-serif;letter-spacing:.05em;" onmouseover="this.style.color='#e74c3c'" onmouseout="this.style.color='rgba(255,255,255,.3)'">limpar</button>
          </div>
        </div>
        <div class="flex gap-2 flex-wrap" id="cmp-board-slots"></div>
      </div>
      
      <div class="flex gap-3">
        <button class="btn-calc flex-1 flex items-center justify-center gap-2" id="cmp-btn" onclick="runCompare()">
          <span id="cmp-txt">CALCULAR CONFRONTO</span><div class="loader" id="cmp-loader"></div>
        </button>
        <button class="btn-rst" onclick="resetCmp()">RESET</button>
      </div>
      <div class="progress-wrap" id="cmp-prog-wrap" style="display:none"><div class="progress-fill" id="cmp-prog-fill"></div></div>
    </div>
    <div class="glass p-4 flex-1">
      <div class="flex items-center justify-between mb-3">
        <p class="stitle">Deck</p>
        <div class="flex gap-1">
          <button class="stab on" id="ctab-all" onclick="filterSuit('all','cmp')" style="font-size:11px;font-family:'Rajdhani',sans-serif;font-weight:600;letter-spacing:.1em;color:var(--gold)">TODOS</button>
          <button class="stab" id="ctab-s" onclick="filterSuit('s','cmp')" style="color:#aaa">♠</button>
          <button class="stab" id="ctab-h" onclick="filterSuit('h','cmp')" style="color:var(--red-suit)">♥</button>
          <button class="stab" id="ctab-d" onclick="filterSuit('d','cmp')" style="color:var(--red-suit)">♦</button>
          <button class="stab" id="ctab-c" onclick="filterSuit('c','cmp')" style="color:#aaa">♣</button>
        </div>
      </div>
      <div id="cmp-deck" style="display:flex;flex-wrap:wrap;gap:4px;max-height:320px;overflow-y:auto;padding:4px"></div>
    </div>
  </div>
  <div class="flex-1 flex flex-col gap-4">

    <!-- MESA DE POKER -->
    <div class="glass overflow-hidden" id="cmp-table-panel">
      <div class="poker-table-wrap">
        <div class="poker-table-inner" id="poker-table-canvas"></div>
      </div>
    </div>

    <div class="glass p-4" id="cmp-hint">
      <div class="flex items-start gap-3">
        <span style="font-size:26px;opacity:.35;flex-shrink:0;margin-top:2px">♠♥</span>
        <div>
          <p class="font-semibold mb-1" style="color:var(--gold);letter-spacing:.05em">Como usar o Confronto</p>
          <p class="text-sm" style="color:rgba(255,255,255,.45);line-height:1.6">
            <strong style="color:rgba(255,255,255,.7)">1.</strong> Clique em um jogador e atribua suas 2 cartas.<br>
            <strong style="color:rgba(255,255,255,.7)">2.</strong> Opcionalmente, adicione cartas ao <strong style="color:var(--gold)">Board</strong> (flop/turn/river).<br>
            <strong style="color:rgba(255,255,255,.7)">3.</strong> Clique em <strong style="color:var(--gold)">CALCULAR CONFRONTO</strong>.
          </p>
        </div>
      </div>
    </div>
    <div class="glass p-4" id="cmp-global" style="display:none">
      <p id="cmp-global-title" class="stitle mb-3">Equity Comparativa — Pré-Flop</p>
      <div class="global-bar" id="cmp-gbar"></div>
      <div class="flex flex-wrap gap-3 mt-2" id="cmp-legend"></div>
    </div>
    <div id="cmp-results" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px"></div>
  </div>
</div>
</div>

<!-- VALE A PENA? -->
<div id="mode-ev" style="display:none">
<div class="flex gap-4 cols">

  <!-- ESQUERDA: inputs -->
  <div class="flex flex-col gap-3 left-col" style="width:380px;flex-shrink:0">

    <!-- Painel de inputs -->
    <div class="glass p-6">
      <p class="stitle mb-5">Situação na Mesa</p>

      <!-- Pot -->
      <div class="mb-5">
        <label class="text-xs mb-2 block" style="color:rgba(255,255,255,.4);letter-spacing:.12em">TAMANHO DO POT</label>
        <div class="ev-field-wrap">
          <input id="ev-pot" type="number" class="ev-input ev-input-lg" min="1" step="1" value="10"
            oninput="calcEV(); if(activeBetMult!==null)setBetPreset(activeBetMult)"/>
          <span class="ev-field-unit" style="font-size:14px">BB</span>
        </div>
      </div>

      <!-- Aposta -->
      <div class="mb-5">
        <label class="text-xs mb-3 block" style="color:rgba(255,255,255,.4);letter-spacing:.12em">APOSTA DO OPONENTE</label>
        <div class="grid grid-cols-5 gap-2 mb-3" id="ev-bet-btns">
          <button class="ev-bet-btn ev-bet-btn-lg" data-mult="0.25" onclick="setBetPreset(0.25)">
            <span style="font-size:14px">¼</span><span style="font-size:9px;opacity:.6;display:block">pot</span>
          </button>
          <button class="ev-bet-btn ev-bet-btn-lg" data-mult="0.5" onclick="setBetPreset(0.5)">
            <span style="font-size:14px">½</span><span style="font-size:9px;opacity:.6;display:block">pot</span>
          </button>
          <button class="ev-bet-btn ev-bet-btn-lg active-bet" data-mult="0.66" onclick="setBetPreset(0.66)">
            <span style="font-size:14px">⅔</span><span style="font-size:9px;opacity:.6;display:block">pot</span>
          </button>
          <button class="ev-bet-btn ev-bet-btn-lg" data-mult="1" onclick="setBetPreset(1)">
            <span style="font-size:14px">1×</span><span style="font-size:9px;opacity:.6;display:block">pot</span>
          </button>
          <button class="ev-bet-btn ev-bet-btn-lg" data-mult="2" onclick="setBetPreset(2)">
            <span style="font-size:14px">AI</span><span style="font-size:9px;opacity:.6;display:block">all-in</span>
          </button>
        </div>
        <div class="ev-field-wrap">
          <input id="ev-bet" type="number" class="ev-input ev-input-lg" min="0" step="0.5" value="6.6" oninput="onBetManual()"/>
          <span class="ev-field-unit" style="font-size:14px">BB</span>
        </div>
      </div>

      <!-- Equity -->
      <div>
        <label class="text-xs mb-2 block" style="color:rgba(255,255,255,.4);letter-spacing:.12em">SUA CHANCE DE GANHAR</label>
        <div class="ev-field-wrap mb-3">
          <input id="ev-equity" type="number" class="ev-input ev-input-lg" min="1" max="99" step="0.5" value="35" oninput="calcEV()"/>
          <span class="ev-field-unit" style="font-size:14px">%</span>
        </div>
        <button onclick="importEquity()" id="import-btn"
          style="width:100%;padding:12px;border-radius:10px;font-family:'Rajdhani',sans-serif;font-weight:700;font-size:13px;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;border:1.5px solid rgba(201,168,76,.4);background:rgba(201,168,76,.08);color:var(--gold);transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px;"
          onmouseover="this.style.background='rgba(201,168,76,.18)';this.style.boxShadow='var(--glow-gold)'"
          onmouseout="this.style.background='rgba(201,168,76,.08)';this.style.boxShadow='none'">
          <span style="font-size:16px">↑</span> IMPORTAR DA CALCULADORA
        </button>
        <p id="import-hint" class="text-xs mt-2" style="color:rgba(255,255,255,.25);line-height:1.5">
          Use a aba <strong style="color:rgba(201,168,76,.5)">Calculadora</strong> para obter sua equity.
        </p>
      </div>
    </div>

    <!-- Composição do pot -->
    <div class="glass p-5" id="ev-pot-summary">
      <p class="stitle mb-4">Composição do Pot</p>
      <!-- Barra proporcional -->
      <div style="display:flex;height:32px;border-radius:8px;overflow:hidden;margin-bottom:12px;gap:2px">
        <div id="ev-pot-fill" style="background:rgba(201,168,76,.35);border-radius:6px;transition:flex .4s ease;flex:10;display:flex;align-items:center;justify-content:center;">
          <span id="ev-pot-pct" style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:var(--gold)">60%</span>
        </div>
        <div id="ev-bet-fill" style="background:rgba(231,76,60,.3);border-radius:6px;transition:flex .4s ease;flex:6.6;display:flex;align-items:center;justify-content:center;">
          <span id="ev-bet-pct" style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:#e74c3c">40%</span>
        </div>
      </div>
      <!-- Labels -->
      <div class="flex justify-between">
        <div>
          <div class="text-xs mb-1" style="color:rgba(255,255,255,.3)">Pot atual</div>
          <div id="ev-pot-lbl" style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:var(--gold)">10 BB</div>
        </div>
        <div style="text-align:center">
          <div class="text-xs mb-1" style="color:rgba(255,255,255,.3)">Aposta</div>
          <div id="ev-bet-lbl" style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:#e74c3c">6.6 BB</div>
        </div>
        <div style="text-align:right">
          <div class="text-xs mb-1" style="color:rgba(255,255,255,.3)">Total</div>
          <div id="ev-total-lbl" style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:rgba(255,255,255,.7)">16.6 BB</div>
        </div>
      </div>
    </div>

  </div>

  <!-- DIREITA: resultado -->
  <div class="flex-1 flex flex-col gap-3">

    <!-- Verdito grande -->
    <div id="ev-verdict" class="verdict-box" style="padding:36px 28px">
      <div id="ev-icon" style="font-size:64px;margin-bottom:14px;line-height:1">🃏</div>
      <div id="ev-title" style="font-family:'Rajdhani',sans-serif;font-size:36px;font-weight:700;letter-spacing:.14em;margin-bottom:12px">Preencha os campos</div>
      <div id="ev-explain" style="font-size:15px;line-height:1.8;color:rgba(255,255,255,.5);max-width:400px;margin:0 auto"></div>
    </div>

    <!-- 3 números-chave -->
    <div class="grid grid-cols-3 gap-3" id="ev-stats-row" style="display:none!important">
      <div class="glass p-4 text-center">
        <div class="text-xs mb-2" style="color:rgba(255,255,255,.3);letter-spacing:.1em">POT ODDS</div>
        <div id="ev-stat-odds" style="font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:700;color:var(--gold)">—</div>
      </div>
      <div class="glass p-4 text-center">
        <div class="text-xs mb-2" style="color:rgba(255,255,255,.3);letter-spacing:.1em">MÍNIMO</div>
        <div id="ev-stat-min" style="font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:700;color:#e8c96d">—</div>
      </div>
      <div class="glass p-4 text-center">
        <div class="text-xs mb-2" style="color:rgba(255,255,255,.3);letter-spacing:.1em">EV DO CALL</div>
        <div id="ev-stat-ev" style="font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:700">—</div>
      </div>
    </div>

    <!-- Barra equity vs mínimo -->
    <div class="glass p-5" id="ev-bar-panel" style="display:none">
      <div class="flex justify-between items-center mb-4">
        <span class="stitle">Sua Equity vs Mínimo Necessário</span>
        <div id="ev-bar-summary" class="text-xs font-mono"></div>
      </div>
      <div class="needs-bar" style="height:18px;margin-bottom:24px;border-radius:9px">
        <div class="needs-fill" id="ev-needs-fill" style="border-radius:9px"></div>
        <div class="needs-marker" id="ev-needs-marker"></div>
        <div class="needs-marker-label" id="ev-needs-label"></div>
      </div>
      <div class="flex justify-between text-xs font-mono" style="color:rgba(255,255,255,.2)">
        <span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span>
      </div>
    </div>

    <!-- Cálculo matemático -->
    <div class="glass p-5 flex-1" id="ev-math-wrap" style="display:none">
      <p class="stitle mb-4">Cálculo Detalhado</p>
      <div id="ev-math-content"></div>
    </div>

  </div>
</div>
</div>

</main>
<footer class="text-center py-3 text-xs font-mono" style="color:rgba(201,168,76,.2);border-top:1px solid rgba(201,168,76,.07)">
  POKERCALC · Monte Carlo Engine · Texas Hold'em
</footer>
</div>

<script>
const RANKS=['2','3','4','5','6','7','8','9','T','J','Q','K','A'];
const SUITS=['s','h','d','c'];
const SYM={s:'♠',h:'♥',d:'♦',c:'♣'};
const CIRC=2*Math.PI*50;
const PCOLS=['#00d472','#e8c96d','#e74c3c','#3b82f6','#a855f7','#f97316','#06b6d4','#ec4899','#84cc16'];
const HCOLS={8:'#f97316',7:'#a855f7',6:'#3b82f6',5:'#06b6d4',4:'#eab308',3:'#e67e22',2:'#e74c3c',1:'#94a3b8',0:'#64748b'};

function isRed(s){return s==='h'||s==='d';}
function lbl(c){const r=c.slice(0,-1),s=c.slice(-1);return{r:r==='T'?'10':r,s:SYM[s],red:isRed(s)};}

// ── STATE ──
let mode='calc';
let hole=[null,null],board=[null,null,null,null,null],calcSlot={type:'hole',index:0},calcSuit='all';
let cmpN=2,cmpHands=Array.from({length:8},()=>[null,null]),cmpActive=0,cmpSuit='all';
let cmpBoard=[null,null,null,null,null];  // board compartilhado do confronto
let cmpBoardActive=false;                  // true = próximo clique vai para o board

// ── MODE ──
function switchMode(m){
  mode=m;
  document.getElementById('mode-calc').style.display=m==='calc'?'block':'none';
  document.getElementById('mode-compare').style.display=m==='compare'?'block':'none';
  document.getElementById('mode-ev').style.display=m==='ev'?'block':'none';
  ['calc','compare','ev'].forEach(id=>{
    const t=document.getElementById('tab-'+id);
    if(t) t.classList.toggle('active',m===id);
  });
  // Sync bottom nav
  document.querySelectorAll('.mob-tab').forEach(t=>t.classList.remove('active'));
  const mt=document.getElementById('mob-tab-'+m);
  if(mt) mt.classList.add('active');
}

// ── MOBILE: bottom nav e deck por naipe ──────────────
const SUIT_LABEL_MAP={s:'♠',h:'♥',d:'♦',c:'♣'};
const SUIT_COLOR_MAP={s:'#9ca3af',h:'var(--red-suit)',d:'var(--red-suit)',c:'#9ca3af'};

function isMobile(){return window.innerWidth<=600;}

function buildMobileDeck(){
  const mob=document.getElementById('deck-mobile');
  if(!mob)return;
  const used=[...hole,...board].filter(Boolean);
  mob.innerHTML='';
  for(const s of SUITS){
    const row=document.createElement('div');
    row.style.cssText='display:flex;align-items:center;gap:4px;';
    // Ícone do naipe
    const lbl2=document.createElement('span');
    lbl2.className='deck-suit-label';
    lbl2.style.color=SUIT_COLOR_MAP[s];
    lbl2.textContent=SUIT_LABEL_MAP[s];
    row.appendChild(lbl2);
    // Linha de cartas
    const cardsRow=document.createElement('div');
    cardsRow.className='deck-suit-row';
    for(const r of RANKS){
      const c=r+s;const{r:dr,s:ds,red}=lbl(c);
      const el=document.createElement('div');
      el.className=`playing-card ${red?'red-card':'black-card'}`;
      if(used.includes(c))el.classList.add('used');
      el.innerHTML=`<span class="card-rank">${dr}</span><span class="card-suit">${ds}</span>`;
      el.onclick=()=>calcPick(c);
      cardsRow.appendChild(el);
    }
    row.appendChild(cardsRow);
    mob.appendChild(row);
  }
}

function applyLayout(){
  const mobile=isMobile();
  const mobDeck=document.getElementById('deck-mobile');
  const dskDeck=document.getElementById('deck-grid-wrap-desktop');
  if(mobDeck) mobDeck.style.display=mobile?'flex':'none';
  if(dskDeck) dskDeck.style.display=mobile?'none':'block';
  if(mobile) buildMobileDeck();
  else buildDeck();
}
window.addEventListener('resize',applyLayout);

// ── SPLASH SCREEN ──
(function(){
  const splash = document.getElementById('splash');
  if(!splash) return;
  // Esconde o splash quando a página estiver pronta
  function hideSplash(){
    splash.classList.add('hidden');
    setTimeout(()=>{ splash.style.display='none'; }, 500);
  }
  // Se a página já carregou (cache), esconde imediatamente
  if(document.readyState === 'complete'){
    setTimeout(hideSplash, 800);
  } else {
    window.addEventListener('load', ()=>{ setTimeout(hideSplash, 800); });
  }
  // Timeout de segurança: esconde após 15s mesmo que não carregue
  setTimeout(hideSplash, 15000);
})();
function filterSuit(s,ctx){
  if(ctx==='calc'){
    calcSuit=s;
    document.querySelectorAll('#mode-calc .stab').forEach(t=>t.classList.remove('on'));
    document.getElementById('tab-'+s).classList.add('on');
    // No mobile o filtro de naipe não se aplica (deck já é por naipe)
    // Mas mantém buildDeck para desktop e buildMobileDeck para mobile
    if(isMobile()) buildMobileDeck(); else buildDeck();
  } else {
    cmpSuit=s;
    document.querySelectorAll('#mode-compare .stab').forEach(t=>t.classList.remove('on'));
    document.getElementById('ctab-'+s).classList.add('on');
    buildCmpDeck();
  }
}

// ── CALC DECK ──
function buildDeck(){
  const g=document.getElementById('deck-grid');g.innerHTML='';
  const used=[...hole,...board].filter(Boolean);
  for(const s of SUITS){
    if(calcSuit!=='all'&&s!==calcSuit)continue;
    for(const r of RANKS){
      const c=r+s,{r:dr,s:ds,red}=lbl(c);
      const el=document.createElement('div');
      el.className=`playing-card ${red?'red-card':'black-card'}`;
      if(used.includes(c))el.classList.add('used');
      el.innerHTML=`<span class="card-rank">${dr}</span><span class="card-suit">${ds}</span>`;
      el.onclick=()=>calcPick(c);g.appendChild(el);
    }
  }
}
function activateCalcSlot(type,idx){calcSlot={type,index:idx};renderCalcSlots();}
function calcPick(c){
  const used=[...hole,...board].filter(Boolean);if(used.includes(c))return;
  const{type,index}=calcSlot;
  if(type==='hole')hole[index]=c;else board[index]=c;
  renderCalcSlots();
  if(isMobile()) buildMobileDeck(); else buildDeck();
  updateStreet();
  if(type==='hole'){const nx=hole.findIndex(x=>!x);if(nx!==-1)activateCalcSlot('hole',nx);else{const bn=board.findIndex(x=>!x);if(bn!==-1)activateCalcSlot('board',bn);}}
  else{const nx=board.findIndex(x=>!x);if(nx!==-1)activateCalcSlot('board',nx);}
}
function renderCalcSlots(){
  [{id:'hole-slots',arr:hole,type:'hole',lbls:['A','B']},
   {id:'board-slots',arr:board,type:'board',lbls:['FLOP','FLOP','FLOP','TURN','RIVER']}].forEach(({id,arr,type,lbls})=>{
    const cont=document.getElementById(id);cont.innerHTML='';
    arr.forEach((c,i)=>{
      const sl=document.createElement('div');const isAct=calcSlot.type===type&&calcSlot.index===i;
      if(c){const{r,s,red}=lbl(c);sl.className=`slot filled ${red?'red-card':'black-card'}`;sl.innerHTML=`<span class="slot-rank">${r}</span><span class="slot-suit">${s}</span>`;sl.onclick=()=>{if(type==='hole')hole[i]=null;else board[i]=null;renderCalcSlots();buildDeck();activateCalcSlot(type,i);updateStreet();};}
      else{sl.className='slot'+(isAct?' active':'');sl.innerHTML=`<span style="color:rgba(201,168,76,.35);font-size:9px;letter-spacing:.1em">${lbls[i]}</span>`;sl.onclick=()=>activateCalcSlot(type,i);}
      cont.appendChild(sl);
    });
  });
  renderCalcTable();
}
function updateStreet(){
  const n=board.filter(Boolean).length;const cur={0:'pre',3:'flop',4:'turn',5:'river'}[n]||'pre';
  ['pre','flop','turn','river'].forEach(s=>{
    const el=document.getElementById('sp-'+s);if(!el)return;
    if(s===cur){el.style.background='rgba(201,168,76,.15)';el.style.borderColor='rgba(201,168,76,.3)';el.style.color='var(--gold)';}
    else{el.style.background='rgba(255,255,255,.04)';el.style.borderColor='rgba(255,255,255,.08)';el.style.color='rgba(255,255,255,.3)';}
  });
}
function setGauge(id,pct){const el=document.getElementById(id);if(el)el.style.strokeDashoffset=CIRC-(pct/100)*CIRC;}


// ── MESA DA CALCULADORA ──────────────────────────────────
function renderCalcTable(winPct){
  const canvas=document.getElementById('calc-poker-table');
  if(!canvas)return;
  const W=canvas.offsetWidth||600;
  const H=canvas.offsetHeight||(W*0.58);
  if(W<10)return;
  canvas.innerHTML='';
  const cx=W/2,cy=H/2;

  // SVG feltro
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width','100%');svg.setAttribute('height','100%');
  svg.style.cssText='position:absolute;inset:0;pointer-events:none;';
  svg.innerHTML=`<defs>
    <filter id="cshadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="5" stdDeviation="12" flood-color="rgba(0,0,0,.8)"/>
    </filter>
    <radialGradient id="cfelt" cx="50%" cy="42%" r="62%">
      <stop offset="0%" stop-color="#1e6b34"/>
      <stop offset="65%" stop-color="#145228"/>
      <stop offset="100%" stop-color="#0b3318"/>
    </radialGradient>
    <radialGradient id="cshine" cx="50%" cy="35%" r="55%">
      <stop offset="0%" stop-color="rgba(255,255,255,.07)"/>
      <stop offset="100%" stop-color="rgba(0,0,0,0)"/>
    </radialGradient>
  </defs>
  <ellipse cx="${cx}" cy="${cy}" rx="${W*0.475}" ry="${H*0.452}" fill="#4a2e0a" filter="url(#cshadow)"/>
  <ellipse cx="${cx}" cy="${cy}" rx="${W*0.438}" ry="${H*0.418}" fill="url(#cfelt)"/>
  <ellipse cx="${cx}" cy="${cy*0.75}" rx="${W*0.31}" ry="${H*0.19}" fill="url(#cshine)"/>
  <ellipse cx="${cx}" cy="${cy}" rx="${W*0.388}" ry="${H*0.362}" fill="none" stroke="rgba(201,168,76,.18)" stroke-width="1.5"/>
  <text x="${cx}" y="${cy+6}" text-anchor="middle" font-family="Rajdhani,sans-serif" font-size="${W*0.022}" font-weight="700" fill="rgba(255,255,255,.05)" letter-spacing="0.18em">POKERCALC</text>`;
  canvas.appendChild(svg);

  const cw=Math.max(Math.min(W*0.06,42),22);
  const ch=cw*1.42;
  const gap=cw*0.2;

  // Board no centro
  const bStartX=cx-(5*cw+4*gap)/2;
  const bY=cy-ch/2;
  for(let i=0;i<5;i++){
    const c=board[i];
    const el=document.createElement('div');
    el.className='t-card'+(c?(isRed(c.slice(-1))?' red':' black'):' empty');
    el.style.cssText=`position:absolute;left:${bStartX+i*(cw+gap)}px;top:${bY}px;width:${cw}px;height:${ch}px;border-radius:${cw*0.13}px;`;
    if(c){const{r,s}=lbl(c);el.innerHTML=`<span class="tr" style="font-size:${cw*0.37}px">${r}</span><span class="ts" style="font-size:${cw*0.37}px">${s}</span>`;}
    else{el.innerHTML=`<span style="font-size:9px;color:rgba(255,255,255,.15)">·</span>`;}
    canvas.appendChild(el);
  }

  // Oponentes com cartas viradas
  // Mesmo sistema de ângulos do Confronto: 270=topo, 90=fundo(herói)
  // Herói fica em 90° (fundo), oponentes ficam no restante
  const nOpp=parseInt(document.getElementById('opponents')?.value||1);
  const allOppAngles=[270,225,315,180,0,135,45]; // topo e lados, nunca fundo
  const oppAngles=allOppAngles.slice(0,nOpp);
  const RX=W*0.41,RY=H*0.375;
  oppAngles.forEach((deg,i)=>{
    const rad=deg*Math.PI/180;
    const sx=cx+RX*Math.cos(rad);
    const sy=cy+RY*Math.sin(rad);
    const ocw=Math.max(Math.min(W*0.044,30),16);
    const och=ocw*1.42;
    const seat=document.createElement('div');
    seat.style.cssText=`position:absolute;left:${sx}px;top:${sy}px;transform:translate(-50%,-50%);display:flex;flex-direction:column;align-items:center;gap:3px;`;
    const cRow=document.createElement('div');
    cRow.style.cssText='display:flex;gap:3px;';
    for(let ci=0;ci<2;ci++){
      const card=document.createElement('div');
      // Carta virada simples — fundo azul escuro sem detalhes
      card.style.cssText=`width:${ocw}px;height:${och}px;border-radius:${ocw*0.13}px;
        background:#0d2040;
        border:1px solid rgba(80,120,180,.25);
        box-shadow:0 2px 6px rgba(0,0,0,.6);`;
      cRow.appendChild(card);
    }
    seat.appendChild(cRow);
    const lbl3=document.createElement('span');
    lbl3.style.cssText=`font-family:'Rajdhani',sans-serif;font-weight:700;font-size:${Math.max(W*0.013,8)}px;color:rgba(255,255,255,.25);letter-spacing:.06em;`;
    lbl3.textContent=`J${i+1}`;
    seat.appendChild(lbl3);
    canvas.appendChild(seat);
  });

  // Herói (posição de baixo)
  const heroY=cy+RY*0.95;
  const hcw=Math.max(Math.min(W*0.058,40),22);
  const hch=hcw*1.42;
  const hero=document.createElement('div');
  hero.style.cssText=`position:absolute;left:${cx}px;top:${heroY}px;transform:translate(-50%,-50%);display:flex;flex-direction:column;align-items:center;gap:4px;`;

  // Cartas do herói
  const hRow=document.createElement('div');
  hRow.style.cssText='display:flex;gap:4px;';
  for(let ci=0;ci<2;ci++){
    const c=hole[ci];
    const cardEl=document.createElement('div');
    cardEl.className='t-card'+(c?(isRed(c.slice(-1))?' red':' black'):' empty');
    cardEl.style.cssText=`position:relative;width:${hcw}px;height:${hch}px;border-radius:${hcw*0.13}px;`;
    if(c){
      const{r,s}=lbl(c);
      cardEl.innerHTML=`<span class="tr" style="font-size:${hcw*0.37}px">${r}</span><span class="ts" style="font-size:${hcw*0.37}px">${s}</span>`;
      if(winPct!==undefined){
        const glow=winPct>=50?'rgba(0,212,114,.6)':winPct>=35?'rgba(201,168,76,.6)':'rgba(231,76,60,.4)';
        cardEl.style.boxShadow=`0 0 14px 3px ${glow},0 3px 8px rgba(0,0,0,.5)`;
      }
    } else {
      cardEl.innerHTML=`<span style="font-size:9px;color:rgba(255,255,255,.2)">?</span>`;
    }
    hRow.appendChild(cardEl);
  }
  hero.appendChild(hRow);

  // Badge de equity
  if(winPct!==undefined){
    const winColor=winPct>=60?'#00d472':winPct>=40?'#e8c96d':'#e74c3c';
    const badge=document.createElement('div');
    badge.style.cssText=`padding:3px 12px;border-radius:12px;
      font-family:'JetBrains Mono',monospace;font-weight:700;font-size:${Math.max(W*0.024,13)}px;
      background:rgba(0,0,0,.65);border:2px solid ${winColor};color:${winColor};
      white-space:nowrap;box-shadow:0 0 12px ${winColor}55;letter-spacing:.04em;`;
    badge.textContent=winPct+'%';
    hero.appendChild(badge);
  }

  const youLbl=document.createElement('span');
  youLbl.style.cssText=`font-family:'Rajdhani',sans-serif;font-weight:700;font-size:${Math.max(W*0.016,9)}px;color:rgba(255,255,255,.35);letter-spacing:.1em;`;
  youLbl.textContent='VOCÊ';
  hero.appendChild(youLbl);
  canvas.appendChild(hero);
}

// ── POLLING ──
async function pollJob(jid, onProg, onDone, onErr, onPartial){
  let attempts = 0;
  const iv=setInterval(async()=>{
    attempts++;
    if(attempts > 1200){ clearInterval(iv); onErr('Tempo esgotado.'); return; }
    try{
      const r=await fetch('/status/'+jid);
      if(r.status===404){ clearInterval(iv); onErr('Servidor reiniciou. Tente calcular novamente.'); return; }
      if(r.status===429){ clearInterval(iv); onErr('Muitas requisições. Aguarde e tente novamente.'); return; }
      const j=await r.json();
      if(j.error && !j.status){ clearInterval(iv); onErr(j.error); return; }
      if(j.progress !== undefined) onProg(j.progress);
      // Resultado parcial disponível — mostra imediatamente
      if(j.partial && onPartial) onPartial(j.partial);
      if(j.status==='done'){ clearInterval(iv); onDone(j.result); }
      if(j.status==='error'){ clearInterval(iv); onErr(j.error||'Erro no cálculo.'); }
    }catch(e){
      if(attempts > 20){ clearInterval(iv); onErr('Erro de conexão.'); }
    }
  },250);
}
function setProgress(prefix,pct){
  const wrap=document.getElementById(prefix+'-prog-wrap');
  const fill=document.getElementById(prefix+'-prog-fill');
  if(wrap)wrap.style.display='block';
  if(fill)fill.style.width=pct+'%';
}
function hideProgress(prefix){
  const wrap=document.getElementById(prefix+'-prog-wrap');
  if(wrap)wrap.style.display='none';
}

// ── CALCULATE ──
async function doCalculate(){
  const h=hole.filter(Boolean);if(h.length<2){flash('Selecione as 2 cartas da mão.');return;}
  const b=board.filter(Boolean);setLoading('calc',true);setProgress('calc',0);
  const sims = 20000;
  try{
    const res=await fetch('/calculate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({hole_cards:h,board:b,opponents:+document.getElementById('opponents').value,simulations:sims})});
    const init=await res.json();
    if(init.error){flash(init.error);setLoading('calc',false);hideProgress('calc');return;}
    pollJob(init.job_id,
      p=>setProgress('calc',p),
      d=>{
        showCalcResults(d, false);
        setLoading('calc',false);
        setTimeout(()=>hideProgress('calc'),600);
      },
      e=>{flash(e||'Erro.');setLoading('calc',false);hideProgress('calc');},
      // Resultado parcial: mostra imediatamente com indicador ~
      partial=>{ showCalcResults(partial, true); }
    );
  }catch{flash('Erro de conexão.');setLoading('calc',false);hideProgress('calc');}
}

function showCalcResults(d, isPartial){
  const{equity:eq,draws,hand_name,simulations,margin_of_error,beating_hands}=d;
  // Atualiza mesa com equity
  try{ renderCalcTable(eq.win); }catch(e){ console.warn('renderCalcTable:', e); }
  // Save equity for EV import (only on final result)
  if(!isPartial){
    lastCalcEquity = eq.win;
    const hint = document.getElementById('import-hint');
    if(hint){ hint.textContent=`Equity disponível: ${eq.win}% — clique "↑ DA CALC" para importar.`; hint.style.color='rgba(201,168,76,.6)'; }
  }
  const simLabel = isPartial ? `~${simulations.toLocaleString('pt-BR')} (estimativa)` : simulations.toLocaleString('pt-BR')+' simulações';
  document.getElementById('sim-counter').textContent = simLabel;
  const mb=document.getElementById('moe-badge');
  mb.style.display='inline';
  mb.textContent = isPartial ? '~ estimativa' : '± '+margin_of_error+'%';
  mb.style.color  = isPartial ? 'rgba(255,200,80,.6)' : 'var(--gold-dim)';
  setGauge('gc-win',eq.win);setGauge('gc-tie',eq.tie);setGauge('gc-lose',eq.lose);
  document.getElementById('win-pct').textContent=eq.win+'%';
  document.getElementById('tie-pct').textContent=eq.tie+'%';
  document.getElementById('lose-pct').textContent=eq.lose+'%';
  document.getElementById('eq-bars').style.display='block';
  setTimeout(()=>{document.getElementById('bw').style.width=eq.win+'%';document.getElementById('bt').style.width=eq.tie+'%';document.getElementById('bl').style.width=eq.lose+'%';},50);
  document.getElementById('wp').textContent=eq.win+'%';document.getElementById('tp').textContent=eq.tie+'%';document.getElementById('lp').textContent=eq.lose+'%';
  const badge=document.getElementById('hand-badge');badge.style.display='inline-flex';document.getElementById('hand-name').textContent=hand_name;
  const box=document.getElementById('draws-box');const keys=Object.keys(draws);
  if(!keys.length){box.innerHTML='<p class="text-xs text-center py-4" style="color:rgba(255,255,255,.2)">Nenhum draw ativo.</p>';}
  else{
    const icons={flush_draw:'♥ Flush Draw',straight_draw:'→ Straight Draw',set_draw:'▲ Trinca',quads_draw:'◆ Quadra',overcard:'↑ Overcard'};
    const dcols={flush_draw:'#9b59b6',straight_draw:'#3498db',set_draw:'#e67e22',quads_draw:'#e74c3c',overcard:'#1abc9c'};
    box.innerHTML='';
    keys.forEach(k=>{
      const dr=draws[k],col=dcols[k]||'var(--gold)',label=icons[k]||dr.label;
      const p=document.createElement('div');p.className='pill mb-2 fade-in';
      p.innerHTML=`<span style="color:${col};font-weight:700;font-size:12px;min-width:120px">${label}</span>
        <div style="flex:1;height:7px;border-radius:4px;background:rgba(255,255,255,.05);overflow:hidden"><div id="db-${k}" style="height:100%;border-radius:4px;width:0%;background:${col};opacity:.75;transition:width .7s cubic-bezier(.34,1.56,.64,1)"></div></div>
        <span class="font-mono text-xs" style="color:${col};min-width:44px;text-align:right">${dr.pct_next}%</span>
        <span class="font-mono text-xs" style="color:rgba(255,255,255,.3)">${dr.outs}o</span>`;
      box.appendChild(p);setTimeout(()=>{const b=document.getElementById('db-'+k);if(b)b.style.width=Math.min(dr.pct_next*2,100)+'%';},80);
    });
  }
  const wcPanel=document.getElementById('winning-cards-panel');
  if(beating_hands){wcPanel.style.display='block';renderBeatingHands(beating_hands);}
  else wcPanel.style.display='none';
}

function renderBeatingHands(data){
  const box=document.getElementById('winning-cards-box');
  const badge=document.getElementById('beat-badge');
  box.innerHTML='';
  if(data.is_nuts){
    badge.textContent='NUTS ♠';badge.style.color='#00d472';badge.style.borderColor='rgba(0,212,114,.3)';badge.style.background='rgba(0,212,114,.08)';
    box.innerHTML='<p class="text-xs text-center py-3" style="color:#00d472;letter-spacing:.1em">✓ Você tem a melhor mão possível neste board!</p>';return;
  }
  badge.textContent=data.total_beat.toLocaleString('pt-BR')+' combos ('+data.pct_beat+'%)';
  badge.style.color='#e74c3c';badge.style.borderColor='rgba(231,76,60,.3)';badge.style.background='rgba(231,76,60,.1)';
  data.groups.forEach((g,gi)=>{
    const col=HCOLS[g.cat]||'#e74c3c';
    const row=document.createElement('div');row.className='fade-in';row.style.marginBottom='14px';
    const hdr=document.createElement('div');hdr.style.cssText='display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;flex-wrap:wrap;gap:6px;';
    const exHtml=g.examples.map(pair=>{
      const cards=pair.map(c=>{const{r,s,red}=lbl(c);return`<div style="width:30px;height:42px;background:var(--card-bg);border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 2px 6px rgba(0,0,0,.5)"><span style="font-family:'Rajdhani',sans-serif;font-weight:700;font-size:11px;line-height:1;color:${red?'var(--red-suit)':'#1a1a1a'}">${r}</span><span style="font-size:11px;line-height:1;color:${red?'var(--red-suit)':'#1a1a1a'}">${s}</span></div>`;}).join('');
      return`<div style="display:flex;gap:3px;padding:3px 5px;border-radius:6px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07)">${cards}</div>`;
    }).join('');
    hdr.innerHTML=`<div style="display:flex;align-items:center;gap:8px"><span style="color:${col};font-weight:700;font-size:13px;letter-spacing:.05em">${g.hand}</span><div style="display:flex;flex-wrap:wrap;gap:4px">${exHtml}</div></div><div style="text-align:right;flex-shrink:0"><span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${col}">${g.count.toLocaleString('pt-BR')}</span><span style="font-size:10px;color:rgba(255,255,255,.3)"> combos · ${g.pct}%</span></div>`;
    row.appendChild(hdr);
    const track=document.createElement('div');track.style.cssText='height:5px;border-radius:3px;background:rgba(255,255,255,.05);overflow:hidden';
    const fill=document.createElement('div');fill.style.cssText=`height:100%;border-radius:3px;width:0%;background:${col};opacity:.75;transition:width .7s cubic-bezier(.34,1.56,.64,1)`;
    track.appendChild(fill);row.appendChild(track);
    if(gi<data.groups.length-1){const sep=document.createElement('div');sep.style.cssText='height:1px;background:rgba(255,255,255,.05);margin-top:12px;';row.appendChild(sep);}
    box.appendChild(row);setTimeout(()=>{fill.style.width=Math.min(g.pct*4,100)+'%';},80+gi*40);
  });
  if(data.total_tie>0){const tie=document.createElement('p');tie.style.cssText='font-size:11px;color:rgba(255,255,255,.3);margin-top:10px;font-family:"JetBrains Mono",monospace;';tie.textContent='+ '+data.total_tie.toLocaleString('pt-BR')+' combos empatam';box.appendChild(tie);}
}

function resetCalc(){
  hole=[null,null];board=[null,null,null,null,null];calcSlot={type:'hole',index:0};
  renderCalcSlots();applyLayout();activateCalcSlot('hole',0);updateStreet();
  ['win','tie','lose'].forEach(k=>{setGauge('gc-'+k,0);document.getElementById(k+'-pct').textContent='—';});
  document.getElementById('eq-bars').style.display='none';document.getElementById('hand-badge').style.display='none';
  document.getElementById('winning-cards-panel').style.display='none';
  document.getElementById('sim-counter').textContent='— simulações';document.getElementById('moe-badge').style.display='none';
  document.getElementById('draws-box').innerHTML='<p class="text-xs text-center py-6" style="color:rgba(255,255,255,.2);letter-spacing:.1em">CALCULE PARA VER OS DRAWS DISPONÍVEIS</p>';
}

// ── COMPARE ──
function chgPlayers(d){cmpN=Math.max(2,Math.min(8,cmpN+d));document.getElementById('nhd').textContent=cmpN;if(cmpActive>=cmpN)cmpActive=0;renderPlayers();buildCmpDeck();}
function selectPlayer(i){cmpActive=i;renderPlayers();buildCmpDeck();}
function renderPlayers(){
  const cont=document.getElementById('players-list');cont.innerHTML='';
  for(let i=0;i<cmpN;i++){
    const col=PCOLS[i%PCOLS.length];const hand=cmpHands[i];const isAct=i===cmpActive;const done=hand[0]&&hand[1];
    const panel=document.createElement('div');
    panel.className='player-panel'+(isAct?' active-player':done?' done':'');panel.onclick=()=>selectPlayer(i);
    panel.innerHTML=`<div style="display:flex;align-items:center;justify-content:space-between;"><div style="display:flex;align-items:center;gap:10px"><span style="color:${col};font-weight:700;font-size:13px;letter-spacing:.06em;min-width:70px">Jogador ${i+1}</span><div style="display:flex;gap:6px" id="pslots-${i}"></div></div><span id="pstatus-${i}"></span></div>`;
    cont.appendChild(panel);
    const slotsDiv=panel.querySelector(`#pslots-${i}`);
    for(let ci=0;ci<2;ci++){
      const c=hand[ci];const sl=document.createElement('div');
      sl.style.cssText='width:40px;height:56px;border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center;flex-shrink:0;cursor:pointer;transition:all .15s;';
      if(c){
        const{r,s,red}=lbl(c);sl.style.background='var(--card-bg)';sl.style.border='2px solid rgba(201,168,76,.5)';sl.style.boxShadow='0 3px 10px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.8)';
        sl.innerHTML=`<span style="font-family:'Rajdhani',sans-serif;font-weight:700;font-size:13px;line-height:1;color:${red?'var(--red-suit)':'#1a1a1a'}">${r}</span><span style="font-size:13px;line-height:1;color:${red?'var(--red-suit)':'#1a1a1a'}">${s}</span>`;
        sl.onclick=e=>{e.stopPropagation();cmpHands[i][ci]=null;selectPlayer(i);};sl.title='Clique para remover';
      }else{
        sl.style.background='rgba(13,35,24,.6)';sl.style.border=isAct?'2px solid var(--gold)':'2px dashed rgba(201,168,76,.3)';
        if(isAct)sl.style.animation='pulse-slot 1.5s ease infinite';
        sl.innerHTML=`<span style="color:rgba(201,168,76,.4);font-size:9px;letter-spacing:.1em">C${ci+1}</span>`;
        sl.onclick=e=>{e.stopPropagation();selectPlayer(i);};
      }
      slotsDiv.appendChild(sl);
    }
    const st=panel.querySelector(`#pstatus-${i}`);
    if(done)st.innerHTML='<span style="color:#00d472;font-size:14px">✓</span>';
    else if(isAct)st.innerHTML='<span style="color:var(--gold);font-size:10px;font-weight:600;letter-spacing:.08em">ATIVO</span>';
    else st.innerHTML=`<span style="color:rgba(255,255,255,.2);font-size:10px;font-family:'JetBrains Mono',monospace">${hand.filter(Boolean).length}/2</span>`;
  }
}
function buildCmpDeck(){
  const g=document.getElementById('cmp-deck');g.innerHTML='';
  const usedHands=cmpHands.slice(0,cmpN).flat().filter(Boolean);
  const usedBoard=cmpBoard.filter(Boolean);
  const used=[...usedHands,...usedBoard];
  for(const s of SUITS){
    if(cmpSuit!=='all'&&s!==cmpSuit)continue;
    for(const r of RANKS){
      const c=r+s,{r:dr,s:ds,red}=lbl(c);
      const el=document.createElement('div');el.className=`playing-card ${red?'red-card':'black-card'}`;
      if(used.includes(c))el.classList.add('used');
      el.innerHTML=`<span class="card-rank">${dr}</span><span class="card-suit">${ds}</span>`;
      el.onclick=()=>cmpPick(c);g.appendChild(el);
    }
  }
  renderPokerTable();
}


// ─────────────────────────────────────────────
// MESA DE POKER
// ─────────────────────────────────────────────
function renderPokerTable(results){
  const canvas=document.getElementById('poker-table-canvas');
  if(!canvas)return;
  const W=canvas.offsetWidth||600;
  const H=canvas.offsetHeight||(W*0.58);
  if(W<10)return;
  canvas.innerHTML='';
  const cx=W/2, cy=H/2;

  // SVG feltro
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width','100%');svg.setAttribute('height','100%');
  svg.style.cssText='position:absolute;inset:0;pointer-events:none;';
  svg.innerHTML=`<defs>
      <filter id="tshadow" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="6" stdDeviation="14" flood-color="rgba(0,0,0,.8)"/>
      </filter>
      <radialGradient id="tfelt" cx="50%" cy="42%" r="62%">
        <stop offset="0%"   stop-color="#1e6b34"/>
        <stop offset="65%"  stop-color="#145228"/>
        <stop offset="100%" stop-color="#0b3318"/>
      </radialGradient>
      <radialGradient id="tshine" cx="50%" cy="35%" r="55%">
        <stop offset="0%"   stop-color="rgba(255,255,255,.07)"/>
        <stop offset="100%" stop-color="rgba(0,0,0,0)"/>
      </radialGradient>
    </defs>
    <ellipse cx="${cx}" cy="${cy}" rx="${W*0.475}" ry="${H*0.452}" fill="#4a2e0a" filter="url(#tshadow)"/>
    <ellipse cx="${cx}" cy="${cy}" rx="${W*0.438}" ry="${H*0.418}" fill="url(#tfelt)"/>
    <ellipse cx="${cx}" cy="${cy*0.75}" rx="${W*0.31}" ry="${H*0.19}" fill="url(#tshine)"/>
    <ellipse cx="${cx}" cy="${cy}" rx="${W*0.388}" ry="${H*0.362}" fill="none" stroke="rgba(201,168,76,.18)" stroke-width="1.5"/>
    <text x="${cx}" y="${cy+6}" text-anchor="middle" font-family="Rajdhani,sans-serif" font-size="${W*0.023}" font-weight="700" fill="rgba(255,255,255,.06)" letter-spacing="0.18em">POKERCALC</text>`;
  canvas.appendChild(svg);

  // Board no centro
  const cw=Math.max(Math.min(W*0.063,44),22);
  const ch=cw*1.42;
  const gap=cw*0.2;
  const bStartX=cx-(5*cw+4*gap)/2;
  const bY=cy-ch/2;
  for(let i=0;i<5;i++){
    const c=cmpBoard[i];
    const el=document.createElement('div');
    el.className='t-card'+(c?(isRed(c.slice(-1))?' red':' black'):' empty');
    el.style.cssText=`position:absolute;left:${bStartX+i*(cw+gap)}px;top:${bY}px;width:${cw}px;height:${ch}px;border-radius:${cw*0.13}px;`;
    if(c){const{r,s}=lbl(c);el.innerHTML=`<span class="tr" style="font-size:${cw*0.37}px">${r}</span><span class="ts" style="font-size:${cw*0.37}px">${s}</span>`;}
    else{el.innerHTML=`<span style="font-size:9px;color:rgba(255,255,255,.15)">·</span>`;}
    canvas.appendChild(el);
  }

  // Assentos
  const allAngles=[270,225,180,135,90,45,0,315,247];
  const RX=W*0.41, RY=H*0.375;
  allAngles.slice(0,cmpN).forEach((deg,i)=>{
    const rad=deg*Math.PI/180;
    const sx=cx+RX*Math.cos(rad);
    const sy=cy+RY*Math.sin(rad);
    const hand=cmpHands[i];
    const col=PCOLS[i%PCOLS.length];
    const eq=results?results[i]:null;
    const maxWin=results?Math.max(...results.map(r=>r.win)):0;
    const isWinner=eq&&eq.win===maxWin&&eq.win>0;
    const isTop=sy<cy*0.65;

    const seat=document.createElement('div');
    seat.className='t-seat';
    seat.style.cssText=`left:${sx}px;top:${sy}px;transform:translate(-50%,-50%);flex-direction:${isTop?'column-reverse':'column'};gap:4px;`;

    // Cartas
    const scw=Math.max(Math.min(W*0.052,36),18);
    const sch=scw*1.42;

    // Wrapper das cartas + badge de probabilidade
    const cardWrap=document.createElement('div');
    cardWrap.style.cssText='position:relative;display:flex;flex-direction:column;align-items:center;gap:2px;';

    const cRow=document.createElement('div');
    cRow.className='t-seat-cards';
    for(let ci=0;ci<2;ci++){
      const c=hand[ci];
      const cardEl=document.createElement('div');
      cardEl.className='t-card'+(c?(isRed(c.slice(-1))?' red':' black'):' empty');
      cardEl.style.cssText=`position:relative;width:${scw}px;height:${sch}px;border-radius:${scw*0.13}px;`;
      if(c){
        const{r,s}=lbl(c);
        cardEl.innerHTML=`<span class="tr" style="font-size:${scw*0.36}px">${r}</span><span class="ts" style="font-size:${scw*0.36}px">${s}</span>`;
        if(isWinner) cardEl.style.boxShadow=`0 0 12px 3px ${col},0 3px 8px rgba(0,0,0,.5)`;
      } else {
        cardEl.innerHTML=`<span style="font-size:9px;color:rgba(255,255,255,.18)">?</span>`;
      }
      cRow.appendChild(cardEl);
    }
    cardWrap.appendChild(cRow);

    // Badge de probabilidade (aparece só depois do cálculo)
    if(eq){
      const badge=document.createElement('div');
      const badgeFs=Math.max(Math.min(W*0.018,13),9);
      badge.style.cssText=`
        padding:2px 6px;border-radius:10px;
        font-family:'JetBrains Mono',monospace;font-weight:700;font-size:${badgeFs}px;
        background:${isWinner?'rgba(0,212,114,.25)':'rgba(13,35,24,.85)'};
        border:1px solid ${isWinner?'rgba(0,212,114,.6)':col+'66'};
        color:${isWinner?'#00d472':col};
        white-space:nowrap;
        box-shadow:${isWinner?'0 0 8px rgba(0,212,114,.3)':'none'};
        transition:all .3s;
      `;
      badge.textContent=eq.win+'%';
      cardWrap.appendChild(badge);
    }
    seat.appendChild(cardWrap);

    // Nome do jogador
    const nm=document.createElement('span');
    nm.className='t-seat-name';
    nm.style.color=col;
    nm.style.fontSize=Math.max(Math.min(W*0.016,11),8)+'px';
    nm.textContent=`J${i+1}`;
    seat.appendChild(nm);

    canvas.appendChild(seat);
  });
}

function renderCmpBoardSlots(){
  const cont=document.getElementById('cmp-board-slots'); if(!cont) return;
  const lbls=['FLOP','FLOP','FLOP','TURN','RIVER'];
  cont.innerHTML='';
  cmpBoard.forEach((c,i)=>{
    const sl=document.createElement('div');
    if(c){
      const{r,s,red}=lbl(c);
      sl.className=`slot filled ${red?'red-card':'black-card'}`;
      sl.style.width='50px'; sl.style.height='70px';
      sl.innerHTML=`<span class="slot-rank">${r}</span><span class="slot-suit">${s}</span>`;
      sl.onclick=()=>{ cmpBoard[i]=null; cmpBoardActive=true; renderCmpBoardSlots(); buildCmpDeck(); updateCmpStreet(); };
      sl.title='Clique para remover';
    } else {
      const isAct=cmpBoardActive && cmpBoard.indexOf(null)===i;
      sl.className='slot'+(isAct?' active':'');
      sl.style.width='50px'; sl.style.height='70px';
      sl.innerHTML=`<span style="color:rgba(201,168,76,.35);font-size:9px;letter-spacing:.1em">${lbls[i]}</span>`;
      sl.onclick=()=>{ cmpBoardActive=true; renderCmpBoardSlots(); buildCmpDeck(); };
    }
    cont.appendChild(sl);
  });
  renderPokerTable(); // atualiza mesa ao mudar board
}

function clearCmpBoard(){
  cmpBoard=[null,null,null,null,null]; cmpBoardActive=false;
  renderCmpBoardSlots(); buildCmpDeck(); updateCmpStreet();
}

function updateCmpStreet(){
  const n=cmpBoard.filter(Boolean).length;
  const st={0:'PRÉ-FLOP',3:'FLOP',4:'TURN',5:'RIVER'}[n]||'PRÉ-FLOP';
  const badge=document.getElementById('cmp-street-badge');
  if(badge) badge.textContent=st;
}
function cmpPick(c){
  const usedHands=cmpHands.slice(0,cmpN).flat().filter(Boolean);
  const usedBoard=cmpBoard.filter(Boolean);
  if([...usedHands,...usedBoard].includes(c)) return;

  // Se board está ativo e tem slot livre, vai para o board
  if(cmpBoardActive){
    const slot=cmpBoard.indexOf(null);
    if(slot!==-1){
      cmpBoard[slot]=c;
      // Avança ou desativa board se completou
      const nextSlot=cmpBoard.indexOf(null);
      if(nextSlot===-1) cmpBoardActive=false;
      renderCmpBoardSlots(); buildCmpDeck(); updateCmpStreet();
      return;
    }
    cmpBoardActive=false;
  }

  // Vai para o jogador ativo
  const hand=cmpHands[cmpActive];const slot=hand.indexOf(null);if(slot===-1)return;
  cmpHands[cmpActive][slot]=c;
  if(cmpHands[cmpActive].every(Boolean)){
    let next=-1;
    for(let i=cmpActive+1;i<cmpN;i++){if(!cmpHands[i][0]||!cmpHands[i][1]){next=i;break;}}
    if(next===-1)for(let i=0;i<cmpActive;i++){if(!cmpHands[i][0]||!cmpHands[i][1]){next=i;break;}}
    if(next!==-1)cmpActive=next;
  }
  renderPlayers();buildCmpDeck();
}
async function runCompare(){
  const hands=cmpHands.slice(0,cmpN);
  for(let i=0;i<hands.length;i++){if(!hands[i][0]||!hands[i][1]){flash(`Jogador ${i+1} está incompleto.`);return;}}
  const sims = 20000;
  const board=cmpBoard.filter(Boolean);
  setLoading('cmp',true);setProgress('cmp',0);
  try{
    const res=await fetch('/compare',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({hands,board,simulations:sims})});
    const init=await res.json();
    if(init.error){flash(init.error);setLoading('cmp',false);hideProgress('cmp');return;}
    pollJob(init.job_id,
      p=>setProgress('cmp',p),
      d=>{
        renderCmpResults(d,board);
        document.getElementById('sim-counter').textContent=sims.toLocaleString('pt-BR')+' simulações';
        const mb=document.getElementById('moe-badge');mb.style.display='inline';mb.textContent='± '+d.margin_of_error+'%';
        document.getElementById('cmp-hint').style.display='none';
        setLoading('cmp',false);setTimeout(()=>hideProgress('cmp'),600);
      },
      e=>{flash(e||'Erro.');setLoading('cmp',false);hideProgress('cmp');}
    );
  }catch{flash('Erro de conexão.');setLoading('cmp',false);hideProgress('cmp');}
}
function renderCmpResults(data,board){
  const{hands}=data;const sorted=[...hands].sort((a,b)=>b.equity.win-a.equity.win);const rankOf={};sorted.forEach((h,i)=>rankOf[h.index]=i+1);
  document.getElementById('cmp-global').style.display='block';
  const streetMap={0:'Pré-Flop',3:'Flop',4:'Turn',5:'River'};
  const streetLabel=streetMap[(board||[]).length]||'Pré-Flop';
  document.getElementById('cmp-global-title').textContent=`Equity Comparativa — ${streetLabel}`;
  // Atualiza mesa com resultados
  const equityByIndex = hands.map(h=>h.equity);
  renderPokerTable(equityByIndex);
  const gbar=document.getElementById('cmp-gbar');gbar.innerHTML='';const leg=document.getElementById('cmp-legend');leg.innerHTML='';let left=0;
  sorted.forEach((h,i)=>{
    const col=PCOLS[h.index%PCOLS.length];const seg=document.createElement('div');seg.className='global-seg';
    seg.style.cssText=`left:${left}%;width:0%;background:${col};opacity:.9;color:#0d1f12`;seg.textContent=h.equity.win>=7?h.equity.win+'%':'';
    gbar.appendChild(seg);setTimeout(()=>{seg.style.width=h.equity.win+'%';},80+i*40);left+=h.equity.win;
    const dot=document.createElement('div');dot.className='flex items-center gap-1.5 text-xs font-mono';
    dot.innerHTML=`<span style="width:9px;height:9px;border-radius:50%;background:${col};display:inline-block;flex-shrink:0"></span><span style="color:rgba(255,255,255,.5)">J${h.index+1}</span><span style="color:${col};font-weight:700">${h.equity.win}%</span>`;
    leg.appendChild(dot);
  });
  const grid=document.getElementById('cmp-results');grid.innerHTML='';
  hands.forEach(h=>{
    const rank=rankOf[h.index],col=PCOLS[h.index%PCOLS.length];
    const card=document.createElement('div');card.className=`result-card fade-in ${rank===1?'winner':rank===hands.length?'loser':''}`;
    const minis=h.cards.map(c=>{const{r,s,red}=lbl(c);return`<div class="mini-card ${red?'red':'black'}"><span class="mr">${r}</span><span class="ms">${s}</span></div>`;}).join('');
    card.innerHTML=`<div style="height:3px;border-radius:2px;background:${col};margin-bottom:12px"></div>
      <div class="flex items-center gap-2 mb-3"><div class="rnk ${rank===1?'r1':rank===2?'r2':'rx'}">${rank}°</div><div><div style="color:${col};font-weight:700;font-size:13px;letter-spacing:.05em">${h.description}</div><div class="text-xs font-mono" style="color:rgba(255,255,255,.3)">Jogador ${h.index+1}</div></div></div>
      <div class="flex gap-2 mb-4">${minis}</div>
      <div class="space-y-1.5">${[['#00d472',h.equity.win,'VITÓRIA'],['var(--gold)',h.equity.tie,'EMPATE'],['#e74c3c',h.equity.lose,'DERROTA']].map(([c,v,t])=>`
        <div class="flex items-center gap-2"><span class="text-xs font-mono" style="color:${c};width:48px">${v}%</span>
        <div style="flex:1;height:6px;border-radius:3px;background:rgba(255,255,255,.05);overflow:hidden"><div class="rcb" data-w="${Math.min(v*1.5,100)}" style="height:100%;border-radius:3px;width:0%;background:${c};transition:width .7s cubic-bezier(.34,1.56,.64,1)"></div></div>
        <span class="text-xs" style="color:rgba(255,255,255,.25);width:46px;text-align:right">${t}</span></div>`).join('')}</div>`;
    grid.appendChild(card);
  });
  setTimeout(()=>{document.querySelectorAll('.rcb').forEach(b=>{b.style.width=b.dataset.w+'%';});},100);
}
function resetCmp(){
  cmpHands=Array.from({length:8},()=>[null,null]);cmpActive=0;
  cmpBoard=[null,null,null,null,null];cmpBoardActive=false;
  renderPlayers();renderCmpBoardSlots();buildCmpDeck();updateCmpStreet();
  document.getElementById('cmp-global').style.display='none';document.getElementById('cmp-results').innerHTML='';
  document.getElementById('cmp-hint').style.display='block';document.getElementById('sim-counter').textContent='— simulações';
  document.getElementById('moe-badge').style.display='none';
}

// ── UTILS ──
function setLoading(m,on){
  if(m==='calc'){document.getElementById('btn-txt').textContent=on?'CALCULANDO...':'CALCULAR ODDS';document.getElementById('loader').style.display=on?'block':'none';document.getElementById('calc-btn').disabled=on;}
  else{document.getElementById('cmp-txt').textContent=on?'CALCULANDO...':'CALCULAR CONFRONTO';document.getElementById('cmp-loader').style.display=on?'block':'none';document.getElementById('cmp-btn').disabled=on;}
}
function flash(msg){
  const el=document.createElement('div');el.textContent=msg;
  el.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(231,76,60,.9);color:white;padding:9px 22px;border-radius:8px;font-family:Rajdhani,sans-serif;font-size:14px;z-index:9999;letter-spacing:.05em;box-shadow:0 4px 16px rgba(0,0,0,.4)';
  document.body.appendChild(el);setTimeout(()=>el.remove(),3000);
}
document.addEventListener('keydown',e=>{
  if(e.key==='Enter')mode==='calc'?doCalculate():runCompare();
  if(e.key==='Escape')mode==='calc'?resetCalc():resetCmp();
});

// ══════════════════════════════════════
// ══════════════════════════════════════
// POT ODDS / EV  — versão simplificada
// ══════════════════════════════════════
let lastCalcEquity = null;
let mathOpen = false;
let activeBetMult = 0.66;

function setBetPreset(mult){
  activeBetMult = mult;
  document.querySelectorAll('.ev-bet-btn').forEach(b=>{
    b.classList.toggle('active-bet', parseFloat(b.dataset.mult)===mult);
  });
  const pot = parseFloat(document.getElementById('ev-pot').value) || 0;
  document.getElementById('ev-bet').value = (pot * mult).toFixed(1);
  calcEV();
}

function onBetManual(){
  activeBetMult = null;
  document.querySelectorAll('.ev-bet-btn').forEach(b=>b.classList.remove('active-bet'));
  calcEV();
}

function importEquity(){
  if(lastCalcEquity === null){ flash('Calcule uma mão na aba Calculadora primeiro.'); return; }
  document.getElementById('ev-equity').value = lastCalcEquity;
  const hint = document.getElementById('import-hint');
  hint.textContent = '✓ Equity importada: ' + lastCalcEquity + '%';
  hint.style.color = '#00d472';
  calcEV();
}

function calcEV(){
  const pot    = parseFloat(document.getElementById('ev-pot').value)    || 0;
  const bet    = parseFloat(document.getElementById('ev-bet').value)    || 0;
  const equity = parseFloat(document.getElementById('ev-equity').value) || 0;

  // Sync preset buttons if pot changed
  if(activeBetMult !== null){
    const expected = parseFloat((pot * activeBetMult).toFixed(1));
    const current  = parseFloat(document.getElementById('ev-bet').value);
    if(Math.abs(expected - current) > 0.2){
      document.getElementById('ev-bet').value = expected;
    }
  }

  if(pot <= 0 || bet <= 0 || equity <= 0 || equity >= 100){ resetEVDisplay(); return; }

  const eq      = equity / 100;
  const reqEq   = bet / (pot + bet);
  const ev      = (eq * pot) - ((1 - eq) * bet);
  const gap     = eq - reqEq;
  const ratio   = `${pot}:${bet}`;

  // ── Atualiza mini visual do pot ──
  const potFill = document.getElementById('ev-pot-fill');
  const betFill = document.getElementById('ev-bet-fill');
  const potLbl  = document.getElementById('ev-pot-lbl');
  const betLbl  = document.getElementById('ev-bet-lbl');
  const totLbl  = document.getElementById('ev-total-lbl');
  const potPct  = document.getElementById('ev-pot-pct');
  const betPct  = document.getElementById('ev-bet-pct');
  const total   = pot + bet;
  if(potFill){ potFill.style.flex = pot; }
  if(betFill){ betFill.style.flex = bet; }
  if(potPct)  potPct.textContent  = (pot/total*100).toFixed(0)+'%';
  if(betPct)  betPct.textContent  = (bet/total*100).toFixed(0)+'%';
  if(potLbl)  potLbl.textContent  = pot+' BB';
  if(betLbl)  betLbl.textContent  = bet.toFixed(1)+' BB';
  if(totLbl)  totLbl.textContent  = total.toFixed(1)+' BB';

  // ── Stats row ──
  const statsRow = document.getElementById('ev-stats-row');
  if(statsRow){ statsRow.style.display='grid'; statsRow.style.removeProperty('display'); statsRow.style.display='grid'; }
  const statOdds = document.getElementById('ev-stat-odds');
  const statMin  = document.getElementById('ev-stat-min');
  const statEV   = document.getElementById('ev-stat-ev');
  if(statOdds) statOdds.textContent = (reqEq*100).toFixed(1)+'%';
  if(statMin)  statMin.textContent  = (reqEq*100).toFixed(1)+'%';
  if(statEV){
    statEV.textContent = (ev>=0?'+':'')+ev.toFixed(2)+' BB';
    statEV.style.color = ev>0.05?'#00d472':ev<-0.05?'#e74c3c':'var(--gold)';
  }

  // ── Verdito ──
  const vBox    = document.getElementById('ev-verdict');
  const vIcon   = document.getElementById('ev-icon');
  const vTitle  = document.getElementById('ev-title');
  const vExpl   = document.getElementById('ev-explain');

  const reqPct  = (reqEq * 100).toFixed(1);
  const eqPct   = equity.toFixed(1);
  const evSign  = ev >= 0 ? '+' : '';
  const evStr   = evSign + ev.toFixed(2) + ' BB';
  const gapPct  = Math.abs(gap * 100).toFixed(1);

  if(gap > 0.015){
    vBox.className = 'verdict-box v-call';
    vIcon.textContent = '✓';
    vTitle.textContent = 'PODE CHAMAR';
    vTitle.style.color = '#00d472';
    vExpl.innerHTML = `Você precisa ganhar pelo menos <strong style="color:var(--gold)">${reqPct}%</strong> das vezes para esse call valer.<br>
      Com <strong style="color:#00d472">${eqPct}%</strong> de chance, você está <strong style="color:#00d472">${gapPct}%</strong> acima do mínimo.<br>
      <span style="color:rgba(255,255,255,.45);font-size:13px">A cada 100 vezes nessa situação, você ganha em média <strong style="color:#00d472">${evStr}</strong>.</span>`;
  } else if(gap < -0.015){
    vBox.className = 'verdict-box v-fold';
    vIcon.textContent = '✗';
    vTitle.textContent = 'MELHOR FOLDAR';
    vTitle.style.color = '#e74c3c';
    vExpl.innerHTML = `Você precisaria de pelo menos <strong style="color:var(--gold)">${reqPct}%</strong> de chance para esse call valer.<br>
      Com apenas <strong style="color:#e74c3c">${eqPct}%</strong>, você está <strong style="color:#e74c3c">${gapPct}%</strong> abaixo do necessário.<br>
      <span style="color:rgba(255,255,255,.45);font-size:13px">Chamar aqui custaria em média <strong style="color:#e74c3c">${evStr}</strong> por vez.</span>`;
  } else {
    vBox.className = 'verdict-box v-margin';
    vIcon.textContent = '~';
    vTitle.textContent = 'DECISÃO MARGINAL';
    vTitle.style.color = 'var(--gold)';
    vExpl.innerHTML = `Sua chance (<strong style="color:var(--gold)">${eqPct}%</strong>) está quase igual ao mínimo necessário (<strong style="color:var(--gold)">${reqPct}%</strong>).<br>
      A diferença é menor que 1.5% — outros fatores da mão (posição, tendências do oponente) devem decidir.<br>
      <span style="color:rgba(255,255,255,.45);font-size:13px">EV esperado: <strong style="color:var(--gold)">${evStr}</strong> por call.</span>`;
  }

  // ── Barra visual ──
  document.getElementById('ev-bar-panel').style.display = 'block';
  const fillPct   = Math.min(eqPct, 100);
  const markerPct = Math.min(reqEq * 100, 100);
  const fillColor = gap > 0.015 ? '#00d472' : gap < -0.015 ? '#e74c3c' : 'var(--gold)';
  document.getElementById('ev-needs-fill').style.width      = fillPct + '%';
  document.getElementById('ev-needs-fill').style.background = fillColor;
  document.getElementById('ev-needs-marker').style.left     = markerPct + '%';
  document.getElementById('ev-needs-label').style.left      = markerPct + '%';
  document.getElementById('ev-needs-label').textContent     = 'mín. ' + reqPct + '%';
  document.getElementById('ev-bar-summary').innerHTML =
    `<span style="color:${fillColor};font-weight:700;font-family:'JetBrains Mono',monospace;font-size:11px">${eqPct}% sua equity</span>
     <span style="color:rgba(255,255,255,.3);font-size:10px;margin:0 6px">vs</span>
     <span style="color:#e74c3c;font-weight:700;font-family:'JetBrains Mono',monospace;font-size:11px">${reqPct}% mínimo</span>`;

  // ── Math expansível ──
  document.getElementById('ev-math-wrap').style.display = 'block';
  document.getElementById('ev-math-content').innerHTML = `
    <div class="math-row"><span style="color:rgba(255,255,255,.4)">Pot</span><span style="color:var(--gold);font-weight:700">${pot} BB</span></div>
    <div class="math-row"><span style="color:rgba(255,255,255,.4)">Aposta do oponente</span><span style="color:var(--gold);font-weight:700">${bet} BB</span></div>
    <div class="math-row"><span style="color:rgba(255,255,255,.4)">Pot total se chamar</span><span style="color:var(--cream)">${(pot+bet).toFixed(1)} BB</span></div>
    <div class="math-row"><span style="color:rgba(255,255,255,.4)">Pot odds (bet ÷ total)</span><span style="color:var(--cream)">${(reqEq*100).toFixed(2)}%</span></div>
    <div class="math-row"><span style="color:rgba(255,255,255,.4)">Cenário vitória (${eqPct}%)</span><span style="color:#00d472">+${(eq*pot).toFixed(2)} BB</span></div>
    <div class="math-row"><span style="color:rgba(255,255,255,.4)">Cenário derrota (${(100-equity).toFixed(1)}%)</span><span style="color:#e74c3c">−${((1-eq)*bet).toFixed(2)} BB</span></div>
    <div class="math-row" style="border-top:1px solid rgba(201,168,76,.2);padding-top:10px;margin-top:4px">
      <span style="color:var(--gold);font-weight:700">EV da call</span>
      <span style="color:${ev>=0?'#00d472':'#e74c3c'};font-weight:700;font-size:14px">${evSign}${ev.toFixed(3)} BB</span>
    </div>
    <p style="color:rgba(255,255,255,.3);font-size:11px;margin-top:12px;line-height:1.5;">
      Fórmula: EV = (chance de ganhar × ganho) − (chance de perder × custo)<br>
      = (${eqPct}% × ${pot}) − (${(100-equity).toFixed(1)}% × ${bet})<br>
      = ${(eq*pot).toFixed(2)} − ${((1-eq)*bet).toFixed(2)} = <strong style="color:${ev>=0?'#00d472':'#e74c3c'}">${evSign}${ev.toFixed(3)} BB</strong>
    </p>`;
}

function resetEVDisplay(){
  const vBox = document.getElementById('ev-verdict');
  vBox.className = 'verdict-box mb-4';
  document.getElementById('ev-icon').textContent = '🃏';
  document.getElementById('ev-icon').style.color = '';
  document.getElementById('ev-title').textContent = 'Preencha os campos';
  document.getElementById('ev-title').style.color = 'rgba(255,255,255,.4)';
  document.getElementById('ev-explain').textContent = '';
  document.getElementById('ev-bar-panel').style.display = 'none';
  document.getElementById('ev-math-wrap').style.display = 'none';
}

// ── QUICK HANDS ──
const QUICK_HANDS = [
  {label:'AA', cards:['As','Ah']},
  {label:'KK', cards:['Ks','Kh']},
  {label:'QQ', cards:['Qs','Qh']},
  {label:'JJ', cards:['Js','Jh']},
  {label:'TT', cards:['Ts','Th']},
  {label:'AKs', cards:['As','Ks']},
  {label:'AKo', cards:['As','Kh']},
  {label:'AQs', cards:['As','Qs']},
  {label:'AJs', cards:['As','Js']},
  {label:'KQs', cards:['Ks','Qs']},
  {label:'76s', cards:['7s','6s']},
  {label:'65s', cards:['6s','5s']},
];

function buildQuickHands(){
  const cont = document.getElementById('quick-hands');
  if(!cont) return;
  cont.innerHTML = '';
  QUICK_HANDS.forEach(qh => {
    const btn = document.createElement('button');
    btn.className = 'qh-btn';
    btn.textContent = qh.label;
    btn.onclick = () => applyQuickHand(qh.cards);
    cont.appendChild(btn);
  });
}

function applyQuickHand(cards){
  // Only apply if both cards are still available
  const usedOnBoard = board.filter(Boolean);
  const c1 = cards[0], c2 = cards[1];
  if(usedOnBoard.includes(c1) || usedOnBoard.includes(c2)){
    flash('Uma dessas cartas já está no board.');
    return;
  }
  hole[0] = c1;
  hole[1] = c2;
  renderCalcSlots();
  buildDeck();
  updateStreet();
  // auto-advance to board
  const bn = board.findIndex(x=>!x);
  if(bn !== -1) activateCalcSlot('board', bn);
}

// ── INIT ──
renderCalcSlots();applyLayout();activateCalcSlot('hole',0);updateStreet();
buildQuickHands();
renderPlayers();renderCmpBoardSlots();buildCmpDeck();updateCmpStreet();
calcEV();

// Redesenha mesas quando janela redimensiona
const pokerCanvas = document.getElementById('poker-table-canvas');
if(pokerCanvas && window.ResizeObserver){
  new ResizeObserver(()=>renderPokerTable()).observe(pokerCanvas);
}
const calcCanvas = document.getElementById('calc-poker-table');
if(calcCanvas && window.ResizeObserver){
  new ResizeObserver(()=>renderCalcTable()).observe(calcCanvas);
}
window.addEventListener('resize', ()=>{
  if(mode==='compare') renderPokerTable();
  if(mode==='calc') renderCalcTable();
});

// ── Touch: tooltips ──
// No mobile (sem hover), toque no ícone ? abre/fecha o tooltip
document.addEventListener('touchstart', e=>{
  const icon = e.target.closest('.tip-icon');
  if(icon){
    e.preventDefault();
    const wrap = icon.closest('.tip-wrap');
    const box  = wrap ? wrap.querySelector('.tip-box') : null;
    if(!box) return;
    // fecha todos os outros abertos
    document.querySelectorAll('.tip-box.touch-open').forEach(b=>{
      if(b!==box){ b.style.display='none'; b.classList.remove('touch-open'); }
    });
    const isOpen = box.classList.contains('touch-open');
    box.style.display = isOpen ? 'none' : 'block';
    box.classList.toggle('touch-open', !isOpen);
  } else {
    // toque fora fecha todos
    document.querySelectorAll('.tip-box.touch-open').forEach(b=>{
      b.style.display='none'; b.classList.remove('touch-open');
    });
  }
},{passive:false});
// Fecha tooltips ao rolar no mobile
document.addEventListener('touchmove', ()=>{
  document.querySelectorAll('.tip-box.touch-open').forEach(b=>{
    b.style.display='none'; b.classList.remove('touch-open');
  });
},{passive:true});
</script>
</body>
</html>"""

@app.route('/healthcheck')
def healthcheck():
    """Endpoint leve para anti-hibernação (UptimeRobot / cron-job.org)."""
    return jsonify({
        'status': 'ok',
        'service': 'pokercalc',
        'jobs_active': sum(1 for j in _JOBS.values() if j['status'] == 'running'),
        'cache_entries': len(_CACHE),
        'ts': int(time.time())
    })

@app.route('/calculate', methods=['OPTIONS'])
@app.route('/compare',   methods=['OPTIONS'])
@app.route('/status/<jid>', methods=['OPTIONS'])
def handle_options(jid=None):
    """Responde preflight CORS do browser."""
    return '', 204

@app.route('/')
def index():
    return HTML

# ─── FAVICON ──────────────────────────────────────────
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="6" fill="#071a10"/>
  <text x="16" y="24" text-anchor="middle" font-size="22" fill="#c9a84c">♠</text>
</svg>"""

@app.route('/favicon.ico')
@app.route('/favicon.svg')
def favicon():
    return Response(FAVICON_SVG, mimetype='image/svg+xml',
                    headers={'Cache-Control':'public,max-age=86400'})

# ─── PÁGINAS DE ERRO ──────────────────────────────────
ERROR_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>PokerCalc — {code}</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;700&family=JetBrains+Mono:wght@700&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'Rajdhani',sans-serif;background:#071a10;color:#f5ead4;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4'%3E%3Crect width='4' height='4' fill='%230d2318'/%3E%3C/svg%3E");}}
.card{{background:rgba(13,35,24,.85);border:1px solid rgba(201,168,76,.25);
  border-radius:16px;padding:48px 40px;text-align:center;max-width:420px;
  box-shadow:0 20px 60px rgba(0,0,0,.6);}}
.suit{{font-size:56px;margin-bottom:16px;display:block;}}
.code{{font-family:'JetBrains Mono',monospace;font-size:56px;font-weight:700;
  color:#c9a84c;text-shadow:0 0 20px rgba(201,168,76,.4);margin-bottom:8px;}}
.title{{font-size:20px;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:rgba(255,255,255,.7);margin-bottom:12px;}}
.msg{{font-size:14px;color:rgba(255,255,255,.4);line-height:1.6;margin-bottom:32px;}}
.btn{{display:inline-block;padding:12px 32px;background:linear-gradient(135deg,#c9a84c,#e8c96d);
  color:#0d1f12;font-family:'Rajdhani',sans-serif;font-weight:700;font-size:14px;
  letter-spacing:.12em;text-transform:uppercase;border-radius:8px;
  text-decoration:none;transition:opacity .2s;}}
.btn:hover{{opacity:.85;}}
</style>
</head>
<body>
<div class="card">
  <span class="suit">{suit}</span>
  <div class="code">{code}</div>
  <div class="title">{title}</div>
  <p class="msg">{message}</p>
  <a href="/" class="btn">← Voltar ao PokerCalc</a>
</div>
</body>
</html>"""

@app.errorhandler(404)
def not_found(e):
    html = ERROR_HTML.format(
        code='404', suit='♦',
        title='Página não encontrada',
        message='A página que você procura não existe.<br>Mas o PokerCalc está te esperando!'
    )
    return html, 404

@app.errorhandler(500)
def server_error(e):
    html = ERROR_HTML.format(
        code='500', suit='♣',
        title='Erro interno',
        message='Algo deu errado no servidor.<br>Já estamos de olho. Tente novamente em instantes.'
    )
    return html, 500

@app.errorhandler(429)
def too_many(e):
    html = ERROR_HTML.format(
        code='429', suit='♠',
        title='Muitas requisições',
        message='Você fez muitas requisições em pouco tempo.<br>Aguarde um momento e tente novamente.'
    )
    return html, 429

if __name__ == '__main__':
    if '--test' in sys.argv:
        sys.exit(0 if run_tests() else 1)
    print("\n  ♠  PokerCalc rodando em → http://localhost:8080")
    print("  Dica: python app.py --test  para rodar os testes")
    print("  Produção: gunicorn app:app --workers 2 --threads 4 --timeout 120\n")
    port  = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug, host='0.0.0.0', port=port)
