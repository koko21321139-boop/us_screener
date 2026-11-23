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
SECTOR_ETFS = {"情報技術": "XLK", "ヘルスケア": "XLV", "金融": "XLF", "一般消費財": "XLY", "通信サービス": "XLC",
               "資本財": "XLI", "生活必需品": "XLP", "エネルギー": "XLE", "公益": "XLU", "不動産": "XLRE", "素材": "XLB"}
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
            f["epsYoYs"] = yoy_list(s)
        rrow = next((k for k in ("Total Revenue", "Operating Revenue") if k in q.index), None)
        if rrow:
            f["revYoYs"] = yoy_list(q.loc[rrow].dropna().sort_index())
    except Exception:
        pass
    # 決算の加速：前年同期比の伸び率が、直近 2 期で高まっている（ミネルヴィニの重視点）
    for k, out in (("epsYoYs", "accelEps"), ("revYoYs", "accelRev")):
        g = f.get(k) or []
        f[out] = None if len(g) < 2 else bool(g[-1] > g[-2] and g[-1] > 0)
    f["nextEarnings"] = next_earnings(t)
    return f


def yoy_list(s):
    """四半期の値から、前年同期比（%）の列を作る（直近 3 期まで）。"""
    out = []
    for i in range(4, len(s)):
        if s.iloc[i - 4] and s.iloc[i - 4] > 0:
            out.append(r((s.iloc[i] / s.iloc[i - 4] - 1) * 100))
    return out[-3:]


def next_earnings(t):
    """次の決算発表予定日（YYYY-MM-DD）。取れなければ None。"""
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else list(cal.loc["Earnings Date"])
        today = dt.date.today()
        ds = sorted(pd.Timestamp(x).date() for x in (dates or []) if x is not None)
        ds = [x for x in ds if x >= today]
        return ds[0].isoformat() if ds else None
    except Exception:
        return None


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


def evaluate(df, bench=None):
    """1 銘柄の指標。RS の順位づけ前の値を返す。bench は指数 ETF の終値（RS ライン用）。"""
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
    fromHigh = (last / hi52 - 1) * 100
    # 50日線からの乖離（ミネルヴィニは伸びきった所での買いを戒める）
    ext50 = (last / ma50.iloc[-1] - 1) * 100
    # RS ライン（指数に対する相対株価）が 52 週高値圏か。株価より先に高値なら「先行」
    rs_high = rs_lead = None
    if bench is not None:
        b = bench.reindex(c.index).ffill()
        ratio = (c / b).dropna()
        if len(ratio) >= 200:
            rs_high = bool(ratio.iloc[-1] >= ratio.iloc[-252:].max() * 0.995)
            rs_lead = bool(rs_high and fromHigh < -3)
    # 出来高の質：直近 50 日の上昇日の出来高合計 ÷ 下落日の出来高合計（1.2 以上で買い集め）
    d = c.diff().iloc[-50:]
    upv, dnv = v.iloc[-50:][d > 0].sum(), v.iloc[-50:][d < 0].sum()
    ud = upv / dnv if dnv else None
    # 3 週タイト：週足の終値が 3 週続けて 1.5% 以内
    wk = c.resample("W-FRI").last().dropna()
    tight = bool(len(wk) >= 3 and (wk.iloc[-3:].max() / wk.iloc[-3:].min() - 1) * 100 <= 1.5)
    # ポケットピボット：上昇した日の出来高が、直近 10 日の下落日の最大出来高を上回り、10日線・50日線の上
    prev10 = c.diff().iloc[-11:-1]
    downmax = v.iloc[-11:-1][prev10 < 0].max() if (prev10 < 0).any() else None
    ma10 = c.rolling(10).mean().iloc[-1]
    pocket = bool(downmax and last > c.iloc[-2] and v.iloc[-1] > downmax and last >= ma10 and last >= ma50.iloc[-1])
    # ATR（14 日の平均的な値幅）。損切り幅は ATR の 2 倍を 3〜8% に収める
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_pct = tr.iloc[-14:].mean() / last * 100
    stop_pct = min(8.0, max(3.0, 2 * atr_pct))
    return {
        "close": last, "chg": (last / c.iloc[-2] - 1) * 100 if len(c) > 1 else None,
        "r21": (last / c.iloc[-22] - 1) * 100 if len(c) > 22 else None,
        "tt": tt, "rsRaw": rs_raw(c),
        "fromHigh": fromHigh, "fromLow": (last / lo52 - 1) * 100,
        "ext50": ext50, "extended": bool(ext50 >= 25), "rsLineHigh": rs_high, "rsLead": rs_lead,
        "ud": ud, "tight": tight, "pocket": pocket, "atrPct": atr_pct, "stopPct": stop_pct,
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


def sector_table(prices, evals, stocks):
    """セクターETFの騰落率と、セクター内の銘柄の状況（通過数・50日線より上の割合・RS）。"""
    out = []
    for name, t in SECTOR_ETFS.items():
        df = prices.get(t)
        row = {"name": name, "etf": t}
        if df is not None and len(df) > 70:
            c = df["Close"].dropna()
            ret = lambda n: r((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2)
            ytd = c[c.index >= pd.Timestamp(c.index[-1].year, 1, 1)]
            base = c[c.index < pd.Timestamp(c.index[-1].year, 1, 1)]
            row.update({"d1": ret(1), "w1": ret(5), "m1": ret(21), "m3": ret(63),
                        "ytd": r((c.iloc[-1] / base.iloc[-1] - 1) * 100, 2) if len(base) else None,
                        "above50": bool(c.iloc[-1] > c.rolling(50).mean().iloc[-1]),
                        "above200": bool(len(c) >= 200 and c.iloc[-1] > c.rolling(200).mean().iloc[-1]),
                        "spark": spark(c.iloc[-63:])})
        members = [(code, e) for code, e in evals.items() if stocks[code]["sector"] == name]
        if members:
            rs = sorted(e["rs"] for _, e in members if e.get("rs") is not None)
            r21 = sorted(e["r21"] for _, e in members if e.get("r21") is not None)
            row.update({"n": len(members), "pass": sum(sum(bool(x) for x in e["tt"]) == 8 for _, e in members),
                        "pct50": r(sum(bool(e["tt"][4]) for _, e in members) / len(members) * 100, 0),
                        "rsMed": rs[len(rs) // 2] if rs else None, "m1Med": r(r21[len(r21) // 2], 1) if r21 else None})
        out.append(row)
    # セクターの RS（ETF の 3 か月騰落率の順位。1 が最強）
    ranked = sorted([x for x in out if x.get("m3") is not None], key=lambda x: -x["m3"])
    for k, x in enumerate(ranked, 1):
        x["rank"] = k
    return out


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


# ---------- 成績表 ----------
def safe_perf(dfs, bench):
    try:
        return performance(dfs, bench, MIN_TURNOVER)
    except Exception as ex:   # 成績表が作れなくてもスクリーニングは止めない
        log("  成績表を作れませんでした", ex)
        return None


def performance(dfs, bench, min_turnover, horizons=(21, 63), step=5):
    """過去の株価から各日のトレンドテンプレート通過銘柄をさかのぼって判定し、その後の騰落率を集計する。
    （今の構成銘柄で計算するので、上場廃止などで消えた銘柄は含まれない＝やや良く出る）"""
    if len(dfs) < 50:
        return None
    C = pd.DataFrame({k: d["Close"] for k, d in dfs.items()}).sort_index()
    H = pd.DataFrame({k: d["High"] for k, d in dfs.items()}).reindex(C.index)
    L = pd.DataFrame({k: d["Low"] for k, d in dfs.items()}).reindex(C.index)
    V = pd.DataFrame({k: d["Volume"] for k, d in dfs.items()}).reindex(C.index).fillna(0)
    m50, m150, m200 = (C.rolling(n, min_periods=n).mean() for n in (50, 150, 200))
    hi, lo = H.rolling(252, min_periods=200).max(), L.rolling(252, min_periods=200).min()
    raw = 2 * C / C.shift(63) + C / C.shift(126) + C / C.shift(189) + C / C.shift(252)
    rs = raw.rank(axis=1, pct=True) * 98 + 1
    ok = ((C > m150) & (C > m200) & (m150 > m200) & (m200 > m200.shift(21)) & (m50 > m150) & (m50 > m200)
          & (C > m50) & (C >= lo * 1.3) & (C >= hi * 0.75) & (rs >= 70)
          & ((C * V).rolling(20).mean() >= min_turnover))
    valid = m200.notna() & ((C * V).rolling(20).mean() >= min_turnover)
    b = bench.reindex(C.index).ffill() if bench is not None else None
    out = {}
    for hz in horizons:
        fwd = C.shift(-hz) / C - 1
        rows = range(260, len(C) - hz, step)
        picks, univ, idx = [], [], []
        for i in rows:
            p = fwd.iloc[i][ok.iloc[i]].dropna()
            u = fwd.iloc[i][valid.iloc[i]].dropna()
            if len(p):
                picks += list(p.values)
            if len(u):
                univ.append(float(u.median()))
            if b is not None and pd.notna(b.iloc[i]) and pd.notna(b.iloc[i + hz]):
                idx.append(b.iloc[i + hz] / b.iloc[i] - 1)
        if not picks:
            continue
        a = np.array(picks) * 100
        out[f"h{hz}"] = {"n": len(a), "dates": len(rows), "mean": r(a.mean()), "median": r(np.median(a)),
                         "win": r((a > 0).mean() * 100, 0), "big": r((a >= 20).mean() * 100, 0),
                         "loss": r((a <= -10).mean() * 100, 0),
                         "univ": r(np.mean(univ) * 100) if univ else None,
                         "bench": r(np.mean(idx) * 100) if idx else None}
    if not out:
        return None
    first = C.index[260] if len(C) > 260 else C.index[0]
    out["from"] = pd.Timestamp(first).date().isoformat()
    out["to"] = pd.Timestamp(C.index[-1]).date().isoformat()
    return out


# ---------- メイン ----------
def run(prices=None, fundamentals_fn=None):
    u = universe()
    codes = pick(u["stocks"])
    tickers = list(codes)
    log(f"対象 {len(codes)} 銘柄（{UNIVERSE}）")
    if prices is None:
        prices = fetch_prices(tickers + [t for t, _ in INDEX_ETFS] + list(SECTOR_ETFS.values()))
    fundamentals_fn = fundamentals_fn or fetch_fundamentals

    bench = prices.get(INDEX_ETFS[0][0])
    bench = bench["Close"] if bench is not None else None
    evals = {}
    for c in codes:
        df = prices.get(c)
        if df is None:
            continue
        try:
            e = evaluate(df, bench)
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
            and (fund.get(c, {}).get("fetched", "") < stale
                 or (fund.get(c, {}).get("nextEarnings") or "9999") < dt.date.today().isoformat())]
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
              "ext50": r(e["ext50"]), "extended": e["extended"], "rsLineHigh": e["rsLineHigh"], "rsLead": e["rsLead"],
              "ud": r(e["ud"], 2), "tight": e["tight"], "pocket": e["pocket"],
              "atrPct": r(e["atrPct"], 1), "stopPct": r(e["stopPct"], 1),
              "pivot": r(e["pivot"], 2), "toPivot": r(e["toPivot"], 1), "con": [r(x) for x in e["con"]]}
        if n >= 7:
            cl, m50, m150, m200 = e["_series"]
            base = cl.iloc[-120]
            it["spark"] = {k: [r((x / base - 1) * 100) for x in sr.iloc[-120:].values]
                           for k, sr in (("c", cl), ("m50", m50), ("m150", m150), ("m200", m200))}
            it["fund"] = {k: f.get(k) for k in ("qEpsYoY", "qRevYoY", "inst", "roe", "epsAnnual", "epsQuarter",
                                                "epsYoYs", "revYoYs", "accelEps", "accelRev", "nextEarnings")}
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
           "market": combined_market(prices), "sectors": sector_table(prices, evals, u["stocks"]), "prevPass": sorted(prev_pass), "items": items,
           "perf": safe_perf({c: prices[c] for c in evals}, bench)}
    save(DATA / "screen.json", out)

    # 通過銘柄の履歴（日付ごと。何日連続で通過しているかを見るため）
    hist = load(DATA / "history.json", {})
    hist[asof] = [i["code"] for i in items if i["ttPass"] == 8]
    hist = dict(sorted(hist.items())[-120:])
    save(DATA / "history.json", hist)
    log(f"{asof}：トレンドテンプレート通過 {len(hist[asof])} 銘柄 / {len(items)}（地合い：{out['market']['state']}）")


if __name__ == "__main__":
    run()
