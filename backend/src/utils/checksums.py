import hashlib,json
def canonical(value):
    """规范化 JSON：排序键、非 ASCII 保留、紧凑分隔，保证跨进程可复算。"""
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()
def sha256_json(value):
    return sha256_text(canonical(value))
