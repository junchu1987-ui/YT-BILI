import hashlib
import random
import requests
import yaml
import os

# 1. Load Config
with open('c:/YT-BILI/YT-BILI/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

appid = config['baidu_fanyi']['appid']
security_key = config['baidu_fanyi']['security_key']

def calculate_sign(appid, q, salt, secret_key):
    sign_str = appid + q + str(salt) + secret_key
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

test_test = "Hello World"
salt = str(random.randint(32768, 65536))
sign = calculate_sign(appid, test_test, salt, security_key)

url = "http://api.fanyi.baidu.com/api/trans/vip/translate"
params = {'q': test_test, 'from': 'en', 'to': 'zh', 'appid': appid, 'salt': salt, 'sign': sign}

try:
    print(f"Testing Baidu Translation API with AppID: {appid}")
    response = requests.get(url, params=params, timeout=10)
    result = response.json()
    if 'trans_result' in result:
        print(f"SUCCESS! Translation: {result['trans_result'][0]['dst']}")
    else:
        print(f"FAILED! Error: {result}")
except Exception as e:
    print(f"ERROR: {e}")
