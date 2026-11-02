# book-ai 蔵書検索

スキャン書籍(医学書など)をOCRし、全文検索して、**原本PDFの該当ページを根拠として確認する**ためのローカル検索システムです。個人の蔵書を自分で検索する用途を想定しており、書籍データ自体はこのリポジトリに含まれません。

- Python 標準ライブラリのみで動作するサーバー(外部パッケージ不要)
- SQLite FTS5(2文字n-gram)+ 厳密な部分一致による日本語検索(1文字検索も可)
- PDF.js による根拠ページ表示(Android Chrome など内蔵PDFビューアに依存しない)
- 引用ID `book_id:part_file:pdf-pN` を返し、将来のRAGが根拠を再取得可能

構成図は [docs/architecture.md](docs/architecture.md) を参照してください。

## リポジトリに含まれないもの

著作権・プライバシー保護のため、次は公開対象外です(`.gitignore` 済み)。

原本PDF / OCR本文 / `search/library.sqlite` / `search/index.json` / `books.json` / 評価結果(書籍本文の抜粋を含む) / ログ

利用者は自分の蔵書から OCR とインデックスを用意してください。

## 構成

| パス | 役割 |
| --- | --- |
| `batch_ocr.py`, `ocr_pipeline.py` | 原本PDFのOCR(Tesseract系)。読み取り専用で原本を扱う |
| `search/build_library_db.py` | OCR結果から `library.sqlite`(派生DB)を構築 |
| `search/app.py` | 検索API・PDF配信サーバー(単一Range対応) |
| `search/index.html` | 検索UI |
| `search/viewer.html`, `search/pdfjs/` | PDF.js ビューア |
| `search/evaluate_search.py`, `search/eval_queries.json` | 検索品質の固定クエリ評価 |

## 前提データ配置

OCR出力は次の構造を想定します(パスは現状コード内の定数 `OCR_BASE` / `OCR_DIR` / `RAW`。環境に合わせて変更してください)。

```
<library>/raw/<category>/<file>.pdf              原本(変更禁止)
<library>/ocr/<category>/<part>.pdf/page-NNN.txt OCR本文
<library>/ocr/<category>/<part>.pdf/metadata.json {source, pages, status, ...}
```

## 起動

```bash
python3 search/build_library_db.py   # 初回・OCR追加後のみ。派生DBを再構築
python3 search/app.py 8721           # 既定は 127.0.0.1:8721
```

`http://127.0.0.1:8721/` を開きます。待受アドレスは環境変数 `BOOK_SEARCH_HOST` で変更できます。

> **注意** 認証・アクセス制御は未実装です。`0.0.0.0` 等で公開する場合は、リバースプロキシや VPN で必ずアクセス制限してください。蔵書の再配布は著作権上の問題になり得ます。

## API

- `GET /books` — 統合後の蔵書一覧(`parts` に実PDFを保持)
- `GET /search?q=<語句>&book_id=<任意>&offset=0&limit=50` — 全文検索(`limit` 上限100)
- `GET /pdf/<実PDFファイル>` — 原本PDF(Range対応)。`#page=N` でページ指定
- `GET /viewer.html?file=<URLエンコード済み/pdf/...>&page=<N>` — PDF.js ビューア

検索結果には `book_title`, `source_file`, `pdf_page`, `book_pdf_page`, `ocr_text`, `search_text`, `snippet`, `citation_id`, `citation`, `source_url`, `viewer_url` などが含まれます。`pdf_page` は実PDF内の物理ページで、印刷ページ番号は推測しません。

## 書籍の統合規則

末尾が `開始-終了.pdf` で、同じ基部名の分割範囲が連続・非重複・2個以上の場合のみ1冊に統合します。欠落・重複・曖昧さがあれば統合しません。

## 公開前チェック

```bash
scripts/check_secrets.sh
```

コミット対象のファイルについて、禁止拡張子(PDF/SQLite/ログ等)・5MB超ファイル・秘密情報パターン・内部IP/個人パスを検査します(`git ls-files --others --exclude-standard` 基準なので `.gitignore` も反映されます)。

## 既知の制限・今後の課題

- 現行は連続部分一致のみ。自然文の質問、複数語AND/OR、同義語、BM25等の順位付けは未対応(`search/evaluate_search.py` で計測しながら改善予定)
- 印刷書籍ページ番号の対応付け
- 複数Range・条件付きリクエスト対応
- 認証・アクセス制御
- RAG連携(引用付き回答)
- パス設定の外部化(現状はコード内定数)

## ライセンス

未設定です。公開時に追加してください(同梱の `search/pdfjs/` は Apache-2.0 の PDF.js)。
