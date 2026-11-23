# us_screener（米国成長株スクリーナー）

ミネルヴィニの**トレンドテンプレート**と O'Neil の **CAN-SLIM** で、米国株（**S&P500＋S&P400**、約 900 社）を毎日スクリーニングし、iPhone で一覧表示するアプリです。データはすべて無料のものを使います。日本株版（stock_screener）と同じ判定方法です。

## しくみ

```
日本時間 火〜土 7:17（米国の大引け後）
  GitHub Actions（.github/workflows/screen.yml）が scripts/screen.py を実行
    → Wikipedia の S&P500 / S&P400 構成銘柄表で対象を決める（週 1 回更新）
    → Yahoo Finance（yfinance）から 2 年分の日足を取得
    → 条件を判定し、data/screen.json と data/history.json に保存してコミット
  GitHub Pages が index.html と data/ を配信
```

## 判定の中身

- **トレンドテンプレート 8 条件**・**RS**（対象銘柄内で 1〜99）・**VCP候補**・**ブレイク**は日本株版と同じ
- **CAN-SLIM**：C（四半期 EPS +25% 以上）／A（年間 EPS が 2 年連続 +20% 以上）／N（52 週高値から 5% 以内）／S（出来高 1.4 倍以上）／L（RS 80 以上）／I（機関投資家の保有 30% 以上。米国株は保有比率が高いので日本株版より厳しめ）／M（地合い）
- **地合い（M）**：S&P500（SPY）と NASDAQ100（QQQ）をそれぞれ判定。両方が上昇トレンドなら「上昇トレンド」、両方弱ければ「下降」、それ以外は「注意」

## 設定（任意。Settings → Secrets and variables → Actions → Variables）

- `UNIVERSE`：`sp900`（既定）／`sp500`／`sp400`
- `MIN_TURNOVER`：20 日平均売買代金の下限（ドル。既定 20000000 = 2,000 万ドル）

## 注意

- yfinance は Yahoo Finance の非公式な取得方法です。仕様変更で突然取れなくなることがあります。
- 構成銘柄は Wikipedia の表から取得しています。表の形が変わると取れなくなることがあり、その場合は前回の一覧を使い続けます。
- 判定は機械的なもので、売買の推奨ではありません。チャートと決算を自分で確認してください。
