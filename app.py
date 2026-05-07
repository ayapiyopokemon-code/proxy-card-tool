import io
import os
import uuid
import tempfile
import requests
from PIL import Image
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory
from reportlab.lib.units import mm
from reportlab.lib.pagesizes import landscape
from reportlab.pdfgen import canvas

app = Flask(__name__)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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
        # 横線
        c.line(cx, cy, cx + dx_out, cy)
        # 縦線
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
    """
    cards: [{'src': str, 'qty': int}, ...]
    paper: 'B4' or 'A3'
    戻り値: PDF のバイト列
    """
    layout = LAYOUTS[paper]
    pw = mm2pt(layout['w'])
    ph = mm2pt(layout['h'])
    cols = layout['cols']
    rows = layout['rows']
    cards_per_page = cols * rows

    # カード間隔 1mm
    gap = mm2pt(1)
    cw = mm2pt(CARD_W)
    ch = mm2pt(CARD_H)

    # グリッド全体の幅・高さ
    grid_w = cols * cw + (cols - 1) * gap
    grid_h = rows * ch + (rows - 1) * gap

    # 左下起点の余白
    margin_x = (pw - grid_w) / 2
    margin_y = (ph - grid_h) / 2

    # カードリストを枚数分展開
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
        # ReportLab は左下原点
        x = margin_x + col * (cw + gap)
        y = margin_y + (rows - 1 - row) * (ch + gap)

        try:
            img = fetch_image(src)
            img_resized = img.resize(
                (int(cw * 3), int(ch * 3)),  # 3倍で縮小（品質維持）
                Image.LANCZOS
            )
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                img_resized.save(tmp.name, 'JPEG', quality=95)
                c.drawImage(tmp.name, x, y, width=cw, height=ch)
            os.unlink(tmp.name)
        except Exception:
            # 画像取得失敗時はグレーの枠を描画
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
    f.save(os.path.join(UPLOAD_FOLDER, name))
    return jsonify({'src': f'/uploads/{name}'})


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/proxy-image')
def proxy_image():
    """外部画像をサーバー経由で取得してブラウザに返す（CORS回避）"""
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
