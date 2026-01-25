# PS C:\Python> streamlit run c:/Python/Tohon_Building/app.py で実行

import traceback
from datetime import datetime
from pathlib import Path
import io

import streamlit as st
import streamlit.components.v1 as components  # ★追加（親DOMへCSS注入）
import pdfplumber
import re
import unicodedata

# ログ出力先（配布フォルダの runtime\app_debug.log）
LOG_PATH = Path(__file__).resolve().parent.parent / "runtime" / "app_debug.log"

def log(msg: str):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8", errors="ignore") as f:
        f.write(f"{datetime.now().isoformat()}  {msg}\n")

st.set_page_config(page_title="不動産登記 建物面積集計", layout="wide")

def clean_text(text):
    if not text:
        return ""
    # 全角数字や記号を半角に正規化
    text = unicodedata.normalize('NFKC', text)
    # 登記簿特有の区切り文字を削除
    text = re.sub(r'[|│┃‖]', '', text)
    # 面積の「:」を「.」に置換
    return text.replace(':', '.')

def is_deleted_line(char, drawing_objs):
    """
    文字の周辺に抹消線が存在するか判定
    """
    c_x0, c_x1 = char['x0'], char['x1']
    c_top, c_bottom = char['top'], char['bottom']
    margin = 2.5

    for obj in drawing_objs:
        o_x0, o_x1 = obj['x0'], obj['x1']
        o_top, o_bottom = obj['top'], obj['bottom']

        if not (o_x1 < c_x0 or o_x0 > c_x1):
            if (c_top - margin) <= o_top <= (c_bottom + margin):
                if (o_x1 - o_x0) > 3.0:
                    return True
    return False

def process_pdf(file):
    log("process_pdf: start")

    # Streamlit UploadedFile をメモリ固定
    if hasattr(file, "getvalue"):
        b = file.getvalue()
        log(f"process_pdf: got bytes len={len(b)}")
        file = io.BytesIO(b)

    results = {
        "主である建物": {"areas": [], "total": 0.0},
        "一棟の建物": {"areas": [], "total": 0.0},
        "専有部分の建物": {"areas": [], "total": 0.0}
    }
    current_section = "主である建物"

    try:
        with pdfplumber.open(file) as pdf:
            for page in pdf.pages:
                chars = page.chars
                drawings = page.lines + page.rects + page.curves

                # --- 枠線のX座標を動的に特定 ---
                v_lines = [
                    l['x0'] for l in page.lines
                    if abs(l['x0'] - l['x1']) < 1
                    and l['x0'] > page.width * 0.5
                    and (l['bottom'] - l['top']) > 50
                ]

                boundary_x = min(v_lines) if v_lines else page.width * 0.65

                y_groups = {}
                for c in chars:
                    if c['x0'] > boundary_x:
                        continue
                    y = round(c['top'], 1)
                    y_groups.setdefault(y, []).append(c)

                for y in sorted(y_groups.keys()):
                    line_chars = sorted(y_groups[y], key=lambda x: x['x0'])
                    line_raw = "".join([c['text'] for c in line_chars])
                    line_norm = clean_text(line_raw)

                    # --- セクション判定 ---
                    clean_nav = re.sub(r'\s+', '', line_raw)

                    if "附属建物" in clean_nav and "表示" in clean_nav:
                        current_section = None
                        continue
                    elif "一棟の建物" in clean_nav and "表示" in clean_nav:
                        current_section = "一棟の建物"
                        continue
                    elif "専有部分" in clean_nav and "表示" in clean_nav:
                        current_section = "専有部分の建物"
                        continue
                    elif "主である建物" in clean_nav and "表示" in clean_nav:
                        current_section = "主である建物"
                        continue

                    if current_section is None:
                        continue

                    # --- 面積抽出 ---
                    area_match = re.search(
                        r'((?:地下)?\d+階(?:部分)?|^\d+)\s*(\d+\.\d+)',
                        line_norm
                    )

                    if area_match:
                        label = area_match.group(1)
                        val_str = area_match.group(2)

                        relevant_chars = [
                            c for c in line_chars
                            if c['text'] in val_str or c['text'] in label
                        ]
                        if any(is_deleted_line(c, drawings) for c in relevant_chars):
                            continue

                        try:
                            val = float(val_str)
                            if val < 50000 and current_section in results:
                                results[current_section]["areas"].append((label, val))
                                results[current_section]["total"] += val
                        except ValueError:
                            continue

        return results

    except Exception as e:
        log("process_pdf: EXCEPTION " + repr(e))
        log(traceback.format_exc())
        raise

# --- UI ---
st.title("🏢 不動産登記 建物面積自動集計")
st.markdown(
    "PDFから「主である建物」「一棟の建物」「専有部分」の面積を抽出します。"
    "※原因欄の数値は自動的に除外されます。"
)

# ★ここだけ追加：親DOM(head)へCSSを注入（あなたのHTML構造に直撃）
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
        /* あなたのDOMに存在する data-testid を直撃 */
        section[data-testid="stFileUploaderDropzone"]{
          min-height: 240px !important;   /* 縦幅を約2倍 */
          padding-top: 70px !important;
          padding-bottom: 70px !important;
          display: flex !important;
          align-items: center !important;
        }

        /* 念のため外側も */
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

uploaded_file = st.file_uploader("登記簿PDFをアップロード", type="pdf")

if uploaded_file:
    try:
        with st.spinner('解析中...'):
            data = process_pdf(uploaded_file)

        # 3つのカラムで表示
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
