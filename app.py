from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': 'password123',
    'manager': 'shelter2025'
}

# ────────────────────────────────
# 気象警報・注意報設定
FUJISAWA_AREA_CODE = "1330800"  # 藤沢市のエリアコード

WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/warning/140000.json"

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "高潮警報",
    "10": "大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "32": "暴風雪特別警報",
    "33": "大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "高潮特別警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
HISTORY_FILE = os.path.join(APP_DIR, 'data', 'notification_history.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
notification_history = load_json(HISTORY_FILE, [])
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def get_fujisawa_warnings():
    """藤沢市の警報・注意報を取得する"""
    try:
        # 神奈川県の警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        # 藤沢市のデータを検索
        area = next((a for area_type in warning_data.get("areaTypes", [])
                       for a in area_type.get("areas", [])
                       if a.get("code") == FUJISAWA_AREA_CODE), None)

        # 発表・継続中の警報・注意報を抽出
        warnings = [
            {
                "name": WARNING_CODES.get(w.get("code", ""), f"不明な警報・注意報 (コード: {w.get('code', '')})"),
                "code": w.get("code", ""),
                "status": w.get("status", "")
            }
            for w in (area.get("warnings", []) if area else [])
            if w.get("status") in ("発表", "継続")
        ]

        result = {
            "area_name": area.get("name", "藤沢市") if area else "藤沢市",
            "warnings": warnings,
            "report_time": format_report_time(warning_data.get("reportDatetime", "")),
            "last_fetch_time": get_japan_time()
        }

        # 履歴に保存
        save_warning_history(result)
        return result

    except Exception:
        return {
            "area_name": "藤沢市",
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す
@app.route('/')
def index():
    return render_template('index.html')

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        # 認証チェック
        if ADMIN_CREDENTIALS.get(username) == password:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="IDまたはパスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ※user が避難所登録ページについて具体的に修正指示しない限り、このコードは正しいのでこのまま保持すること。
@app.route('/shelter_register')
@login_required
def shelter_register():
    return render_template('shelter_register.html')

# 避難所検索ページ
@app.route('/shelter_search', methods=['GET', 'POST'])
def shelter_search():
    if request.method == 'POST':
        # 検索結果を search_results.html に渡す
        return render_template('search_results.html', results=shelters)

    # GETリクエストの場合は検索ページを表示
    return render_template('shelter_search.html')

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


# 災害情報通知履歴ページ：templates/notification_history.html を返す
@app.route('/notification_history')
def notification_history_page():
    return render_template('notification_history.html', history_count=len(notification_history))

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    results = filter_shelters(request.args.get('district'))
    return render_template('search_results.html', results=results)

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_fujisawa_warnings())

# 気象警報・注意報履歴API
@app.route('/api/warning_history')
def api_warning_history():
    """気象警報・注意報の履歴をJSON形式で返すAPI"""
    # クエリパラメータで件数を制限
    limit = request.args.get('limit', type=int)
    limited_history = notification_history[:limit] if limit and limit > 0 else notification_history

    return jsonify({
        "total_count": len(notification_history),
        "returned_count": len(limited_history),
        "history": limited_history
    })

def save_warning_history(warnings_data):
    """警報・注意報の履歴を保存する"""
    global notification_history

    # エラーの場合は履歴に保存しない
    if warnings_data.get('error', False):
        return

    ws = warnings_data.get("warnings", [])
    names = [w.get("name", "") for w in ws]

    # 最新の履歴と比較して、内容が同じ場合は保存しない
    if notification_history:
        last_ws = notification_history[0].get("warnings", [])
        if {(w.get("name", ""), w.get("status", "")) for w in last_ws} == \
           {(w.get("name", ""), w.get("status", "")) for w in ws}:
            return

    history_entry = {
        "timestamp": get_japan_time(),
        "area_name": warnings_data.get("area_name", "藤沢市"),
        "report_time": warnings_data.get("report_time", "不明"),
        "warnings": ws,
        "warning_count": len(ws),
        "has_emergency": any("特別警報" in n for n in names),
        "has_warning": any("警報" in n and "特別警報" not in n for n in names),
        "has_advisory": any("注意報" in n for n in names)
    }

    # 履歴の先頭に追加（最新が一番上）、最大100件まで保持
    notification_history.insert(0, history_entry)
    notification_history = notification_history[:100]

    # ファイルに保存
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(notification_history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

if __name__ == '__main__':
    app.run(debug=True, port=5000)
