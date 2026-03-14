# PS C:\Python> streamlit run c:/Python/Tohon_Building/app.py で実行

# ============================================================
# 不動産登記PDFから建物面積を抽出するStreamlitアプリ
# ・pdfplumberで文字位置を取得
# ・「主である建物」「一棟の建物」「専有部分の建物」を判定
# ・面積行を抽出
# ・面積値に抹消線がかかっている場合は除外
# ============================================================

import traceback
from datetime import datetime
from pathlib import Path
import io

import streamlit as st
import streamlit.components.v1 as components
import pdfplumber
import re
import unicodedata


# ============================================================
# ログ出力
# ・runtime/app_debug.log に処理内容を書き出す
# ・不具合時の追跡用
# ============================================================

LOG_PATH = Path(__file__).resolve().parent.parent / "runtime" / "app_debug.log"


def log(msg: str):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8", errors="ignore") as f:
        f.write(f"{datetime.now().isoformat()}  {msg}\n")


# ============================================================
# Streamlit画面設定
# ============================================================

st.set_page_config(page_title="不動産登記 建物面積集計", layout="wide")


# ============================================================
# 文字の正規化
# ・全角数字 → 半角
# ・罫線文字削除
# ・「:」→「.」
# ============================================================

def clean_text(text: str) -> str:
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[|│┃‖]", "", text)
    text = text.replace(":", ".")
    return text


# ============================================================
# ページ内の横線を収集
# ・抹消線候補は水平線なので、それだけを集める
# ・page.lines / page.rects / page.curves を対象にする
# ============================================================

def collect_horizontal_lines(page):
    lines = []

    # 通常の line オブジェクト
    for l in page.lines:
        if abs(l["top"] - l["bottom"]) <= 1.5:
            lines.append({
                "x0": min(l["x0"], l["x1"]),
                "x1": max(l["x0"], l["x1"]),
                "y": (l["top"] + l["bottom"]) / 2
            })

    # 薄い矩形を横線として扱う
    for r in page.rects:
        height = abs(r["bottom"] - r["top"])
        width = abs(r["x1"] - r["x0"])
        if height <= 1.5 and width > 3:
            lines.append({
                "x0": min(r["x0"], r["x1"]),
                "x1": max(r["x0"], r["x1"]),
                "y": (r["top"] + r["bottom"]) / 2
            })

    # curve の bbox も横線候補に含める
    for c in page.curves:
        try:
            x0 = min(c["x0"], c["x1"])
            x1 = max(c["x0"], c["x1"])
            top = min(c["top"], c["bottom"])
            bottom = max(c["top"], c["bottom"])

            if abs(top - bottom) <= 1.5 and (x1 - x0) > 3:
                lines.append({
                    "x0": x0,
                    "x1": x1,
                    "y": (top + bottom) / 2
                })
        except Exception:
            pass

    return lines


# ============================================================
# 1文字に抹消線がかかっているか判定
# ・文字中央限定ではなく、やや下側まで許容
# ・ただし文字の外側すぎる線は除外
# ・各文字に対して「線が文字内部を横切るか」を見る
# ============================================================

def is_deleted_char(char, horizontal_lines):
    c_x0 = char["x0"]
    c_x1 = char["x1"]
    c_top = char["top"]
    c_bottom = char["bottom"]

    c_w = max(c_x1 - c_x0, 0.1)
    c_h = max(c_bottom - c_top, 0.1)

    # 抹消線として許容する縦位置
    # 中央よりやや上～下端の少し手前まで
    y_min = c_top + c_h * 0.28
    y_max = c_bottom - c_h * 0.02

    for ln in horizontal_lines:
        y = ln["y"]

        # 文字内部を通る線のみ対象
        if not (y_min <= y <= y_max):
            continue

        overlap = max(0, min(c_x1, ln["x1"]) - max(c_x0, ln["x0"]))

        # 文字幅の一定割合以上に線がかかっていれば抹消候補
        if overlap >= c_w * 0.35:
            return True

    return False


# ============================================================
# 文字列全体に抹消線があるか判定
# ・数値文字列全体に対して判定する
# ・一定割合以上の文字に抹消線がかかっていたら抹消扱い
# ============================================================

def is_deleted_text_span(chars, horizontal_lines):
    if not chars:
        return False

    # 空白相当文字は除外
    target_chars = [c for c in chars if clean_text(c["text"]).strip()]

    if not target_chars:
        return False

    hit_count = 0

    for c in target_chars:
        if is_deleted_char(c, horizontal_lines):
            hit_count += 1

    # 半数以上、かつ最低2文字に線がかかっていれば抹消扱い
    return hit_count >= max(2, (len(target_chars) + 1) // 2)


# ============================================================
# 1行の文字列を正規化しつつ、
# 正規化後の各文字が元PDFのどのcharに対応するかを保持する
# ============================================================

def build_normalized_line(line_chars):
    norm_items = []

    for c in line_chars:
        text = clean_text(c["text"])

        for ch in text:
            norm_items.append({
                "norm_char": ch,
                "orig_char": c
            })

    line_norm = "".join(item["norm_char"] for item in norm_items)

    return line_norm, norm_items


# ============================================================
# 正規化後文字列の位置範囲から、
# 元PDFのchar配列を取り出す
# ・正規表現の match.span() で得た位置を元に使用
# ============================================================

def chars_from_norm_span(norm_items, start, end):
    chars = []
    seen = set()

    for i in range(start, end):
        if i < 0 or i >= len(norm_items):
            continue

        c = norm_items[i]["orig_char"]
        key = id(c)

        if key not in seen:
            seen.add(key)
            chars.append(c)

    return chars


# ============================================================
# PDF解析本体
# ・ページごとに処理
# ・左側～面積欄までを対象に行単位で解析
# ・面積パターンを検出し、抹消値を除外して集計
# ============================================================

def process_pdf(file):
    log("process_pdf: start")

    # Streamlit UploadedFile を BytesIO に変換して固定
    if hasattr(file, "getvalue"):
        b = file.getvalue()
        log(f"process_pdf: got bytes len={len(b)}")
        file = io.BytesIO(b)

    # セクション別の出力箱
    results = {
        "主である建物": {"areas": [], "total": 0.0},
        "一棟の建物": {"areas": [], "total": 0.0},
        "専有部分の建物": {"areas": [], "total": 0.0}
    }

    current_section = "主である建物"

    try:
        with pdfplumber.open(file) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                chars = page.chars
                horizontal_lines = collect_horizontal_lines(page)

                # ====================================================
                # 面積欄より右の「原因及びその日付」欄を除外するため、
                # 長い縦罫線から境界X座標を推定する
                # 見つからない場合はページ幅の65%を仮の境界にする
                # ====================================================

                v_lines = [
                    l["x0"] for l in page.lines
                    if abs(l["x0"] - l["x1"]) < 1
                    and l["x0"] > page.width * 0.5
                    and (l["bottom"] - l["top"]) > 50
                ]

                boundary_x = min(v_lines) if v_lines else page.width * 0.65

                # ====================================================
                # 行グルーピング
                # ・同じ top 値近辺の文字を同一行としてまとめる
                # ====================================================

                y_groups = {}

                for c in chars:
                    # 原因欄より右は対象外
                    if c["x0"] > boundary_x:
                        continue

                    y = round(c["top"], 1)
                    y_groups.setdefault(y, []).append(c)

                # ====================================================
                # 行ごとに解析
                # ====================================================

                for y in sorted(y_groups.keys()):
                    line_chars = sorted(y_groups[y], key=lambda x: x["x0"])
                    line_raw = "".join(c["text"] for c in line_chars)
                    line_norm, norm_items = build_normalized_line(line_chars)

                    # セクション見出し判定用
                    clean_nav = re.sub(r"\s+", "", line_raw)

                    # --------------------------------------------
                    # セクション切替
                    # --------------------------------------------
                    if "附属建物" in clean_nav and "表示" in clean_nav:
                        current_section = None
                        continue

                    if "一棟の建物" in clean_nav and "表示" in clean_nav:
                        current_section = "一棟の建物"
                        continue

                    if "専有部分" in clean_nav and "表示" in clean_nav:
                        current_section = "専有部分の建物"
                        continue

                    if "主である建物" in clean_nav and "表示" in clean_nav:
                        current_section = "主である建物"
                        continue

                    if current_section is None:
                        continue

                    # --------------------------------------------
                    # 面積行抽出
                    # 例:
                    # 1階 7313.05
                    # 地下4階 1234.56
                    # 3  1234.56 のような簡略表記も一応許容
                    # --------------------------------------------
                    m = re.search(
                        r"((?:地下)?\d+階(?:部分)?|^\d+)\s*(\d+\.\d+)",
                        line_norm
                    )

                    if not m:
                        continue

                    label = m.group(1)
                    val_str = m.group(2)

                    # --------------------------------------------
                    # 正規化後のマッチ範囲を元に、
                    # 対応する元PDF文字を取得
                    # ・label_chars: 階ラベル
                    # ・value_chars: 面積値
                    # 抹消判定は面積値だけを見る
                    # --------------------------------------------
                    label_chars = chars_from_norm_span(norm_items, *m.span(1))
                    value_chars = chars_from_norm_span(norm_items, *m.span(2))

                    # 面積値に抹消線がある場合は除外
                    if is_deleted_text_span(value_chars, horizontal_lines):
                        log(f"page={page_no}, y={y}: deleted skip -> {line_norm}")
                        continue

                    # 数値化して集計
                    try:
                        val = float(val_str)

                        # 異常値防止
                        if val < 50000 and current_section in results:
                            results[current_section]["areas"].append((label, val))
                            results[current_section]["total"] += val
                            log(f"page={page_no}, y={y}: hit [{current_section}] {label} {val}")

                    except ValueError:
                        continue

        return results

    except Exception as e:
        log("process_pdf: EXCEPTION " + repr(e))
        log(traceback.format_exc())
        raise


# ============================================================
# 画面UI
# ============================================================

st.title("🏢 不動産登記 建物面積自動集計")
st.markdown(
    "PDFから「主である建物」「一棟の建物」「専有部分」の面積を抽出します。"
    " ※抹消された面積は除外します。"
)


# ============================================================
# アップローダの縦幅調整
# ・見やすさのため、親DOMにCSSを注入する
# ============================================================

components.html(
    """
    <script>
    (function () {
      const STYLE_ID = "uploader-height-patch-v1";
      const doc = window.parent.document;
      if (doc.getElementById(STYLE_ID)) return;

      const style = doc.createElement("style");
      style.id = STYLE_ID;
      style.textContent = `
        section[data-testid="stFileUploaderDropzone"]{
          min-height: 240px !important;
          padding-top: 70px !important;
          padding-bottom: 70px !important;
          display: flex !important;
          align-items: center !important;
        }

        div[data-testid="stFileUploader"]{
          width: 100%;
        }
      `;
      doc.head.appendChild(style);
    })();
    </script>
    """,
    height=0,
)


# ============================================================
# PDFアップロード
# ============================================================

uploaded_file = st.file_uploader("登記簿PDFをアップロード", type="pdf")


# ============================================================
# アップロード後の解析処理
# ・3カラムで各セクションを表示
# ============================================================

if uploaded_file:
    try:
        with st.spinner("解析中..."):
            data = process_pdf(uploaded_file)

        cols = st.columns(3)
        sections = ["主である建物", "一棟の建物", "専有部分の建物"]

        for i, section in enumerate(sections):
            with cols[i]:
                st.subheader(section)

                if data[section]["areas"]:
                    for label, area in data[section]["areas"]:
                        st.write(f"{label}: {area:,.2f} ㎡")

                    st.divider()
                    st.metric("合計", f"{data[section]['total']:,.2f} ㎡")
                else:
                    st.write("該当なし")

    except Exception as e:
        st.error("解析中にエラーが発生しました。")
        st.exception(e)
        st.stop()
