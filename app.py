from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename

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
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 気象庁の市区町村コード（仕様書 No. 10201 別紙3: 青森市）
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')
REPORTS_FILE = os.path.join(APP_DIR, 'data', 'reports.json')
REPORTS_DB_FILE = os.path.join(APP_DIR, 'data', 'reports.db')
DISASTER_REPORTS_FILE = os.path.join(APP_DIR, 'data', 'disaster_reports.json')
REPORT_UPLOAD_DIR = os.path.join(APP_DIR, 'static', 'reports')
ALLOWED_REPORT_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
MAX_REPORT_DETAILS_LENGTH = 2000

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def init_reports_database():
    """レポート用SQLiteテーブルを作成し、既存JSONを初回移行する"""
    with sqlite3.connect(REPORTS_DB_FILE) as connection:
        connection.execute(
            '''CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY,
                case_number TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL,
                disaster_type TEXT NOT NULL DEFAULT '',
                rank TEXT NOT NULL DEFAULT '',
                audience TEXT NOT NULL DEFAULT '',
                district TEXT NOT NULL DEFAULT '',
                district2 TEXT NOT NULL DEFAULT '',
                landmark TEXT NOT NULL DEFAULT '',
                sender_org TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                parent_id INTEGER,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )'''
        )
        connection.execute(
            '''CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                report_id INTEGER,
                parent_id INTEGER,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            )'''
        )
        count = connection.execute('SELECT COUNT(*) FROM reports').fetchone()[0]
        if count == 0:
            legacy_reports = load_json(REPORTS_FILE, [])
            for report in legacy_reports:
                columns = (
                    'id', 'case_number', 'channel', 'disaster_type', 'rank',
                    'audience', 'district', 'district2', 'landmark',
                    'sender_org', 'sender_name', 'location', 'body', 'status',
                    'parent_id', 'created_by', 'created_at', 'updated_at'
                )
                values = [report.get(column, '') for column in columns]
                connection.execute(
                    f"INSERT OR IGNORE INTO reports ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    values
                )


def load_reports_from_database():
    """SQLiteからレポートを新しい順で読み込む"""
    with sqlite3.connect(REPORTS_DB_FILE) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            'SELECT * FROM reports ORDER BY id DESC'
        ).fetchall()
    return [dict(row) for row in rows]


init_reports_database()
shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
reports = load_reports_from_database()
disaster_reports = load_json(DISASTER_REPORTS_FILE, [])

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_shelters():
    """避難所データをファイルに保存する"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(shelters, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def save_reports():
    """指示・発信レポートをSQLiteへ保存し、JSONにもバックアップする"""
    try:
        columns = (
            'id', 'case_number', 'channel', 'disaster_type', 'rank', 'audience',
            'district', 'district2', 'landmark', 'sender_org', 'sender_name',
            'location', 'body', 'status', 'parent_id', 'created_by',
            'created_at', 'updated_at'
        )
        with sqlite3.connect(REPORTS_DB_FILE, timeout=10) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM reports')
            connection.executemany(
                f"INSERT INTO reports ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                [[report.get(column, '') for column in columns] for report in reports]
            )
        with open(REPORTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(reports, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def save_disaster_reports():
    """市民通報をJSONへ保存する"""
    try:
        with open(DISASTER_REPORTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(disaster_reports, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def save_audit_log(action, report_id=None, parent_id=None):
    """レポート操作の監査ログを保存する"""
    try:
        with sqlite3.connect(REPORTS_DB_FILE, timeout=10) as connection:
            connection.execute(
                '''INSERT INTO audit_logs
                   (action, report_id, parent_id, actor, created_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (
                    action,
                    report_id,
                    parent_id,
                    session.get('username', 'admin'),
                    get_japan_time(),
                )
            )
        return True
    except Exception:
        return False


def next_case_number():
    """既存レポートから日付別の案件番号を採番する"""
    today = datetime.now(JST).strftime('%Y-%m%d')
    prefix = f'{today}-'
    numbers = [
        int(report['case_number'].split('-')[-1])
        for report in reports
        if report.get('case_number', '').startswith(prefix)
        and report['case_number'].split('-')[-1].isdigit()
    ]
    return f'{prefix}{max(numbers, default=0) + 1:04d}'


REPORT_FIELDS = (
    'channel', 'disaster_type', 'rank', 'audience', 'district',
    'district2', 'landmark', 'sender_org', 'sender_name', 'location', 'body'
)


def report_payload(source):
    """フォームまたはJSONからレポート項目を取り出す"""
    payload = {}
    for field in REPORT_FIELDS:
        if field in ('district', 'district2'):
            if hasattr(source, 'getlist'):
                values = source.getlist(field)
            else:
                values = source.get(field, '')
                values = values if isinstance(values, list) else [values]
            payload[field] = '、'.join(
                dict.fromkeys(str(value).strip() for value in values if str(value).strip())
            )
        else:
            payload[field] = str(source.get(field, '') or '').strip()
    return payload


def district_values(value):
    """保存形式にかかわらず地区を個別の値として扱う"""
    return {part.strip() for part in str(value or '').replace(',', '、').split('、') if part.strip()}


def report_errors(payload):
    """チャンネルごとの必須項目を検証する"""
    if payload.get('channel') not in ('INTERNAL', 'PUBLIC'):
        return ['channel は INTERNAL または PUBLIC を指定してください。']

    required = (
        ('disaster_type', 'rank', 'district', 'sender_org', 'body')
        if payload['channel'] == 'INTERNAL'
        else ('audience', 'district', 'sender_org', 'body')
    )
    return [field for field in required if not payload.get(field)]


def create_report(payload, parent_id=None):
    """保存用のレポートを作成する"""
    now = get_japan_time()
    report = {
        'id': max((item.get('id', 0) for item in reports), default=0) + 1,
        'case_number': next_case_number(),
        **payload,
        'status': 'PUBLISHED',
        'parent_id': parent_id,
        'created_by': session.get('username', 'admin'),
        'created_at': now,
        'updated_at': now,
    }
    reports.insert(0, report)
    return report
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


def filter_shelters(district=None, keyword=None, features=None, latitude=None, longitude=None):
    """避難所の地区、キーワード、属性、現在地から検索する"""
    features = features or []
    keyword = (keyword or '').strip().lower()
    results = []
    for shelter in shelters:
        searchable = ' '.join(str(shelter.get(field, '')) for field in (
            'name', 'address', 'district', 'notes'
        )).lower()
        if district and shelter.get('district') != district:
            continue
        if keyword and keyword not in searchable:
            continue
        if any(feature == 'open' and shelter.get('status') not in ('開設中', 'open', 'OPEN') for feature in features):
            continue
        if any(feature == 'closed' and shelter.get('status') not in ('閉鎖中', 'closed', 'CLOSED') for feature in features):
            continue
        if any(feature == 'barrier_free' and not shelter.get('barrier_free') for feature in features):
            continue
        if any(feature == 'pets' and not shelter.get('pets') for feature in features):
            continue
        if any(feature == 'low_crowding' and shelter.get('crowding_percent', 101) > 50 for feature in features):
            continue
        if 'nearby' in features and latitude is not None and longitude is not None:
            if shelter.get('latitude') is None or shelter.get('longitude') is None:
                continue
            distance = ((float(shelter['latitude']) - latitude) ** 2 + (float(shelter['longitude']) - longitude) ** 2) ** .5
            if distance > 0.025:
                continue
        results.append(shelter)
    return results


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time(),
            "map_location": {
                "latitude": 40.8246,
                "longitude": 140.7400,
                "label": "気象庁: 青森市の警報・注意報"
            } if warnings else None
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "map_location": None,
            "error": True
        }


def latest_public_announcements():
    """訂正の連鎖を解決し、公開可能な掲示板掲載だけを返す"""
    public_reports = [report for report in reports if report.get('channel') == 'PUBLIC']
    children = {}
    for report in public_reports:
        children.setdefault(report.get('parent_id'), []).append(report)

    latest = []
    for report in public_reports:
        if report.get('status') != 'PUBLISHED':
            continue
        current = report
        while children.get(current.get('id')):
            current = max(children[current.get('id')], key=lambda item: item.get('id', 0))
        if current.get('id') == report.get('id') and current.get('status') == 'PUBLISHED':
            latest.append(report)
    return sorted(latest, key=lambda item: item.get('id', 0), reverse=True)


def public_announcement_payload(report):
    """ホーム画面へ渡す掲示板掲載の公開項目だけを作る"""
    tags = [report.get(field, '') for field in (
        'disaster_type', 'rank', 'audience', 'district', 'district2',
        'landmark', 'sender_org'
    )]
    return {
        'id': report.get('id'),
        'case_number': report.get('case_number'),
        'audience': report.get('audience', ''),
        'disaster_type': report.get('disaster_type', ''),
        'rank': report.get('rank', ''),
        'district': report.get('district', ''),
        'district2': report.get('district2', ''),
        'landmark': report.get('landmark', ''),
        'sender_org': report.get('sender_org', ''),
        'sender_name': report.get('sender_name', ''),
        'location': report.get('location', ''),
        'body': report.get('body', ''),
        'published_at': report.get('created_at', ''),
        'tags': [tag for tag in tags if tag],
    }


def report_region(latitude):
    if latitude is None:
        return 'all'
    return 'north' if latitude >= 40.9 else 'south'


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = []
    for notice in instructions:
        if notice.get('target') != '住民':
            continue
        notice = dict(notice)
        notice['tags'] = notice.get('tags') or [
            value for value in (
                notice.get('target'), notice.get('status'), notice.get('shelter')
            ) if value
        ]
        resident_notices.append(notice)
    public_announcements = [
        public_announcement_payload(report)
        for report in latest_public_announcements()
    ]
    city_announcements = [
        {
            'content': item['body'],
            'source': '青森市役所',
            'created_at': item['published_at'],
            'location': item.get('location', ''),
            'latitude': item.get('latitude'),
            'longitude': item.get('longitude'),
            'tags': item.get('tags', []),
        }
        for item in public_announcements
        if item.get('sender_org') == '青森市役所'
    ]
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        official_notices=resident_notices + city_announcements,
        public_announcements=public_announcements,
        disaster_reports=sorted(disaster_reports, key=lambda item: item.get('id', 0), reverse=True),
        report_success=request.args.get('reported') == '1',
        report_error=request.args.get('report_error', ''),
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

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
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            return render_template(
                'shelter_register.html',
                error=True,
                message='避難所名を入力してください。'
            )

        next_id = max((shelter.get('id', 0) for shelter in shelters), default=0) + 1
        shelters.append({'id': next_id, 'name': name})
        if not save_shelters():
            shelters.pop()
            return render_template(
                'shelter_register.html',
                error=True,
                message='避難所の登録に失敗しました。'
            )

        return render_template(
            'shelter_register.html',
            success=True,
            message='避難所を登録しました。'
        )

    return render_template('shelter_register.html')

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template('shelter_search.html', shelters=shelters)

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


# 指示ボード：住民向けの指示を一覧で確認する
@app.route('/board', methods=['GET', 'POST'])
@login_required
def board():
    channel = request.args.get('channel', 'INTERNAL').upper()
    if channel not in ('INTERNAL', 'PUBLIC'):
        channel = 'INTERNAL'

    error = ''
    if request.method == 'POST':
        payload = report_payload(request.form)
        channel = payload['channel'].upper()
        missing = report_errors(payload)
        if missing:
            error = '必須項目を入力してください。'
        else:
            report = create_report(payload)
            if save_reports():
                save_audit_log('CREATE', report['id'])
                return redirect(url_for('board', channel=channel, sent=1))
            reports.pop(0)
            error = '保存に失敗しました。'

    history = [dict(report) for report in reports if report.get('channel') == channel]
    report_by_id = {report.get('id'): report for report in reports}
    for report in history:
        parent = report_by_id.get(report.get('parent_id'))
        corrected = next(
            (item for item in reports if item.get('parent_id') == report.get('id')),
            None,
        )
        report['correction_relation'] = (
            f"訂正元: {parent.get('case_number')}" if parent else
            f"訂正版: {corrected.get('case_number')}" if corrected else
            ''
        )
    return render_template(
        'board.html',
        channel=channel,
        history=history,
        error=error,
        sent=request.args.get('sent') == '1'
    )


@app.route('/api/reports', methods=['GET', 'POST'])
@login_required
def reports_api():
    if request.method == 'GET':
        channel = request.args.get('channel', '').upper()
        district = request.args.get('district', '').strip()
        result = [
            report for report in reports
            if (not channel or report.get('channel') == channel)
            and (not district or district in district_values(report.get('district')))
        ]
        return jsonify(result)

    payload = report_payload(request.get_json(silent=True) or request.form)
    errors = report_errors(payload)
    if errors:
        return jsonify({'errors': errors}), 400

    report = create_report(payload)
    if not save_reports():
        reports.pop(0)
        return jsonify({'error': 'レポートの保存に失敗しました。'}), 500
    save_audit_log('CREATE', report['id'])
    return jsonify(report), 201


@app.route('/api/reports/<int:report_id>', methods=['GET', 'PUT'])
@login_required
def report_detail_api(report_id):
    report = next((item for item in reports if item.get('id') == report_id), None)
    if not report:
        return jsonify({'error': 'レポートが見つかりません。'}), 404

    if request.method == 'GET':
        return jsonify(report)

    payload = report_payload(request.get_json(silent=True) or request.form)
    payload['channel'] = payload['channel'] or report.get('channel', '')
    errors = report_errors(payload)
    if errors:
        return jsonify({'errors': errors}), 400
    report.update(payload)
    report['updated_at'] = get_japan_time()
    if not save_reports():
        return jsonify({'error': 'レポートの更新に失敗しました。'}), 500
    save_audit_log('UPDATE', report['id'])
    return jsonify(report)


@app.route('/api/reports/<int:report_id>/correct', methods=['POST'])
@login_required
def correct_report_api(report_id):
    original = next((item for item in reports if item.get('id') == report_id), None)
    if not original:
        return jsonify({'error': 'レポートが見つかりません。'}), 404

    source = dict(original)
    source.update(request.get_json(silent=True) or request.form)
    payload = report_payload(source)
    errors = report_errors(payload)
    if errors:
        return jsonify({'errors': errors}), 400

    original['status'] = 'CORRECTED'
    original['updated_at'] = get_japan_time()
    corrected = create_report(payload, parent_id=original['id'])
    if not save_reports():
        reports.pop(0)
        original['status'] = 'PUBLISHED'
        return jsonify({'error': '訂正内容の保存に失敗しました。'}), 500
    save_audit_log('CORRECT', corrected['id'], original['id'])
    return jsonify(corrected), 201


@app.route('/api/reports/<int:report_id>/revoke', methods=['POST'])
@login_required
def revoke_report_api(report_id):
    original = next((item for item in reports if item.get('id') == report_id), None)
    if not original:
        return jsonify({'error': 'レポートが見つかりません。'}), 404

    original['status'] = 'REVOKED'
    original['updated_at'] = get_japan_time()
    if not save_reports():
        original['status'] = 'PUBLISHED'
        return jsonify({'error': '取消内容の保存に失敗しました。'}), 500
    save_audit_log('REVOKE', original['id'])
    return jsonify(original)


@app.route('/api/masters')
@login_required
def masters_api():
    return jsonify({
        'disaster_types': [
            '地震', '津波', '大雨・洪水', '土砂災害', '台風・暴風',
            '高潮', '大雪・雪崩', '火災', '火山', '事故・危険物',
            '原子力', '感染症・健康危機', 'その他'
        ],
        'ranks': ['緊急', '警戒', '注意'],
        'audiences': ['住民', '防災関係機関'],
        'districts': [
            '青森市全域', '青森地区', '浪岡地区', '油川', '荒川', '石江',
            '浦町', '大野', '沖館', '奥内', '合浦', '金沢', '久栗坂',
            '幸畑', '三内', '篠田', '新城', '千刈', '千富町', '造道',
            '筒井', '堤町', '戸山', '長島', '西滝', '浜館', '原別',
            '古川', '本町', '松原', '港町', '妙見', '矢田前', '八重田',
            '安田', '横内', '柳川', '山手台', 'その他'
        ],
        'sender_orgs': ['青森市役所', '消防本部', '警察署'],
        'landmarks': ['青森駅前', '堤橋', 'その他'],
    })


@app.route('/api/audit_logs')
@login_required
def audit_logs_api():
    """監査ログを新しい順で返す"""
    with sqlite3.connect(REPORTS_DB_FILE) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''SELECT id, action, report_id, parent_id, actor, created_at
               FROM audit_logs ORDER BY id DESC LIMIT 100'''
        ).fetchall()
    return jsonify([dict(row) for row in rows])

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    feature_names = ('barrier_free', 'pets', 'low_crowding', 'nearby', 'open', 'closed')
    features = [name for name in feature_names if request.args.get(name) == '1']
    latitude = request.args.get('latitude', type=float)
    longitude = request.args.get('longitude', type=float)
    results = filter_shelters(
        district=request.args.get('district'),
        keyword=request.args.get('keyword'),
        features=features,
        latitude=latitude,
        longitude=longitude,
    )
    return render_template('search_results.html', results=results)

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    feature_names = ('barrier_free', 'pets', 'low_crowding', 'nearby', 'open', 'closed')
    features = [name for name in feature_names if request.args.get(name) == '1']
    results = filter_shelters(
        district=request.args.get('district'),
        keyword=request.args.get('keyword'),
        features=features,
        latitude=request.args.get('latitude', type=float),
        longitude=request.args.get('longitude', type=float),
    )

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())


@app.route('/api/public/announcements')
def public_announcements_api():
    """ログイン不要でホーム画面へ公開する掲示板掲載API"""
    items = [public_announcement_payload(report) for report in latest_public_announcements()]
    filters = {
        'district': request.args.get('district', '').strip(),
        'disaster_type': request.args.get('disaster_type', '').strip(),
        'rank': request.args.get('rank', '').strip(),
        'audience': request.args.get('audience', '').strip(),
        'sender_org': request.args.get('sender_org', '').strip(),
    }
    items = [
        item for item in items
        if all(not value or value in str(item.get(field, '')) for field, value in filters.items())
    ]
    sort_key = request.args.get('sort', 'newest')
    if sort_key == 'disaster_type':
        items.sort(key=lambda item: item.get('disaster_type', ''))
    elif sort_key == 'rank':
        rank_order = {'緊急': 0, '警戒': 1, '注意': 2}
        items.sort(key=lambda item: rank_order.get(item.get('rank', ''), 99))
    elif sort_key == 'district':
        items.sort(key=lambda item: item.get('district', ''))
    else:
        items.sort(key=lambda item: item.get('id', 0), reverse=True)
    try:
        limit = min(max(int(request.args.get('limit', 50)), 1), 100)
    except ValueError:
        limit = 50
    return jsonify({'items': items[:limit], 'total': len(items)})


@app.route('/disaster_reports', methods=['POST'])
def disaster_reports_api():
    """市民からの災害通報を安全に保存する"""
    disaster_type = request.form.get('disaster_type', '').strip()
    details = request.form.get('details', '').strip()
    if not disaster_type:
        return redirect(url_for('index', report_error='災害種別を選択してください。'))
    if len(details) > MAX_REPORT_DETAILS_LENGTH:
        return redirect(url_for('index', report_error='詳細情報は2000文字以内で入力してください。'))

    image = request.files.get('photo')
    image_url = ''
    saved_image_path = None
    if image and image.filename:
        safe_name = secure_filename(image.filename)
        extension = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
        if extension not in ALLOWED_REPORT_EXTENSIONS:
            return redirect(url_for('index', report_error='許可されていない画像形式です。'))
        report_id = max((item.get('id', 0) for item in disaster_reports), default=0) + 1
        os.makedirs(REPORT_UPLOAD_DIR, exist_ok=True)
        filename = f'{get_japan_time().replace("年", "").replace("月", "").replace("日", "").replace(" ", "").replace(":", "")}-{report_id}.{extension}'
        saved_image_path = os.path.join(REPORT_UPLOAD_DIR, filename)
        image.save(saved_image_path)
        image_url = url_for('static', filename=f'reports/{filename}')
    else:
        report_id = max((item.get('id', 0) for item in disaster_reports), default=0) + 1

    def coordinate(name):
        value = request.form.get(name, '').strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    latitude = coordinate('latitude')
    longitude = coordinate('longitude')
    report = {
        'id': report_id,
        'disaster_type': disaster_type,
        'details': details,
        'image_url': image_url,
        'latitude': latitude,
        'longitude': longitude,
        'region': report_region(latitude),
        'created_at': get_japan_time(),
    }
    disaster_reports.insert(0, report)
    if not save_disaster_reports():
        disaster_reports.pop(0)
        if saved_image_path and os.path.exists(saved_image_path):
            os.remove(saved_image_path)
        return redirect(url_for('index', report_error='通報の保存に失敗しました。'))
    return redirect(url_for('index', reported=1))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
