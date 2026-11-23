#!/usr/bin/env python3
"""ミネルヴィニのトレンドテンプレート＋CAN-SLIM で米国株（S&P500＋S&P400）をスクリーニングする。

データ（すべて無料）
  銘柄一覧 … Wikipedia の S&P500 / S&P400 構成銘柄の表（週 1 回更新）
  株価・決算 … Yahoo Finance（yfinance。非公式のため仕様変更で止まることがある）

環境変数
  UNIVERSE      sp900（既定：S&P500＋S&P400）/ sp500 / sp400
  MIN_TURNOVER  20 日平均売買代金の下限（ドル。既定 2,000 万ドル）
  FUND_DAYS     決算データを取り直す間隔（日。既定 7）
"""
import datetime as dt, io, json, math, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JST = dt.timezone(dt.timedelta(hours=9))
UNIVERSE = os.environ.get("UNIVERSE") or "sp900"
MIN_TURNOVER = float(os.environ.get("MIN_TURNOVER") or 2e7)
FUND_DAYS = int(os.environ.get("FUND_DAYS") or 7)
INDEX_ETFS = [("SPY", "S&P500（SPY）"), ("QQQ", "NASDAQ100（QQQ）")]   # 地合いの判定に使う

WIKI = {"S&P500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "S&P400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"}
SECTOR_JA = {"Information Technology": "情報技術", "Health Care": "ヘルスケア", "Financials": "金融",
             "Consumer Discretionary": "一般消費財", "Communication Services": "通信サービス", "Industrials": "資本財",
             "Consumer Staples": "生活必需品", "Energy": "エネルギー", "Utilities": "公益", "Real Estate": "不動産",
             "Materials": "素材"}

TT_LABELS = [
    "株価 > 150日線・200日線", "150日線 > 200日線", "200日線が1か月以上上向き", "50日線 > 150日線・200日線",
    "株価 > 50日線", "52週安値から +30% 以上", "52週高値から -25% 以内", "RS 70 以上",
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", "utf-8")


def r(x, n=1):
    """JSON 用に丸める（NaN は None）。"""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), n)


# ---------- 銘柄一覧 ----------
def fetch_index(url):
    """Wikipedia の構成銘柄表から ティッカー・社名・セクター を取る。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (us-screener; personal use)"})
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    for t in pd.read_html(io.StringIO(html)):
        cols = {str(c).strip(): c for c in t.columns}
        sym = next((cols[c] for c in cols if c in ("Symbol", "Ticker symbol", "Ticker")), None)
        name = next((cols[c] for c in cols if c in ("Security", "Company")), None)
        sec = next((cols[c] for c in cols if "GICS Sector" in c or c == "Sector"), None)
        if sym is not None and name is not None and len(t) > 300:
            return [(str(r[sym]).strip().replace(".", "-"), str(r[name]).strip(),
                     SECTOR_JA.get(str(r[sec]).strip(), str(r[sec]).strip()) if sec is not None else "")
                    for _, r in t.iterrows()]
    raise RuntimeError("構成銘柄の表が見つかりません")


def universe():
    path = DATA / "universe.json"
    u = load(path, {})
    today = dt.datetime.now(JST).date()
    if u.get("updated", "") >= (today - dt.timedelta(days=6)).isoformat() and u.get("stocks"):
        return u
    log("構成銘柄を更新します")
    stocks = {}
    for idx, url in WIKI.items():
        try:
            rows = fetch_index(url)
        except Exception as e:
            log(f"  {idx} を取得できませんでした", e)
            continue
        for sym, name, sector in rows:
            stocks.setdefault(sym, {"name": name, "sector": sector, "market": "US", "scale": idx})
        log(f"  {idx} {len(rows)} 社")
    if len(stocks) < 800:
        log(f"  {len(stocks)} 社しか取れなかったため前回分を使います")
        if u.get("stocks"):
            return u
        if not stocks:
            raise SystemExit("構成銘柄を取得できませんでした")
        return {"stocks": stocks}
    u = {"updated": today.isoformat(), "stocks": stocks}
    save(path, u)
    return u


def pick(stocks):
    if UNIVERSE == "sp500":
        return [c for c, s in stocks.items() if s["scale"] == "S&P500"]
    if UNIVERSE == "sp400":
        return [c for c, s in stocks.items() if s["scale"] == "S&P400"]
    return list(stocks)


# ---------- 株価（yfinance） ----------
def fetch_prices(tickers, period="2y"):
    """{ticker: DataFrame(Open, High, Low, Close, Volume)} を返す。取れなかった銘柄は入らない。"""
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period=period, interval="1d", group_by="ticker", auto_adjust=True,
                                 threads=True, progress=False)
                break
            except Exception as e:
                log(f"  株価の取得に失敗（{attempt + 1}回目）", e)
                time.sleep(20)
        else:
            continue
        for t in chunk:
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if len(sub) >= 60:
                out[t] = sub
        log(f"  株価 {min(i + 200, len(tickers))}/{len(tickers)}")
        time.sleep(2)
    return out


def fetch_fundamentals(ticker):
    """CAN-SLIM の C・A・I に使う決算データ。取れない項目は None。"""
    import yfinance as yf
    t = yf.Ticker(ticker)
    f = {"fetched": dt.date.today().isoformat()}
    try:
        info = t.info or {}
    except Exception:
        info = {}
    f["qEpsYoY"] = pct_or_none(info.get("earningsQuarterlyGrowth"))
    f["qRevYoY"] = pct_or_none(info.get("revenueGrowth"))
    f["inst"] = pct_or_none(info.get("heldPercentInstitutions"))
    f["roe"] = pct_or_none(info.get("returnOnEquity"))
    f["epsAnnual"] = []
    try:
        inc = t.income_stmt
        row = next((k for k in ("Diluted EPS", "Basic EPS") if k in inc.index), None)
        if row:
            s = inc.loc[row].dropna().sort_index()
            f["epsAnnual"] = [[str(k)[:7], r(v, 2)] for k, v in s.items()][-4:]
    except Exception:
        pass
    try:   # 四半期 EPS：5 期あれば前年同期比を自前で計算（info の値より新しいことがある）
        q = t.quarterly_income_stmt
        row = next((k for k in ("Diluted EPS", "Basic EPS") if k in q.index), None)
        if row:
            s = q.loc[row].dropna().sort_index()
            f["epsQuarter"] = [[str(k)[:7], r(v, 2)] for k, v in s.items()][-6:]
            if len(s) >= 5 and s.iloc[-5] > 0:
                f["qEpsYoY"] = r((s.iloc[-1] / s.iloc[-5] - 1) * 100)
    except Exception:
        pass
    return f


def pct_or_none(v):
    try:
        return r(float(v) * 100) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------- 判定 ----------
def rs_raw(close):
    """IBD 流の相対力：直近 3 か月を 2 倍に重み付けした 1 年の騰落率。"""
    c = close.values
    if len(c) < 253:
        return None
    q = lambda n: c[-1] / c[-1 - n] - 1
    return 2 * q(63) + q(126) + q(189) + q(252)


def contractions(high, low, n=3, w=20):
    """直近 n×w 日を w 日ずつに区切った値幅（%）。VCP の「収縮」を見る。"""
    out = []
    for k in range(n, 0, -1):
        h = high.iloc[-k * w: len(high) - (k - 1) * w if k > 1 else None]
        l = low.iloc[-k * w: len(low) - (k - 1) * w if k > 1 else None]
        out.append((h.max() - l.min()) / h.max() * 100)
    return out


def evaluate(df):
    """1 銘柄の指標。RS の順位づけ前の値を返す。"""
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"].fillna(0)
    if len(c) < 200:
        return None
    ma50, ma150, ma200 = (c.rolling(n).mean() for n in (50, 150, 200))
    last = c.iloc[-1]
    hi52, lo52 = h.iloc[-252:].max(), l.iloc[-252:].min()
    m200_then = ma200.iloc[-22] if len(ma200.dropna()) > 22 else np.nan
    tt = [
        bool(last > ma150.iloc[-1] and last > ma200.iloc[-1]),
        bool(ma150.iloc[-1] > ma200.iloc[-1]),
        bool(ma200.iloc[-1] > m200_then),
        bool(ma50.iloc[-1] > ma150.iloc[-1] and ma50.iloc[-1] > ma200.iloc[-1]),
        bool(last > ma50.iloc[-1]),
        bool(last >= lo52 * 1.30),
        bool(last >= hi52 * 0.75),
        None,   # RS は全銘柄がそろってから
    ]
    vol50 = v.iloc[-50:].mean()
    vol10 = v.iloc[-10:].mean()
    turnover = (c.iloc[-20:] * v.iloc[-20:]).mean()
    con = contractions(h, l)
    pivot = h.iloc[-21:-1].max()     # 直近 20 日（当日を除く）の高値
    return {
        "close": last, "chg": (last / c.iloc[-2] - 1) * 100 if len(c) > 1 else None,
        "tt": tt, "rsRaw": rs_raw(c),
        "fromHigh": (last / hi52 - 1) * 100, "fromLow": (last / lo52 - 1) * 100,
        "volRatio": v.iloc[-1] / vol50 if vol50 else None, "dryUp": vol10 / vol50 if vol50 else None,
        "turnover": turnover, "con": con,
        # VCP 候補：値幅が段階的に縮み、直近は 10% 以内、出来高も細っている
        "vcp": bool(con[0] > con[1] > con[2] and con[2] <= 10 and vol50 and vol10 / vol50 < 0.8),
        "breakout": bool(last > pivot and vol50 and v.iloc[-1] >= 1.5 * vol50),
        "pivot": pivot, "toPivot": (pivot / last - 1) * 100,
        "newHigh": bool(h.iloc[-1] >= hi52),
        "ma": (ma50.iloc[-1], ma150.iloc[-1], ma200.iloc[-1]),
        "_series": (c, ma50, ma150, ma200),
    }


def market_state(df):
    """地合い（CAN-SLIM の M）：指数 ETF の移動平均と売り抜け日。"""
    if df is None or len(df) < 200:
        return {"state": "不明", "note": "指数 ETF のデータが取れませんでした"}
    c, v = df["Close"], df["Volume"].fillna(0)
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    ret = c.pct_change()
    dist = int(((ret.iloc[-25:] <= -0.002) & (v.iloc[-25:] > v.shift(1).iloc[-25:])).sum())
    above50, above200, up = c.iloc[-1] > ma50.iloc[-1], c.iloc[-1] > ma200.iloc[-1], ma50.iloc[-1] > ma200.iloc[-1]
    if above50 and above200 and up and dist <= 4:
        state = "上昇トレンド"
    elif above200 and dist <= 6:
        state = "注意（調整局面）"
    else:
        state = "下降・弱い地合い"
    return {"state": state, "close": r(c.iloc[-1], 2), "above50": bool(above50), "above200": bool(above200),
            "ma50over200": bool(up), "distDays": dist, "chg": r((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2),
            "spark": spark(c.iloc[-120:])}


def combined_market(prices):
    """S&P500 と NASDAQ100 の両方を見る。両方上昇なら上昇トレンド、両方弱ければ下降。"""
    idx = []
    for t, label in INDEX_ETFS:
        m = market_state(prices.get(t))
        m["label"] = label
        idx.append(m)
    known = [m["state"] for m in idx if m["state"] != "不明"]
    if not known:
        overall = "不明"
    elif all(s == "上昇トレンド" for s in known):
        overall = "上昇トレンド"
    elif all(s.startswith("下降") for s in known):
        overall = "下降・弱い地合い"
    else:
        overall = "注意（調整局面）"
    return {**idx[0], "state": overall, "indices": idx}


def spark(series):
    s = series.dropna()
    if s.empty:
        return []
    base = s.iloc[0]
    return [r((x / base - 1) * 100, 1) for x in s.values]


def canslim(e, f, rs):
    """満たした文字を返す（判定できない文字は None）。基準は O'Neil の目安。"""
    out = {}
    out["C"] = None if f.get("qEpsYoY") is None else f["qEpsYoY"] >= 25
    eps = [v for _, v in f.get("epsAnnual") or [] if v is not None][-3:]
    if len(eps) < 3:
        out["A"] = None
    elif all(x > 0 for x in eps):   # 直近 3 期とも黒字のときだけ伸び率を計算（赤字をはさむと率が意味をなさない）
        g = [(eps[i] / eps[i - 1] - 1) * 100 for i in (1, 2)]
        out["A"] = all(x >= 20 for x in g) and (eps[2] / eps[0]) ** 0.5 - 1 >= 0.20
    else:
        out["A"] = False
    out["N"] = e["fromHigh"] >= -5
    out["S"] = bool(e["breakout"] or (e["volRatio"] or 0) >= 1.4)
    out["L"] = rs is not None and rs >= 80
    out["I"] = None if f.get("inst") is None else f["inst"] >= 30   # 米国株は機関保有が多いので基準を上げる
    return {k: (None if v is None else bool(v)) for k, v in out.items()}


# ---------- メイン ----------
def run(prices=None, fundamentals_fn=None):
    u = universe()
    codes = pick(u["stocks"])
    tickers = list(codes)
    log(f"対象 {len(codes)} 銘柄（{UNIVERSE}）")
    if prices is None:
        prices = fetch_prices(tickers + [t for t, _ in INDEX_ETFS])
    fundamentals_fn = fundamentals_fn or fetch_fundamentals

    evals = {}
    for c in codes:
        df = prices.get(c)
        if df is None:
            continue
        try:
            e = evaluate(df)
        except Exception as ex:
            log(f"  {c} 計算できず", ex)
            continue
        if e and e["turnover"] >= MIN_TURNOVER:
            evals[c] = e
    if len(evals) < 50:
        sys.exit(f"有効な銘柄が {len(evals)} しかありません。株価の取得に失敗した可能性があります")
    asof = max(prices[c].index[-1] for c in evals)
    asof = pd.Timestamp(asof).date().isoformat()

    # RS 順位（1〜99）
    raw = pd.Series({c: e["rsRaw"] for c, e in evals.items() if e["rsRaw"] is not None})
    rank = (raw.rank(pct=True) * 98 + 1).round().astype(int)
    for c, e in evals.items():
        e["rs"] = int(rank[c]) if c in rank else None
        e["tt"][7] = e["rs"] is not None and e["rs"] >= 70

    # 決算データ（トレンドテンプレートを 7 条件以上満たす銘柄だけ。キャッシュあり）
    fpath = DATA / "fundamentals.json"
    fund = load(fpath, {})
    stale = (dt.date.today() - dt.timedelta(days=FUND_DAYS)).isoformat()
    need = [c for c, e in evals.items() if sum(bool(x) for x in e["tt"]) >= 7
            and fund.get(c, {}).get("fetched", "") < stale]
    log(f"決算データを取得 {len(need)} 銘柄")
    for i, c in enumerate(need):
        try:
            fund[c] = fundamentals_fn(c)
        except Exception as ex:
            log(f"  {c} 決算データ取得失敗", ex)
        if i % 20 == 19:
            save(fpath, fund)
        time.sleep(0.6 if fundamentals_fn is fetch_fundamentals else 0)
    save(fpath, fund)

    prev = load(DATA / "screen.json", {})
    prev_pass = {i["code"] for i in prev.get("items", []) if i.get("ttPass") == 8} if prev.get("asof") != asof \
        else set(prev.get("prevPass", []))

    items = []
    for c, e in evals.items():
        s = u["stocks"][c]
        n = sum(bool(x) for x in e["tt"])
        f = fund.get(c, {}) if n >= 7 else {}
        it = {"code": c, "name": s["name"], "sector": s["sector"], "market": s["market"], "scale": s["scale"],
              "close": r(e["close"], 2), "chg": r(e["chg"], 2), "rs": e["rs"], "tt": e["tt"], "ttPass": n,
              "fromHigh": r(e["fromHigh"]), "fromLow": r(e["fromLow"]), "volRatio": r(e["volRatio"], 2),
              "dryUp": r(e["dryUp"], 2), "turnover": r(e["turnover"] / 1e6, 0),
              "vcp": e["vcp"], "breakout": e["breakout"], "newHigh": e["newHigh"],
              "pivot": r(e["pivot"], 2), "toPivot": r(e["toPivot"], 1), "con": [r(x) for x in e["con"]]}
        if n >= 7:
            cl, m50, m150, m200 = e["_series"]
            base = cl.iloc[-120]
            it["spark"] = {k: [r((x / base - 1) * 100) for x in sr.iloc[-120:].values]
                           for k, sr in (("c", cl), ("m50", m50), ("m150", m150), ("m200", m200))}
            it["fund"] = {k: f.get(k) for k in ("qEpsYoY", "qRevYoY", "inst", "roe", "epsAnnual", "epsQuarter")}
            try:
                it["cs"] = canslim(e, f, e["rs"])
            except Exception as ex:   # 決算データの形が想定外でも全体は止めない
                log(f"  {c} CAN-SLIM 判定できず", ex)
                it["cs"] = {}
            it["new"] = n == 8 and c not in prev_pass
        items.append(it)
    items.sort(key=lambda i: (-i["ttPass"], -(i["rs"] or 0)))

    out = {"asof": asof, "updated": dt.datetime.now(JST).isoformat(timespec="minutes"), "universe": UNIVERSE,
           "count": len(items), "minTurnover": MIN_TURNOVER, "ttLabels": TT_LABELS,
           "market": combined_market(prices), "prevPass": sorted(prev_pass), "items": items}
    save(DATA / "screen.json", out)

    # 通過銘柄の履歴（日付ごと。何日連続で通過しているかを見るため）
    hist = load(DATA / "history.json", {})
    hist[asof] = [i["code"] for i in items if i["ttPass"] == 8]
    hist = dict(sorted(hist.items())[-120:])
    save(DATA / "history.json", hist)
    log(f"{asof}：トレンドテンプレート通過 {len(hist[asof])} 銘柄 / {len(items)}（地合い：{out['market']['state']}）")


if __name__ == "__main__":
    run()
