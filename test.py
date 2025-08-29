from flask import Flask, request, jsonify, g
from flask_cors import CORS
import sqlite3
import datetime
import requests  # 用于调用AI评分API
import json  # 处理JSON数据
# 新增：导入输入相关模块
import getpass
# 新增：导入百度千帆SDK的IAM模块和错误处理
from qianfan.resources.console.iam import IAM
from qianfan.errors import QianfanError

app = Flask(__name__)
CORS(app)  # 允许跨域请求


def get_qianfan_token_by_sdk(ak, sk, expire_seconds=2592000):
    """
    基于百度千帆SDK获取有效Bearer Token（最终兼容版）
    :param ak: 百度千帆应用API Key
    :param sk: 百度千帆应用Secret Key
    :param expire_seconds: Token有效期（最大30天=2592000秒）
    :return: 成功返回带"Bearer "前缀的Token，失败返回None
    """
    try:
        print("=" * 60)
        print("开始通过SDK获取百度千帆Token...")
        # 调用SDK接口创建Token
        response = IAM.create_bearer_token(
            expire_in_seconds=expire_seconds,
            ak=ak,
            sk=sk
        )

        # 关键修复：直接检查响应体是否有token字段（不依赖状态码）
        # 1. 先确保响应有body属性，且是字典格式
        if hasattr(response, 'body') and isinstance(response.body, dict):
            # 2. 如果包含token字段，说明获取成功
            if 'token' in response.body:
                token_value = response.body['token']
                # 拼接成Bearer格式（API调用需要这个前缀）
                final_token = f"Bearer {token_value}"
                # 计算有效期（从createTime和expireTime提取，或用默认值）
                expire_hours = expire_seconds // 3600
                print(f"✅ Token获取成功！")
                print(f"有效期: {expire_hours}小时（{expire_seconds}秒）")
                print(f"Token预览: {final_token[:30]}***{final_token[-20:]}")  # 隐藏中间部分
                print(f"响应详情（含token）: {json.dumps(response.body, indent=2)}")
                print("=" * 60)
                return final_token
            # 3. 没有token字段，打印错误信息
            else:
                error_msg = response.body.get('error_msg', '响应体无token字段')
                error_code = response.body.get('error_code', '无错误码')
                print(f"❌ Token获取失败: 错误码[{error_code}] - {error_msg}")
        # 4. 响应体格式不对（不是字典）
        else:
            response_str = str(response.body) if hasattr(response, 'body') else str(response)
            print(f"❌ 响应格式异常: 不是有效的字典，详情: {response_str}")

        print("=" * 60)
        return None

    except QianfanError as e:
        print(f"❌ 千帆SDK错误: {type(e).__name__} - {str(e)}")
        print("=" * 60)
        return None
    except Exception as e:
        print(f"❌ 未知错误（Token获取）: {type(e).__name__} - {str(e)}")
        print("=" * 60)
        return None

# ---------------------- 百度千帆API配置 ----------------------
QIANFAN_API_URL = "https://qianfan.baidubce.com/v2/chat/completions"

# 新增：获取用户输入的AK和SK
print("请注意，你还没有输入你的AK和SK，请输入后再使用！")
QIANFAN_API_KEY = input("请输入百度千帆应用API Key (AK): ").strip()
# 使用getpass避免输入SK时明文显示
QIANFAN_SECRET_KEY = getpass.getpass("请输入百度千帆应用Secret Key (SK): ").strip()

# 验证输入不为空
if not QIANFAN_API_KEY or not QIANFAN_SECRET_KEY:
    print("错误：AK和SK不能为空！程序将退出。")
    exit(1)

# 动态获取Token（使用新增的SDK函数）
QIANFAN_AUTH_TOKEN = get_qianfan_token_by_sdk(QIANFAN_API_KEY, QIANFAN_SECRET_KEY)
if not QIANFAN_AUTH_TOKEN:
    print("⚠️ 警告：百度千帆Token获取失败，AI评分功能将使用默认5分！")
else:
    print("✅ 百度千帆Token初始化完成，AI评分功能可用")

# ---------------------- 数据库相关函数（保持不变） ----------------------
DATABASE = 'productivity_tracker.db'


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            effort_description TEXT NOT NULL,
            ability_description TEXT NOT NULL,
            effort_score INTEGER NOT NULL,
            ability_score INTEGER NOT NULL,
            user_adjusted_score INTEGER,
            date DATE NOT NULL
        )
        ''')
        db.commit()


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# ---------------------- AI评分函数（保留调试日志） ----------------------
def ai_score(task_type, effort_desc, ability_desc):
    # 先判断Token是否有效，无效直接返回5分
    if not QIANFAN_AUTH_TOKEN:
        print("⚠️ AI评分：Token无效，返回默认5分5分")
        return 5, 5

    try:
        # 1. 构建严格的评分提示词
        prompt = f"""请严格按照以下要求给用户评分，不允许添加任何额外文字！
任务类型：【{task_type}】
努力描述：【{effort_desc}】
能力描述：【{ability_desc}】
评分要求：
1. 努力程度和能力各评1-10分（1最低，10最高）；
2. 仅返回一行文字，格式必须是：努力程度：X，能力：X（X是数字）；
3. 除了上述格式，不能有任何其他内容（包括解释、标点、换行）。"""

        # 2. 构建API请求参数
        payload = json.dumps({
            "model": "deepseek-v3.1-250821",
            "messages": [
                {"role": "system", "content": "你是评分助手，仅按要求格式返回分数，不额外说话"},
                {"role": "user", "content": prompt}
            ]
        })
        headers = {
            'Content-Type': 'application/json',
            'Authorization': QIANFAN_AUTH_TOKEN  # 使用SDK获取的Token
        }

        # 调试日志：打印请求参数
        print("=" * 50)
        print("AI评分 - 发送给百度千帆API的参数：")
        print(f"Headers: Authorization={headers['Authorization'][:20]}***")  # 隐藏Token中间部分
        print(f"Payload: {payload}")

        # 3. 发送API请求
        response = requests.request("POST", QIANFAN_API_URL, headers=headers, data=payload, timeout=10)

        # 调试日志：打印API响应
        print(f"\nAI评分 - 百度千帆API响应：")
        print(f"状态码: {response.status_code}")
        print(f"响应内容: {response.text}")

        # 4. 解析响应
        response_data = json.loads(response.text)
        print(f"\nAI评分 - 解析后的响应JSON：")
        print(json.dumps(response_data, indent=2))

        # 5. 提取并验证分数
        if "choices" in response_data and len(response_data["choices"]) > 0:
            result = response_data["choices"][0]["message"]["content"].strip()
            print(f"\nAI评分 - 提取的原始结果：{result}")

            # 兼容全角/半角逗号，避免解析失败
            result = result.replace("，", ",").strip()
            # 提取分数（容错处理）
            if "努力程度：" in result and "能力：" in result:
                effort_part = result.split("努力程度：")[1].split(",")[0]
                ability_part = result.split("能力：")[1]
                # 确保分数是数字
                if effort_part.isdigit() and ability_part.isdigit():
                    effort_score = int(effort_part)
                    ability_score = int(ability_part)
                    # 限制分数在1-10之间
                    effort_score = max(1, min(10, effort_score))
                    ability_score = max(1, min(10, ability_score))

                    print(f"✅ AI评分成功：努力{effort_score}分，能力{ability_score}分")
                    print("=" * 50)
                    return effort_score, ability_score
                else:
                    print(f"❌ 分数提取失败：分数不是数字（{effort_part}/{ability_part}）")
            else:
                print(f"❌ 分数提取失败：结果格式不对（缺少'努力程度：'或'能力：'）")

        # 格式异常时返回5分
        print(f"❌ AI评分：API返回格式异常，返回默认5分5分")
        print("=" * 50)
        return 5, 5

    except Exception as e:
        # 捕获所有异常并打印详情
        print(f"❌ AI评分出错：{type(e).__name__} - {str(e)}")
        print("=" * 50)
        return 5, 5


# ---------------------- API路由（保持不变） ----------------------
@app.route('/api/submit-task', methods=['POST'])
def submit_task():
    data = request.get_json()

    task_type = data.get('task_type')
    effort_desc = data.get('effort_description')
    ability_desc = data.get('ability_description')

    if not all([task_type, effort_desc, ability_desc]):
        return jsonify({'error': '缺少必要字段'}), 400

    effort_score, ability_score = ai_score(task_type, effort_desc, ability_desc)
    today = datetime.date.today().isoformat()

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        'INSERT INTO tasks (task_type, effort_description, ability_description, effort_score, ability_score, date) VALUES (?, ?, ?, ?, ?, ?)',
        (task_type, effort_desc, ability_desc, effort_score, ability_score, today)
    )
    task_id = cursor.lastrowid
    db.commit()

    return jsonify({
        'task_id': task_id,
        'effort_score': effort_score,
        'ability_score': ability_score,
        'message': '任务提交成功'
    })


@app.route('/api/weekly-data', methods=['GET'])
def get_weekly_data():
    today = datetime.date.today()
    start_of_week = today - datetime.timedelta(days=today.weekday())
    end_of_week = start_of_week + datetime.timedelta(days=6)

    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
    SELECT date, AVG(COALESCE(user_adjusted_score, effort_score)) as avg_effort_score
    FROM tasks
    WHERE date BETWEEN ? AND ?
    GROUP BY date
    ORDER BY date
    ''', (start_of_week.isoformat(), end_of_week.isoformat()))

    daily_data = cursor.fetchall()
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    result = {
        'daily_scores': [],
        'total_score': 0,
        'max_possible': 70
    }

    current_date = start_of_week
    for i in range(7):
        date_str = current_date.isoformat()
        score = 0
        for row in daily_data:
            if row['date'] == date_str:
                score = round(row['avg_effort_score'])
                break
        result['daily_scores'].append({'day': weekdays[i], 'date': date_str, 'score': score})
        result['total_score'] += score
        current_date += datetime.timedelta(days=1)

    return jsonify(result)


@app.route('/api/adjust-score', methods=['POST'])
def adjust_score():
    data = request.get_json()
    task_id = data.get('task_id')
    adjusted_score = data.get('adjusted_score')

    if not all([task_id, adjusted_score]):
        return jsonify({'error': '缺少必要字段'}), 400
    if not (1 <= adjusted_score <= 10):
        return jsonify({'error': '调整分数必须在1-10之间'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('UPDATE tasks SET user_adjusted_score = ? WHERE id = ?', (adjusted_score, task_id))
    db.commit()

    if cursor.rowcount == 0:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({'message': '分数调整成功'})


# ---------------------- 初始化与启动 ----------------------
init_db()

if __name__ == '__main__':
    app.run(debug=True)