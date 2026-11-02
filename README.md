# book-ai 蔵書検索

> BONSAI Work(現Pitchi-workの前身)のテストプロジェクトとして作った実験コードです。

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
| `batch_ocr.py` | 原本PDFをまとめてOCR |
| `ocr_pipeline.py` | 単一PDFのOCR |
| `ocr_diagnostics.py` | OCR結果の誤認字を診断 |
| `search/build_index.py` | `books.json`・`index.json` を生成 |
| `search/build_library_db.py` | 検索用SQLite(派生DB)を構築 |
| `search/app.py` | 検索API・PDF配信サーバー |
| `search/index.html` | 検索UI |
| `search/viewer.html`, `search/pdfjs/` | PDF.js ビューア |
| `search/evaluate_search.py` | 固定クエリで検索品質を評価 |

## 動作要件

- poppler-utils(`pdfinfo` / `pdftoppm`)— OCR時に使用
- tesseract(日本語データ `jpn` を含むこと)— OCR時に使用。`batch_ocr.py` は `jpn+eng` を指定するので `eng` も必要
- Python: 開発環境は 3.14.4。他のバージョンは**未検証**
- SQLite の FTS5 が有効であること(`build_library_db.py` が使用)
- 追加の Python パッケージは不要(標準ライブラリのみ)

> 検索のみ行う場合は poppler / tesseract は不要と思われます(コードからの推測で、未検証)。

## 前提データ配置

OCR出力は次の構造を想定します(パスは現状コード内の定数 `OCR_BASE` / `OCR_DIR` / `RAW`。環境に合わせて変更してください)。

```
<library>/raw/<category>/<file>.pdf              原本(変更禁止)
<library>/ocr/<category>/<part>.pdf/page-NNN.txt OCR本文
<library>/ocr/<category>/<part>.pdf/metadata.json {source, pages, status, ...}
```

## 使い方(実行順)

1. **OCR**(OCR済みデータが無い場合のみ)

   ```bash
   python3 batch_ocr.py --dry-run               # 対象の確認のみ
   python3 batch_ocr.py --category <カテゴリ名>   # 指定カテゴリだけOCR
   python3 batch_ocr.py                          # 全カテゴリ
   ```

   その他: `--limit N`(件数制限)、`--first N`、`--reset <slug>`(その1冊の結果を削除して再OCR)。
   **`--reset-all` は出力先(`OUT_BASE`)配下を削除します。実行前に必ず出力先を確認してください。**
   `--category` に指定できる名前は、現状 `batch_ocr.py` 内に固定された一覧です(個人の蔵書分類のため、自分の環境に合わせて書き換えが必要です)。

2. **`python3 search/build_index.py`** — `books.json` と `search/index.json` を生成
3. **`python3 search/build_library_db.py`** — `search/library.sqlite` を構築(OCR追加後も再実行)
4. **`python3 search/app.py 8721`** — サーバー起動。既定は `127.0.0.1:8721`

`http://127.0.0.1:8721/` を開きます。待受アドレスは環境変数 `BOOK_SEARCH_HOST` で変更できます。

**未検証の挙動**(実行して確認していません):

- `books.json` / `index.json` が無い状態で `app.py` を起動した場合の挙動
- `build_index.py` を省略して `build_library_db.py` → `app.py` だけで動くか(`app.py` は `books.json` のルートを持ちます)
- 「`build_index.py` → `build_library_db.py` → `app.py`」の実行順で正しく動くか(スクリプトの記述からの推測)
- Python 3.14.4 以外・SQLite FTS5 無効環境での動作

> **注意** 認証・アクセス制御は未実装です。`0.0.0.0` 等で公開する場合は、リバースプロキシや VPN で必ずアクセス制限してください。

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

## 免責

- 個人の蔵書を**自分で**検索する用途の実験コードです。動作や品質は保証しません。
- 他人の書籍を配布・公開検索する用途には使わないでください。書籍PDF・OCR本文・検索インデックスの再配布は著作権上の問題になり得ます。利用者自身の責任で、私的利用の範囲に留めてください。

## ライセンス

- 本リポジトリは MIT License です(`LICENSE` 参照)。
- `search/pdfjs/` は PDF.js(Apache-2.0)です。`THIRD_PARTY_NOTICES.md` を参照してください。
