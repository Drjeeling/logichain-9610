import os, json, re
from dotenv import load_dotenv
load_dotenv('.env')
from openai import OpenAI
client = OpenAI(api_key=os.getenv('DEEPSEEK_API_KEY'), base_url='https://api.deepseek.com')

domestic = ['义乌','宁波港','上海港','苏州','广州','深圳','深圳港','广州港','青岛港','天津港','北京','西安','成都','重庆','武汉','南京']

prompt = f"""从用户物流需求中抽取【国内段】起点和终点。输出 JSON。

国内段可用节点：{', '.join(domestic)}

【关键规则】：
- origin 和 destination 都必须是【国内段】节点
- 国外目的地（洛杉矶等）不是国内段 destination，选最近的国内海港
- weight 三选一：cost_priority(省钱)/time_priority(最快)/balanced(折中)

只输出 JSON：
{{"origin": "...", "destination": "...", "weight": "...", "use_air": false}}

用户需求：义乌发20吨日用百货到洛杉矶，11月全流程，预算优先"""

resp = client.chat.completions.create(
    model='deepseek-chat',
    messages=[{'role':'user','content':prompt}],
    temperature=0.0,
    max_tokens=100,
)
content = resp.choices[0].message.content.strip()
print('原文:', repr(content))
content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE)
print('清理后:', repr(content))
data = json.loads(content)
print('解析:', data)
print('origin in domestic:', data.get('origin') in domestic)
print('dest in domestic:', data.get('destination') in domestic)
