import os, shutil, threading, random, json
import chess, chess.engine
from flask import Flask, jsonify, request, abort

app = Flask(__name__, static_folder="static", static_url_path="/static")
_lock = threading.Lock()
_engine = None

def find_engine():
    for c in (os.environ.get("STOCKFISH"), shutil.which("stockfish"), "/usr/games/stockfish",
              "/usr/local/bin/stockfish", "/opt/homebrew/bin/stockfish", "stockfish.exe"):
        if c and (os.path.exists(c) or shutil.which(c)):
            return c
    raise SystemExit("Stockfish not found. Install it, or set STOCKFISH=/path/to/stockfish")

def with_engine(fn):
    global _engine
    with _lock:
        for attempt in (0, 1):
            try:
                if _engine is None:
                    _engine = chess.engine.SimpleEngine.popen_uci(find_engine())
                    _engine.configure({"Threads": 2, "Hash": 64})
                return fn(_engine)
            except (chess.engine.EngineTerminatedError, chess.engine.EngineError):
                try: _engine.quit()
                except Exception: pass
                _engine = None
                if attempt: raise

def board_from_request():
    data = request.get_json(force=True, silent=True) or {}
    try: return chess.Board(data["fen"]), data
    except (KeyError, ValueError): abort(400, "bad fen")

def status(b):
    o = b.outcome(claim_draw=True)
    return {
        "over": o is not None,
        "result": o.result() if o else None,
        "winner": (None if o is None or o.winner is None else ("white" if o.winner else "black")),
        "reason": o.termination.name.replace("_", " ").lower() if o else None,
        "check": b.is_check(),
        "turn": "white" if b.turn else "black",
    }

@app.get("/")
def index():
    return app.send_static_file("index.html")

@app.post("/api/legal")
def legal():
    b, _ = board_from_request()
    d = {}
    for m in b.legal_moves:
        d.setdefault(chess.square_name(m.from_square), set()).add(chess.square_name(m.to_square))
    return jsonify(legal={k: sorted(v) for k, v in d.items()}, status=status(b))

@app.post("/api/move")
def move():
    b, data = board_from_request()
    try: m = chess.Move.from_uci(data["uci"])
    except (KeyError, ValueError): abort(400, "bad move")
    if m not in b.legal_moves: abort(400, "illegal move")
    san = b.san(m)
    b.push(m)
    return jsonify(fen=b.fen(), san=san, uci=m.uci(), status=status(b))

@app.post("/api/eval")
def eval_pos():
    b, data = board_from_request()
    depth = int(data.get("depth", 14))
    def go(eng):
        return eng.analyse(b, chess.engine.Limit(depth=depth))
    info = with_engine(go)
    cp = info["score"].pov(chess.WHITE).score(mate_score=10000)
    return jsonify(cp=cp)

def verdict(score, color):
    s = score.pov(color)
    if s.is_mate():
        n = s.mate()
        return (f"Forced mate in {abs(n)}." if n > 0 else f"Opponent has forced mate in {abs(n)}."), None
    cp = s.score()
    txt = "About equal." if abs(cp) < 50 else f"{'Better' if cp > 0 else 'Worse'}."
    return txt, round(cp / 100, 2)

def theme_and_reason(b, m):
    piece = b.piece_at(m.from_square)
    name = chess.piece_name(piece.piece_type)
    to = chess.square_name(m.to_square)
    if b.is_castling(m): return "castle", "Gets your king safe and connects your rooks."
    nb = b.copy(); nb.push(m)
    if nb.is_checkmate(): return "mate", "Delivers checkmate."
    parts, theme = [], "quiet"
    if b.is_capture(m):
        cap = "pawn" if b.is_en_passant(m) else chess.piece_name(b.piece_at(m.to_square).piece_type)
        parts.append(f"captures the {cap} on {to}"); theme = "capture"
    if m.promotion: parts.append("promotes the pawn"); theme = "promotion"
    if nb.is_check(): parts.append("gives check"); theme = theme if theme != "quiet" else "check"
    if not parts:
        if piece.piece_type == chess.PAWN: parts.append(f"advances your pawn to {to}")
        else: parts.append(f"improves your {name} by moving it to {to}")
    return theme, "This move " + " and ".join(parts) + "."

@app.post("/api/hint")
def hint():
    b, data = board_from_request()
    if b.is_game_over(): abort(400, "game over")
    color = b.turn
    depth = int(data.get("depth", 12))
    mode = data.get("mode", "club_player")
    get_threat = data.get("threat", False)

    def go(eng):
        multipv = 4 if mode in ("club_player", "human_natural") else (3 if mode == "positional" else 1)
        analysis = eng.analyse(b, chess.engine.Limit(depth=depth), multipv=multipv)
        if isinstance(analysis, dict):
            analysis = [analysis]
        
        moves_info = []
        for pv_dict in analysis:
            if "pv" in pv_dict and pv_dict["pv"]:
                moves_info.append({
                    "move": pv_dict["pv"][0],
                    "score": pv_dict["score"],
                    "pv": pv_dict["pv"]
                })
        
        if not moves_info:
            legal_list = list(b.legal_moves)
            return {"pv": [legal_list[0]], "score": chess.engine.PovScore(chess.engine.Cp(0), color)}, None
        
        chosen = moves_info[0]
        if mode == "club_player" and len(moves_info) > 1:
            weights = [0.50, 0.25, 0.15, 0.10][:len(moves_info)]
            chosen = random.choices(moves_info, weights=weights)[0]
        elif mode == "human_natural" and len(moves_info) > 1:
            weights = [0.70, 0.20, 0.10][:len(moves_info)]
            chosen = random.choices(moves_info, weights=weights)[0]
        elif mode == "positional" and len(moves_info) > 1:
            chosen = moves_info[min(1, len(moves_info)-1)]

        threat = None
        if get_threat and not b.is_check():
            nb = b.copy(); nb.push(chess.Move.null())
            ti = eng.analyse(nb, chess.engine.Limit(depth=10))
            if "pv" in ti and ti["pv"]:
                tm = ti["pv"][0]
                ts = ti["score"].pov(nb.turn)
                if tm and (nb.is_capture(tm) or nb.gives_check(tm)) and (
                        ts.is_mate() or (ts.score(mate_score=10000) or 0) >= 100):
                    threat = nb.san(tm)
        return chosen, threat

    chosen_info, threat = with_engine(go)
    best = chosen_info["move"]
    
    v_text, cp = verdict(chosen_info["score"], color)
    theme, why = theme_and_reason(b, best)
    pv_b, pv = b.copy(), []
    for pm in chosen_info["pv"][:5]:
        pv.append(pv_b.san(pm)); pv_b.push(pm)
    return jsonify(
        verdict=v_text, eval=cp, threat=threat, theme=theme, reason=why,
        best_uci=best.uci(), best_san=b.san(best),
        from_sq=chess.square_name(best.from_square), to_sq=chess.square_name(best.to_square),
        line=" ".join(pv))

if __name__ == "__main__":
    find_engine()
    print("Open http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
