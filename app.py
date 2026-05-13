import io
import os
import re
import uuid
import tempfile
import requests
from bs4 import BeautifulSoup
from PIL import Image
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from supabase import create_client, Client

app = Flask(__name__)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Supabase設定
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://sjzuuruhegxrzygumljm.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'sb_publishable_s0OH8Wvol_cr7gi4K77qRA_tsj9ghst')
BUCKET_NAME = 'proxy-images'
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CARD_W = 63   # mm
CARD_H = 88   # mm
TOMBO = 3     # トンボ線の長さ mm

LAYOUTS = {
    'A4': {'w': 210, 'h': 297, 'cols': 3, 'rows': 3},
    'B4': {'w': 257, 'h': 364, 'cols': 4, 'rows': 4},
    'A3': {'w': 420, 'h': 297, 'cols': 6, 'rows': 3},
}


def mm2pt(v):
    return v * mm


def draw_tombo(c, x, y, card_w, card_h):
    """カード四隅にL字トンボ線を描画（x, y はカード左下座標、単位pt）"""
    t = mm2pt(TOMBO)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.25)
    corners = [
        (x,           y + card_h),  # 左上
        (x + card_w,  y + card_h),  # 右上
        (x,           y),           # 左下
        (x + card_w,  y),           # 右下
    ]
    for i, (cx, cy) in enumerate(corners):
        dx_out = -t if i % 2 == 0 else t
        dy_out = t if i < 2 else -t
        c.line(cx, cy, cx + dx_out, cy)
        c.line(cx, cy, cx, cy + dy_out)


def fetch_image(src):
    """URL またはアップロードパスから PIL.Image を返す"""
    if src.startswith('http://') or src.startswith('https://'):
        resp = requests.get(src, timeout=15)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert('RGB')
    elif src.startswith('/uploads/'):
        filename = src[len('/uploads/'):]
        return Image.open(os.path.join(UPLOAD_FOLDER, filename)).convert('RGB')
    else:
        return Image.open(src).convert('RGB')


def generate_pdf(cards, paper):
    layout = LAYOUTS[paper]
    pw = mm2pt(layout['w'])
    ph = mm2pt(layout['h'])
    cols = layout['cols']
    rows = layout['rows']
    cards_per_page = cols * rows

    gap = mm2pt(1)
    cw = mm2pt(CARD_W)
    ch = mm2pt(CARD_H)

    grid_w = cols * cw + (cols - 1) * gap
    grid_h = rows * ch + (rows - 1) * gap

    margin_x = (pw - grid_w) / 2
    margin_y = (ph - grid_h) / 2

    card_list = []
    for card in cards:
        qty = max(1, int(card.get('qty', 1)))
        for _ in range(qty):
            card_list.append(card['src'])

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(pw, ph))

    for idx, src in enumerate(card_list):
        page_idx = idx % cards_per_page
        if idx > 0 and page_idx == 0:
            c.showPage()

        col = page_idx % cols
        row = page_idx // cols
        x = margin_x + col * (cw + gap)
        y = margin_y + (rows - 1 - row) * (ch + gap)

        try:
            img = fetch_image(src)
            img_resized = img.resize(
                (int(cw * 3), int(ch * 3)),
                Image.LANCZOS
            )
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                img_resized.save(tmp.name, 'JPEG', quality=95)
                c.drawImage(tmp.name, x, y, width=cw, height=ch)
            os.unlink(tmp.name)
        except Exception:
            c.setFillColorRGB(0.85, 0.85, 0.85)
            c.rect(x, y, cw, ch, fill=1, stroke=0)
            c.setFillColorRGB(0.4, 0.4, 0.4)
            c.setFont('Helvetica', mm2pt(3))
            c.drawCentredString(x + cw / 2, y + ch / 2, 'Image Error')

        draw_tombo(c, x, y, cw, ch)

    c.save()
    buf.seek(0)
    return buf.read()


@app.route('/')
def index():
    resp = render_template('index.html')
    from flask import make_response
    r = make_response(resp)
    r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return r


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'):
        return jsonify({'error': 'Unsupported format'}), 400

    name = f'{uuid.uuid4().hex}{ext}'
    file_bytes = f.read()

    # ローカルに保存
    local_path = os.path.join(UPLOAD_FOLDER, name)
    with open(local_path, 'wb') as tmp_file:
        tmp_file.write(file_bytes)

    # Supabase Storageにアップロード
    try:
        content_type = f.content_type or 'image/jpeg'
        supabase.storage.from_(BUCKET_NAME).upload(
            path=name,
            file=file_bytes,
            file_options={'content-type': content_type}
        )
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(name)
        return jsonify({'src': public_url})
    except Exception:
        # Supabase失敗時はローカルURLにフォールバック
        return jsonify({'src': f'/uploads/{name}'})


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/proxy-image')
def proxy_image():
    url = request.args.get('url', '')
    if not url.startswith(('http://', 'https://')):
        return '', 400
    try:
        resp = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        return resp.content, 200, {'Content-Type': content_type}
    except Exception:
        return '', 502


# ─── デッキコードインポート ────────────────────────────────

DECK_CODE_PATTERN = re.compile(r'^[a-zA-Z0-9]{6}-[a-zA-Z0-9]{6}-[a-zA-Z0-9]{6}$')
DECK_PRINT_SKIP = {
    'ポケモン', 'グッズ', 'ポケモンのどうぐ', 'サポート', 'スタジアム', 'エネルギー',
    '枚数', 'エキスパンション', 'コレクションNo.', '小計', '合計'
}


@app.route('/import-deck-code', methods=['POST'])
def import_deck_code():
    data = request.get_json()
    code = (data.get('code') or '').strip()

    # URLからデッキコードを抽出（貼り付けがURLの場合も対応）
    url_match = re.search(r'([a-zA-Z0-9]{6}-[a-zA-Z0-9]{6}-[a-zA-Z0-9]{6})', code)
    if url_match:
        code = url_match.group(1)

    if not DECK_CODE_PATTERN.match(code):
        return jsonify({'error': 'デッキコードの形式が正しくありません（例: 9gngnQ-zAMw9G-96H9nn）'}), 400

    try:
        url = f'https://www.pokemon-card.com/deck/print.html/deckID/{code}/'
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        resp.raise_for_status()
    except Exception:
        return jsonify({'error': '公式サイトへの接続に失敗しました'}), 502

    soup = BeautifulSoup(resp.text, 'html.parser')
    cards = []
    for table in soup.find_all('table'):
        for row in table.find_all('tr'):
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if len(cells) < 2:
                continue
            name, qty_str = cells[0], cells[1]
            if name in DECK_PRINT_SKIP or qty_str in DECK_PRINT_SKIP:
                continue
            try:
                qty = int(qty_str)
                if name and qty > 0:
                    cards.append({'name': name, 'qty': qty})
            except ValueError:
                pass

    if not cards:
        return jsonify({'error': 'デッキデータを取得できませんでした。デッキコードを確認してください'}), 404

    return jsonify({'cards': cards, 'code': code})


# ─── デッキ API ───────────────────────────────────────────

@app.route('/decks', methods=['GET'])
def get_decks():
    try:
        result = supabase.table('decks').select('*').order('created_at', desc=True).execute()
        return jsonify(result.data)
    except Exception as e:
        return jsonify([])


@app.route('/decks', methods=['POST'])
def save_deck():
    data = request.get_json()
    name = data.get('name', '').strip()
    cards = data.get('cards', [])
    if not name:
        return jsonify({'error': 'デッキ名を入力してください'}), 400
    if not cards:
        return jsonify({'error': 'カードがありません'}), 400
    try:
        result = supabase.table('decks').insert({'name': name, 'cards': cards}).execute()
        return jsonify(result.data[0])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/decks/<deck_id>', methods=['DELETE'])
def delete_deck(deck_id):
    try:
        supabase.table('decks').delete().eq('id', deck_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── PDF生成 ──────────────────────────────────────────────

@app.route('/generate', methods=['POST'])
def generate():
    data = request.get_json()
    cards = data.get('cards', [])
    paper = data.get('paper', 'A3')
    if paper not in LAYOUTS:
        return jsonify({'error': 'Invalid paper size'}), 400
    if not cards:
        return jsonify({'error': 'No cards'}), 400

    pdf_bytes = generate_pdf(cards, paper)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'proxy_{paper}.pdf'
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5003))
    app.run(host='0.0.0.0', port=port)
